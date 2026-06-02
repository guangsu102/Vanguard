"""
User FSM Module

Implements user lifecycle state machine.

Features:
- State definitions and transitions
- Event-driven state changes
- State persistence and history
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Callable, Optional

import structlog

from app.core.user.models import User, UserState

logger = structlog.get_logger()


class UserEvent(str, Enum):
    """User state transition events."""

    REGISTER = "registered"
    TRIAL_STARTED = "trial_started"
    TRIAL_EXPIRED = "trial_expired"
    CONVERTED = "converted"
    CHURN_WARNING = "churn_warning"
    REACTIVATED = "reactivated"
    CHURNED = "churned"
    BLOCKED = "blocked"
    UNBLOCKED = "unblocked"


@dataclass
class StateTransition:
    """Record of a state transition."""

    from_state: UserState
    to_state: UserState
    event: UserEvent
    timestamp: datetime
    metadata: dict = field(default_factory=dict)


class UserFSM:
    """
    User state machine for managing user lifecycle.

    Manages user state transitions based on events and business rules.

    State Flow:
        NEW -> PENDING (trial_started) -> ACTIVE (converted)
                                         -> SILENT (trial_expired)
        SILENT -> ACTIVE (reactivated)
               -> CHURNED (churned)
        ANY -> BLOCKED (blocked)
    """

    TRANSITIONS: dict[tuple[UserState, UserEvent], UserState] = {
        (UserState.NEW, UserEvent.TRIAL_STARTED): UserState.PENDING,
        (UserState.PENDING, UserEvent.CONVERTED): UserState.ACTIVE,
        (UserState.PENDING, UserEvent.TRIAL_EXPIRED): UserState.SILENT,
        (UserState.ACTIVE, UserEvent.TRIAL_EXPIRED): UserState.SILENT,
        (UserState.SILENT, UserEvent.REACTIVATED): UserState.ACTIVE,
        (UserState.SILENT, UserEvent.CHURNED): UserState.CHURNED,
        (UserState.NEW, UserEvent.BLOCKED): UserState.BLOCKED,
        (UserState.PENDING, UserEvent.BLOCKED): UserState.BLOCKED,
        (UserState.ACTIVE, UserEvent.BLOCKED): UserState.BLOCKED,
        (UserState.SILENT, UserEvent.BLOCKED): UserState.BLOCKED,
        (UserState.BLOCKED, UserEvent.UNBLOCKED): UserState.SILENT,
    }

    def __init__(self):
        """Initialize UserFSM."""
        self.logger = logger.bind(module="user_fsm")
        self._transition_hooks: dict[UserEvent, list[Callable]] = {
            event: [] for event in UserEvent
        }
        self._state_hooks: dict[UserState, list[Callable]] = {
            state: [] for state in UserState
        }

    def register_transition_hook(self, event: UserEvent, hook: Callable) -> None:
        """
        Register a hook to run on state transition.

        Args:
            event: Event that triggers the hook
            hook: Async function(user, old_state, new_state, event)
        """
        self._transition_hooks[event].append(hook)

    def register_state_hook(self, state: UserState, hook: Callable) -> None:
        """
        Register a hook to run when entering a state.

        Args:
            state: State to hook
            hook: Async function(user, state)
        """
        self._state_hooks[state].append(hook)

    def can_transition(self, current_state: UserState, event: UserEvent) -> bool:
        """
        Check if transition is valid.

        Args:
            current_state: Current user state
            event: Event to trigger

        Returns:
            True if transition is valid
        """
        return (current_state, event) in self.TRANSITIONS

    def get_next_state(
        self,
        current_state: UserState,
        event: UserEvent,
    ) -> Optional[UserState]:
        """
        Get next state for transition.

        Args:
            current_state: Current state
            event: Triggering event

        Returns:
            Next state or None if invalid transition
        """
        return self.TRANSITIONS.get((current_state, event))

    async def transition(
        self,
        user: User,
        event: UserEvent,
        metadata: Optional[dict] = None,
    ) -> tuple[bool, Optional[UserState]]:
        """
        Perform state transition.

        Args:
            user: User to transition
            event: Event to trigger
            metadata: Optional metadata for the transition

        Returns:
            Tuple of (success, new_state)
        """
        current_state = user.state
        new_state = self.get_next_state(current_state, event)

        if new_state is None:
            self.logger.warning(
                "invalid_transition",
                user_id=user.id,
                current_state=current_state.value,
                trigger_event=event.value,
            )
            return False, None

        old_state = user.state
        user.state = new_state

        self.logger.info(
            "state_transition",
            user_id=user.id,
            old_state=old_state.value,
            new_state=new_state.value,
            trigger_event=event.value,
        )

        await self._run_transition_hooks(user, old_state, new_state, event, metadata)

        if old_state != new_state:
            await self._run_state_hooks(user, new_state)

        return True, new_state

    async def _run_transition_hooks(
        self,
        user: User,
        old_state: UserState,
        new_state: UserState,
        event: UserEvent,
        metadata: Optional[dict],
    ) -> None:
        """Run hooks for transition."""
        hooks = self._transition_hooks.get(event, [])
        for hook in hooks:
            try:
                await hook(user, old_state, new_state, event, metadata or {})
            except Exception as e:
                self.logger.error(
                    "transition_hook_error",
                    user_id=user.id,
                    hook=hook.__name__,
                    error=str(e),
                )

    async def _run_state_hooks(
        self,
        user: User,
        state: UserState,
    ) -> None:
        """Run hooks for entering state."""
        hooks = self._state_hooks.get(state, [])
        for hook in hooks:
            try:
                await hook(user, state)
            except Exception as e:
                self.logger.error(
                    "state_hook_error",
                    user_id=user.id,
                    state=state.value,
                    hook=hook.__name__,
                    error=str(e),
                )

    def get_available_events(self, state: UserState) -> list[UserEvent]:
        """
        Get list of events that can trigger transitions from current state.

        Args:
            state: Current state

        Returns:
            List of valid events
        """
        return [
            event
            for (s, event), next_state in self.TRANSITIONS.items()
            if s == state
        ]

    def get_state_duration(self, state: UserState) -> Optional[timedelta]:
        """
        Get typical duration for a state.

        Args:
            state: State to query

        Returns:
            Expected duration or None
        """
        durations = {
            UserState.NEW: timedelta(hours=1),
            UserState.PENDING: timedelta(days=7),
            UserState.ACTIVE: None,
            UserState.SILENT: timedelta(days=14),
            UserState.CHURNED: None,
            UserState.BLOCKED: None,
        }
        return durations.get(state)

    def is_terminal_state(self, state: UserState) -> bool:
        """
        Check if state is terminal (no outgoing transitions).

        Args:
            state: State to check

        Returns:
            True if terminal
        """
        return state in [UserState.CHURNED, UserState.BLOCKED]

    def is_active_state(self, state: UserState) -> bool:
        """
        Check if state represents an active user.

        Args:
            state: State to check

        Returns:
            True if active
        """
        return state in [UserState.NEW, UserState.PENDING, UserState.ACTIVE]
