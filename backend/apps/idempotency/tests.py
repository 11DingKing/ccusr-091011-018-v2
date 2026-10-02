"""
幂等凭证测试
"""
import io
from datetime import timedelta
from unittest import mock

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from openpyxl import Workbook
from rest_framework.test import APIClient

from apps.authentication.backends import generate_token
from apps.authentication.models import User
from apps.warehouse.models import Category, Unit, Variety
from . import services
from .models import IdempotencyRecord


class IdempotencyFixture(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("idem-user", "testpass123", role="admin")
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {generate_token(self.user)}")
        self.unit = Unit.objects.create(name="件", created_by=self.user)

    def post_units(self, payload, key=None, org=None):
        headers = {}
        if key is not None:
            headers["HTTP_IDEMPOTENCY_KEY"] = key
        if org is not None:
            headers["HTTP_X_ORGANIZATION_ID"] = org
        return self.client.post("/api/units/", payload, format="json", **headers)

    def get_record(self, key, domain="warehouse.units.create", org="default"):
        return IdempotencyRecord.objects.get(organization=org, domain=domain, key=key)


class IdempotencyReplayTest(IdempotencyFixture):
    def test_first_success_is_stored_and_replayed(self):
        first = self.post_units({"name": "箱"}, key="k-1")
        replay = self.post_units({"name": "箱"}, key="k-1")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay["Idempotency-Replayed"], "true")
        # 重放返回首次的业务结果，且只创建了一条业务记录
        self.assertEqual(replay.json()["data"]["id"], first.json()["data"]["id"])
        self.assertEqual(Unit.objects.filter(name="箱").count(), 1)

        record = self.get_record("k-1")
        self.assertEqual(record.status, "succeeded")
        self.assertEqual(record.response_code, 200)
        self.assertEqual(record.response_body["data"]["id"], first.json()["data"]["id"])

    def test_same_key_with_different_payload_conflicts(self):
        first = self.post_units({"name": "箱"}, key="k-2")
        conflict = self.post_units({"name": "袋"}, key="k-2")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(conflict.status_code, 409)
        self.assertFalse(conflict.json()["success"])
        self.assertFalse(Unit.objects.filter(name="袋").exists())

    def test_business_rejection_is_stored_and_replayed(self):
        Unit.objects.create(name="箱", created_by=self.user)

        first = self.post_units({"name": "箱"}, key="k-3")
        replay = self.post_units({"name": "箱"}, key="k-3")

        self.assertEqual(first.status_code, 400)
        self.assertEqual(replay.status_code, 400)
        self.assertEqual(replay["Idempotency-Replayed"], "true")
        self.assertEqual(replay.json()["message"], first.json()["message"])

        record = self.get_record("k-3")
        self.assertEqual(record.status, "rejected")
        self.assertEqual(record.response_code, 400)

    def test_transaction_failure_allows_retry(self):
        with mock.patch.object(Unit.objects, "create", side_effect=RuntimeError("db error")):
            failed = self.post_units({"name": "箱"}, key="k-4")
        self.assertEqual(failed.status_code, 500)

        record = self.get_record("k-4")
        self.assertEqual(record.status, "failed")
        self.assertTrue(record.error_detail)
        self.assertFalse(Unit.objects.filter(name="箱").exists())

        # 事务失败未产生业务结果，同一凭证可重新执行
        retry = self.post_units({"name": "箱"}, key="k-4")
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(Unit.objects.filter(name="箱").count(), 1)
        record.refresh_from_db()
        self.assertEqual(record.status, "succeeded")

    def test_failed_record_with_different_payload_conflicts(self):
        with mock.patch.object(Unit.objects, "create", side_effect=RuntimeError("db error")):
            self.post_units({"name": "箱"}, key="k-5")
        conflict = self.post_units({"name": "袋"}, key="k-5")
        self.assertEqual(conflict.status_code, 409)

    def test_concurrent_first_request_is_rejected(self):
        first = self.post_units({"name": "箱"}, key="k-6")
        self.assertEqual(first.status_code, 200)

        # 模拟并发首次请求：记录仍处于处理中且租约有效
        record = self.get_record("k-6")
        record.status = "processing"
        record.locked_until = timezone.now() + timedelta(seconds=60)
        record.save()

        concurrent = self.post_units({"name": "箱"}, key="k-6")
        self.assertEqual(concurrent.status_code, 409)
        self.assertIn("处理中", concurrent.json()["message"])
        self.assertEqual(Unit.objects.filter(name="箱").count(), 1)

    def test_stale_processing_lease_allows_retry_after_restart(self):
        # 模拟服务重启：上次请求中断，记录停留在处理中且租约已过期
        first = self.post_units({"name": "箱"}, key="k-7")
        self.assertEqual(first.status_code, 200)
        Unit.objects.filter(name="箱").delete()

        record = self.get_record("k-7")
        record.status = "processing"
        record.response_code = None
        record.response_body = None
        record.locked_until = timezone.now() - timedelta(seconds=1)
        record.save()

        retry = self.post_units({"name": "箱"}, key="k-7")
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(Unit.objects.filter(name="箱").count(), 1)
        record.refresh_from_db()
        self.assertEqual(record.status, "succeeded")


class IdempotencyScopeTest(IdempotencyFixture):
    def test_key_isolated_by_organization(self):
        org_a = self.post_units({"name": "箱"}, key="k-10", org="org-a")
        org_b = self.post_units({"name": "袋"}, key="k-10", org="org-b")

        self.assertEqual(org_a.status_code, 200)
        self.assertEqual(org_b.status_code, 200)
        self.assertEqual(Unit.objects.filter(name__in=["箱", "袋"]).count(), 2)
        self.assertEqual(IdempotencyRecord.objects.filter(key="k-10").count(), 2)

    def test_key_isolated_by_domain(self):
        unit_resp = self.post_units({"name": "箱"}, key="k-11")
        category_resp = self.client.post(
            "/api/categories/",
            {"name": "封存介质", "unit": self.unit.id},
            format="json",
            HTTP_IDEMPOTENCY_KEY="k-11",
        )

        self.assertEqual(unit_resp.status_code, 200)
        self.assertEqual(category_resp.status_code, 200)
        self.assertEqual(Category.objects.filter(name="封存介质").count(), 1)
        self.assertEqual(IdempotencyRecord.objects.filter(key="k-11").count(), 2)

    def test_expired_key_can_be_reused(self):
        first = self.post_units({"name": "箱"}, key="k-12")
        self.assertEqual(first.status_code, 200)

        record = self.get_record("k-12")
        record.expires_at = timezone.now() - timedelta(seconds=1)
        record.save()

        # 凭证过期后按全新请求处理，即使载荷不同也不构成冲突
        reused = self.post_units({"name": "袋"}, key="k-12")
        self.assertEqual(reused.status_code, 200)
        self.assertEqual(Unit.objects.filter(name="袋").count(), 1)

        # 复用后的新结果恢复正常的重放行为
        replay = self.post_units({"name": "袋"}, key="k-12")
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay["Idempotency-Replayed"], "true")


class IdempotencyCompatibilityTest(IdempotencyFixture):
    def test_request_without_key_keeps_legacy_behavior(self):
        created = self.post_units({"name": "箱"})
        duplicate = self.post_units({"name": "箱"})

        self.assertEqual(created.status_code, 200)
        # 不带凭证时保持原有行为：重复创建被业务校验拒绝
        self.assertEqual(duplicate.status_code, 400)
        self.assertEqual(IdempotencyRecord.objects.count(), 0)

    def test_oversized_key_is_rejected(self):
        response = self.post_units({"name": "箱"}, key="x" * 200)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(IdempotencyRecord.objects.count(), 0)

    def test_authentication_failure_does_not_pin_key(self):
        # 未认证的请求不占用凭证：重新登录后同一凭证可正常执行
        anonymous = APIClient()
        failed = anonymous.post(
            "/api/units/", {"name": "箱"}, format="json", HTTP_IDEMPOTENCY_KEY="k-30"
        )
        self.assertEqual(failed.status_code, 401)

        record = self.get_record("k-30")
        self.assertEqual(record.status, "failed")

        retry = self.post_units({"name": "箱"}, key="k-30")
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(Unit.objects.filter(name="箱").count(), 1)

    def test_form_encoded_endpoint_replays(self):
        payload = {"police_no": "OUT100", "name": "赵六", "phone": "13900139000", "id_card": ""}
        first = self.client.post(
            "/api/stock-out-persons/", payload, HTTP_IDEMPOTENCY_KEY="k-20"
        )
        replay = self.client.post(
            "/api/stock-out-persons/", payload, HTTP_IDEMPOTENCY_KEY="k-20"
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay["Idempotency-Replayed"], "true")
        self.assertEqual(replay.json()["data"]["id"], first.json()["data"]["id"])

    def test_user_creation_endpoint_replays(self):
        payload = {"username": "new-user", "password": "pass123456", "confirm_password": "pass123456"}
        first = self.client.post(
            "/api/auth/users/", payload, format="json", HTTP_IDEMPOTENCY_KEY="k-21"
        )
        replay = self.client.post(
            "/api/auth/users/", payload, format="json", HTTP_IDEMPOTENCY_KEY="k-21"
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay["Idempotency-Replayed"], "true")
        self.assertEqual(User.objects.filter(username="new-user").count(), 1)

    def test_file_upload_endpoint_replays(self):
        Category.objects.create(name="受控器材", unit=self.unit, created_by=self.user)
        content = self._build_import_file()

        first = self._upload_import(content, "k-22")
        replay = self._upload_import(content, "k-22")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["data"]["imported_count"], 1)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay["Idempotency-Replayed"], "true")
        self.assertEqual(replay.json()["data"]["imported_count"], 1)
        self.assertEqual(Variety.objects.filter(name="封存硬盘").count(), 1)

    def _build_import_file(self):
        wb = Workbook()
        ws = wb.active
        ws.append(["品种", "品类", "单位"])
        ws.append(["封存硬盘", "受控器材", "件"])
        buffer = io.BytesIO()
        wb.save(buffer)
        return buffer.getvalue()

    def _upload_import(self, content, key):
        from django.core.files.uploadedfile import SimpleUploadedFile

        upload = SimpleUploadedFile(
            "import.xlsx",
            content,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        return self.client.post(
            "/api/varieties/import/", {"file": upload}, format="multipart", HTTP_IDEMPOTENCY_KEY=key
        )


class IdempotencyPurgeTest(IdempotencyFixture):
    def test_purge_command_removes_only_expired_records(self):
        now = timezone.now()
        IdempotencyRecord.objects.create(
            organization="default", domain="warehouse.units.create", key="expired",
            request_hash="h1", status="succeeded", expires_at=now - timedelta(seconds=1),
        )
        IdempotencyRecord.objects.create(
            organization="default", domain="warehouse.units.create", key="live",
            request_hash="h2", status="succeeded", expires_at=now + timedelta(hours=1),
        )

        call_command("purge_idempotency_records")

        self.assertFalse(IdempotencyRecord.objects.filter(key="expired").exists())
        self.assertTrue(IdempotencyRecord.objects.filter(key="live").exists())
