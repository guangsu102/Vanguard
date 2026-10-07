"""Idempotent candidate-preview admission helpers.

Candidate preview is invoked by the scheduler, event handlers and retry
workers.  They must all use the same operation key before doing any Telegram
read.  This module keeps the policy separate from the actual preview client so
it can be used from existing services without duplicating the read logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

from app.core.account.idempotency import OperationClaim, OperationGuard, operation_key


class PreviewLedger(Protocol):
    """Durable preview result store implemented by the acquisition service."""

    async def get_by_operation_key(self, key: str) -> Any: ...

    async def mark_running(self, key: str, token: str) -> Any: ...


@dataclass(frozen=True, slots=True)
class PreviewAdmission:
    key: str
    claim: OperationClaim | None
    existing: Any = None

    @property
    def should_read(self) -> bool:
        # A durable result (including failed/terminal results) or an existing
        # running claim must be returned by the caller without another read.
        return self.claim is not None and self.existing is None


async def admit_candidate_preview(
    *,
    account_id: int | str,
    candidate_id: int | str,
    preview_version: str | int,
    ledger: PreviewLedger,
    guard: OperationGuard,
) -> PreviewAdmission:
    """Atomically admit one logical candidate preview.

    Call this before any Telegram request.  ``existing`` is returned for both
    completed and failed previews.  A second scheduler sees the running lock
    and receives ``claim=None``; it must wait for the first worker's durable
    result instead of issuing another preview/read.
    """

    key = operation_key(
        account_id=account_id,
        operation="candidate_preview",
        subject_id=candidate_id,
        version=preview_version,
    )
    existing = await ledger.get_by_operation_key(key)
    if existing is not None:
        return PreviewAdmission(key=key, claim=None, existing=existing)

    token = uuid4().hex
    claim = await guard.claim(key, token)
    if claim is None:
        # A concurrent worker owns this operation.  Never perform a fallback
        # read here; the caller should defer until its durable result appears.
        return PreviewAdmission(key=key, claim=None)

    # Persist the running marker after claiming the distributed lock.  If the
    # insert loses a race, the caller must release the claim and reuse the
    # winner's result rather than reading again.
    marker = await ledger.mark_running(key, token)
    if marker is not None and marker is not True:
        await guard.release(claim)
        return PreviewAdmission(key=key, claim=None, existing=marker)
    return PreviewAdmission(key=key, claim=claim)

