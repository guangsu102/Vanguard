"""
Managed group binding API for guardian bots.
"""

from datetime import datetime
import re
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.guardian_validation import ensure_guardian_bot_account
from app.core.account.models import (
    AccountRiskLevel,
    AccountStatus,
    AccountType,
    GuardianBotProfile,
    TelegramAccount,
)
from app.core.account.pool import get_account_pool
from app.core.account.risk_guard import AccountRiskGuard
from app.core.account.telegram_execution import TelegramExecutionError, TelegramExecutionService
from app.core.database import get_db
from app.core.group.models import Group
from app.integrations.telegram.client import TelegramAPIError, TelegramClient, TelegramConfig
from app.modules.guardian.models import (
    ManagedGroupBinding,
    ManagedGroupBindingStatus,
    ManagedGroupBotRole,
)
from app.modules.guardian.sync import (
    ManagedGroupSyncConflict,
    guardian_role_and_status_from_member,
    sync_managed_group_binding,
)

router = APIRouter()


class ManagedGroupBindingCreate(BaseModel):
    group_id: Optional[int] = Field(None, description="内部群ID")
    telegram_group_id: int = Field(..., description="Telegram群组ID")
    title: Optional[str] = None
    username: Optional[str] = None
    member_count: int = 0
    bot_account_id: int
    binding_status: str = Field(default="active")
    bot_role: str = Field(default="admin")
    permissions_snapshot: Optional[dict] = None
    chat_type: str = Field(default="group", pattern="^(group|supergroup|channel)$")


class ManagedGroupBindingUpdate(BaseModel):
    binding_status: Optional[str] = None
    bot_role: Optional[str] = None
    permissions_snapshot: Optional[dict] = None
    last_synced_at: Optional[datetime] = None


class ManagedGroupBindingResponse(BaseModel):
    id: int
    group_id: int
    telegram_group_id: int
    title: Optional[str] = None
    username: Optional[str] = None
    member_count: int
    bot_account_id: int
    bot_identifier: str
    bot_display_name: Optional[str] = None
    binding_status: str
    bot_role: str
    permissions_snapshot: Optional[dict] = None
    bound_at: str
    last_synced_at: Optional[str] = None
    chat_type: str = "group"
    all_members_muted: bool = False


class ManagedGroupBindingListResponse(BaseModel):
    code: int = 0
    message: str = "success"
    data: list[ManagedGroupBindingResponse]
    total: int


class ManagedGroupPinnedMessageRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=4096, description="要发送并置顶的消息内容")
    parse_mode: str = Field(
        default="Markdown", description="Telegram parse_mode，如 Markdown、HTML"
    )
    disable_web_page_preview: bool = Field(default=False, description="是否关闭链接预览")
    disable_notification: bool = Field(default=True, description="置顶是否静默通知")
    button_text: Optional[str] = Field(default=None, max_length=64)
    button_url: Optional[str] = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def validate_channel_button(self) -> "ManagedGroupPinnedMessageRequest":
        self.button_text = self.button_text.strip() if self.button_text else None
        self.button_url = self.button_url.strip() if self.button_url else None
        if bool(self.button_text) != bool(self.button_url):
            raise ValueError("button_text and button_url must be provided together")
        if self.button_url and not re.fullmatch(
            r"https://t\.me/[A-Za-z][A-Za-z0-9_]{4,31}/?", self.button_url
        ):
            raise ValueError("button_url must be a public Telegram channel URL")
        return self


class ManagedGroupPinnedMessageResponse(BaseModel):
    code: int = 0
    message: str = "success"
    data: dict


class ManagedGroupPinnedMessageConfig(BaseModel):
    enabled: bool = True
    content: str = Field(default="", max_length=4096, description="默认置顶公告内容")
    parse_mode: str = Field(
        default="", description="Telegram parse_mode，如 Markdown、HTML 或空字符串"
    )
    disable_web_page_preview: bool = False
    disable_notification: bool = True
    button_text: Optional[str] = Field(default=None, max_length=64)
    button_url: Optional[str] = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def validate_channel_button(self) -> "ManagedGroupPinnedMessageConfig":
        self.button_text = self.button_text.strip() if self.button_text else None
        self.button_url = self.button_url.strip() if self.button_url else None
        if bool(self.button_text) != bool(self.button_url):
            raise ValueError("button_text and button_url must be provided together")
        if self.button_url and not re.fullmatch(
            r"https://t\.me/[A-Za-z][A-Za-z0-9_]{4,31}/?", self.button_url
        ):
            raise ValueError("button_url must be a public Telegram channel URL")
        return self


class ManagedGroupPinnedMessageConfigResponse(BaseModel):
    code: int = 0
    message: str = "success"
    data: ManagedGroupPinnedMessageConfig


class ManagedGroupMuteAllRequest(BaseModel):
    muted: bool


class ManagedChannelMessageRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=4096)
    parse_mode: str = Field(default="", pattern="^(|Markdown|HTML)$")
    disable_web_page_preview: bool = False
    disable_notification: bool = False


class ManagedChannelCreateRequest(BaseModel):
    creator_account_id: int
    bot_account_id: int
    title: str = Field(..., min_length=1, max_length=128)
    about: str = Field(default="", max_length=255)
    is_public: bool = True
    username: Optional[str] = Field(
        default=None, max_length=32, pattern="^[A-Za-z][A-Za-z0-9_]{4,31}$"
    )

    @model_validator(mode="after")
    def validate_public_username(self) -> "ManagedChannelCreateRequest":
        if self.is_public and not self.username:
            raise ValueError("username is required for a public channel")
        if not self.is_public:
            self.username = None
        return self


class ManagedChannelUsernameRequest(BaseModel):
    username: str = Field(default="", max_length=32, pattern="^(|[A-Za-z][A-Za-z0-9_]{4,31})$")


class ManagedOperationResponse(BaseModel):
    code: int = 0
    message: str = "success"
    data: dict


class ManagedGroupSyncConfirmedRequest(BaseModel):
    bot_account_id: int = Field(..., description="要用于扫描的 Guardian Bot 账号ID")
    statuses: list[str] = Field(default_factory=lambda: ["active"], description="要扫描的群池状态")
    limit: int = Field(default=200, ge=1, le=1000)


class ManagedGroupSyncConfirmedResponse(BaseModel):
    code: int = 0
    message: str = "success"
    data: dict


@router.post(
    "/channels", response_model=ManagedOperationResponse, status_code=status.HTTP_201_CREATED
)
async def create_managed_channel(
    request: ManagedChannelCreateRequest,
    db: AsyncSession = Depends(get_db),
) -> ManagedOperationResponse:
    """Create a broadcast channel with a user session and assign its guardian bot."""
    creator = await db.get(TelegramAccount, request.creator_account_id)
    if not creator or creator.account_type != AccountType.PROMOTER:
        raise HTTPException(
            status_code=400, detail="creator_account_id must reference a promoter user account"
        )
    if not creator.is_active or creator.status in {AccountStatus.ERROR, AccountStatus.BANNED}:
        raise HTTPException(status_code=400, detail="Creator account is inactive or unavailable")
    if creator.risk_level in {AccountRiskLevel.FROZEN.value, AccountRiskLevel.QUARANTINED.value}:
        raise HTTPException(status_code=400, detail="Creator account is frozen or quarantined")

    profile = await _enabled_guardian_profile(db, request.bot_account_id)
    bot_client = TelegramClient(TelegramConfig(bot_token=profile.bot_token))
    try:
        bot_user = await bot_client.get_me()
        profile.bot_user_id = bot_user.user_id
        profile.bot_username = bot_user.username or profile.bot_username
    except TelegramAPIError as exc:
        raise HTTPException(
            status_code=400, detail=f"Unable to resolve guardian bot: {exc.message}"
        ) from exc
    finally:
        await bot_client.close()
    if not profile.bot_username:
        raise HTTPException(
            status_code=400, detail="Guardian bot username is required to assign it to a channel"
        )

    account_pool = get_account_pool()
    await account_pool.add_account_from_db(creator)
    wrapper = None
    warnings: list[str] = []
    try:
        wrapper = await account_pool.acquire_by_id(creator.id, purpose="managed_channel_create")
        if wrapper is None:
            raise HTTPException(status_code=400, detail="Creator account session is unavailable")

        execution = TelegramExecutionService(AccountRiskGuard(db))
        channel = await execution.create_channel(
            wrapper,
            request.title.strip(),
            about=request.about.strip(),
        )

        from telethon import functions, types, utils

        telegram_channel_id = int(utils.get_peer_id(channel))
        username = request.username if request.is_public else None
        if username:
            try:
                await wrapper.client(
                    functions.channels.UpdateUsernameRequest(channel=channel, username=username)
                )
            except Exception as exc:
                warnings.append(f"Public username was not set: {exc}")
                username = None

        bot_setup_ok = False
        try:
            bot_entity = await wrapper.client.get_entity(profile.bot_username)
            # A bot cannot join a broadcast channel as a regular member. Promoting it
            # directly both adds it to the channel and grants the required rights.
            await wrapper.client(
                functions.channels.EditAdminRequest(
                    channel=channel,
                    user_id=bot_entity,
                    admin_rights=types.ChatAdminRights(
                        post_messages=True,
                        edit_messages=True,
                        delete_messages=True,
                    ),
                    rank="Vanguard",
                )
            )
            bot_setup_ok = True
        except Exception as exc:
            warnings.append(
                "Guardian bot admin assignment failed: "
                f"{type(exc).__name__}: {exc}"
            )

        public_setup_ok = not request.is_public or bool(username)

        result = await sync_managed_group_binding(
            db,
            bot_account_id=request.bot_account_id,
            telegram_group_id=telegram_channel_id,
            title=request.title.strip(),
            username=username,
            member_count=1,
            binding_status=(
                ManagedGroupBindingStatus.ACTIVE
                if bot_setup_ok and public_setup_ok
                else ManagedGroupBindingStatus.DEGRADED
            ),
            bot_role=(ManagedGroupBotRole.ADMIN if bot_setup_ok else ManagedGroupBotRole.MEMBER),
            permissions_snapshot={
                "source": "managed_channel_create",
                "creator_account_id": creator.id,
                "bot_assignment_complete": bot_setup_ok,
                "requested_username": request.username,
                "channel_visibility": "public" if username else "private",
                "creation_warnings": warnings,
                "created_at": datetime.utcnow().isoformat(),
            },
            chat_type="channel",
            discovery_source="managed_channel_create",
            allow_existing=False,
        )
        await db.commit()
        await db.refresh(result.binding, attribute_names=["group", "bot_account"])
    except HTTPException:
        raise
    except TelegramExecutionError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Channel creation failed: {exc}") from exc
    finally:
        if wrapper is not None:
            await account_pool.release(wrapper)

    return ManagedOperationResponse(
        message="Channel created" if not warnings else "Channel created with follow-up required",
        data={
            "binding": _serialize_binding(result.binding).model_dump(),
            "warnings": warnings,
            "bot_assignment_complete": bot_setup_ok,
            "public_username_complete": public_setup_ok,
        },
    )


def _parse_permissions(raw: Optional[str]) -> Optional[dict]:
    if not raw:
        return None
    import json

    try:
        return json.loads(raw)
    except Exception:
        return {"raw": raw}


def _permissions_dict(binding: ManagedGroupBinding) -> dict[str, Any]:
    parsed = _parse_permissions(binding.permissions_snapshot)
    return parsed if isinstance(parsed, dict) else {}


def _binding_chat_type(binding: ManagedGroupBinding) -> str:
    value = str(_permissions_dict(binding).get("chat_type") or "group")
    return value if value in {"group", "supergroup", "channel"} else "group"


def _all_members_muted(binding: ManagedGroupBinding) -> bool:
    return _permissions_dict(binding).get("all_members_muted") is True


def _lockdown_permissions() -> dict[str, bool]:
    return {
        "can_send_messages": False,
        "can_send_audios": False,
        "can_send_documents": False,
        "can_send_photos": False,
        "can_send_videos": False,
        "can_send_video_notes": False,
        "can_send_voice_notes": False,
        "can_send_polls": False,
        "can_send_other_messages": False,
        "can_add_web_page_previews": False,
        "can_invite_users": False,
    }


def _fallback_unmuted_permissions() -> dict[str, bool]:
    permissions = dict.fromkeys(_lockdown_permissions(), True)
    permissions["can_change_info"] = False
    permissions["can_pin_messages"] = False
    permissions["can_manage_topics"] = False
    return permissions


def _pinned_config_from_binding(binding: ManagedGroupBinding) -> ManagedGroupPinnedMessageConfig:
    snapshot = _permissions_dict(binding)
    config = snapshot.get("pinned_message_config")
    if not isinstance(config, dict):
        config = {}
    return ManagedGroupPinnedMessageConfig(**config)


def _set_permissions_key(binding: ManagedGroupBinding, key: str, value: Any) -> None:
    import json

    snapshot = _permissions_dict(binding)
    snapshot[key] = value
    binding.permissions_snapshot = json.dumps(snapshot, ensure_ascii=False)


def _bot_api_chat_id_candidates(group_id: int) -> list[int]:
    candidates = [int(group_id)]
    if group_id > 0:
        candidates.append(int(f"-100{group_id}"))
    return list(dict.fromkeys(candidates))


def _serialize_binding(binding: ManagedGroupBinding) -> ManagedGroupBindingResponse:
    return ManagedGroupBindingResponse(
        id=binding.id,
        group_id=binding.group_id,
        telegram_group_id=binding.telegram_group_id,
        title=binding.group.title if binding.group else None,
        username=binding.group.username if binding.group else None,
        member_count=binding.group.member_count if binding.group else 0,
        bot_account_id=binding.bot_account_id,
        bot_identifier=binding.bot_account.identifier if binding.bot_account else "",
        bot_display_name=binding.bot_account.display_name if binding.bot_account else None,
        binding_status=binding.binding_status.value,
        bot_role=binding.bot_role.value,
        permissions_snapshot=_parse_permissions(binding.permissions_snapshot),
        bound_at=binding.bound_at.isoformat() if binding.bound_at else "",
        last_synced_at=binding.last_synced_at.isoformat() if binding.last_synced_at else None,
        chat_type=_binding_chat_type(binding),
        all_members_muted=_all_members_muted(binding),
    )


async def _enabled_guardian_profile(db: AsyncSession, account_id: int) -> GuardianBotProfile:
    result = await db.execute(
        select(GuardianBotProfile).where(GuardianBotProfile.account_id == account_id)
    )
    profile = result.scalar_one_or_none()
    if not profile or not profile.enabled:
        raise HTTPException(status_code=400, detail="Guardian bot profile is disabled or missing")
    return profile


async def _channel_creator_account(
    db: AsyncSession,
    binding: ManagedGroupBinding,
) -> TelegramAccount:
    creator_account_id = _permissions_dict(binding).get("creator_account_id")
    try:
        creator_account_id = int(creator_account_id)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail="Channel creator account is missing from the binding",
        ) from None
    account = await db.get(TelegramAccount, creator_account_id)
    if not account or account.account_type != AccountType.PROMOTER:
        raise HTTPException(status_code=400, detail="Channel creator account is unavailable")
    if not account.is_active or account.status in {AccountStatus.ERROR, AccountStatus.BANNED}:
        raise HTTPException(status_code=400, detail="Channel creator account is inactive")
    return account


async def _assert_bot_admin_permission(
    client: TelegramClient,
    profile: GuardianBotProfile,
    chat_id: int,
    permission: str,
) -> dict[str, Any]:
    bot_user = await client.get_me()
    member = await client.get_chat_member(chat_id, bot_user.user_id)
    status_value = member.get("status")
    allowed = status_value == "creator" or (
        status_value == "administrator" and member.get(permission) is True
    )
    if not allowed:
        raise HTTPException(status_code=403, detail=f"Guardian bot lacks {permission} permission")
    profile.bot_user_id = bot_user.user_id
    profile.bot_username = bot_user.username or profile.bot_username
    return member


@router.get("", response_model=ManagedGroupBindingListResponse)
async def list_managed_groups(
    bot_account_id: Optional[int] = None,
    binding_status: Optional[str] = None,
    limit: int = Query(default=100, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> ManagedGroupBindingListResponse:
    query = select(ManagedGroupBinding)
    count_query = select(func.count(ManagedGroupBinding.id))

    if bot_account_id is not None:
        query = query.where(ManagedGroupBinding.bot_account_id == bot_account_id)
        count_query = count_query.where(ManagedGroupBinding.bot_account_id == bot_account_id)

    if binding_status:
        try:
            enum_value = ManagedGroupBindingStatus(binding_status)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid binding_status")
        query = query.where(ManagedGroupBinding.binding_status == enum_value)
        count_query = count_query.where(ManagedGroupBinding.binding_status == enum_value)

    rows = await db.execute(query.order_by(desc(ManagedGroupBinding.id)).limit(limit))
    total = (await db.execute(count_query)).scalar() or 0
    return ManagedGroupBindingListResponse(
        data=[_serialize_binding(item) for item in rows.scalars().all()],
        total=total,
    )


@router.post("/sync-confirmed", response_model=ManagedGroupSyncConfirmedResponse)
async def sync_confirmed_groups_for_bot(
    request: ManagedGroupSyncConfirmedRequest,
    db: AsyncSession = Depends(get_db),
) -> ManagedGroupSyncConfirmedResponse:
    await ensure_guardian_bot_account(db, request.bot_account_id)

    profile_result = await db.execute(
        select(GuardianBotProfile).where(GuardianBotProfile.account_id == request.bot_account_id)
    )
    profile = profile_result.scalar_one_or_none()
    if not profile or not profile.enabled:
        raise HTTPException(status_code=400, detail="Guardian bot profile is disabled or missing")

    statuses = [item.strip() for item in request.statuses if item.strip()]
    if not statuses:
        statuses = ["active"]

    groups = (
        (
            await db.execute(
                select(Group)
                .where(Group.status.in_(statuses))
                .order_by(desc(Group.updated_at))
                .limit(request.limit)
            )
        )
        .scalars()
        .all()
    )

    client = TelegramClient(TelegramConfig(bot_token=profile.bot_token))
    synced = 0
    skipped = 0
    errors: list[dict[str, Any]] = []
    details: list[dict[str, Any]] = []
    try:
        bot_user = await client.get_me()
        profile.bot_username = bot_user.username or profile.bot_username
        profile.bot_user_id = bot_user.user_id or profile.bot_user_id
        for group in groups:
            try:
                member: dict[str, Any] | None = None
                telegram_group_id = group.group_id
                last_member_error: Optional[Exception] = None
                for candidate_chat_id in _bot_api_chat_id_candidates(group.group_id):
                    try:
                        member = await client.get_chat_member(candidate_chat_id, bot_user.user_id)
                        telegram_group_id = candidate_chat_id
                        break
                    except Exception as exc:
                        last_member_error = exc
                if member is None:
                    raise last_member_error or RuntimeError("Unable to inspect bot membership")

                bot_role, binding_status = guardian_role_and_status_from_member(member)
                if binding_status != ManagedGroupBindingStatus.ACTIVE:
                    skipped += 1
                    details.append(
                        {
                            "telegram_group_id": telegram_group_id,
                            "status": member.get("status"),
                            "action": "skip_not_admin",
                        }
                    )
                    continue

                member_count = group.member_count
                title = group.title
                username = group.username
                chat_type = "group"
                try:
                    chat_info = await client.get_chat(telegram_group_id)
                    title = chat_info.title or title
                    username = chat_info.username if chat_info.username is not None else username
                    member_count = (
                        chat_info.member_count
                        if chat_info.member_count is not None
                        else member_count
                    )
                    chat_type = "channel" if chat_info.type == "channel" else "group"
                except Exception:
                    pass

                await sync_managed_group_binding(
                    db,
                    bot_account_id=request.bot_account_id,
                    telegram_group_id=telegram_group_id,
                    group_id=group.id,
                    title=title,
                    username=username,
                    member_count=member_count,
                    binding_status=binding_status,
                    bot_role=bot_role,
                    permissions_snapshot={
                        "bot_member": member,
                        "source": "confirmed_group_scan",
                        "synced_at": datetime.utcnow().isoformat(),
                    },
                    chat_type=chat_type,
                    discovery_source="guardian_confirmed_scan",
                    allow_existing=True,
                )
                synced += 1
                details.append(
                    {
                        "telegram_group_id": telegram_group_id,
                        "status": member.get("status"),
                        "action": "synced",
                    }
                )
            except ManagedGroupSyncConflict as exc:
                skipped += 1
                details.append(
                    {
                        "telegram_group_id": group.group_id,
                        "action": "skip_conflict",
                        "error": str(exc),
                    }
                )
            except Exception as exc:
                skipped += 1
                errors.append({"telegram_group_id": group.group_id, "error": str(exc)})
    finally:
        await client.close()

    profile.sync_status = "synced" if not errors else "partial"
    profile.last_synced_at = datetime.utcnow()
    await db.commit()
    return ManagedGroupSyncConfirmedResponse(
        message="Confirmed groups sync completed",
        data={
            "checked": len(groups),
            "synced": synced,
            "skipped": skipped,
            "errors": errors[:20],
            "details": details[:50],
        },
    )


@router.post("", response_model=ManagedGroupBindingResponse, status_code=status.HTTP_201_CREATED)
async def create_managed_group_binding(
    request: ManagedGroupBindingCreate,
    db: AsyncSession = Depends(get_db),
) -> ManagedGroupBindingResponse:
    await ensure_guardian_bot_account(db, request.bot_account_id)

    try:
        result = await sync_managed_group_binding(
            db,
            bot_account_id=request.bot_account_id,
            telegram_group_id=request.telegram_group_id,
            group_id=request.group_id,
            title=request.title,
            username=request.username,
            member_count=request.member_count,
            binding_status=ManagedGroupBindingStatus(request.binding_status),
            bot_role=ManagedGroupBotRole(request.bot_role),
            permissions_snapshot=request.permissions_snapshot,
            chat_type=request.chat_type,
            discovery_source="guardian_binding",
            allow_existing=False,
        )
    except ManagedGroupSyncConflict as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await db.commit()
    await db.refresh(result.binding)
    return _serialize_binding(result.binding)


@router.put("/{binding_id:int}", response_model=ManagedGroupBindingResponse)
async def update_managed_group_binding(
    binding_id: int,
    request: ManagedGroupBindingUpdate,
    db: AsyncSession = Depends(get_db),
) -> ManagedGroupBindingResponse:
    binding = await db.get(ManagedGroupBinding, binding_id)
    if not binding:
        raise HTTPException(status_code=404, detail="Managed group binding not found")

    data = request.model_dump(exclude_none=True)
    if "binding_status" in data:
        data["binding_status"] = ManagedGroupBindingStatus(data["binding_status"])
    if "bot_role" in data:
        data["bot_role"] = ManagedGroupBotRole(data["bot_role"])
    if "permissions_snapshot" in data:
        import json

        data["permissions_snapshot"] = json.dumps(data["permissions_snapshot"], ensure_ascii=False)

    for field, value in data.items():
        setattr(binding, field, value)

    await db.commit()
    await db.refresh(binding)
    return _serialize_binding(binding)


@router.post("/{binding_id:int}/mute-all", response_model=ManagedOperationResponse)
async def set_managed_group_mute_all(
    binding_id: int,
    request: ManagedGroupMuteAllRequest,
    db: AsyncSession = Depends(get_db),
) -> ManagedOperationResponse:
    """Mute or restore all ordinary members by changing the group's default permissions."""
    binding = await db.get(ManagedGroupBinding, binding_id)
    if not binding:
        raise HTTPException(status_code=404, detail="Managed group binding not found")
    if _binding_chat_type(binding) == "channel":
        raise HTTPException(status_code=400, detail="Channels do not support member mute-all")
    if binding.binding_status != ManagedGroupBindingStatus.ACTIVE:
        raise HTTPException(status_code=400, detail="Managed group binding is not active")
    if _all_members_muted(binding) == request.muted:
        return ManagedOperationResponse(
            message="Mute-all state unchanged",
            data={"binding_id": binding.id, "all_members_muted": request.muted},
        )

    profile = await _enabled_guardian_profile(db, binding.bot_account_id)
    client = TelegramClient(TelegramConfig(bot_token=profile.bot_token))
    snapshot = _permissions_dict(binding)
    try:
        member = await _assert_bot_admin_permission(
            client, profile, binding.telegram_group_id, "can_restrict_members"
        )
        if request.muted:
            previous = await client.get_chat_permissions(binding.telegram_group_id)
            snapshot["permissions_before_mute_all"] = previous
            permissions = _lockdown_permissions()
        else:
            previous = snapshot.get("permissions_before_mute_all")
            permissions = (
                dict(previous)
                if isinstance(previous, dict) and previous
                else _fallback_unmuted_permissions()
            )

        changed = await TelegramExecutionService(AccountRiskGuard(db)).set_default_chat_permissions(
            client,
            binding.telegram_group_id,
            permissions,
            source="managed_group_mute_all" if request.muted else "managed_group_unmute_all",
        )
        if not changed:
            raise HTTPException(
                status_code=400, detail="Telegram did not confirm the permission update"
            )
        snapshot.update(
            {
                "all_members_muted": request.muted,
                "all_members_mute_updated_at": datetime.utcnow().isoformat(),
                "bot_member": member,
            }
        )
        import json

        binding.permissions_snapshot = json.dumps(snapshot, ensure_ascii=False)
        binding.last_synced_at = datetime.utcnow()
        await db.commit()
    except TelegramAPIError as exc:
        raise HTTPException(
            status_code=400, detail=f"Telegram {exc.method or 'API'} failed: {exc.message}"
        ) from exc
    except TelegramExecutionError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    finally:
        await client.close()

    return ManagedOperationResponse(
        message="All members muted" if request.muted else "All member permissions restored",
        data={
            "binding_id": binding.id,
            "telegram_group_id": binding.telegram_group_id,
            "all_members_muted": request.muted,
        },
    )


@router.post("/{binding_id:int}/channel-message", response_model=ManagedOperationResponse)
async def send_managed_channel_message(
    binding_id: int,
    request: ManagedChannelMessageRequest,
    db: AsyncSession = Depends(get_db),
) -> ManagedOperationResponse:
    binding = await db.get(ManagedGroupBinding, binding_id)
    if not binding:
        raise HTTPException(status_code=404, detail="Managed channel binding not found")
    if _binding_chat_type(binding) != "channel":
        raise HTTPException(status_code=400, detail="This binding is not a channel")
    if binding.binding_status != ManagedGroupBindingStatus.ACTIVE:
        raise HTTPException(status_code=400, detail="Managed channel binding is not active")

    profile = await _enabled_guardian_profile(db, binding.bot_account_id)
    client = TelegramClient(TelegramConfig(bot_token=profile.bot_token))
    try:
        member = await _assert_bot_admin_permission(
            client, profile, binding.telegram_group_id, "can_post_messages"
        )
        message_id = await TelegramExecutionService(AccountRiskGuard(db)).send_bot_message(
            client,
            binding.telegram_group_id,
            request.content.strip(),
            parse_mode=request.parse_mode,
            disable_web_page_preview=request.disable_web_page_preview,
            disable_notification=request.disable_notification,
            source="managed_channel_message",
        )
        if message_id is None:
            raise HTTPException(
                status_code=400, detail="Telegram did not return a channel message ID"
            )
        snapshot = _permissions_dict(binding)
        snapshot.update(
            {
                "bot_member": member,
                "last_channel_message_id": message_id,
                "last_channel_message_at": datetime.utcnow().isoformat(),
            }
        )
        import json

        binding.permissions_snapshot = json.dumps(snapshot, ensure_ascii=False)
        binding.last_synced_at = datetime.utcnow()
        await db.commit()
    except TelegramAPIError as exc:
        raise HTTPException(
            status_code=400, detail=f"Telegram {exc.method or 'API'} failed: {exc.message}"
        ) from exc
    except TelegramExecutionError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    finally:
        await client.close()

    return ManagedOperationResponse(
        message="Channel message sent",
        data={
            "binding_id": binding.id,
            "telegram_channel_id": binding.telegram_group_id,
            "message_id": message_id,
        },
    )


@router.put("/{binding_id:int}/channel-username", response_model=ManagedOperationResponse)
async def update_managed_channel_username(
    binding_id: int,
    request: ManagedChannelUsernameRequest,
    db: AsyncSession = Depends(get_db),
) -> ManagedOperationResponse:
    binding = await db.get(ManagedGroupBinding, binding_id)
    if not binding:
        raise HTTPException(status_code=404, detail="Managed channel binding not found")
    if _binding_chat_type(binding) != "channel":
        raise HTTPException(status_code=400, detail="This binding is not a channel")

    creator = await _channel_creator_account(db, binding)
    account_pool = get_account_pool()
    await account_pool.add_account_from_db(creator)
    wrapper = None
    username = request.username.strip()
    try:
        wrapper = await account_pool.acquire_by_id(creator.id, purpose="managed_channel_username")
        if wrapper is None:
            raise HTTPException(
                status_code=400, detail="Channel creator account session is unavailable"
            )
        updated = await TelegramExecutionService(AccountRiskGuard(db)).update_channel_username(
            wrapper,
            binding.telegram_group_id,
            username,
        )
        if not updated:
            raise HTTPException(
                status_code=400, detail="Telegram did not confirm the username update"
            )

        snapshot = _permissions_dict(binding)
        snapshot.update(
            {
                "requested_username": username or None,
                "channel_visibility": "public" if username else "private",
                "username_updated_at": datetime.utcnow().isoformat(),
            }
        )
        import json

        binding.permissions_snapshot = json.dumps(snapshot, ensure_ascii=False)
        group = await db.get(Group, binding.group_id)
        if group is not None:
            group.username = username or None
        binding.last_synced_at = datetime.utcnow()
        await db.commit()
        await db.refresh(binding, attribute_names=["group"])
    except HTTPException:
        raise
    except TelegramExecutionError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=400, detail=f"Channel username update failed: {exc}"
        ) from exc
    finally:
        if wrapper is not None:
            await account_pool.release(wrapper)

    return ManagedOperationResponse(
        message="Channel username updated" if username else "Channel changed to private",
        data={
            "binding_id": binding.id,
            "telegram_channel_id": binding.telegram_group_id,
            "username": username or None,
            "channel_visibility": "public" if username else "private",
        },
    )


@router.post("/{binding_id:int}/channel-status/refresh", response_model=ManagedOperationResponse)
async def refresh_managed_channel_status(
    binding_id: int,
    db: AsyncSession = Depends(get_db),
) -> ManagedOperationResponse:
    binding = await db.get(ManagedGroupBinding, binding_id)
    if not binding:
        raise HTTPException(status_code=404, detail="Managed channel binding not found")
    if _binding_chat_type(binding) != "channel":
        raise HTTPException(status_code=400, detail="This binding is not a channel")

    profile = await _enabled_guardian_profile(db, binding.bot_account_id)
    client = TelegramClient(TelegramConfig(bot_token=profile.bot_token))
    try:
        bot_user = await client.get_me()
        member = await client.get_chat_member(binding.telegram_group_id, bot_user.user_id)
        member_status = member.get("status")
        can_post = member_status == "creator" or (
            member_status == "administrator" and member.get("can_post_messages") is True
        )
        binding.bot_role = (
            ManagedGroupBotRole.OWNER
            if member_status == "creator"
            else ManagedGroupBotRole.ADMIN
            if member_status == "administrator"
            else ManagedGroupBotRole.MEMBER
        )
        binding.binding_status = (
            ManagedGroupBindingStatus.ACTIVE if can_post else ManagedGroupBindingStatus.DEGRADED
        )
        snapshot = _permissions_dict(binding)
        snapshot.pop("channel_status_error", None)
        snapshot.update(
            {
                "bot_member": member,
                "bot_assignment_complete": can_post,
                "channel_status_checked_at": datetime.utcnow().isoformat(),
            }
        )
        import json

        binding.permissions_snapshot = json.dumps(snapshot, ensure_ascii=False)
        binding.last_synced_at = datetime.utcnow()
        profile.bot_user_id = bot_user.user_id
        profile.bot_username = bot_user.username or profile.bot_username
        await db.commit()
        await db.refresh(binding, attribute_names=["group", "bot_account"])
    except TelegramAPIError as exc:
        binding.binding_status = ManagedGroupBindingStatus.DEGRADED
        snapshot = _permissions_dict(binding)
        snapshot.update(
            {
                "bot_assignment_complete": False,
                "channel_status_error": exc.message,
                "channel_status_checked_at": datetime.utcnow().isoformat(),
            }
        )
        import json

        binding.permissions_snapshot = json.dumps(snapshot, ensure_ascii=False)
        binding.last_synced_at = datetime.utcnow()
        await db.commit()
        raise HTTPException(
            status_code=400,
            detail=f"Telegram {exc.method or 'API'} failed: {exc.message}",
        ) from exc
    finally:
        await client.close()

    return ManagedOperationResponse(
        message="Channel status refreshed",
        data={
            "binding": _serialize_binding(binding).model_dump(),
            "bot_assignment_complete": can_post,
        },
    )


@router.get(
    "/{binding_id:int}/pinned-message-config",
    response_model=ManagedGroupPinnedMessageConfigResponse,
)
async def get_managed_group_pinned_message_config(
    binding_id: int,
    db: AsyncSession = Depends(get_db),
) -> ManagedGroupPinnedMessageConfigResponse:
    binding = await db.get(ManagedGroupBinding, binding_id)
    if not binding:
        raise HTTPException(status_code=404, detail="Managed group binding not found")
    return ManagedGroupPinnedMessageConfigResponse(data=_pinned_config_from_binding(binding))


@router.put(
    "/{binding_id:int}/pinned-message-config",
    response_model=ManagedGroupPinnedMessageConfigResponse,
)
async def save_managed_group_pinned_message_config(
    binding_id: int,
    request: ManagedGroupPinnedMessageConfig,
    db: AsyncSession = Depends(get_db),
) -> ManagedGroupPinnedMessageConfigResponse:
    binding = await db.get(ManagedGroupBinding, binding_id)
    if not binding:
        raise HTTPException(status_code=404, detail="Managed group binding not found")
    _set_permissions_key(binding, "pinned_message_config", request.model_dump())
    binding.last_synced_at = datetime.utcnow()
    await db.commit()
    return ManagedGroupPinnedMessageConfigResponse(data=request)


@router.post("/{binding_id:int}/pinned-message", response_model=ManagedGroupPinnedMessageResponse)
async def send_managed_group_pinned_message(
    binding_id: int,
    request: ManagedGroupPinnedMessageRequest,
    db: AsyncSession = Depends(get_db),
) -> ManagedGroupPinnedMessageResponse:
    binding = await db.get(ManagedGroupBinding, binding_id)
    if not binding:
        raise HTTPException(status_code=404, detail="Managed group binding not found")
    if binding.binding_status != ManagedGroupBindingStatus.ACTIVE:
        raise HTTPException(status_code=400, detail="Managed group binding is not active")

    profile_result = await db.execute(
        select(GuardianBotProfile).where(GuardianBotProfile.account_id == binding.bot_account_id)
    )
    profile = profile_result.scalar_one_or_none()
    if not profile or not profile.enabled:
        raise HTTPException(status_code=400, detail="Guardian bot profile is disabled or missing")

    client = TelegramClient(TelegramConfig(bot_token=profile.bot_token))
    execution = TelegramExecutionService(AccountRiskGuard(db))
    try:
        await _assert_bot_admin_permission(
            client, profile, binding.telegram_group_id, "can_pin_messages"
        )
        reply_markup = None
        if request.button_text and request.button_url:
            reply_markup = {
                "inline_keyboard": [[{"text": request.button_text, "url": request.button_url}]]
            }
        message_id = await execution.send_pinned_bot_message(
            client,
            binding.telegram_group_id,
            request.content.strip(),
            parse_mode=request.parse_mode,
            disable_web_page_preview=request.disable_web_page_preview,
            disable_notification=request.disable_notification,
            reply_markup=reply_markup,
            source="managed_group_channel_announcement",
        )
    except TelegramAPIError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Telegram {exc.method or 'API'} failed: {exc.message}",
        )
    except TelegramExecutionError as exc:
        raise HTTPException(status_code=429, detail=str(exc))
    finally:
        await client.close()

    return ManagedGroupPinnedMessageResponse(
        message="Pinned message sent",
        data={
            "binding_id": binding.id,
            "telegram_group_id": binding.telegram_group_id,
            "message_id": message_id,
            "pinned": True,
            "button_url": request.button_url,
        },
    )


@router.delete("/{binding_id:int}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_managed_group_binding(binding_id: int, db: AsyncSession = Depends(get_db)) -> None:
    binding = await db.get(ManagedGroupBinding, binding_id)
    if not binding:
        raise HTTPException(status_code=404, detail="Managed group binding not found")
    await db.delete(binding)
    await db.commit()
