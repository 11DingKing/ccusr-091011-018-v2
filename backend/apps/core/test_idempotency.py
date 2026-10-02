"""
幂等凭证机制测试
"""
import threading
from datetime import timedelta
from decimal import Decimal

from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.authentication.backends import generate_token
from apps.authentication.models import User
from apps.core.idempotency import hash_payload
from apps.core.models import IdempotencyRecord
from apps.warehouse.models import Category, Goods, StockIn, StockOut, Unit, Variety


class IdempotencyFixture(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user('idem-user', 'testpass123', role='admin')
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {generate_token(self.user)}")
        unit = Unit.objects.create(name='件', created_by=self.user)
        category = Category.objects.create(name='受控器材', unit=unit, created_by=self.user)
        variety = Variety.objects.create(name='记录终端', category=category, created_by=self.user)
        self.goods = Goods.objects.create(
            variety=variety, name='执法记录终端', code='DEV-IDEM-1',
            quantity=Decimal('10'), warning_threshold=Decimal('2'),
        )
        self.in_payload = {'goods': self.goods.id, 'quantity': '3', 'supplier': '送检单位'}
        self.out_payload = {
            'goods': self.goods.id, 'quantity': '2', 'receiver': '张三', 'receiver_dept': '一队',
        }

    def post_with_key(self, url, payload, key='key-001', org=None, extra=None):
        headers = {'HTTP_IDEMPOTENCY_KEY': key}
        if org is not None:
            headers['HTTP_X_ORG_CODE'] = org
        if extra:
            headers.update(extra)
        return self.client.post(url, payload, format='json', **headers)


class IdempotencySuccessReplayTest(IdempotencyFixture):
    def test_stock_in_success_then_replay_returns_same_record(self):
        first = self.post_with_key('/api/stock-in/', self.in_payload, key='in-1')
        self.assertEqual(first.status_code, 200, first.content)
        first_id = first.json()['data']['id']

        # 模拟移动网络超时后的自动重发
        replay = self.post_with_key('/api/stock-in/', self.in_payload, key='in-1')
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json()['data']['id'], first_id)
        self.assertEqual(StockIn.objects.count(), 1)
        self.goods.refresh_from_db()
        self.assertEqual(self.goods.quantity, Decimal('13'))

        record = IdempotencyRecord.objects.get(scope='stock-in', idem_key='in-1')
        self.assertEqual(record.status, IdempotencyRecord.STATUS_SUCCESS)
        self.assertEqual(record.response_code, 200)
        self.assertEqual(record.response_body['data']['id'], first_id)
        self.assertEqual(record.created_by, self.user)

    def test_payload_key_order_insensitive(self):
        first = self.post_with_key('/api/stock-in/', self.in_payload, key='in-2')
        reordered = {'supplier': '送检单位', 'quantity': '3', 'goods': self.goods.id}
        replay = self.post_with_key('/api/stock-in/', reordered, key='in-2')
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json()['data']['id'], first.json()['data']['id'])
        self.assertEqual(StockIn.objects.count(), 1)

    def test_stock_out_success_then_replay(self):
        first = self.post_with_key('/api/stock-out/', self.out_payload, key='out-1')
        self.assertEqual(first.status_code, 200)
        replay = self.post_with_key('/api/stock-out/', self.out_payload, key='out-1')
        self.assertEqual(replay.json()['data']['id'], first.json()['data']['id'])
        self.assertEqual(StockOut.objects.count(), 1)
        self.goods.refresh_from_db()
        self.assertEqual(self.goods.quantity, Decimal('8'))


class IdempotencyRejectionTest(IdempotencyFixture):
    def test_business_rejection_is_stored_and_replayed(self):
        payload = dict(self.out_payload, quantity='999')
        first = self.post_with_key('/api/stock-out/', payload, key='out-rej')
        self.assertEqual(first.status_code, 400)
        self.assertIn('库存不足', first.json()['message'])
        self.assertEqual(StockOut.objects.count(), 0)

        # 库存后来补足，但同凭证重放仍必须返回首次的业务拒绝
        self.goods.quantity = Decimal('9999')
        self.goods.save()
        replay = self.post_with_key('/api/stock-out/', payload, key='out-rej')
        self.assertEqual(replay.status_code, 400)
        self.assertIn('库存不足', replay.json()['message'])
        self.assertEqual(StockOut.objects.count(), 0)

        record = IdempotencyRecord.objects.get(idem_key='out-rej')
        self.assertEqual(record.status, IdempotencyRecord.STATUS_REJECTED)
        self.assertEqual(record.response_code, 400)

    def test_validation_rejection_also_idempotent(self):
        bad = {'goods': self.goods.id, 'quantity': '0', 'receiver': '张三'}
        first = self.post_with_key('/api/stock-out/', bad, key='out-val')
        self.assertEqual(first.status_code, 400)
        replay = self.post_with_key('/api/stock-out/', bad, key='out-val')
        self.assertEqual(replay.status_code, 400)
        self.assertEqual(replay.json()['message'], first.json()['message'])
        self.assertEqual(StockOut.objects.count(), 0)


class IdempotencyConflictTest(IdempotencyFixture):
    def test_same_key_different_payload_is_conflict(self):
        first = self.post_with_key('/api/stock-in/', self.in_payload, key='dup-key')
        self.assertEqual(first.status_code, 200)
        changed = dict(self.in_payload, quantity='4')
        conflict = self.post_with_key('/api/stock-in/', changed, key='dup-key')
        self.assertEqual(conflict.status_code, 409)
        self.assertFalse(conflict.json()['success'])
        # 冲突请求不得产生任何业务数据
        self.assertEqual(StockIn.objects.count(), 1)
        self.goods.refresh_from_db()
        self.assertEqual(self.goods.quantity, Decimal('13'))

    def test_same_key_different_scope_isolated(self):
        in_resp = self.post_with_key('/api/stock-in/', self.in_payload, key='cross')
        out_resp = self.post_with_key('/api/stock-out/', self.out_payload, key='cross')
        self.assertEqual(in_resp.status_code, 200)
        self.assertEqual(out_resp.status_code, 200)
        self.assertEqual(
            IdempotencyRecord.objects.filter(idem_key='cross').count(), 2
        )

    def test_same_key_different_org_isolated(self):
        a = self.post_with_key('/api/stock-in/', self.in_payload, key='org-key', org='ORG-A')
        b = self.post_with_key('/api/stock-in/', self.in_payload, key='org-key', org='ORG-B')
        self.assertEqual(a.status_code, 200)
        self.assertEqual(b.status_code, 200)
        self.assertNotEqual(a.json()['data']['id'], b.json()['data']['id'])
        self.assertEqual(StockIn.objects.count(), 2)

        # 同机构重放各自命中自己的首次结果
        replay_a = self.post_with_key('/api/stock-in/', self.in_payload, key='org-key', org='ORG-A')
        self.assertEqual(replay_a.json()['data']['id'], a.json()['data']['id'])


class IdempotencyConcurrentTest(IdempotencyFixture, TransactionTestCase):
    def test_concurrent_first_requests_execute_once(self):
        results = []
        errors = []
        barrier = threading.Barrier(4)

        def fire():
            client = APIClient()
            client.credentials(HTTP_AUTHORIZATION=f"Bearer {generate_token(self.user)}")
            barrier.wait()
            try:
                resp = client.post(
                    '/api/stock-in/', self.in_payload, format='json',
                    HTTP_IDEMPOTENCY_KEY='concurrent-1',
                )
                results.append(resp)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                from django.db import connection
                connection.close()

        threads = [threading.Thread(target=fire) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(StockIn.objects.count(), 1)
        self.goods.refresh_from_db()
        self.assertEqual(self.goods.quantity, Decimal('13'))
        statuses = sorted(r.status_code for r in results)
        self.assertEqual(statuses[0], 200, statuses)
        self.assertTrue(all(s in (200, 409) for s in statuses), statuses)


class IdempotencyExpiryTest(IdempotencyFixture):
    def test_expired_terminal_record_refuses_replay(self):
        first = self.post_with_key('/api/stock-in/', self.in_payload, key='exp-1')
        self.assertEqual(first.status_code, 200)

        record = IdempotencyRecord.objects.get(idem_key='exp-1')
        record.expires_at = timezone.now() - timedelta(minutes=1)
        record.save()

        replay = self.post_with_key('/api/stock-in/', self.in_payload, key='exp-1')
        self.assertEqual(replay.status_code, 410)
        # 过期凭证同样不允许换载荷复用
        changed = dict(self.in_payload, quantity='5')
        reuse = self.post_with_key('/api/stock-in/', changed, key='exp-1')
        self.assertEqual(reuse.status_code, 410)
        self.assertEqual(StockIn.objects.count(), 1)


class IdempotencyRestartDanglingTest(IdempotencyFixture):
    def test_stale_processing_after_crash_is_explained(self):
        record = IdempotencyRecord.objects.create(
            org='default', scope='stock-in', idem_key='stale-1',
            payload_hash=hash_payload(self.in_payload),
            status=IdempotencyRecord.STATUS_PROCESSING,
            locked_at=timezone.now() - timedelta(minutes=10),
            expires_at=timezone.now() + timedelta(hours=1),
        )
        resp = self.post_with_key('/api/stock-in/', self.in_payload, key='stale-1')
        self.assertEqual(resp.status_code, 409)
        self.assertIn('结果未知', resp.json()['message'])
        self.assertEqual(StockIn.objects.count(), 0)
        record.refresh_from_db()
        self.assertEqual(record.status, IdempotencyRecord.STATUS_PROCESSING)

    def test_fresh_processing_concurrent_duplicate_rejected(self):
        IdempotencyRecord.objects.create(
            org='default', scope='stock-in', idem_key='fresh-1',
            payload_hash=hash_payload(self.in_payload),
            status=IdempotencyRecord.STATUS_PROCESSING,
            locked_at=timezone.now(),
            expires_at=timezone.now() + timedelta(hours=1),
        )
        resp = self.post_with_key('/api/stock-in/', self.in_payload, key='fresh-1')
        self.assertEqual(resp.status_code, 409)
        self.assertIn('处理中', resp.json()['message'])


class IdempotencyFailureRetryTest(IdempotencyFixture):
    @staticmethod
    def _patch_goods_manager(exc_message):
        """让库存读取在业务事务内失败，模拟数据库故障/服务异常。"""
        from apps.warehouse import views as warehouse_views

        original = warehouse_views.Goods.objects

        class ExplodingManager:
            def select_for_update(self):
                raise RuntimeError(exc_message)

            def __getattr__(self, name):
                return getattr(original, name)

        warehouse_views.Goods.objects = ExplodingManager()
        return original

    def test_transaction_failure_allows_same_payload_retry(self):
        from apps.warehouse import views as warehouse_views

        original = self._patch_goods_manager('模拟库存服务不可用')
        try:
            failed = self.post_with_key('/api/stock-in/', self.in_payload, key='fail-1')
        finally:
            warehouse_views.Goods.objects = original
        self.assertEqual(failed.status_code, 500)
        self.assertEqual(StockIn.objects.count(), 0)

        record = IdempotencyRecord.objects.get(idem_key='fail-1')
        self.assertEqual(record.status, IdempotencyRecord.STATUS_ERROR)
        self.assertIn('模拟库存服务不可用', record.error_message)

        # 同凭证、同载荷重试：应当正常执行成功
        retry = self.post_with_key('/api/stock-in/', self.in_payload, key='fail-1')
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(StockIn.objects.count(), 1)
        record.refresh_from_db()
        self.assertEqual(record.status, IdempotencyRecord.STATUS_SUCCESS)

    def test_different_payload_after_error_is_conflict(self):
        from apps.warehouse import views as warehouse_views

        original = self._patch_goods_manager('boom')
        try:
            self.post_with_key('/api/stock-in/', self.in_payload, key='fail-2')
        finally:
            warehouse_views.Goods.objects = original

        changed = dict(self.in_payload, quantity='8')
        resp = self.post_with_key('/api/stock-in/', changed, key='fail-2')
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(StockIn.objects.count(), 0)


class IdempotencyLegacyCompatTest(IdempotencyFixture):
    def test_no_header_passes_through_unchanged(self):
        # 不带幂等凭证的内部/旧客户端调用：两次提交产生两条记录（保持历史行为）
        first = self.client.post('/api/stock-in/', self.in_payload, format='json')
        second = self.client.post('/api/stock-in/', self.in_payload, format='json')
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(StockIn.objects.count(), 2)
        self.assertEqual(IdempotencyRecord.objects.count(), 0)
