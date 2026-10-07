"""One-request SELECT reuse for the capacity projection, never execution gates."""

from typing import Any

from sqlalchemy.orm import raiseload


class _CapacitySavepoint:
    """Close only this savepoint; never commit the enclosing transaction."""

    def __init__(self, reader: "CapacityReads", savepoint: Any):
        self.reader = reader
        self.savepoint = savepoint

    async def commit(self) -> None:
        await self.savepoint.commit()

    async def rollback(self) -> None:
        try:
            await self.savepoint.rollback()
        finally:
            # SQLAlchemy may expire ORM objects when rolling back a savepoint.
            # Neither those objects nor buffered results may be reused after it.
            self.reader.results.clear()
            self.reader.objects.clear()


class CapacityReads:
    """Reuse buffered results only within one read-only capacity calculation.

    No shared cache, writes, locks, or outer transaction operations are permitted.
    Savepoints isolate optional forecasts without ending the business transaction.
    Actual send/leave workers retain their original sessions and fresh checks.
    """

    def __init__(self, db: Any):
        self.db = db
        self.results: dict[tuple, Any] = {}
        self.objects: dict[tuple, Any] = {}

    @property
    def is_active(self) -> bool:
        return self.db.is_active

    async def begin_nested(self) -> _CapacitySavepoint:
        # Preserve AsyncSession's pending-business flush and propagate failures.
        # Forecasts still receive this facade, so writes and locks remain denied.
        return _CapacitySavepoint(self, await self.db.begin_nested())

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        if not statement.is_select or statement._for_update_arg is not None:
            raise RuntimeError("capacity_projection_requires_unlocked_select")
        # Projection helpers explicitly load the relationships they need. Avoid
        # model-default joins/select-in cascades (account -> proxy/config etc.).
        # Unexpected relationship access fails rather than issuing hidden reads.
        statement = statement.options(raiseload("*"))
        shape = statement._generate_cache_key()
        if shape is None:
            return await self.db.execute(statement, *args, **kwargs)
        # SQLAlchemy's structural key already identifies bind positions. Reading
        # their values avoids recompiling SQL just to discover query parameters.
        key = (
            shape.key,
            repr(tuple(param.effective_value for param in shape.bindparams)),
            repr(args),
            repr(kwargs),
        )
        if key not in self.results:
            result = await self.db.execute(statement, *args, **kwargs)
            self.results[key] = result.freeze()
        return self.results[key]()

    async def scalars(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        return (await self.execute(statement, *args, **kwargs)).scalars()

    async def scalar(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        return (await self.execute(statement, *args, **kwargs)).scalar()

    async def get(self, entity: Any, ident: Any, **kwargs: Any) -> Any:
        if kwargs.get("with_for_update"):
            raise RuntimeError("capacity_projection_requires_unlocked_select")
        key = (entity, repr(ident), repr(kwargs.get("options")))
        if key not in self.objects:
            options = [raiseload("*"), *(kwargs.get("options") or ())]
            self.objects[key] = await self.db.get(entity, ident, **{**kwargs, "options": options})
        return self.objects[key]

    async def flush(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("capacity_projection_is_read_only")

    async def commit(self) -> None:
        raise RuntimeError("capacity_projection_is_read_only")

    def add(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("capacity_projection_is_read_only")

    def get_bind(self) -> Any:
        return self.db.get_bind()
