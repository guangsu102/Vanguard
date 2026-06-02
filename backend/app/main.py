"""
Vanguard Backend Application Entry Point

XBoard Telegram Bot Matrix - Main Application Module
"""

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import (
    accounts,
    proxies,
    groups,
    keywords,
    users,
    campaigns,
    rules,
    stats,
    websocket,
    moderation,
    verification,
    punishments,
    acquisition,
    broadcasts,
    xboard,
    auth,
    automation,
    group_governance,
    group_search_keywords,
    guardian_bots,
    managed_groups,
    moderation_sensitive_keywords,
    workers,
)
from app.api.settings import router as settings_router
from app.core.config import settings
from app.core.database import init_db, close_db
from app.core.redis import init_redis, close_redis
from app.integrations.xboard import close_all_xboard_clients


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan manager for startup and shutdown events."""
    # Startup
    await init_db(create_tables=not settings.is_production)
    await init_redis()

    # Initialize Telegram client pools
    from app.core.account import AccountPool
    app.state.account_pool = AccountPool()
    
    yield
    
    # Shutdown
    await app.state.account_pool.close_all()
    await close_all_xboard_clients()
    await close_redis()
    await close_db()


# Create FastAPI application
app = FastAPI(
    title="Vanguard API",
    description="XBoard Telegram Bot Matrix Backend API",
    version="1.0.0",
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url="/redoc" if settings.DEBUG else None,
    lifespan=lifespan,
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Global exception handler
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Handle uncaught exceptions."""
    import structlog
    logger = structlog.get_logger()
    logger.error("unhandled_exception", error=str(exc), path=request.url.path)
    
    return JSONResponse(
        status_code=500,
        content={"code": 5000, "message": "Internal server error", "data": None}
    )


# Health check endpoint
@app.get("/health")
async def health_check() -> dict:
    """Health check endpoint for load balancers and monitoring."""
    return {"status": "healthy", "version": "1.0.0"}


# Include API routers
app.include_router(auth, prefix="/api", tags=["Authentication"])
app.include_router(accounts, prefix="/api/accounts", tags=["Accounts"])
app.include_router(proxies, prefix="/api/proxies", tags=["Proxies"])
app.include_router(groups, prefix="/api/groups", tags=["Groups"])
app.include_router(keywords, prefix="/api/keywords", tags=["Keywords"])
app.include_router(users, prefix="/api/users", tags=["Users"])
app.include_router(campaigns, prefix="/api/campaigns", tags=["Campaigns"])
app.include_router(rules, prefix="/api/rules", tags=["Rules"])
app.include_router(moderation, prefix="/api/moderation", tags=["审核管理"])
app.include_router(stats, prefix="/api/stats", tags=["Stats"])
app.include_router(settings_router, prefix="/api/settings", tags=["Settings"])
app.include_router(websocket, prefix="/api/ws", tags=["WebSocket"])
app.include_router(verification, prefix="/api/verification", tags=["Verification"])
app.include_router(punishments, prefix="/api/punishments", tags=["Punishments"])
app.include_router(acquisition, prefix="/api/acquisition", tags=["Acquisition"])
app.include_router(broadcasts, prefix="/api/broadcasts", tags=["Broadcasts"])
app.include_router(xboard, prefix="/api/v1", tags=["XBoard"])
app.include_router(automation, prefix="/api/automation", tags=["Automation"])
app.include_router(group_search_keywords, prefix="/api/group-search-keywords", tags=["Group Search Keywords"])
app.include_router(guardian_bots, prefix="/api/guardian-bots", tags=["Guardian Bots"])
app.include_router(managed_groups, prefix="/api/managed-groups", tags=["Managed Groups"])
app.include_router(group_governance, prefix="/api/group-governance", tags=["Group Governance"])
app.include_router(moderation_sensitive_keywords, prefix="/api/moderation-sensitive-keywords", tags=["Moderation Sensitive Keywords"])
app.include_router(workers, prefix="/api/workers", tags=["Telegram Workers"])


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.DEBUG,
        workers=1 if settings.DEBUG else 4,
    )
