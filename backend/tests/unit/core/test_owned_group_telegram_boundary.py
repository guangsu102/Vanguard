import asyncio
from datetime import datetime, timedelta
from enum import Enum
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telethon.tl import types

from app.core.account.risk_guard import AccountRiskAction
from app.core.account.telegram_execution import (
    TelegramExecutionError,
    TelegramExecutionService,
    TelegramSendOutcomeUnknownError,
    TelegramSendPreflightError,
    TelegramSendReservationReleasePendingError,
)
from app.core.account.telegram_membership import classify_telethon_membership_response
from app.modules.acquisition.auto_reply.speaker import Speaker
from app.modules.owned_group.messaging_target import OwnedGroupAccountMembershipProbe


def _guard(*, allowed: bool = True, reason: str = "reserved") -> SimpleNamespace:
    return SimpleNamespace(
        db=SimpleNamespace(rollback=AsyncMock()),
        check_and_reserve=AsyncMock(return_value=SimpleNamespace(allowed=allowed, reason=reason)),
        release_owned_group_message_reservation=AsyncMock(return_value=True),
        record_failure=AsyncMock(),
        record_success=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_owned_send_uses_dedicated_action_without_persisting_content() -> None:
    client = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(id=321)))
    account = SimpleNamespace(account_id=7, client=client)
    guard = _guard()

    message_id = await TelegramExecutionService(guard).send_owned_group_message(
        account,
        -10077,
        "private group text",
        reply_to=11,
        execution_id=99,
    )

    assert message_id == 321
    client.send_message.assert_awaited_once_with(
        -10077,
        "private group text",
        reply_to=11,
    )
    kwargs = guard.check_and_reserve.await_args.kwargs
    assert guard.check_and_reserve.await_args.args[1] is AccountRiskAction.OWNED_GROUP_MESSAGE
    assert kwargs["details"]["source"] == "owned_group_message"
    assert kwargs["details"]["execution_id"] == 99
    assert kwargs["details"]["reply_to"] == 11
    assert kwargs["details"]["risk_reservation_id"].startswith("owned-group-attempt-")
    assert "content" not in kwargs["details"]


@pytest.mark.asyncio
async def test_owned_send_persists_marker_immediately_before_telegram_write() -> None:
    events: list[str] = []

    async def before_write() -> None:
        events.append("write-marker")

    async def send_message(*args, **kwargs):
        assert events == ["write-marker"]
        events.append("telegram-write")
        return SimpleNamespace(id=322)

    client = SimpleNamespace(send_message=AsyncMock(side_effect=send_message))
    account = SimpleNamespace(account_id=7, client=client)

    message_id = await TelegramExecutionService(_guard()).send_owned_group_message(
        account,
        -10077,
        "hello",
        execution_id=100,
        before_telegram_write=before_write,
    )

    assert message_id == 322
    assert events == ["write-marker", "telegram-write"]


@pytest.mark.asyncio
async def test_owned_send_marker_failure_never_touches_telegram() -> None:
    async def fail_marker() -> None:
        raise RuntimeError("database unavailable")

    client = SimpleNamespace(send_message=AsyncMock())
    account = SimpleNamespace(account_id=7, client=client)

    guard = _guard()
    with pytest.raises(TelegramSendPreflightError):
        await TelegramExecutionService(guard).send_owned_group_message(
            account,
            -10077,
            "hello",
            execution_id=101,
            before_telegram_write=fail_marker,
        )

    client.send_message.assert_not_awaited()
    guard.release_owned_group_message_reservation.assert_awaited_once()
    guard.record_failure.assert_not_awaited()


@pytest.mark.asyncio
async def test_owned_send_unconfirmed_release_keeps_recovery_identity() -> None:
    async def fail_marker() -> None:
        raise RuntimeError("database unavailable")

    client = SimpleNamespace(send_message=AsyncMock())
    account = SimpleNamespace(account_id=7, client=client)
    guard = _guard()
    guard.release_owned_group_message_reservation.return_value = False

    with pytest.raises(TelegramSendReservationReleasePendingError):
        await TelegramExecutionService(guard).send_owned_group_message(
            account,
            -10077,
            "hello",
            execution_id=105,
            send_attempt_id="send:release-pending",
            before_telegram_write=fail_marker,
        )

    client.send_message.assert_not_awaited()
    guard.record_failure.assert_not_awaited()


@pytest.mark.asyncio
async def test_owned_send_risk_exception_is_confirmed_preflight_failure() -> None:
    client = SimpleNamespace(send_message=AsyncMock())
    account = SimpleNamespace(account_id=7, client=client)
    guard = _guard()
    guard.check_and_reserve.side_effect = RuntimeError("proxy_password=do-not-log")

    with pytest.raises(TelegramSendPreflightError) as captured:
        await TelegramExecutionService(guard).send_owned_group_message(
            account,
            -10077,
            "hello",
            execution_id=102,
            send_attempt_id="send:attempt-102",
        )

    assert isinstance(captured.value.__cause__, RuntimeError)
    client.send_message.assert_not_awaited()
    guard.record_failure.assert_not_awaited()


@pytest.mark.asyncio
async def test_owned_send_cancelled_during_risk_preflight_releases_current_attempt() -> None:
    client = SimpleNamespace(send_message=AsyncMock())
    account = SimpleNamespace(account_id=7, client=client)
    guard = _guard()
    guard.check_and_reserve.side_effect = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await TelegramExecutionService(guard).send_owned_group_message(
            account,
            -10077,
            "hello",
            execution_id=104,
            send_attempt_id="send:cancelled-attempt",
        )

    guard.release_owned_group_message_reservation.assert_awaited_once()
    client.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_owned_send_reservation_is_unique_per_real_attempt() -> None:
    client = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(id=323)))
    account = SimpleNamespace(account_id=7, client=client)
    guard = _guard()
    service = TelegramExecutionService(guard)

    await service.send_owned_group_message(
        account,
        -10077,
        "first",
        execution_id=103,
        send_attempt_id="send:attempt-one",
    )
    await service.send_owned_group_message(
        account,
        -10077,
        "retry",
        execution_id=103,
        send_attempt_id="send:attempt-two",
    )

    reservations = [
        call.kwargs["details"]["risk_reservation_id"]
        for call in guard.check_and_reserve.await_args_list
    ]
    assert reservations[0] != reservations[1]
    assert client.send_message.await_count == 2


@pytest.mark.asyncio
async def test_owned_send_risk_block_never_touches_telegram() -> None:
    client = SimpleNamespace(send_message=AsyncMock())
    account = SimpleNamespace(account_id=7, client=client)

    with pytest.raises(TelegramExecutionError, match="risk_guard_blocked"):
        await TelegramExecutionService(
            _guard(allowed=False, reason="platform_group_write_banned")
        ).send_owned_group_message(
            account,
            -10077,
            "hello",
            execution_id=99,
        )

    client.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_send_risk_audit_failure_keeps_known_message_id() -> None:
    client = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(id=654)))
    account = SimpleNamespace(account_id=7, client=client)
    guard = _guard()
    guard.record_success.side_effect = RuntimeError("audit database unavailable")

    message_id = await TelegramExecutionService(guard).send_owned_group_message(
        account,
        -10077,
        "hello",
        execution_id=100,
    )

    assert message_id == 654
    assert client.send_message.await_count == 1
    guard.db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_message_id_is_non_retryable_unknown_outcome() -> None:
    client = SimpleNamespace(send_message=AsyncMock(return_value=object()))
    account = SimpleNamespace(account_id=7, client=client)
    guard = _guard()

    with pytest.raises(TelegramSendOutcomeUnknownError):
        await TelegramExecutionService(guard).send_owned_group_message(
            account,
            -10077,
            "hello",
            execution_id=101,
        )
    assert client.send_message.await_count == 1
    guard.record_failure.assert_awaited_once()
    guard.record_success.assert_not_awaited()


@pytest.mark.asyncio
async def test_transport_error_during_send_has_unknown_outcome() -> None:
    client = SimpleNamespace(
        send_message=AsyncMock(side_effect=ConnectionResetError("connection reset"))
    )
    account = SimpleNamespace(account_id=7, client=client)
    guard = _guard()

    with pytest.raises(TelegramSendOutcomeUnknownError) as captured:
        await TelegramExecutionService(guard).send_owned_group_message(
            account,
            -10077,
            "hello",
            execution_id=102,
        )

    assert isinstance(captured.value.__cause__, ConnectionResetError)
    assert client.send_message.await_count == 1
    guard.record_failure.assert_awaited_once()


def _speaker_parts(*, membership, client):
    if not hasattr(client, "get_me"):
        client.get_me = AsyncMock(return_value=SimpleNamespace(id=7007))
    account = SimpleNamespace(account_id=7, client=client)
    pool = SimpleNamespace(
        get_account_by_id=AsyncMock(return_value=account),
        acquire_by_id=AsyncMock(return_value=account),
        sync_from_db=AsyncMock(),
        release=AsyncMock(),
    )
    db = AsyncMock()
    db.scalar.return_value = membership
    speaker = Speaker(
        db=db,
        account_pool=pool,
        group_manager=MagicMock(),
        template_engine=MagicMock(),
    )
    target = SimpleNamespace(
        owned_group_asset_id=90,
        core_group_id=91,
        telegram_chat_id=-10090,
        managed_binding_id=92,
    )
    return speaker, pool, db, target


@pytest.mark.asyncio
async def test_account_acquisition_error_is_redacted_before_logging() -> None:
    secret = "session_string=opaque-account-session-secret"
    speaker, pool, _, target = _speaker_parts(
        membership=SimpleNamespace(),
        client=SimpleNamespace(),
    )
    pool.acquire_by_id.side_effect = RuntimeError(f"account unavailable: {secret}")
    speaker.logger = SimpleNamespace(warning=MagicMock(), info=MagicMock())

    result = await speaker.send_owned_group_message(
        target=target,
        account_id=7,
        content="hello",
        purpose="community_ai",
        content_category="community",
        execution_id=64,
    )

    logged_error = speaker.logger.warning.call_args.kwargs["error"]
    assert "opaque-account-session-secret" not in logged_error
    assert "[REDACTED]" in logged_error
    assert result.error == "Specific account unavailable"


@pytest.mark.asyncio
async def test_telegram_send_error_is_redacted_in_result_and_log() -> None:
    token = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
    invite = "https://t.me/+PrivateInviteSecret"
    session = "opaque-send-session-secret"
    membership = SimpleNamespace(
        status="member_verified",
        last_verified_at=datetime.utcnow(),
        telegram_user_id=7007,
    )
    speaker, _, _, target = _speaker_parts(
        membership=membership,
        client=SimpleNamespace(),
    )
    speaker.telegram_execution = SimpleNamespace(
        send_owned_group_message=AsyncMock(
            side_effect=RuntimeError(
                f"send failed bot_token={token} session_string={session} invite_link={invite}"
            )
        )
    )
    speaker.logger = SimpleNamespace(warning=MagicMock(), info=MagicMock())

    result = await speaker.send_owned_group_message(
        target=target,
        account_id=7,
        content="hello",
        purpose="community_ai",
        content_category="community",
        execution_id=65,
    )

    logged_error = speaker.logger.warning.call_args.kwargs["error"]
    for raw_secret in (token, invite, session):
        assert raw_secret not in logged_error
        assert raw_secret not in (result.error or "")
    assert "[REDACTED" in logged_error
    assert result.error == logged_error


@pytest.mark.asyncio
async def test_speaker_does_not_retry_unclassified_transport_failure() -> None:
    membership = SimpleNamespace(
        status="member_verified",
        last_verified_at=datetime.utcnow(),
        telegram_user_id=7007,
    )
    client = SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(id=7007)))
    speaker, _, _, target = _speaker_parts(membership=membership, client=client)
    speaker.telegram_execution = SimpleNamespace(
        send_owned_group_message=AsyncMock(
            side_effect=ConnectionResetError("connection reset during send")
        )
    )

    result = await speaker.send_owned_group_message(
        target=target,
        account_id=7,
        content="hello",
        purpose="community_ai",
        content_category="community",
        execution_id=66,
    )

    assert result.error_code == "TELEGRAM_SEND_OUTCOME_UNKNOWN"
    assert result.outcome_unknown is True
    assert result.retryable is False


@pytest.mark.asyncio
async def test_speaker_retries_only_explicit_preflight_send_failure() -> None:
    membership = SimpleNamespace(
        status="member_verified",
        last_verified_at=datetime.utcnow(),
        telegram_user_id=7007,
    )
    client = SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(id=7007)))
    speaker, _, _, target = _speaker_parts(membership=membership, client=client)
    speaker.telegram_execution = SimpleNamespace(
        send_owned_group_message=AsyncMock(
            side_effect=TelegramSendPreflightError("connection unavailable before write")
        )
    )

    result = await speaker.send_owned_group_message(
        target=target,
        account_id=7,
        content="hello",
        purpose="community_ai",
        content_category="community",
        execution_id=67,
    )

    assert result.error_code == "TELEGRAM_SEND_FAILED"
    assert result.outcome_unknown is False
    assert result.retryable is True


class _FakeTelethonMembershipClient:
    def __init__(self, participant: object) -> None:
        self.get_me = AsyncMock(return_value=SimpleNamespace(id=7007))
        self.get_entity = AsyncMock(return_value=SimpleNamespace(id=-10090))
        self.request = AsyncMock(return_value=SimpleNamespace(participant=participant))

    async def __call__(self, request: object) -> object:
        return await self.request(request)


def test_telethon_membership_classifier_is_explicitly_allowlisted() -> None:
    verified = SimpleNamespace(participant=types.ChannelParticipant(user_id=7007, date=None))
    left = SimpleNamespace(participant=types.ChannelParticipantLeft(types.PeerUser(7007)))
    restricted = SimpleNamespace(
        participant=types.ChannelParticipantBanned(
            peer=types.PeerUser(7007),
            kicked_by=1,
            date=None,
            banned_rights=types.ChatBannedRights(until_date=None, send_messages=True),
            left=False,
        )
    )

    assert classify_telethon_membership_response(verified) == "verified"
    assert classify_telethon_membership_response(left) == "not_member"
    assert classify_telethon_membership_response(restricted) == "not_member"
    assert classify_telethon_membership_response(SimpleNamespace(participant=None)) == "unknown"


@pytest.mark.asyncio
async def test_speaker_stale_probe_and_send_share_one_exact_account_lease() -> None:
    membership = SimpleNamespace(
        status="member_verified",
        last_verified_at=datetime.utcnow() - timedelta(hours=25),
        telegram_user_id=7007,
    )
    client = SimpleNamespace(
        get_me=AsyncMock(return_value=SimpleNamespace(id=7007)),
        get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member")),
    )
    speaker, pool, db, target = _speaker_parts(
        membership=membership,
        client=client,
    )
    speaker.telegram_execution = SimpleNamespace(
        send_owned_group_message=AsyncMock(return_value=801)
    )

    result = await speaker.send_owned_group_message(
        target=target,
        account_id=7,
        content="hello",
        purpose="community_ai",
        content_category="community",
        execution_id=5,
    )

    assert result.success is True
    pool.acquire_by_id.assert_awaited_once_with(7, purpose="owned_group_message")
    pool.release.assert_awaited_once()
    client.get_me.assert_awaited_once()
    client.get_chat_member.assert_awaited_once_with(-10090, 7007)
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_fresh_membership_without_trusted_identity_fails_closed() -> None:
    membership = SimpleNamespace(
        status="skipped_already_member",
        last_verified_at=datetime.utcnow(),
        telegram_user_id=None,
    )
    client = SimpleNamespace(
        get_me=AsyncMock(return_value=SimpleNamespace(id=8008)),
        get_chat_member=AsyncMock(),
    )
    speaker, pool, db, target = _speaker_parts(
        membership=membership,
        client=client,
    )
    telegram_execution = SimpleNamespace(send_owned_group_message=AsyncMock())
    speaker.telegram_execution = telegram_execution

    result = await speaker.send_owned_group_message(
        target=target,
        account_id=7,
        content="hello",
        purpose="community_ai",
        content_category="community",
        execution_id=69,
    )

    assert result.error_code == "ACCOUNT_NOT_ELIGIBLE"
    assert result.retryable is False
    assert membership.status == "unknown_needs_reconcile"
    assert membership.telegram_user_id is None
    client.get_me.assert_awaited_once()
    client.get_chat_member.assert_not_awaited()
    telegram_execution.send_owned_group_message.assert_not_awaited()
    db.commit.assert_awaited_once()
    pool.release.assert_awaited_once()


@pytest.mark.asyncio
async def test_explicit_non_member_marks_reconcile_without_risk_send() -> None:
    membership = SimpleNamespace(
        status="member_verified",
        last_verified_at=datetime.utcnow() - timedelta(hours=25),
        telegram_user_id=7007,
    )
    client = SimpleNamespace(
        get_me=AsyncMock(return_value=SimpleNamespace(id=7007)),
        get_chat_member=AsyncMock(return_value=SimpleNamespace(status="left")),
    )
    speaker, pool, db, target = _speaker_parts(
        membership=membership,
        client=client,
    )
    telegram_execution = SimpleNamespace(send_owned_group_message=AsyncMock())
    speaker.telegram_execution = telegram_execution

    result = await speaker.send_owned_group_message(
        target=target,
        account_id=7,
        content="hello",
        purpose="community_ai",
        content_category="community",
        execution_id=6,
    )

    assert result.success is False
    assert result.error_code == "ACCOUNT_NOT_ELIGIBLE"
    assert result.retryable is False
    assert membership.status == "unknown_needs_reconcile"
    db.commit.assert_awaited_once()
    telegram_execution.send_owned_group_message.assert_not_awaited()
    pool.release.assert_awaited_once()


@pytest.mark.asyncio
async def test_speaker_rejects_telethon_left_participant_before_risk_send() -> None:
    membership = SimpleNamespace(
        status="member_verified",
        last_verified_at=datetime.utcnow() - timedelta(hours=25),
        telegram_user_id=7007,
    )
    client = _FakeTelethonMembershipClient(types.ChannelParticipantLeft(types.PeerUser(7007)))
    speaker, pool, db, target = _speaker_parts(membership=membership, client=client)
    telegram_execution = SimpleNamespace(send_owned_group_message=AsyncMock())
    speaker.telegram_execution = telegram_execution

    result = await speaker.send_owned_group_message(
        target=target,
        account_id=7,
        content="hello",
        purpose="community_ai",
        content_category="community",
        execution_id=63,
    )

    assert result.error_code == "ACCOUNT_NOT_ELIGIBLE"
    assert result.retryable is False
    assert membership.status == "unknown_needs_reconcile"
    db.commit.assert_awaited_once()
    telegram_execution.send_owned_group_message.assert_not_awaited()
    pool.release.assert_awaited_once()


@pytest.mark.asyncio
async def test_enable_probe_rejects_telethon_left_participant() -> None:
    client = _FakeTelethonMembershipClient(types.ChannelParticipantLeft(types.PeerUser(7007)))
    wrapper = SimpleNamespace(client=client)
    pool = SimpleNamespace(
        add_account_from_db=AsyncMock(),
        acquire_by_id=AsyncMock(return_value=wrapper),
        release=AsyncMock(),
    )
    probe = OwnedGroupAccountMembershipProbe(pool)

    result = await probe(
        asset=SimpleNamespace(telegram_chat_id=-10090),
        account=SimpleNamespace(id=7),
    )

    assert result.verified is False
    assert result.reason_code == "membership_not_verified"
    assert result.telegram_user_id == 7007
    pool.release.assert_awaited_once_with(wrapper)


@pytest.mark.asyncio
async def test_fresh_membership_still_checks_session_identity_before_risk_send() -> None:
    membership = SimpleNamespace(
        status="member_verified",
        last_verified_at=datetime.utcnow(),
        telegram_user_id=7007,
    )
    client = SimpleNamespace(
        get_me=AsyncMock(return_value=SimpleNamespace(id=8008)),
        get_chat_member=AsyncMock(),
    )
    speaker, pool, db, target = _speaker_parts(
        membership=membership,
        client=client,
    )
    telegram_execution = SimpleNamespace(send_owned_group_message=AsyncMock())
    speaker.telegram_execution = telegram_execution

    result = await speaker.send_owned_group_message(
        target=target,
        account_id=7,
        content="hello",
        purpose="community_ai",
        content_category="community",
        execution_id=60,
    )

    assert result.success is False
    assert result.error_code == "ACCOUNT_NOT_ELIGIBLE"
    assert result.retryable is False
    assert membership.status == "unknown_needs_reconcile"
    assert membership.telegram_user_id == 7007
    db.commit.assert_awaited_once()
    client.get_me.assert_awaited_once()
    client.get_chat_member.assert_not_awaited()
    telegram_execution.send_owned_group_message.assert_not_awaited()
    pool.release.assert_awaited_once()


@pytest.mark.asyncio
async def test_pool_returning_different_account_fails_closed_before_identity_or_send() -> None:
    membership = SimpleNamespace(
        status="member_verified",
        last_verified_at=datetime.utcnow(),
        telegram_user_id=7007,
    )
    expected_client = SimpleNamespace(
        get_me=AsyncMock(return_value=SimpleNamespace(id=7007))
    )
    speaker, pool, _, target = _speaker_parts(
        membership=membership,
        client=expected_client,
    )
    wrong_client = SimpleNamespace(
        get_me=AsyncMock(return_value=SimpleNamespace(id=8008))
    )
    wrong_account = SimpleNamespace(account_id=8, client=wrong_client)
    pool.acquire_by_id.return_value = wrong_account
    telegram_execution = SimpleNamespace(send_owned_group_message=AsyncMock())
    speaker.telegram_execution = telegram_execution

    result = await speaker.send_owned_group_message(
        target=target,
        account_id=7,
        content="hello",
        purpose="community_ai",
        content_category="community",
        execution_id=68,
    )

    assert result.error_code == "ACCOUNT_NOT_ELIGIBLE"
    assert result.retryable is False
    wrong_client.get_me.assert_not_awaited()
    telegram_execution.send_owned_group_message.assert_not_awaited()
    pool.release.assert_awaited_once_with(wrong_account)


@pytest.mark.asyncio
async def test_enum_non_member_marks_reconcile_without_risk_send() -> None:
    class ChatMemberStatus(Enum):
        LEFT = "left"

    membership = SimpleNamespace(
        status="member_verified",
        last_verified_at=datetime.utcnow() - timedelta(hours=25),
        telegram_user_id=7007,
    )
    client = SimpleNamespace(
        get_me=AsyncMock(return_value=SimpleNamespace(id=7007)),
        get_chat_member=AsyncMock(return_value=SimpleNamespace(status=ChatMemberStatus.LEFT)),
    )
    speaker, pool, _, target = _speaker_parts(
        membership=membership,
        client=client,
    )
    telegram_execution = SimpleNamespace(send_owned_group_message=AsyncMock())
    speaker.telegram_execution = telegram_execution

    result = await speaker.send_owned_group_message(
        target=target,
        account_id=7,
        content="hello",
        purpose="community_ai",
        content_category="community",
        execution_id=61,
    )

    assert result.error_code == "ACCOUNT_NOT_ELIGIBLE"
    assert result.retryable is False
    assert membership.status == "unknown_needs_reconcile"
    telegram_execution.send_owned_group_message.assert_not_awaited()
    pool.release.assert_awaited_once()


@pytest.mark.asyncio
async def test_empty_membership_probe_result_fails_closed_before_risk_send() -> None:
    membership = SimpleNamespace(
        status="member_verified",
        last_verified_at=datetime.utcnow() - timedelta(hours=25),
        telegram_user_id=7007,
    )
    client = SimpleNamespace(
        get_me=AsyncMock(return_value=SimpleNamespace(id=7007)),
        get_chat_member=AsyncMock(return_value=None),
    )
    speaker, pool, _, target = _speaker_parts(
        membership=membership,
        client=client,
    )
    telegram_execution = SimpleNamespace(send_owned_group_message=AsyncMock())
    speaker.telegram_execution = telegram_execution

    result = await speaker.send_owned_group_message(
        target=target,
        account_id=7,
        content="hello",
        purpose="community_ai",
        content_category="community",
        execution_id=62,
    )

    assert result.error_code == "ACCOUNT_MEMBERSHIP_PROBE_FAILED"
    assert result.retryable is True
    telegram_execution.send_owned_group_message.assert_not_awaited()
    pool.release.assert_awaited_once()


class FloodWaitError(RuntimeError):
    seconds = 120


@pytest.mark.asyncio
async def test_short_flood_wait_retry_covers_risk_pause_and_buffer() -> None:
    membership = SimpleNamespace(
        status="member_verified",
        last_verified_at=datetime.utcnow(),
        telegram_user_id=7007,
    )
    speaker, pool, db, target = _speaker_parts(
        membership=membership,
        client=SimpleNamespace(),
    )
    speaker.telegram_execution = SimpleNamespace(
        send_owned_group_message=AsyncMock(side_effect=FloodWaitError("wait 120"))
    )
    db.get.return_value = SimpleNamespace(
        risk_pause_until=datetime.utcnow() + timedelta(seconds=240)
    )

    result = await speaker.send_owned_group_message(
        target=target,
        account_id=7,
        content="hello",
        purpose="community_ai",
        content_category="community",
        execution_id=7,
    )

    assert result.error_code == "FLOOD_WAIT"
    assert result.retryable is True
    assert result.retry_after_seconds >= 239
    pool.release.assert_awaited_once()


@pytest.mark.asyncio
async def test_speaker_surfaces_unconfirmed_risk_release_for_stale_recovery() -> None:
    membership = SimpleNamespace(
        status="member_verified",
        last_verified_at=datetime.utcnow(),
        telegram_user_id=7007,
    )
    speaker, pool, _, target = _speaker_parts(
        membership=membership,
        client=SimpleNamespace(),
    )
    speaker.telegram_execution = SimpleNamespace(
        send_owned_group_message=AsyncMock(
            side_effect=TelegramSendReservationReleasePendingError("release pending")
        )
    )

    result = await speaker.send_owned_group_message(
        target=target,
        account_id=7,
        content="hello",
        purpose="community_ai",
        content_category="community",
        execution_id=106,
        send_attempt_id="send:release-pending",
    )

    assert result.error_code == "RISK_RESERVATION_RELEASE_PENDING"
    assert result.retryable is False
    assert result.outcome_unknown is False
    pool.release.assert_awaited_once()


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (type("PeerFloodError", (RuntimeError,), {})("peer flood"), "PEER_FLOOD"),
        (
            type("UserRestrictedError", (RuntimeError,), {})("user restricted"),
            "ACCOUNT_RESTRICTED",
        ),
        (
            type("PhoneNumberBannedError", (RuntimeError,), {})("phone number banned"),
            "ACCOUNT_BANNED",
        ),
        (
            type("UserNotParticipantError", (RuntimeError,), {})("not a participant"),
            "GROUP_WRITE_FORBIDDEN",
        ),
    ],
)
@pytest.mark.asyncio
async def test_permanent_telegram_errors_are_not_retried(error, expected_code) -> None:
    membership = SimpleNamespace(
        status="member_verified",
        last_verified_at=datetime.utcnow(),
        telegram_user_id=7007,
    )
    speaker, _, _, target = _speaker_parts(
        membership=membership,
        client=SimpleNamespace(),
    )
    speaker.telegram_execution = SimpleNamespace(
        send_owned_group_message=AsyncMock(side_effect=error)
    )

    result = await speaker.send_owned_group_message(
        target=target,
        account_id=7,
        content="hello",
        purpose="community_ai",
        content_category="community",
        execution_id=8,
    )

    assert result.error_code == expected_code
    assert result.retryable is False
