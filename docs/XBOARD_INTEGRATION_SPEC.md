# XBoard 集成实现规范

**适用范围**：Vanguard 项目与 XBoard 系统的业务对接

**文档目标**：提供一份可直接交付给 XB 系统实现的对接规范，明确业务边界、接口约定、数据流、错误处理与验收标准，供双方按同一标准开发与联调。

**当前版本**：v1.0  
**编写日期**：2026-05-23

---

## 1. 背景与目标

### 1.1 背景

Vanguard 项目包含 Telegram 引流、关键词触发、自动回复、私聊引导、追踪归因等能力。XBoard 系统作为外部业务系统，需要接收 Vanguard 侧的业务事件，并根据事件完成以下动作：

- 用户状态同步
- 订单/注册/激活等转化数据接收
- 优惠券或权益发放
- 归因信息记录
- 后续运营状态回写

### 1.2 集成目标

本次集成的目标不是简单“调用一个接口”，而是建立一条完整的业务链路：

1. Vanguard 侧产生业务事件
2. 事件经过标准化后发送给 XBoard
3. XBoard 返回处理结果和业务状态
4. Vanguard 持久化事件结果，并在必要时重试或补偿
5. 双方可通过统一 trace_id / tracking_code 定位一次转化

### 1.3 设计原则

- **幂等优先**：同一业务事件重复上报不得造成重复发放或重复建档
- **异步优先**：核心业务链路不应因 XBoard 短暂不可用而阻塞
- **可追踪**：每个事件都必须有唯一追踪 ID
- **可补偿**：失败后支持重试、补发、人工修复
- **配置化**：接口地址、签名密钥、超时、重试次数均应可配置

---

## 2. 集成范围

### 2.1 本期纳入范围

建议本期优先实现以下能力：

1. **用户注册/激活状态同步**
2. **优惠券发放结果回传**
3. **转化归因信息上报**
4. **用户基础信息同步**
5. **事件回执查询**

### 2.2 暂不纳入范围

以下能力可作为后续扩展，不建议本期强绑定：

- 复杂订单退款/撤销流程
- 多层级代理分佣
- 人工审核工作流
- 实时双向长连接同步

---

## 3. 业务术语定义

### 3.1 事件类型

| 事件类型 | 含义 | 触发来源 |
|---|---|---|
| `user.lead_created` | 获取到新线索 | 关键词触发 / 私聊引导 |
| `user.registered` | 用户完成注册 | 追踪回传 / XBoard 回写 |
| `user.activated` | 用户完成激活 | XBoard 状态变更 |
| `coupon.issued` | 优惠券发放成功 | Vanguard 侧发放动作完成 |
| `coupon.failed` | 优惠券发放失败 | 发放异常 |
| `tracking.clicked` | 链接被点击 | 追踪入口 |
| `tracking.converted` | 发生转化 | XBoard 回写或确认 |

### 3.2 核心标识

| 字段 | 说明 |
|---|---|
| `trace_id` | 单次业务链路唯一 ID，用于全链路追踪 |
| `tracking_code` | 归因码，可能来自 deep link、ref 参数或业务侧生成值 |
| `external_user_id` | XBoard 侧用户 ID |
| `tg_user_id` | Telegram 用户 ID |
| `tg_group_id` | Telegram 群组 ID |
| `event_id` | 单个事件唯一 ID，用于幂等 |

---

## 4. 总体架构

### 4.1 推荐架构

建议采用“事件驱动 + HTTP API”模式：

1. Vanguard 在关键节点生成标准事件
2. 事件写入本地数据库或事件表
3. 由异步任务将事件推送到 XBoard
4. XBoard 返回处理结果
5. Vanguard 更新事件状态并记录响应内容

### 4.2 组件职责

#### Vanguard 侧
- 生成事件
- 维护本地事件记录
- 执行重试和补偿
- 处理 XBoard 回执
- 记录审计日志

#### XBoard 侧
- 接收事件
- 校验签名和幂等性
- 完成业务处理
- 返回标准响应
- 提供事件查询接口

---

## 5. 数据流说明

### 5.1 用户线索流

1. 用户在 Telegram 中命中关键词
2. `TriggerHandler` / `Speaker` / `PrivateHandler` 产生业务动作
3. Vanguard 生成 `lead_created` 或 `tracking.converted` 事件
4. 事件发送到 XBoard
5. XBoard 记录线索并更新用户状态
6. 返回处理结果

### 5.2 优惠券流

1. Vanguard 触发发券动作
2. 本地执行成功后生成 `coupon.issued` 事件
3. 上报 XBoard
4. XBoard 更新用户权益/订单状态
5. 若失败，则进入重试队列并可人工补发

### 5.3 状态回写流

1. XBoard 检测到用户注册/激活/消费等状态变化
2. XBoard 调用 Vanguard 提供的回调接口
3. Vanguard 更新本地用户状态和归因记录
4. 两端通过 `trace_id` 关联同一笔业务

---

## 6. 接口规范

> 说明：以下为建议标准。若 XBoard 已有既定接口命名，可保持字段语义一致，仅调整路径和签名方式。

### 6.1 通用约定

#### 请求头

| Header | 必填 | 说明 |
|---|---|---|
| `Content-Type` | 是 | `application/json` |
| `X-Request-Id` | 是 | 请求唯一 ID |
| `X-Timestamp` | 是 | 毫秒时间戳 |
| `X-Signature` | 是 | 签名值 |
| `X-App-Id` | 是 | 调用方标识 |

#### 响应格式

```json
{
  "code": 0,
  "message": "ok",
  "data": {},
  "trace_id": "trace_20260523_xxx"
}
```

#### 错误格式

```json
{
  "code": 4001,
  "message": "invalid signature",
  "data": null,
  "trace_id": "trace_20260523_xxx"
}
```

### 6.2 事件上报接口

**接口名称**：事件接收

**建议路径**：`POST /api/v1/events/ingest`

#### 请求字段

```json
{
  "event_id": "evt_20260523_000001",
  "trace_id": "trace_20260523_abc123",
  "event_type": "user.registered",
  "tracking_code": "ref_xxx",
  "tg_user_id": 123456789,
  "tg_group_id": -1001234567890,
  "external_user_id": "xb_9988",
  "occurred_at": "2026-05-23T10:00:00Z",
  "payload": {
    "keyword": "节点",
    "source": "telegram",
    "message_id": 555,
    "campaign_id": 1001
  }
}
```

#### 响应字段

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "accepted": true,
    "idempotent": false,
    "xboard_event_id": "xb_evt_7788"
  },
  "trace_id": "trace_20260523_abc123"
}
```

#### 处理要求

- `event_id` 必须幂等
- 相同 `event_id` 重复上报时，XBoard 直接返回历史结果
- 若业务处理失败，响应中应包含可机器识别的错误码

### 6.3 用户状态查询接口

**接口名称**：用户状态查询

**建议路径**：`GET /api/v1/users/status?tg_user_id=...&tracking_code=...`

#### 返回示例

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "tg_user_id": 123456789,
    "registered": true,
    "activated": false,
    "coupon_status": "issued",
    "last_event_at": "2026-05-23T10:20:00Z"
  }
}
```

### 6.4 优惠券回传接口

**接口名称**：优惠券结果回传

**建议路径**：`POST /api/v1/coupons/report`

#### 请求字段

```json
{
  "event_id": "evt_20260523_000002",
  "trace_id": "trace_20260523_abc123",
  "tg_user_id": 123456789,
  "coupon_code": "VIP100",
  "coupon_status": "issued",
  "issued_at": "2026-05-23T10:05:00Z",
  "payload": {
    "source_event_id": "evt_20260523_000001"
  }
}
```

### 6.5 XBoard 回调 Vanguard 接口

**接口名称**：状态变更回调

**建议路径**：`POST /api/v1/xboard/callback/status`

#### 场景
- 用户注册完成
- 用户激活完成
- 订单支付成功
- 权益状态变更

#### 请求字段

```json
{
  "callback_id": "cb_20260523_0001",
  "trace_id": "trace_20260523_abc123",
  "event_type": "user.activated",
  "tg_user_id": 123456789,
  "external_user_id": "xb_9988",
  "status": "activated",
  "occurred_at": "2026-05-23T10:30:00Z",
  "payload": {
    "order_no": "ORD123456",
    "channel": "telegram"
  }
}
```

---

## 7. 签名与鉴权

### 7.1 鉴权方式

建议采用 HMAC-SHA256 签名：

- 将请求体 JSON 按固定规则序列化
- 拼接时间戳与随机串
- 使用双方约定的密钥签名
- XBoard / Vanguard 双方均校验签名

### 7.2 防重放建议

- `X-Timestamp` 与服务器时间差超过 5 分钟直接拒绝
- `X-Request-Id` 在有效时间窗内不得重复
- `event_id` 必须全局唯一

### 7.3 密钥管理

- 通过环境变量配置，不得写死在代码中
- 生产环境应支持密钥轮换
- 日志中不得输出完整签名串与密钥

---

## 8. 幂等与重试

### 8.1 幂等策略

XBoard 侧建议基于以下任一组合实现幂等：

- `event_id`
- `trace_id + event_type`
- `external_user_id + event_type + occurred_at`

优先级建议：`event_id` > `trace_id` > 业务键。

### 8.2 重试策略

Vanguard 侧建议采用指数退避：

- 第 1 次失败后 10 秒重试
- 第 2 次失败后 30 秒重试
- 第 3 次失败后 2 分钟重试
- 第 4 次失败后 10 分钟重试
- 超过最大重试次数后进入死信或人工处理队列

### 8.3 失败分类

| 错误类型 | 是否重试 | 处理方式 |
|---|---|---|
| 网络超时 | 是 | 进入重试队列 |
| 5xx 服务异常 | 是 | 进入重试队列 |
| 签名错误 | 否 | 立即失败并报警 |
| 参数校验失败 | 否 | 修正后重新提交 |
| 幂等冲突 | 否 | 读取历史结果 |

---

## 9. 本地数据模型建议

### 9.1 事件表

建议新增或复用一张本地事件表，至少包含：

- `id`
- `event_id`
- `trace_id`
- `event_type`
- `tracking_code`
- `payload`
- `status`（pending / sent / success / failed / retrying）
- `retry_count`
- `last_error`
- `xboard_event_id`
- `created_at`
- `updated_at`

### 9.2 用户关联表

建议记录以下映射关系：

- `tg_user_id`
- `external_user_id`
- `tracking_code`
- `campaign_id`
- `bind_status`
- `bind_at`

---

## 10. Vanguard 侧实现建议

### 10.1 建议新增模块

建议在后端增加独立的 XBoard 集成层，职责如下：

- `XBoardClient`：封装 HTTP 请求、签名、重试
- `XBoardEventService`：构造业务事件
- `XBoardCallbackController`：处理 XBoard 回调
- `XBoardEventRepository`：事件持久化
- `XBoardSyncJob`：异步补偿任务

### 10.2 推荐调用位置

可在以下场景触发上报：

- `keyword_trigger/handler.py`：关键词触发后记录线索
- `private_msg/private_handler.py`：私聊引导成功后上报
- `tracking/tracker.py`：点击/转化链路上报
- `coupon/*`：发券成功/失败后上报
- 用户注册/激活状态变更服务

### 10.3 错误处理规范

- 任何 XBoard 相关异常不得影响主业务消息发送
- 失败应写入日志和事件表
- 若是非致命失败，继续执行主链路
- 需要人工介入时，抛出可读错误码并保留 trace_id

---

## 11. XBoard 侧实现要求

### 11.1 接口要求

XBoard 侧需要至少满足：

- 接收 JSON 请求体
- 返回统一结构
- 校验签名
- 支持幂等
- 返回详细错误码
- 提供查询接口

### 11.2 推荐错误码

| 错误码 | 含义 |
|---|---|
| `0` | 成功 |
| `4001` | 签名校验失败 |
| `4002` | 参数非法 |
| `4003` | 时间戳过期 |
| `4091` | 幂等冲突但已有结果 |
| `5001` | XBoard 内部异常 |
| `5002` | 业务处理失败 |

---

## 12. 联调清单

### 12.1 联调前准备

- [ ] 双方确认接口地址
- [ ] 双方确认签名算法与密钥
- [ ] 双方确认事件类型枚举
- [ ] 双方确认字段映射关系
- [ ] 双方确认重试与超时策略

### 12.2 联调验证项

- [ ] `user.registered` 可正常入库
- [ ] `user.activated` 可正常回写
- [ ] `coupon.issued` 可正常回传
- [ ] 重复请求不会重复发放
- [ ] 失败后可重试成功
- [ ] 回调接口可正确更新本地状态
- [ ] `trace_id` 可串联完整链路

### 12.3 验收标准

- 核心事件上报成功率不低于 99%
- 幂等接口重复调用无副作用
- 单次请求超时可控，失败后可重试
- 事件链路日志完整可追踪
- XBoard 与 Vanguard 状态一致性可核对

---

## 13. 建议交付物

建议 XB 系统按以下交付物实现：

1. API 文档
2. 签名说明
3. 错误码表
4. 幂等说明
5. 测试环境地址
6. 联调测试用例
7. 回调地址白名单配置

---

## 14. 未决问题清单

以下问题需要在正式开发前由双方确认：

1. XBoard 现有系统是否已有统一事件中心
2. XBoard 是否接受本规范中的事件字段命名
3. 签名算法是否沿用 HMAC-SHA256
4. 回调接口是主动推送还是拉取轮询
5. 优惠券发放是否由 Vanguard 主导还是 XBoard 主导
6. 是否需要补充短信/邮件等其他渠道同步

---

## 15. 结论

本规范建议将 XBoard 集成拆成“事件上报 + 状态回调 + 幂等重试 + 统一追踪”四层来实现。这样做的好处是：

- 能与 Vanguard 现有自动回复、关键词触发、追踪模块自然衔接
- 对 XBoard 的实现要求清晰，便于按文档直接开发
- 后续如果新增活动类型或转化类型，只需扩展事件枚举和 payload

如果你认可这份结构，下一步可以直接把这份文档交给 XB 系统开发；如果你愿意，我也可以继续帮你补一份 **更偏接口字段级别的对接表**，方便他们直接按表实现。
