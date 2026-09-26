"""Identity compatibility for legacy raw IDs and Telegram marked peer IDs."""

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.group.models import Group
from app.modules.owned_group.models import OwnedGroupAsset


def telegram_group_id_aliases(value: int | None) -> set[int]:
    if value is None:
        return set()
    value = int(value)
    if value < -1_000_000_000_000:
        return {value, -value - 1_000_000_000_000}
    if value < 0:
        return {value, -value}
    if value > 0:
        # Legacy raw IDs do not retain whether the entity was a chat or channel.
        return {value, -value, -1_000_000_000_000 - value}
    return set()


async def is_owned_group_target(
    db: AsyncSession,
    *,
    core_group_id: int | None = None,
    telegram_group_id: int | None = None,
) -> bool:
    aliases = telegram_group_id_aliases(telegram_group_id)
    predicates = []
    if core_group_id is not None:
        predicates.append(OwnedGroupAsset.core_group_id == core_group_id)
    if aliases:
        predicates.extend(
            [
                OwnedGroupAsset.telegram_chat_id.in_(aliases),
                OwnedGroupAsset.core_group_id.in_(
                    select(Group.id).where(Group.group_id.in_(aliases))
                ),
            ]
        )
    if not predicates:
        return False
    # Archiving a local asset is not confirmation that its Telegram group is gone.
    return await db.scalar(select(OwnedGroupAsset.id).where(or_(*predicates)).limit(1)) is not None
