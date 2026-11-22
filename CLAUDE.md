# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Vanguard is a Telegram automation marketing matrix system for XBoard. It manages multiple Telegram accounts for lead generation, customer service, and community moderation. The system consists of three main components:

- **Backend API** (FastAPI/Python 3.12+): REST API, business logic, database operations
- **Telegram Workers** (backend/app/workers): Multi-account Telegram bot automation
- **Frontend** (Vue 3 + Element Plus): Web management interface

## Development Commands

### Backend (FastAPI)

```bash
cd backend

# Install dependencies
pip install -r requirements.txt

# Run development server
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Run tests
pytest tests/ -v
pytest tests/ -v --cov=app --cov-report=html

# Database migrations
alembic revision --autogenerate -m "description"
alembic upgrade head
alembic downgrade -1

# Code quality
ruff check app/
ruff format app/
mypy app/
```

### Frontend (Vue 3)

```bash
cd frontend

# Install dependencies
npm install

# Development server
npm run dev

# Build for production
npm run build
npm run build:check  # Build with type checking

# Preview production build
npm run preview

# Linting and type checking
npm run lint
npm run type-check

# Tests
npm run test
npm run test:watch
```

### Telegram Workers

Telegram automation runs through `backend/app/workers/telegram_worker.py`; use the backend dependencies and tests.

### Docker Compose

```bash
# Start all services (development)
docker-compose up -d

# Start specific service
docker-compose up -d backend
docker-compose up -d postgres redis

# View logs
docker-compose logs -f backend

# Rebuild and restart
docker-compose up -d --build

# Stop all services
docker-compose down

# Production deployment
docker-compose -f docker-compose.production.yml up -d
```

## Architecture

### Backend Structure

```
backend/app/
├── api/                    # API route handlers
│   ├── accounts.py         # Telegram account management
│   ├── proxies.py          # Proxy configuration
│   ├── groups.py           # Group management
│   ├── keywords.py         # Keyword triggers
│   ├── users.py            # User tracking
│   ├── campaigns.py        # Marketing campaigns
│   ├── rules.py            # Moderation rules
│   ├── stats.py            # Statistics & analytics
│   ├── auth.py             # Authentication
│   ├── websocket.py        # WebSocket connections
│   └── xboard.py           # XBoard integration
├── core/                   # Core business logic
│   ├── account/            # Account management & pooling
│   ├── ai/                 # LLM integrations (copywriting, intent classification)
│   ├── campaign/           # Campaign execution
│   ├── group/              # Group operations
│   ├── keyword/            # Keyword matching engine
│   ├── message/            # Message handling
│   ├── network/            # Proxy management
│   ├── scheduler/          # Task scheduling
│   └── user/               # User management
├── integrations/           # External service integrations
│   ├── llm/                # OpenAI, Anthropic
│   ├── telegram/           # Telethon, Pyrogram clients
│   └── xboard/             # XBoard API client
├── modules/                # Feature modules
│   ├── acquisition/        # Lead generation
│   └── guardian/           # Community moderation
├── config.py               # Configuration management
├── database.py             # SQLAlchemy async setup
├── redis.py                # Redis connection
└── main.py                 # FastAPI application entry
```

### Frontend Structure

```
frontend/src/
├── views/                  # Page components
│   ├── Dashboard.vue       # Overview & statistics
│   ├── Accounts.vue        # Telegram account management
│   ├── Proxies.vue         # Proxy configuration
│   ├── Groups.vue          # Group management
│   ├── Keywords.vue        # Keyword triggers
│   ├── Users.vue           # User tracking
│   ├── Campaigns.vue       # Campaign management
│   ├── Rules.vue           # Moderation rules
│   ├── Stats.vue           # Analytics
│   ├── Settings.vue        # System settings
│   ├── Login.vue           # Authentication
│   └── Layout.vue          # Main layout wrapper
├── components/             # Reusable components
│   ├── TableCard.vue       # Data table wrapper
│   ├── SearchBar.vue       # Search filters
│   ├── FormDrawer.vue      # Form drawer
│   ├── StatusTag.vue       # Status indicator
│   └── ECharts.vue         # Chart wrapper
├── stores/                 # Pinia state management
│   ├── auth.ts             # Authentication state
│   ├── account.ts          # Account state
│   ├── proxy.ts            # Proxy state
│   └── ...
├── api/                    # API client modules
│   ├── client.ts           # Axios instance
│   ├── accounts.ts         # Account API
│   ├── proxies.ts          # Proxy API
│   └── ...
└── router/                 # Vue Router configuration
```

### Telegram Worker Entry

`backend/app/workers/telegram_worker.py` runs acquisition and guardian workloads using the shared account pool.

## Key Architectural Patterns

### Account Pooling
The backend maintains a pool of Telegram client connections (`AccountPool` in `app/core/account/pool.py`). Accounts are loaded on startup and reused across requests. When working with Telegram operations, always use the pool rather than creating new clients.

### Async/Await Throughout
Both backend and bot use async/await patterns. Database operations use SQLAlchemy 2.0 async, Redis uses aioredis, and Telegram clients use async APIs (Telethon/Pyrogram).

### Keyword Matching Engine
Keywords are stored in the database with regex patterns. The matching engine (`app/core/keyword/`) loads keywords into memory on startup and uses Redis for deduplication to prevent duplicate triggers within a cooldown window.

### Proxy Rotation
Proxies are managed in `app/core/network/` and rotated automatically for Telegram accounts. Each account can be bound to a specific proxy, or use the proxy pool for automatic rotation.

### XBoard Integration
The system integrates with XBoard (VPN panel) via webhooks and API calls. XBoard events (new orders, renewals) trigger actions in Vanguard (send welcome messages, issue trial accounts, etc.).

## Environment Configuration

### Required Environment Variables

**Backend (.env)**:
- `DATABASE_URL` - PostgreSQL connection string (async format: `postgresql+asyncpg://...`)
- `REDIS_URL` - Redis connection string with password
- `JWT_SECRET` - Secret key for JWT tokens (minimum 64 characters)
- `TELEGRAM_API_ID` - Telegram API ID from my.telegram.org
- `TELEGRAM_API_HASH` - Telegram API hash
- `BOT_TOKEN` - Telegram bot token from @BotFather
- `CORS_ORIGINS` - Comma-separated list of allowed origins
- `VANGUARD_SIGNING_SECRET` - Shared HMAC secret for inbound XBoard requests
- `VANGUARD_CALLBACK_SIGNING_SECRET` - Shared HMAC secret for XBoard callbacks

**Frontend (.env.development, .env.production)**:
- `VITE_API_BASE_URL` - Backend API base URL. Production uses the same-origin `/api` path at `https://vanguard.dh113.xyz`.

### Configuration Files
- `backend/alembic.ini` - Database migration configuration
- `frontend/vite.config.ts` - Vite build configuration
- `docker-compose.yml` - Development orchestration
- `docker-compose.production.yml` - Production orchestration

## Database Migrations

Migrations are managed with Alembic. When modifying SQLAlchemy models:

1. Create migration: `cd backend && alembic revision --autogenerate -m "description"`
2. Review generated migration in `backend/migrations/versions/`
3. Apply migration: `alembic upgrade head`
4. Rollback if needed: `alembic downgrade -1`

**Important**: Always review auto-generated migrations before applying. Alembic may not detect all changes (e.g., column renames, constraint changes).

## Testing

### Backend Tests
- Located in `backend/tests/`
- Use pytest with async support (`pytest-asyncio`)
- Database tests use pytest-docker for isolated PostgreSQL instances
- Run with coverage: `pytest --cov=app --cov-report=html`

### Frontend Tests
- Located in `frontend/src/**/*.spec.ts`
- Use Vitest + Vue Test Utils
- Run with: `npm run test` or `npm run test:watch`

## Common Issues

### Frontend: `toUpperCase()` on undefined
When displaying data in tables, always use optional chaining for potentially undefined values:
```javascript
// Bad
{{ row.protocol.toUpperCase() }}

// Good
{{ row.protocol?.toUpperCase() || 'N/A' }}
```

### Backend: Database Connection Pool Exhausted
If you see "connection pool exhausted" errors, check for:
- Unclosed database sessions (always use `async with get_db()`)
- Long-running queries blocking connections
- Insufficient pool size in `DATABASE_URL` (add `?pool_size=20&max_overflow=10`)

### Bot: Session Files
Telegram session files (`.session`) are stored in `backend/sessions/`. These contain authentication credentials and should never be committed to git. If a session becomes invalid, delete the file and re-authenticate.

### Proxy Connection Issues
If Telegram accounts fail to connect:
1. Check proxy health: `GET /api/proxies` and verify `status` field
2. Test proxy latency: `POST /api/proxies/{id}/test`
3. Refresh proxy status: `POST /api/proxies/refresh-status`
4. Check proxy binding: Each account can be bound to a specific proxy in the database

## Deployment

Production runs on `oracle4c24g-us` (`147.224.11.146`) at [vanguard.dh113.xyz](https://vanguard.dh113.xyz). The former `xd` host has expired.

Use the canonical runtime at `/opt/shared-postgres/stacks/vanguard/compose.json`. Deploy only the intended application services with `--no-deps` and preserve shared PostgreSQL, Redis, and other site routes.

Cloudflare uses Full (strict) mode and the `dh113-origin` certificate on K3s Traefik. Follow the [production deployment guide](SERVER_DEPLOY.md) for release commands, verification, and certificate details.

## Code Style

### Backend (Python)
- Use Ruff for linting and formatting
- Type hints required for function signatures
- Async/await for all I/O operations
- Pydantic models for request/response validation
- Structured logging with structlog

### Frontend (TypeScript/Vue)
- Vue 3 Composition API with `<script setup>`
- TypeScript strict mode
- ESLint + Prettier for formatting
- Pinia for state management (not Vuex)
- Element Plus for UI components

## Security Notes

- Never commit `.env` files or session files
- JWT secrets must be at least 64 characters
- All API endpoints except `/health` and `/api/auth/login` require authentication
- CORS is configured to allow only specified origins
- Passwords are hashed with bcrypt before storage
- Telegram API credentials are sensitive and should be rotated if exposed
