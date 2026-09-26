"""One-request SELECT reuse for the capacity projection, never execution gates."""

from typing import Any


class CapacityReads:
    """Reuse buffered results only within one read-only capacity calculation.

    No shared cache, writes, locks, or transaction operations are permitted. Actual
    send/leave workers retain their original sessions and fresh boundary checks.
    """

    def __init__(self, db: Any):
        self.db = db
        self.results: dict[tuple, Any] = {}
        self.objects: dict[tuple, Any] = {}

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        if not statement.is_select or statement._for_update_arg is not None:
            raise RuntimeError("capacity_projection_requires_unlocked_select")
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
            self.objects[key] = await self.db.get(entity, ident, **kwargs)
        return self.objects[key]

    async def flush(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("capacity_projection_is_read_only")

    async def commit(self) -> None:
        raise RuntimeError("capacity_projection_is_read_only")

    def add(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("capacity_projection_is_read_only")

    def get_bind(self) -> Any:
        return self.db.get_bind()
