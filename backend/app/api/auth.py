"""
Authentication API endpoints
"""
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
import jwt
import bcrypt
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db

router = APIRouter()
security = HTTPBearer()


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    token: str
    user: dict


class UserInfo(BaseModel):
    id: int
    username: str
    role: str
    email: Optional[str] = None
    avatar: Optional[str] = None
    created_at: str


class UpdatePasswordRequest(BaseModel):
    old_password: str
    new_password: str


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify password"""
    return bcrypt.checkpw(plain_password.encode('utf-8'), hashed_password.encode('utf-8'))


def get_password_hash(password: str) -> str:
    """Hash password"""
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Create JWT access token"""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(days=7)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.JWT_SECRET, algorithm="HS256")
    return encoded_jwt


def decode_token(token: str) -> dict:
    """Decode JWT token"""
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"])
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired"
        )
    except jwt.JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials"
        )


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db)
):
    """Get current authenticated user"""
    token = credentials.credentials
    payload = decode_token(token)
    user_id = payload.get("sub")

    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials"
        )

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


@router.post("/auth/login", response_model=dict)
async def login(request: LoginRequest, db: AsyncSession = Depends(get_db)):
    """User login"""
    result = await db.execute(
        text("SELECT id, username, password, role, email, avatar FROM admin_user WHERE username = :username"),
        {"username": request.username}
    )
    user = result.fetchone()

    if not user or not verify_password(request.password, user[2]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误"
        )

    # Create access token
    access_token = create_access_token(data={"sub": str(user[0])})

    return {
        "code": 0,
        "message": "登录成功",
        "data": {
            "token": access_token,
            "user": {
                "id": user[0],
                "username": user[1],
                "role": user[3],
                "email": user[4],
                "avatar": user[5]
            }
        }
    }


@router.post("/auth/logout")
async def logout(current_user: dict = Depends(get_current_user)):
    """User logout"""
    return {
        "code": 0,
        "message": "退出成功",
        "data": None
    }


@router.get("/auth/user", response_model=dict)
async def get_user_info(current_user: dict = Depends(get_current_user)):
    """Get current user info"""
    return {
        "code": 0,
        "message": "success",
        "data": current_user
    }


@router.put("/auth/password")
async def update_password(
    request: UpdatePasswordRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Update user password"""
    # Verify old password
    result = await db.execute(
        text("SELECT password FROM admin_user WHERE id = :user_id"),
        {"user_id": current_user["id"]}
    )
    row = result.fetchone()

    if not row or not verify_password(request.old_password, row[0]):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="原密码错误"
        )

    # Update password
    new_password_hash = get_password_hash(request.new_password)
    await db.execute(
        text("UPDATE admin_user SET password = :password, updated_at = NOW() WHERE id = :user_id"),
        {"password": new_password_hash, "user_id": current_user["id"]}
    )
    await db.commit()

    return {
        "code": 0,
        "message": "密码修改成功",
        "data": None
    }
