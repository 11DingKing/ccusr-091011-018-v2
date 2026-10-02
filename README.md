# 监管物资保管服务

该项目为监管仓、证物室和受控物资保管点提供服务端 API，覆盖人员授权、物资分类、批次登记、收发记录、审批、预警、审计日志与统计报表。数据保存在 SQLite，所有测试和接口验收均可在单个 Linux 应用容器内离线完成。

## 运行环境

- Python 3.11
- Django REST Framework
- SQLite

## 安装与初始化

```bash
python -m pip install -r backend/requirements.txt
cd backend
python manage.py migrate --run-syncdb
```

## 测试

```bash
cd backend
pytest -q
```

## 编译检查

```bash
python -m compileall -q backend
```

## API 验收

```bash
cd backend
python manage.py migrate --run-syncdb
python manage.py shell -c "from rest_framework.test import APIClient; from apps.authentication.models import User; u=User.objects.create_user('smoke','safe-pass',role='admin'); c=APIClient(); r=c.post('/api/auth/login/',{'username':'smoke','password':'safe-pass'},format='json'); print(r.status_code, bool(r.json()['data']['token']))"
```

## 幂等凭证

移动网络不稳定时客户端会自动重发请求。关键写接口（单位、品类、品种、品种导入、出库人员、用户的创建）支持幂等凭证，防止超时重发产生重复业务记录：

- 请求头 `Idempotency-Key: <凭证>` 携带凭证，可选 `X-Organization-Id: <机构>` 指定机构；凭证按 **机构 + 操作域 + 凭证** 隔离，互不干扰。
- 首次请求的结果（成功或业务拒绝）会持久化保存；相同凭证与相同载荷的重发直接重放首次响应，响应头带 `Idempotency-Replayed: true`，不会重复创建记录。
- 同一凭证搭配不同载荷返回 `409` 冲突；并发首次请求只执行一次，其余返回 `409` 处理中。
- 首次请求事务失败（5xx）或认证失效（401）时不保存可重放结果，同一凭证可重新执行；执行中断（如服务重启）的凭证在租约超时（默认 120 秒）后同样可重新执行。
- 凭证有效期默认 24 小时（`IDEMPOTENCY_KEY_TTL_HOURS`），过期后按全新请求处理；可执行 `python manage.py purge_idempotency_records` 清理过期记录。
- 不携带凭证的请求保持原有行为，内部调用不受影响。

## 容器

```bash
docker build -t custody-service .
docker run --rm custody-service
```
