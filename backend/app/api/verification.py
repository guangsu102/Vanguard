"""
Verification API Router

RESTful API for verification sessions and group verification config.
"""

from datetime import datetime, timedelta
from typing import Optional
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select, func, desc, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.modules.guardian.models import (
    VerificationSession,
    VerificationType,
    VerificationState,
    GroupVerificationConfig,
)


router = APIRouter()


# =============================================================================
# Request/Response Models
# =============================================================================

class VerificationCreate(BaseModel):
    """Verification session creation request."""
    user_id: int = Field(..., description="User ID")
    chat_id: int = Field(..., description="Group chat ID")
    verify_type: str = Field(default="captcha", description="Verification type: captcha/question")
    question: Optional[str] = Field(None, description="Question for question type")
    answer: Optional[str] = Field(None, description="Answer for question type")


class VerificationVerify(BaseModel):
    """Verification request."""
    session_id: str = Field(..., description="Verification session ID")
    captcha_code: Optional[str] = Field(None, description="Captcha answer")
    answer: Optional[str] = Field(None, description="Question answer")


class VerificationResponse(BaseModel):
    """Verification session response."""
    id: int
    session_id: str
    user_id: int
    chat_id: int
    verify_type: str
    state: str
    question: Optional[str] = None
    attempt_count: int
    max_attempts: int
    expires_at: str
    created_at: str


class VerificationConfigCreate(BaseModel):
    """Verification config creation request."""
    group_id: int = Field(..., description="Group ID")
    enable_verification: bool = Field(default=False)
    verification_type: str = Field(default="captcha")
    questions: Optional[list[dict]] = Field(None, description="QA pairs")
    welcome_message: Optional[str] = Field(None)
    timeout_minutes: int = Field(default=5)
    whitelist_bypass: bool = Field(default=True)


class VerificationConfigResponse(BaseModel):
    """Verification config response."""
    id: int
    group_id: int
    enable_verification: bool
    verification_type: str
    questions: Optional[list[dict]] = None
    welcome_message: Optional[str] = None
    timeout_minutes: int
    whitelist_bypass: bool
    updated_at: str


# =============================================================================
# Helper Functions
# =============================================================================

def _session_to_response(session: VerificationSession) -> VerificationResponse:
    """Convert session to response."""
    return VerificationResponse(
        id=session.id,
        session_id=session.session_id,
        user_id=session.user_id,
        chat_id=session.chat_id,
        verify_type=session.verify_type.value,
        state=session.state.value,
        question=session.question,
        attempt_count=session.attempt_count,
        max_attempts=session.max_attempts,
        expires_at=session.expires_at.isoformat() if session.expires_at else "",
        created_at=session.created_at.isoformat() if session.created_at else "",
    )


# =============================================================================
# Verification Session Endpoints
# =============================================================================

@router.post("", response_model=VerificationResponse, status_code=status.HTTP_201_CREATED)
async def create_verification(
    request: VerificationCreate,
    db: AsyncSession = Depends(get_db),
) -> VerificationResponse:
    """
    Create a new verification session.

    For captcha type, a random code will be generated.
    For question type, the provided question and answer will be used.
    """
    session_id = str(uuid.uuid4())
    timeout_minutes = 5

    verify_type = VerificationType(request.verify_type)
    state = VerificationState.PENDING

    expires_at = datetime.utcnow() + timedelta(minutes=timeout_minutes)

    session = VerificationSession(
        session_id=session_id,
        user_id=request.user_id,
        chat_id=request.chat_id,
        verify_type=verify_type,
        state=state,
        question=request.question,
        answer=request.answer,
        expires_at=expires_at,
    )

    db.add(session)
    await db.commit()
    await db.refresh(session)

    return _session_to_response(session)


@router.post("/verify")
async def verify_session(
    request: VerificationVerify,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Verify a verification session."""
    result = await db.execute(
        select(VerificationSession).where(VerificationSession.session_id == request.session_id)
    )
    session = result.scalar_one_or_none()

    if not session:
        raise HTTPException(status_code=404, detail="Verification session not found")

    if session.state != VerificationState.PENDING:
        raise HTTPException(status_code=400, detail="Session is not in pending state")

    if datetime.utcnow() > session.expires_at:
        session.state = VerificationState.EXPIRED
        await db.commit()
        raise HTTPException(status_code=400, detail="Verification session expired")

    if session.attempt_count >= session.max_attempts:
        session.state = VerificationState.FAILED
        await db.commit()
        raise HTTPException(status_code=400, detail="Maximum attempts exceeded")

    session.attempt_count += 1

    is_correct = False
    if session.verify_type == VerificationType.CAPTCHA:
        is_correct = request.captcha_code and request.captcha_code.upper() == session.captcha_code
    else:
        is_correct = request.answer and request.answer.strip().lower() == session.answer.strip().lower()

    if is_correct:
        session.state = VerificationState.PASSED
        session.completed_at = datetime.utcnow()
        await db.commit()
        return {
            "code": 0,
            "message": "Verification passed",
            "data": {"state": "passed", "session_id": session.session_id}
        }
    else:
        await db.commit()
        remaining = session.max_attempts - session.attempt_count
        if remaining <= 0:
            session.state = VerificationState.FAILED
            await db.commit()
            raise HTTPException(status_code=400, detail="Verification failed - maximum attempts exceeded")

        return {
            "code": 0,
            "message": f"Incorrect answer, {remaining} attempts remaining",
            "data": {"state": "pending", "remaining_attempts": remaining}
        }


@router.get("/{session_id}", response_model=VerificationResponse)
async def get_verification(
    session_id: str,
    db: AsyncSession = Depends(get_db),
) -> VerificationResponse:
    """Get verification session by ID."""
    result = await db.execute(
        select(VerificationSession).where(VerificationSession.session_id == session_id)
    )
    session = result.scalar_one_or_none()

    if not session:
        raise HTTPException(status_code=404, detail="Verification session not found")

    return _session_to_response(session)


@router.get("/user/{user_id}")
async def get_user_verifications(
    user_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Get all verification sessions for a user."""
    result = await db.execute(
        select(VerificationSession)
        .where(VerificationSession.user_id == user_id)
        .order_by(desc(VerificationSession.created_at))
        .limit(10)
    )
    sessions = result.scalars().all()

    return {
        "code": 0,
        "message": "success",
        "data": [_session_to_response(s) for s in sessions]
    }


# =============================================================================
# Verification Config Endpoints
# =============================================================================

@router.get("/config/{group_id}", response_model=VerificationConfigResponse)
async def get_verification_config(
    group_id: int,
    db: AsyncSession = Depends(get_db),
) -> VerificationConfigResponse:
    """Get verification config for a group."""
    result = await db.execute(
        select(GroupVerificationConfig).where(GroupVerificationConfig.group_id == group_id)
    )
    config = result.scalar_one_or_none()

    if not config:
        raise HTTPException(status_code=404, detail="Verification config not found")

    return VerificationConfigResponse(
        id=config.id,
        group_id=config.group_id,
        enable_verification=config.enable_verification,
        verification_type=config.verification_type.value,
        questions=config.get_questions(),
        welcome_message=config.welcome_message,
        timeout_minutes=config.timeout_minutes,
        whitelist_bypass=config.whitelist_bypass,
        updated_at=config.updated_at.isoformat() if config.updated_at else "",
    )


@router.post("/config", response_model=VerificationConfigResponse, status_code=status.HTTP_201_CREATED)
async def create_or_update_config(
    request: VerificationConfigCreate,
    db: AsyncSession = Depends(get_db),
) -> VerificationConfigResponse:
    """Create or update verification config for a group."""
    result = await db.execute(
        select(GroupVerificationConfig).where(GroupVerificationConfig.group_id == request.group_id)
    )
    config = result.scalar_one_or_none()

    if config:
        config.enable_verification = request.enable_verification
        config.verification_type = VerificationType(request.verification_type)
        if request.questions:
            config.set_questions(request.questions)
        if request.welcome_message is not None:
            config.welcome_message = request.welcome_message
        config.timeout_minutes = request.timeout_minutes
        config.whitelist_bypass = request.whitelist_bypass
    else:
        config = GroupVerificationConfig(
            group_id=request.group_id,
            enable_verification=request.enable_verification,
            verification_type=VerificationType(request.verification_type),
            timeout_minutes=request.timeout_minutes,
            whitelist_bypass=request.whitelist_bypass,
        )
        if request.questions:
            config.set_questions(request.questions)
        if request.welcome_message is not None:
            config.welcome_message = request.welcome_message
        db.add(config)

    await db.commit()
    await db.refresh(config)

    return VerificationConfigResponse(
        id=config.id,
        group_id=config.group_id,
        enable_verification=config.enable_verification,
        verification_type=config.verification_type.value,
        questions=config.get_questions(),
        welcome_message=config.welcome_message,
        timeout_minutes=config.timeout_minutes,
        whitelist_bypass=config.whitelist_bypass,
        updated_at=config.updated_at.isoformat() if config.updated_at else "",
    )


@router.delete("/config/{group_id}")
async def delete_verification_config(
    group_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Delete verification config for a group."""
    result = await db.execute(
        select(GroupVerificationConfig).where(GroupVerificationConfig.group_id == group_id)
    )
    config = result.scalar_one_or_none()

    if not config:
        raise HTTPException(status_code=404, detail="Verification config not found")

    await db.delete(config)
    await db.commit()

    return {"code": 0, "message": "Config deleted"}
