# XBoard Telegram Bot Matrix

> Legacy/reference only. The active Vanguard mainline is now
> `backend` as configuration/data center plus `backend.app.workers.telegram_worker`
> for 7x24 Telegram execution. Do not add new XBoard Bearer `/bot/...`
> integrations here; the supported XBoard protocol is HMAC signed `/api/v1/...`.

Telegram 自动化营销矩阵 - 使用 Telegram 用户账号矩阵，支持多账号运营。

## 架构说明

```
模块 A: 引流空投 Bot
├── 主号 (@UserA) - 处理主要引流任务
├── 辅助号 1 (@UserB) - 扩展覆盖
└── 辅助号 N (@UserN) - 可按需扩展

模块 B: 核心运营 Bot
├── 客服 1 号 (@Service1)
├── 客服 2 号 (@Service2)
└── ...

模块 C: 社群风纪 Bot
├── 管理员 1 号 (@Admin1)
├── 管理员 2 号 (@Admin2)
└── ...
```

## 技术栈

| 组件 | 技术选型 | 版本 |
|------|----------|------|
| Telegram SDK | Telethon | 1.35+ |
| Python | Python | 3.11+ |
| 数据库 | PostgreSQL | 15+ |
| 缓存 | Redis | 7+ |
| 海报生成 | Pillow + qrcode | - |
| 容器化 | Docker Compose | v2 |

## 项目结构

```
bot-matrix/
├── src/
│   ├── bots/               # Bot 模块
│   │   ├── lead_gen.py     # 模块 A: 引流空投
│   │   ├── service.py      # 模块 B: 核心运营
│   │   └── group_ops.py    # 模块 C: 社群风纪
│   ├── core/               # 核心组件
│   │   ├── account_manager.py  # 多账号管理器
│   │   ├── database.py     # PostgreSQL
│   │   ├── cache.py        # Redis
│   │   ├── api.py          # XBoard API
│   │   └── middleware.py   # 风控
│   └── utils/              # 工具
│       ├── poster.py        # 海报生成
│       └── content.py       # 文案
├── config/
│   └── config.yaml         # 配置文件
├── sessions/               # Telegram 会话存储
├── migrations/             # 数据库迁移
├── tests/                 # 测试
└── docker-compose.yml      # Docker 部署
```

## 快速开始

### 1. 获取 Telegram API 凭证

1. 访问 [my.telegram.org](https://my.telegram.org)
2. 登录你的 Telegram 账号
3. 点击 "API development tools"
4. 创建应用，获取 `api_id` 和 `api_hash`

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 配置

```bash
cp .env.example .env
```

编辑 `config/config.yaml` 和 `.env`：

```yaml
telegram:
  lead_gen:
    - name: "引流主号"
      enabled: true
      session_name: "lead_gen_main"
      phone: "+86138xxxxxxxx"
      api_id: 12345678
      api_hash: "your_api_hash_here"
```

### 4. 首次登录

首次运行会提示输入验证码（通过 Telegram 发送）。

```bash
python -m src.main
```

### 5. Docker 部署

```bash
docker-compose up -d
```

## 模块说明

### 模块 A: 引流空投 Bot
- 强制频道订阅检查
- 自动化发卡与试用账号
- 防薅羊毛风控引擎

### 模块 B: 核心运营 Bot
- 每日签到（100MB-1GB 随机流量）
- 弃单挽回（30分钟未付款推送折扣码）
- 一键裂变海报生成

### 模块 C: 社群风纪 Bot
- 竞品广告清洗（正则拦截）
- 节点状态播报（晚高峰定时）

## 开发

### 代码规范

```bash
black src/
isort src/
flake8 src/
```

### 测试

```bash
pytest tests/ -v
```

## 安全注意

- 妥善保管 `api_id`、`api_hash` 和手机号
- 会话文件（`.session`）包含登录凭证，请勿泄露
- 生产环境使用环境变量存储敏感信息

## 许可证

MIT License
