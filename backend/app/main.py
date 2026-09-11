"""
Vanguard Backend Application Entry Point

XBoard Telegram Bot Matrix - Main Application Module
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import (
    account_personas,
    accounts,
    acquisition,
    ad_only_recommendations,
    auth,
    automation,
    broadcasts,
    campaigns,
    group_governance,
    group_search_keywords,
    groups,
    guardian_bots,
    keywords,
    managed_groups,
    moderation,
    moderation_sensitive_keywords,
    owned_group_audit,
    owned_group_bots,
    owned_group_controls,
    owned_group_governance,
    owned_group_invites,
    owned_group_messages,
    owned_group_operations,
    owned_groups,
    private_chats,
    proxies,
    punishments,
    qq,
    resource_search,
    rules,
    stats,
    sub2api_alerts,
    users,
    verification,
    workers,
    xboard,
)
from app.api import (
    websocket as websocket_router,
)
from app.api.safety_gate import router as safety_gate_router
from app.api.settings import router as settings_router
from app.api.websocket import start_redis_bridge, stop_redis_bridge
from app.core.account.proxy_policy_events import (
    start_account_proxy_policy_listener,
    stop_account_proxy_policy_listener,
)
from app.core.config import settings
from app.core.database import close_db, init_db
from app.core.persona_observability import (
    refresh_persona_configured_gauge_at_startup,
)
from app.core.redis import close_redis, init_redis
from app.core.security import get_current_user
from app.integrations.sub2api import close_all_sub2api_clients
from app.integrations.xboard import close_all_xboard_clients


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan manager for startup and shutdown events."""
    # Startup
    await init_db(create_tables=not settings.is_production)
    try:
        await refresh_persona_configured_gauge_at_startup()
    except Exception:
        pass
    await init_redis()
    await start_account_proxy_policy_listener()
    await start_redis_bridge()

    # Initialize Telegram client pools
    from app.core.account import AccountPool
    app.state.account_pool = AccountPool()

    yield

    # Shutdown
    await app.state.account_pool.close_all()
    await close_all_sub2api_clients()
    await close_all_xboard_clients()
    await stop_redis_bridge()
    await stop_account_proxy_policy_listener()
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
app.include_router(accounts, prefix="/api/accounts", tags=["Accounts"], dependencies=[Depends(get_current_user)])
app.include_router(proxies, prefix="/api/proxies", tags=["Proxies"], dependencies=[Depends(get_current_user)])
app.include_router(groups, prefix="/api/groups", tags=["Groups"], dependencies=[Depends(get_current_user)])
app.include_router(keywords, prefix="/api/keywords", tags=["Keywords"], dependencies=[Depends(get_current_user)])
app.include_router(users, prefix="/api/users", tags=["Users"], dependencies=[Depends(get_current_user)])
app.include_router(campaigns, prefix="/api/campaigns", tags=["Campaigns"], dependencies=[Depends(get_current_user)])
app.include_router(rules, prefix="/api/rules", tags=["Rules"], dependencies=[Depends(get_current_user)])
app.include_router(moderation, prefix="/api/moderation", tags=["审核管理"], dependencies=[Depends(get_current_user)])
app.include_router(stats, prefix="/api/stats", tags=["Stats"], dependencies=[Depends(get_current_user)])
app.include_router(settings_router, prefix="/api/settings", tags=["Settings"], dependencies=[Depends(get_current_user)])
app.include_router(websocket_router, prefix="/api/ws", tags=["WebSocket"])
app.include_router(verification, prefix="/api/verification", tags=["Verification"], dependencies=[Depends(get_current_user)])
app.include_router(punishments, prefix="/api/punishments", tags=["Punishments"], dependencies=[Depends(get_current_user)])
app.include_router(acquisition, prefix="/api/acquisition", tags=["Acquisition"], dependencies=[Depends(get_current_user)])
app.include_router(broadcasts, prefix="/api/broadcasts", tags=["Broadcasts"], dependencies=[Depends(get_current_user)])
app.include_router(xboard, prefix="/api/v1", tags=["XBoard"])
app.include_router(automation, prefix="/api/automation", tags=["Automation"], dependencies=[Depends(get_current_user)])
app.include_router(ad_only_recommendations, prefix="/api/automation", tags=["Ad-only Handover"], dependencies=[Depends(get_current_user)])
app.include_router(group_search_keywords, prefix="/api/group-search-keywords", tags=["Group Search Keywords"], dependencies=[Depends(get_current_user)])
app.include_router(guardian_bots, prefix="/api/guardian-bots", tags=["Guardian Bots"], dependencies=[Depends(get_current_user)])
app.include_router(managed_groups, prefix="/api/managed-groups", tags=["Managed Groups"], dependencies=[Depends(get_current_user)])
app.include_router(group_governance, prefix="/api/group-governance", tags=["Group Governance"], dependencies=[Depends(get_current_user)])
app.include_router(moderation_sensitive_keywords, prefix="/api/moderation-sensitive-keywords", tags=["Moderation Sensitive Keywords"], dependencies=[Depends(get_current_user)])
app.include_router(workers, prefix="/api/workers", tags=["Execution Workers"])
app.include_router(qq, prefix="/api/qq", tags=["NapCat OneBot"], dependencies=[Depends(get_current_user)])
app.include_router(private_chats, prefix="/api/private-chats", tags=["Telegram Private Chats"], dependencies=[Depends(get_current_user)])
app.include_router(sub2api_alerts, prefix="/api/integrations/sub2api", tags=["Sub2API Alerts"])
app.include_router(safety_gate_router, prefix="/api/safety-gate", tags=["P0 Safety Gate"], dependencies=[Depends(get_current_user)])
app.include_router(resource_search, prefix="/api/resource-search", tags=["Resource Search"], dependencies=[Depends(get_current_user)])
app.include_router(owned_groups, prefix="/api/owned-groups", tags=["Owned Groups"], dependencies=[Depends(get_current_user)])
app.include_router(owned_group_controls, prefix="/api/owned-groups", tags=["Owned Group Controls"], dependencies=[Depends(get_current_user)])
app.include_router(owned_group_audit, prefix="/api/owned-groups", tags=["Owned Group Audit"], dependencies=[Depends(get_current_user)])
app.include_router(owned_group_bots, prefix="/api/owned-groups", tags=["Owned Group Bot Profiles"], dependencies=[Depends(get_current_user)])
app.include_router(owned_group_invites, prefix="/api/owned-groups", tags=["Owned Group Invite Links"], dependencies=[Depends(get_current_user)])
app.include_router(owned_group_governance, prefix="/api/owned-groups", tags=["Owned Group Governance"], dependencies=[Depends(get_current_user)])
app.include_router(owned_group_messages, prefix="/api/owned-groups", tags=["Owned Group Messaging"], dependencies=[Depends(get_current_user)])
app.include_router(account_personas, prefix="/api/accounts", tags=["Account AI Persona"], dependencies=[Depends(get_current_user)])
app.include_router(owned_group_operations, prefix="/api/owned-groups", tags=["Owned Group Operations Center"], dependencies=[Depends(get_current_user)])


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.DEBUG,
        workers=1 if settings.DEBUG else 4,
    )


