# Vanguard 接入 XBoard API 文档

版本：v1.0  
日期：2026-05-23  
Base URL：`https://{xboard-domain}/api/v1`

## 1. 鉴权与签名

Vanguard 调用 XBoard 时必须携带以下 Header：

| Header | 必填 | 说明 |
|---|---|---|
| `Content-Type` | 是 | `application/json` |
| `X-App-Id` | 是 | XBoard 分配给 Vanguard 的应用标识 |
| `X-Timestamp` | 是 | Unix 毫秒时间戳 |
| `X-Request-Id` | 是 | 单次请求唯一 ID，5 分钟内不可重复 |
| `X-Signature` | 是 | HMAC-SHA256 签名 |

签名原文按以下 6 行拼接，行之间使用 `\n`：

```text
HTTP_METHOD
PATH
QUERY_STRING
X-Timestamp
X-Request-Id
RAW_BODY
```

示例：

```text
POST
/api/v1/events/ingest

1779501600000
req_20260523_0001
{"event_id":"evt_20260523_000001","trace_id":"trace_20260523_abc123","event_type":"user.registered","occurred_at":"2026-05-23T10:00:00Z"}
```

`X-Signature = hex(hmac_sha256(signing_string, signing_secret))`。

注意：

- `PATH` 必须包含 `/api/v1`。
- `QUERY_STRING` 使用 URL 中原始 query，不含 `?`；无 query 时为空行。
- `RAW_BODY` 必须是实际发送的请求体字符串；GET 请求为空字符串。
- 时间戳与 XBoard 服务器时间差超过 300 秒会被拒绝。
- `X-Request-Id` 在时间窗内重复会被拒绝。

## 2. 统一响应

成功：

```json
{
  "code": 0,
  "message": "ok",
  "data": {},
  "trace_id": "trace_20260523_abc123"
}
```

失败：

```json
{
  "code": 4001,
  "message": "invalid signature",
  "data": null,
  "trace_id": "trace_20260523_abc123"
}
```

错误码：

| code | 含义 |
|---:|---|
| `0` | 成功 |
| `4001` | 签名或 App ID 错误 |
| `4002` | 参数非法或 Header 缺失 |
| `4003` | 时间戳过期 |
| `4004` | XBoard 未启用 Vanguard 集成 |
| `4005` | `X-Request-Id` 重复 |
| `5002` | 业务处理失败 |

## 3. 事件上报

路径：`POST /api/v1/events/ingest`

请求：

```json
{
  "event_id": "evt_20260523_000001",
  "trace_id": "trace_20260523_abc123",
  "event_type": "user.registered",
  "tracking_code": "ref_xxx",
  "tg_user_id": 123456789,
  "tg_group_id": -1001234567890,
  "external_user_id": "9988",
  "occurred_at": "2026-05-23T10:00:00Z",
  "payload": {
    "keyword": "节点",
    "source": "telegram",
    "message_id": 555,
    "campaign_id": 1001,
    "email": "user@example.com"
  }
}
```

支持的 `event_type`：

| event_type | XBoard 处理 |
|---|---|
| `user.lead_created` | 记录线索和归因 |
| `tracking.clicked` | 记录点击 |
| `tracking.converted` | 记录转化 |
| `user.registered` | 绑定已有 XBoard 用户 |
| `user.activated` | 标记激活状态 |
| `coupon.issued` | 记录发券成功 |
| `coupon.failed` | 记录发券失败 |

响应：

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "accepted": true,
    "idempotent": false,
    "xboard_event_id": "xb_evt_1"
  },
  "trace_id": "trace_20260523_abc123"
}
```

幂等规则：

- `event_id` 全局唯一。
- 相同 `event_id` 重复上报时，XBoard 不重复处理业务，直接返回历史结果。
- `external_user_id` 可传 XBoard 数字用户 ID；如使用 XBoard `uuid`，也可传字符串 UUID。
- 如事件无法匹配到 XBoard 用户，XBoard 只记录线索/归因，不自动创建用户。

## 4. 用户状态查询

路径：`GET /api/v1/users/status`

查询参数至少传一个：

| 参数 | 必填 | 说明 |
|---|---|---|
| `tg_user_id` | 否 | Telegram 用户 ID |
| `tracking_code` | 否 | 归因码 |
| `external_user_id` | 否 | XBoard 用户 ID 或 UUID |
| `trace_id` | 否 | 便于链路追踪 |

示例：

```http
GET /api/v1/users/status?tg_user_id=123456789&tracking_code=ref_xxx&trace_id=trace_20260523_abc123
```

响应：

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "tg_user_id": 123456789,
    "external_user_id": "9988",
    "tracking_code": "ref_xxx",
    "registered": true,
    "activated": false,
    "coupon_status": "issued",
    "last_event_at": "2026-05-23T10:20:00+00:00",
    "xboard_user_id": 9988,
    "order_status": 3
  },
  "trace_id": "trace_20260523_abc123"
}
```

`activated=true` 的判定：

- Vanguard 已上报 `user.activated`；或
- XBoard 用户存在、未封禁、已有套餐，且套餐未过期。

## 5. 优惠券结果回传

路径：`POST /api/v1/coupons/report`

请求：

```json
{
  "event_id": "evt_20260523_000002",
  "trace_id": "trace_20260523_abc123",
  "tg_user_id": 123456789,
  "external_user_id": "9988",
  "tracking_code": "ref_xxx",
  "coupon_code": "VIP100",
  "coupon_status": "issued",
  "issued_at": "2026-05-23T10:05:00Z",
  "payload": {
    "source_event_id": "evt_20260523_000001"
  }
}
```

字段说明：

| 字段 | 必填 | 说明 |
|---|---|---|
| `event_id` | 是 | 幂等 ID |
| `trace_id` | 是 | 链路追踪 ID |
| `coupon_code` | 是 | 优惠券码 |
| `coupon_status` | 是 | `issued` 或 `failed` |
| `tg_user_id` | 否 | Telegram 用户 ID |
| `external_user_id` | 否 | XBoard 用户 ID 或 UUID |
| `tracking_code` | 否 | 归因码 |
| `issued_at` | 否 | 发券时间 |
| `payload` | 否 | 扩展信息 |

响应同事件上报。

## 6. XBoard 回调 Vanguard

如 XBoard 开启主动回调，会在以下场景向 Vanguard 回调：

- 用户注册成功：`user.registered`
- 订单开通完成：`user.activated`

请求体：

```json
{
  "callback_id": "cb_20260523_0001",
  "trace_id": "trace_xb_20260523_xxx",
  "event_type": "user.activated",
  "tg_user_id": 123456789,
  "external_user_id": "9988",
  "status": "activated",
  "occurred_at": "2026-05-23T10:30:00+08:00",
  "payload": {
    "xboard_user_id": 9988,
    "user_uuid": "xxxx-xxxx",
    "tracking_code": "ref_xxx",
    "order_no": "ORD123456",
    "plan_id": 1,
    "order_status": 3,
    "channel": "xboard"
  }
}
```

回调也使用同样的签名规则。Vanguard 应基于 `callback_id` 做幂等。

## 7. XBoard 环境变量

XBoard 侧需配置：

```env
VANGUARD_INTEGRATION_ENABLED=true
VANGUARD_APP_ID=vanguard
VANGUARD_SIGNING_SECRET=replace-with-shared-secret
VANGUARD_TIMESTAMP_TOLERANCE=300

VANGUARD_CALLBACK_ENABLED=true
VANGUARD_CALLBACK_URL=https://{vanguard-domain}/api/v1/xboard/callback/status
VANGUARD_CALLBACK_APP_ID=xboard
VANGUARD_CALLBACK_SIGNING_SECRET=replace-with-callback-secret
VANGUARD_CALLBACK_TIMEOUT=5
VANGUARD_CALLBACK_QUEUE=vanguard_callback
```

如需密钥轮换，可用逗号分隔多个入站密钥：

```env
VANGUARD_SIGNING_SECRETS=old-secret,new-secret
```

## 8. 联调清单

- XBoard 开启 `VANGUARD_INTEGRATION_ENABLED=true`。
- Vanguard 与 XBoard 使用同一入站 `VANGUARD_APP_ID` 和签名密钥。
- `POST /events/ingest` 上报 `user.lead_created` 成功入库。
- 同一 `event_id` 重复上报返回 `idempotent=true`。
- `GET /users/status` 可按 `tg_user_id` 或 `tracking_code` 查询。
- `POST /coupons/report` 可记录 `issued` 和 `failed`。
- 若开启 XBoard 回调，Vanguard 按 `callback_id` 幂等处理。
