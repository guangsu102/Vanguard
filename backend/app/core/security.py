"""
API Security Module

Centralized authentication dependencies for Vanguard API endpoints.
"""
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db

security = HTTPBearer()


def _jwt_secret() -> str:
    return settings.JWT_SECRET or settings.SECRET_KEY


def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    """Create a JWT access token using the central signing configuration."""
    import jwt

    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(days=7))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, _jwt_secret(), algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    """Decode JWT token."""
    import jwt
    try:
        payload = jwt.decode(
            token,
            _jwt_secret(),
            algorithms=[settings.JWT_ALGORITHM]
        )
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired"
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token"
        )


def verify_access_token(token: str) -> dict | None:
    """Verify an access token for non-HTTP dependency contexts such as WebSocket."""
    if not token:
        return None

    try:
        payload = decode_token(token)
    except HTTPException:
        return None

    if not payload.get("sub"):
        return None
    return payload


def _get_user_id_from_payload(payload: dict) -> int:
    user_id = payload.get("sub")

    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials"
        )

    try:
        return int(user_id)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials"
        ) from None


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db),
):
    """Get current authenticated user from JWT token."""
    token = credentials.credentials
    payload = decode_token(token)
    user_id = _get_user_id_from_payload(payload)

    result = await db.execute(
        text("SELECT id, username, role, email, avatar, created_at FROM admin_user WHERE id = :user_id"),
        {"user_id": user_id}
    )
    user = result.fetchone()

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found"
        )

    return {
        "id": user[0],
        "username": user[1],
        "role": user[2],
        "email": user[3],
        "avatar": user[4],
        "created_at": user[5].isoformat() if user[5] else None
    }


async def require_admin(
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Require admin role. Raises 403 if user is not admin."""
    if current_user.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required"
        )
    return current_user
