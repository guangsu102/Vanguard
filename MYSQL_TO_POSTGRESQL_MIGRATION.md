# MySQL 到 PostgreSQL 技术方案调整报告

**调整日期**: 2026-05-24  
**调整原因**: 技术方案统一，将 MySQL 调整为 PostgreSQL  
**调整范围**: 依赖配置、文档说明

---

## 📊 调整摘要

已将项目中所有 MySQL 相关配置调整为 PostgreSQL，确保技术栈统一。

### 核心变更

- **数据库**: MySQL 8.0 → **PostgreSQL 15**
- **Python 驱动**: aiomysql → **asyncpg**
- **连接字符串**: `mysql://` → `postgresql+asyncpg://`
- **Docker 镜像**: mysql:8.0 → **postgres:15-alpine**

---

## ✅ 已修改文件

### 1. backend/requirements.txt

**修改前**:
```txt
aiomysql>=0.2.0
```

**修改后**:
```txt
asyncpg>=0.29.0
```

---

### 2. backend/pyproject.toml

**修改前**:
```toml
"aiomysql>=0.2.0",
```

**修改后**:
```toml
"asyncpg>=0.29.0",
```

---

### 3. docs/技术架构文档.md

**修改内容**:

1. **架构图中的数据库名称**:
   - `MySQL` → `PostgreSQL`

2. **技术栈表格**:
   - `MySQL 8.0` → `PostgreSQL 15`

3. **Docker Compose 配置示例**:
   - 容器名: `vanguard-mysql` → `vanguard-postgres`
   - 镜像: `mysql:8.0` → `postgres:15-alpine`
   - 数据卷: `mysql_data` → `postgres_data`
   - 数据路径: `/var/lib/mysql` → `/var/lib/postgresql/data`

4. **环境变量示例**:
   - `DATABASE_URL=mysql://root:password@mysql:3306/vanguard`
   - → `DATABASE_URL=postgresql+asyncpg://vanguard:password@postgres:5432/vanguard`

5. **依赖项**:
   - `aiomysql>=0.2.0` → `asyncpg>=0.29.0`

6. **技术选型说明**:
   - 更新为 PostgreSQL 的优势说明

---

### 4. docs/开发计划.md

**修改内容**:

1. **容器列表**:
   - `vanguard-mysql    # MySQL 8.0`
   - → `vanguard-postgres  # PostgreSQL 15`

---

## ✅ 无需修改的文件

以下文件已经正确使用 PostgreSQL，无需修改：

### 1. docker-compose.yml ✅

```yaml
postgres:
  image: postgres:15-alpine
  container_name: vanguard-postgres
  environment:
    - POSTGRES_USER=${POSTGRES_USER:-vanguard}
    - POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
    - POSTGRES_DB=${POSTGRES_DB:-vanguard}
  volumes:
    - postgres_data:/var/lib/postgresql/data
```

### 2. .env.example ✅

```bash
POSTGRES_USER=vanguard
POSTGRES_PASSWORD=change_me_in_production
POSTGRES_DB=vanguard
DATABASE_URL=postgresql+asyncpg://vanguard:change_me_in_production@postgres:5432/vanguard
```

### 3. backend/app/core/database.py ✅

使用 SQLAlchemy 2.0 的异步引擎，支持 PostgreSQL：

```python
engine = create_async_engine(
    settings.DATABASE_URL,  # postgresql+asyncpg://...
    pool_size=settings.DATABASE_POOL_SIZE,
    max_overflow=settings.DATABASE_MAX_OVERFLOW,
    pool_pre_ping=True,
    pool_recycle=3600,
    echo=settings.DEBUG,
)
```

### 4. .github/workflows/ci-cd.yaml ✅

CI/CD 配置已使用 PostgreSQL：

```yaml
services:
  postgres:
    image: postgres:15-alpine
    env:
      POSTGRES_USER: test
      POSTGRES_PASSWORD: test
      POSTGRES_DB: vanguard_test
    ports:
      - 5432:5432
```

---

## 📋 PostgreSQL vs MySQL 对比

### 为什么选择 PostgreSQL？

| 特性 | PostgreSQL | MySQL |
|------|-----------|-------|
| **JSONB 支持** | ✅ 原生支持，性能优秀 | ⚠️ JSON 类型性能较差 |
| **全文搜索** | ✅ 内置全文搜索 | ❌ 需要额外配置 |
| **复杂查询** | ✅ 支持 CTE、窗口函数 | ⚠️ 部分支持 |
| **并发控制** | ✅ MVCC，无锁读 | ⚠️ 行锁 |
| **扩展性** | ✅ 丰富的扩展生态 | ⚠️ 扩展较少 |
| **数据类型** | ✅ 数组、范围、UUID 等 | ⚠️ 类型较少 |
| **开源协议** | ✅ PostgreSQL License | ⚠️ GPL (部分功能商业) |
| **社区活跃度** | ✅ 非常活跃 | ✅ 活跃 |

### 项目中的优势

1. **JSONB 支持**: 适合存储 Telegram 消息的元数据、用户配置等
2. **全文搜索**: 适合关键词匹配、消息搜索
3. **数组类型**: 适合存储标签、权限列表等
4. **更好的并发**: MVCC 机制，读写不阻塞
5. **更强的数据完整性**: 严格的约束检查

---

## 🔧 迁移指南

### 如果之前使用了 MySQL

如果项目之前使用了 MySQL，需要进行数据迁移：

#### 1. 导出 MySQL 数据

```bash
mysqldump -u root -p vanguard > vanguard_mysql.sql
```

#### 2. 转换 SQL 语法

使用工具转换 MySQL SQL 到 PostgreSQL：
- [pgloader](https://github.com/dimitri/pgloader) - 自动迁移工具
- 手动调整 SQL 语法差异

#### 3. 导入 PostgreSQL

```bash
psql -U vanguard -d vanguard < vanguard_postgres.sql
```

#### 4. 更新依赖

```bash
cd backend
pip uninstall aiomysql
pip install asyncpg>=0.29.0
```

---

## ✅ 验证清单

- [x] requirements.txt 已更新为 asyncpg
- [x] pyproject.toml 已更新为 asyncpg
- [x] 技术架构文档已更新为 PostgreSQL
- [x] 开发计划文档已更新为 PostgreSQL
- [x] docker-compose.yml 使用 PostgreSQL (已确认)
- [x] .env.example 使用 PostgreSQL (已确认)
- [x] CI/CD 配置使用 PostgreSQL (已确认)
- [x] 代码使用 SQLAlchemy，兼容 PostgreSQL (已确认)

---

## 📝 注意事项

### 1. 连接字符串格式

**PostgreSQL 连接字符串**:
```
postgresql+asyncpg://用户名:密码@主机:端口/数据库名
```

**示例**:
```
postgresql+asyncpg://vanguard:password@localhost:5432/vanguard
```

### 2. 驱动选择

- **asyncpg**: 纯 Python 实现，性能最好，推荐使用
- **psycopg3**: 传统驱动的新版本，功能完整
- **psycopg2**: 旧版驱动，不推荐

### 3. SQL 语法差异

主要差异：
- 自增主键: `AUTO_INCREMENT` → `SERIAL` 或 `IDENTITY`
- 字符串连接: `CONCAT()` → `||`
- 限制结果: `LIMIT` 语法相同
- 日期函数: `NOW()` → `CURRENT_TIMESTAMP`

**注意**: 项目使用 SQLAlchemy ORM，这些差异由 ORM 自动处理。

### 4. 性能优化

PostgreSQL 特有的优化：
- 使用 `EXPLAIN ANALYZE` 分析查询
- 创建合适的索引（B-tree, GiST, GIN）
- 使用 `VACUUM` 和 `ANALYZE` 维护统计信息
- 调整 `shared_buffers` 和 `work_mem` 参数

---

## 🎯 总结

### 调整完成

✅ 所有 MySQL 相关配置已成功调整为 PostgreSQL  
✅ 依赖包已更新为 asyncpg  
✅ 文档已全部更新  
✅ 现有代码无需修改（使用 SQLAlchemy ORM）

### 技术栈统一

- **数据库**: PostgreSQL 15
- **Python 驱动**: asyncpg 0.29.0+
- **ORM**: SQLAlchemy 2.0 (异步)
- **连接池**: SQLAlchemy 内置连接池
- **迁移工具**: Alembic

### 下一步

1. 如果有现有 MySQL 数据，执行数据迁移
2. 更新开发环境的依赖: `pip install -r requirements.txt`
3. 验证数据库连接: 运行测试套件
4. 更新部署文档（如需要）

---

**调整人**: Claude (Anthropic)  
**调整日期**: 2026-05-24  
**调整状态**: ✅ 完成
