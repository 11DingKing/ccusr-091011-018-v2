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

## 关键写接口幂等凭证

收件（`POST /api/stock-in/`）与放行（`POST /api/stock-out/`）支持幂等凭证，用于移动网络不稳定时客户端超时重发的去重：

- 请求头携带 `Idempotency-Key: <客户端生成的唯一凭证>` 即启用；可选 `X-Org-Code` 声明机构，凭证按「机构 + 操作域 + 凭证」隔离，不携带该头的内部调用和旧客户端行为完全不变。
- 首次请求的成功结果与业务拒绝（4xx）都会冻结保存，同凭证、同载荷重放直接返回首次结果，不会产生第二条业务记录。
- 同凭证配不同载荷返回 `409`（凭证串用/误用）；凭证超过保留期（默认 72 小时，`IDEMPOTENCY_KEY_TTL_HOURS`）返回 `410`，需重新生成凭证。
- 首次请求事务失败时不产生业务数据，凭证记为失败，允许同载荷安全重试；若服务在处理中崩溃/重启，悬挂凭证返回 `409` 并提示先核对业务结果，不盲目重放。

## 容器

```bash
docker build -t custody-service .
docker run --rm custody-service
```
