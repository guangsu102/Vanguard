# XBoard Bot Matrix - 开发规范文档

## 1. 代码规范

### 1.1 Python 代码风格

项目遵循 **PEP 8** 规范，并使用以下工具强制执行：

- **Black** - 代码格式化（行长 100）
- **isort** - import 语句排序
- **flake8** - 代码风格检查
- **mypy** - 静态类型检查

#### 格式化命令

```bash
# 格式化代码
black src/

# 排序 imports
isort src/

# 检查代码
flake8 src/

# 类型检查
mypy src/
```

#### pre-commit hooks（可选）

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/psf/black
    rev: 24.4.2
    hooks:
      - id: black

  - repo: https://github.com/pycqa/isort
    rev: 5.13.2
    hooks:
      - id: isort

  - repo: https://github.com/pycqa/flake8
    rev: 7.0.0
    hooks:
      - id: flake8
```

### 1.2 命名规范

| 类型 | 规范 | 示例 |
|------|------|------|
| 类名 | PascalCase | `LeadGenBot`, `RedisClient` |
| 函数/方法 | snake_case | `create_trial_account`, `handle_checkin` |
| 变量 | snake_case | `bot_token`, `user_id` |
| 常量 | UPPER_SNAKE_CASE | `MAX_RETRY_TIMES`, `DEFAULT_TIMEOUT` |
| 私有属性 | _underscore | `_client`, `_config` |

### 1.3 类型注解

- 所有公共函数必须包含类型注解
- 使用 `typing` 模块的类型
- 推荐使用 `Pydantic` 进行配置和 DTO 类型定义

```python
from typing import Optional

async def create_trial_account(
    self,
    tg_uid: int,
    username: str,
    validity_hours: int = 24
) -> dict[str, bool]:
    ...
```

## 2. Git 分支管理

### 2.1 分支命名

```
feature/<feature-name>      # 新功能
bugfix/<bug-description>     # Bug 修复
hotfix/<urgent-fix>         # 紧急修复
refactor/<module-name>       # 重构
docs/<topic>                 # 文档更新
```

示例：
```
feature/lead-gen-bot
bugfix/subscription-check
hotfix/critical-memory-leak
```

### 2.2 Commit 规范

格式：`type(scope): subject`

```
feat(lead_gen): 添加强制订阅检查功能
fix(service): 修复签到冷却时间计算错误
docs(readme): 更新部署文档
style(api): 格式化 API 客户端代码
refactor(poster): 重构海报生成逻辑
test(bots): 添加 Bot 单元测试
chore(deps): 升级 aiogram 版本
```

Type 类型：
- `feat` - 新功能
- `fix` - Bug 修复
- `docs` - 文档更新
- `style` - 代码格式（不影响功能）
- `refactor` - 重构
- `test` - 测试相关
- `chore` - 构建/工具相关

### 2.3 Pull Request

- PR 标题：`[Feature/Bugfix/Hotfix] 简短描述`
- 描述必须包含：
  - 功能说明
  - 涉及文件
  - 测试说明
  - 截图（如有 UI 变更）

## 3. 数据库规范

### 3.1 迁移管理

使用 **Alembic** 进行数据库迁移：

```bash
# 创建迁移
alembic revision --autogenerate -m "add trial accounts table"

# 执行迁移
alembic upgrade head

# 回滚
alembic downgrade -1
```

### 3.2 表命名

- 使用复数名词：`tg_users`, `checkin_records`
- 小写下划线分隔

### 3.3 字段命名

| 字段类型 | 命名规范 |
|----------|----------|
| 主键 | `id` 或 `{table}_id` |
| 外键 | `{table}_id` |
| 时间戳 | `created_at`, `updated_at` |
| 软删除 | `deleted_at` |

## 4. API 规范

### 4.1 XBoard API 调用

所有与 XBoard 主系统的交互必须通过 HTTP API：

```python
# 禁止直接操作 XBoard 数据库
# 正确做法：
response = await self.api.create_trial_user(tg_uid=user_id, ...)

# 错误做法：
await xboard_db.execute("INSERT INTO users ...")
```

### 4.2 API 错误处理

```python
async def safe_api_call():
    try:
        result = await self.api.some_endpoint()
        if not result.get("success"):
            logger.error(f"API 调用失败: {result.get('message')}")
            return None
        return result["data"]
    except httpx.TimeoutException:
        logger.error("API 请求超时")
        return None
    except Exception as e:
        logger.exception("API 调用异常")
        return None
```

## 5. 日志规范

### 5.1 日志级别

| 级别 | 使用场景 |
|------|----------|
| DEBUG | 开发调试信息 |
| INFO | 正常业务流程 |
| WARNING | 潜在问题（可恢复） |
| ERROR | 错误但可继续 |
| CRITICAL | 严重错误需立即处理 |

### 5.2 日志格式

```python
logger.info(f"用户 {user_id} 签到成功，获得 {bonus_mb}MB")
logger.warning(f"检测到异常 IP: {ip}, 请求频率: {count}")
logger.error(f"创建试用账号失败: {e}")
```

## 6. 配置管理

### 6.1 敏感信息

敏感配置必须通过环境变量或 `.env` 文件管理：

```yaml
# config.yaml - 使用占位符
telegram:
  bot_token: "${LEAD_GEN_BOT_TOKEN}"

# .env - 实际值
LEAD_GEN_BOT_TOKEN=123456:ABC...
```

### 6.2 配置校验

使用 Pydantic 进行配置校验：

```python
from pydantic import BaseModel

class BotConfig(BaseModel):
    lead_gen_bot_token: str
    service_bot_token: str
    group_ops_bot_token: str
```

## 7. 测试规范

### 7.1 测试文件组织

```
tests/
├── conftest.py              # pytest 配置和 fixtures
├── unit/                    # 单元测试
│   ├── test_database.py
│   ├── test_cache.py
│   └── test_poster.py
├── integration/             # 集成测试
│   ├── test_api_client.py
│   └── test_bot_flow.py
└── fixtures/                # 测试数据
    └── sample_users.py
```

### 7.2 测试覆盖率

- 核心业务逻辑：≥ 80%
- Bot 处理器：≥ 70%
- 工具函数：≥ 90%

### 7.3 Mock 使用

```python
@pytest.fixture
def mock_api():
    with patch("src.core.api.XBoardAPIClient") as mock:
        yield mock
```

## 8. 安全规范

### 8.1 Bot Token 安全

- Token 绝不提交到 Git
- 使用环境变量或 Vault 管理

### 8.2 用户数据保护

- 不存储用户敏感信息
- TG UID 仅用于标识，不可逆推用户身份

### 8.3 速率限制

- 所有用户操作必须有频率限制
- Redis 实现滑动窗口计数

## 9. 部署规范

### 9.1 Docker 镜像

- 使用官方 Python slim 镜像
- 非 root 用户运行
- 最小化层数

### 9.2 健康检查

```dockerfile
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8080/health')"
```

### 9.3 优雅关闭

- 处理 SIGTERM 信号
- 完成正在处理的消息
- 关闭数据库连接

## 10. 监控与告警

### 10.1 日志输出

- 结构化日志（JSON 格式可选）
- 包含 request_id 追踪

### 10.2 Sentry 集成

```python
import sentry_sdk

sentry_sdk.init(
    dsn=config["monitoring"]["sentry_dsn"],
    traces_sample_rate=0.1
)
```

### 10.3 关键指标

- Bot 响应时间
- API 调用成功率
- 错误率
- 用户活跃度
