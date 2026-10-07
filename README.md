<!-- oracle-shared-postgres-20260930 -->
> **Oracle 部署（2026-09-30）：共用 PostgreSQL 18 实例，业务分库分账号。** 先阅读 [共享数据库与部署边界](docs/ORACLE_SHARED_POSTGRES.md)，不要在 Oracle 常规发布时启动旧独立 PG。Redis 保持现状。

# =============================================================================
# Vanguard Project
# XBoard Telegram Bot Matrix
# =============================================================================

## Project Structure

```
vanguard/
├── backend/                 # FastAPI Backend (Python)
│   ├── app/
│   │   ├── core/           # Core modules (account, group, keyword, scheduler, etc.)
│   │   ├── modules/         # Business modules (acquisition, guardian)
│   │   ├── integrations/    # External integrations (Telegram, XBoard, LLM)
│   │   └── api/            # REST API endpoints
│   ├── migrations/          # Database migrations (Alembic)
│   ├── tests/              # Unit and integration tests
│   ├── Dockerfile
│   └── requirements.txt
│
├── bot-matrix/              # Legacy/reference Telegram Bot implementation
│   ├── src/
│   │   ├── bots/          # Bot implementations
│   │   ├── core/          # Core utilities
│   │   └── integrations/  # External integrations
│   ├── tests/              # Tests
│   ├── Dockerfile
│   └── requirements.txt
│
├── frontend/               # Vue 3 Frontend
│   ├── src/
│   │   ├── views/         # Page components
│   │   ├── components/     # Reusable components
│   │   ├── stores/        # Pinia stores
│   │   ├── api/           # API clients
│   │   └── router/        # Vue Router
│   ├── Dockerfile
│   └── package.json
│
├── nginx/                  # Nginx configuration
│   ├── nginx.conf
│   └── ssl/               # SSL certificates
│
├── monitoring/              # Prometheus & Grafana config
├── scripts/                # Deployment scripts
├── docs/                   # Project documentation; start with docs/README.md
├── logs/                   # Application logs
│
├── docker-compose.yml       # Docker Compose orchestration
├── Dockerfile.backend       # Backend Docker image
├── Dockerfile.bot          # Bot Docker image
├── .env.example            # Environment variables template
└── README.md
```

## Quick Start

Documentation map: [docs/README.md](docs/README.md). The active Telegram
execution path is backend/app/workers/telegram_worker.py; bot-matrix is
kept as legacy/reference code behind an explicit Compose profile.

### Prerequisites

- Docker & Docker Compose
- Python 3.12+
- Node.js 18+

### Development Setup

1. Clone the repository
2. Copy environment variables:
   ```bash
   cp .env.example .env
   ```

3. Start with Docker Compose:
   ```bash
   docker-compose up -d
   ```

4. Access the application:
   - Web UI: http://localhost:3000
   - API Docs: http://localhost:8000/docs

### Manual Development

#### Backend

```bash
cd backend
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

#### Frontend

```bash
cd frontend
pnpm install
pnpm dev
```

## Technology Stack

### Backend
- **Framework**: FastAPI (Python 3.12+)
- **ORM**: SQLAlchemy 2.0 (async)
- **Database**: PostgreSQL 15
- **Cache**: Redis 7
- **Task Queue**: Celery
- **Telegram**: Telethon / Pyrogram

### Frontend
- **Framework**: Vue 3 (Composition API)
- **UI Library**: Element Plus
- **State Management**: Pinia
- **Build Tool**: Vite
- **HTTP Client**: Axios

## API Endpoints

| Category | Prefix | Description |
|----------|--------|-------------|
| Accounts | /api/accounts | Telegram account management |
| Proxies | /api/proxies | Proxy configuration |
| Groups | /api/groups | Group management |
| Keywords | /api/keywords | Keyword management |
| Users | /api/users | User tracking |
| Campaigns | /api/campaigns | Marketing campaigns |
| Rules | /api/rules | Moderation rules |
| Stats | /api/stats | Statistics & reports |

## Environment Variables

See `.env.example` for all available configuration options.

## License

Proprietary
