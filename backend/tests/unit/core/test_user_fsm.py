"""
Unit Tests for User FSM Module

Tests cover:
- State transition validation
- Event handling
- Hook registration
- Terminal state detection
- Active state checking
- Available events retrieval
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from app.core.user.fsm import UserFSM, UserEvent, StateTransition
from app.core.user.models import User, UserState


class TestUserFSMInitialization:
    """Test UserFSM initialization."""

    def test_init(self):
        """Test FSM initialization."""
        fsm = UserFSM()

        assert fsm._transition_hooks is not None
        assert fsm._state_hooks is not None
        assert len(fsm._transition_hooks) == len(UserEvent)
        assert len(fsm._state_hooks) == len(UserState)


class TestStateTransitions:
    """Test state transition logic."""

    @pytest.fixture
    def fsm(self):
        """Create UserFSM instance."""
        return UserFSM()

    @pytest.fixture
    def mock_user(self):
        """Create mock user."""
        user = MagicMock(spec=User)
        user.id = 1
        user.state = UserState.NEW
        return user

    def test_can_transition_valid(self, fsm):
        """Test valid transition check."""
        assert fsm.can_transition(UserState.NEW, UserEvent.TRIAL_STARTED) is True

    def test_can_transition_invalid(self, fsm):
        """Test invalid transition check."""
        assert fsm.can_transition(UserState.NEW, UserEvent.CONVERTED) is False

    def test_can_transition_blocked_to_active(self, fsm):
        """Test blocked state transition."""
        assert fsm.can_transition(UserState.BLOCKED, UserEvent.UNBLOCKED) is True

    def test_get_next_state_valid(self, fsm):
        """Test getting next state for valid transition."""
        next_state = fsm.get_next_state(UserState.NEW, UserEvent.TRIAL_STARTED)
        assert next_state == UserState.PENDING

    def test_get_next_state_invalid(self, fsm):
        """Test getting next state for invalid transition."""
        next_state = fsm.get_next_state(UserState.NEW, UserEvent.CONVERTED)
        assert next_state is None

    @pytest.mark.asyncio
    async def test_transition_success(self, fsm, mock_user):
        """Test successful state transition."""
        mock_user.state = UserState.NEW

        success, new_state = await fsm.transition(mock_user, UserEvent.TRIAL_STARTED)

        assert success is True
        assert new_state == UserState.PENDING
        assert mock_user.state == UserState.PENDING

    @pytest.mark.asyncio
    async def test_transition_invalid(self, fsm, mock_user):
        """Test invalid state transition."""
        mock_user.state = UserState.NEW

        success, new_state = await fsm.transition(mock_user, UserEvent.CONVERTED)

        assert success is False
        assert new_state is None
        assert mock_user.state == UserState.NEW

    @pytest.mark.asyncio
    async def test_transition_pending_to_active(self, fsm, mock_user):
        """Test pending to active transition."""
        mock_user.state = UserState.PENDING

        success, new_state = await fsm.transition(mock_user, UserEvent.CONVERTED)

        assert success is True
        assert new_state == UserState.ACTIVE

    @pytest.mark.asyncio
    async def test_transition_trial_expired(self, fsm, mock_user):
        """Test trial expiration transition."""
        mock_user.state = UserState.PENDING

        success, new_state = await fsm.transition(mock_user, UserEvent.TRIAL_EXPIRED)

        assert success is True
        assert new_state == UserState.SILENT

    @pytest.mark.asyncio
    async def test_transition_silent_to_active(self, fsm, mock_user):
        """Test silent to active (reactivation) transition."""
        mock_user.state = UserState.SILENT

        success, new_state = await fsm.transition(mock_user, UserEvent.REACTIVATED)

        assert success is True
        assert new_state == UserState.ACTIVE

    @pytest.mark.asyncio
    async def test_transition_silent_to_churned(self, fsm, mock_user):
        """Test silent to churned transition."""
        mock_user.state = UserState.SILENT

        success, new_state = await fsm.transition(mock_user, UserEvent.CHURNED)

        assert success is True
        assert new_state == UserState.CHURNED

    @pytest.mark.asyncio
    async def test_block_transition(self, fsm, mock_user):
        """Test blocking user from any state."""
        states = [
            UserState.NEW,
            UserState.PENDING,
            UserState.ACTIVE,
            UserState.SILENT,
        ]

        for state in states:
            mock_user.state = state
            success, new_state = await fsm.transition(mock_user, UserEvent.BLOCKED)

            assert success is True
            assert new_state == UserState.BLOCKED

    @pytest.mark.asyncio
    async def test_unblock_transition(self, fsm, mock_user):
        """Test unblocking user."""
        mock_user.state = UserState.BLOCKED

        success, new_state = await fsm.transition(mock_user, UserEvent.UNBLOCKED)

        assert success is True
        assert new_state == UserState.SILENT


class TestHooks:
    """Test hook registration and execution."""

    @pytest.fixture
    def fsm(self):
        """Create UserFSM instance."""
        return UserFSM()

    @pytest.fixture
    def mock_user(self):
        """Create mock user."""
        user = MagicMock(spec=User)
        user.id = 1
        user.state = UserState.NEW
        return user

    def test_register_transition_hook(self, fsm):
        """Test registering transition hook."""
        hook = AsyncMock()
        fsm.register_transition_hook(UserEvent.TRIAL_STARTED, hook)

        assert hook in fsm._transition_hooks[UserEvent.TRIAL_STARTED]

    def test_register_state_hook(self, fsm):
        """Test registering state hook."""
        hook = AsyncMock()
        fsm.register_state_hook(UserState.PENDING, hook)

        assert hook in fsm._state_hooks[UserState.PENDING]

    @pytest.mark.asyncio
    async def test_transition_hook_executed(self, fsm, mock_user):
        """Test transition hook is executed."""
        hook = AsyncMock()
        fsm.register_transition_hook(UserEvent.TRIAL_STARTED, hook)

        await fsm.transition(mock_user, UserEvent.TRIAL_STARTED)

        hook.assert_called_once()
        call_args = hook.call_args
        assert call_args[0][0] == mock_user
        assert call_args[0][1] == UserState.NEW
        assert call_args[0][2] == UserState.PENDING
        assert call_args[0][3] == UserEvent.TRIAL_STARTED

    @pytest.mark.asyncio
    async def test_state_hook_executed(self, fsm, mock_user):
        """Test state hook is executed."""
        hook = AsyncMock()
        fsm.register_state_hook(UserState.PENDING, hook)

        await fsm.transition(mock_user, UserEvent.TRIAL_STARTED)

        hook.assert_called_once_with(mock_user, UserState.PENDING)

    @pytest.mark.asyncio
    async def test_hook_error_handled(self, fsm, mock_user):
        """Test hook error doesn't break transition."""
        error_hook = AsyncMock(side_effect=Exception("Hook error"))
        fsm.register_transition_hook(UserEvent.TRIAL_STARTED, error_hook)

        success, new_state = await fsm.transition(mock_user, UserEvent.TRIAL_STARTED)

        assert success is True
        assert new_state == UserState.PENDING


class TestStateQueries:
    """Test state-related queries."""

    @pytest.fixture
    def fsm(self):
        """Create UserFSM instance."""
        return UserFSM()

    def test_get_available_events_from_new(self, fsm):
        """Test getting available events from NEW state."""
        events = fsm.get_available_events(UserState.NEW)

        assert UserEvent.TRIAL_STARTED in events
        assert UserEvent.BLOCKED in events
        assert len(events) == 2

    def test_get_available_events_from_pending(self, fsm):
        """Test getting available events from PENDING state."""
        events = fsm.get_available_events(UserState.PENDING)

        assert UserEvent.CONVERTED in events
        assert UserEvent.TRIAL_EXPIRED in events
        assert UserEvent.BLOCKED in events
        assert len(events) == 3

    def test_get_available_events_from_active(self, fsm):
        """Test getting available events from ACTIVE state."""
        events = fsm.get_available_events(UserState.ACTIVE)

        assert UserEvent.TRIAL_EXPIRED in events
        assert UserEvent.BLOCKED in events
        assert len(events) == 2

    def test_get_available_events_from_silent(self, fsm):
        """Test getting available events from SILENT state."""
        events = fsm.get_available_events(UserState.SILENT)

        assert UserEvent.REACTIVATED in events
        assert UserEvent.CHURNED in events
        assert UserEvent.BLOCKED in events
        assert len(events) == 3

    def test_get_available_events_from_terminal_state(self, fsm):
        """Test getting available events from terminal states."""
        churned_events = fsm.get_available_events(UserState.CHURNED)
        blocked_events = fsm.get_available_events(UserState.BLOCKED)

        assert len(churned_events) == 0
        assert len(blocked_events) == 1
        assert UserEvent.UNBLOCKED in blocked_events

    def test_get_state_duration(self, fsm):
        """Test getting state durations."""
        assert fsm.get_state_duration(UserState.NEW) == timedelta(hours=1)
        assert fsm.get_state_duration(UserState.PENDING) == timedelta(days=7)
        assert fsm.get_state_duration(UserState.SILENT) == timedelta(days=14)
        assert fsm.get_state_duration(UserState.ACTIVE) is None
        assert fsm.get_state_duration(UserState.CHURNED) is None
        assert fsm.get_state_duration(UserState.BLOCKED) is None


class TestStateClassification:
    """Test state classification methods."""

    @pytest.fixture
    def fsm(self):
        """Create UserFSM instance."""
        return UserFSM()

    def test_is_terminal_state(self, fsm):
        """Test terminal state detection."""
        assert fsm.is_terminal_state(UserState.CHURNED) is True
        assert fsm.is_terminal_state(UserState.BLOCKED) is True
        assert fsm.is_terminal_state(UserState.ACTIVE) is False
        assert fsm.is_terminal_state(UserState.SILENT) is False

    def test_is_active_state(self, fsm):
        """Test active state detection."""
        assert fsm.is_active_state(UserState.NEW) is True
        assert fsm.is_active_state(UserState.PENDING) is True
        assert fsm.is_active_state(UserState.ACTIVE) is True
        assert fsm.is_active_state(UserState.SILENT) is False
        assert fsm.is_active_state(UserState.CHURNED) is False
        assert fsm.is_active_state(UserState.BLOCKED) is False


class TestStateTransitionRecord:
    """Test StateTransition dataclass."""

    def test_state_transition_creation(self):
        """Test creating StateTransition record."""
        now = datetime.utcnow()
        transition = StateTransition(
            from_state=UserState.NEW,
            to_state=UserState.PENDING,
            event=UserEvent.TRIAL_STARTED,
            timestamp=now,
            metadata={"source": "test"},
        )

        assert transition.from_state == UserState.NEW
        assert transition.to_state == UserState.PENDING
        assert transition.event == UserEvent.TRIAL_STARTED
        assert transition.timestamp == now
        assert transition.metadata == {"source": "test"}

    def test_state_transition_default_metadata(self):
        """Test StateTransition with default metadata."""
        transition = StateTransition(
            from_state=UserState.ACTIVE,
            to_state=UserState.SILENT,
            event=UserEvent.TRIAL_EXPIRED,
            timestamp=datetime.utcnow(),
        )

        assert transition.metadata == {}


class TestUserEventEnum:
    """Test UserEvent enum values."""

    def test_all_events_defined(self):
        """Test all expected events are defined."""
        expected_events = {
            UserEvent.REGISTER,
            UserEvent.TRIAL_STARTED,
            UserEvent.TRIAL_EXPIRED,
            UserEvent.CONVERTED,
            UserEvent.CHURN_WARNING,
            UserEvent.REACTIVATED,
            UserEvent.CHURNED,
            UserEvent.BLOCKED,
            UserEvent.UNBLOCKED,
        }

        assert set(UserEvent) == expected_events

    def test_event_string_values(self):
        """Test event string values."""
        assert UserEvent.REGISTER.value == "registered"
        assert UserEvent.TRIAL_STARTED.value == "trial_started"
        assert UserEvent.CONVERTED.value == "converted"
        assert UserEvent.BLOCKED.value == "blocked"
