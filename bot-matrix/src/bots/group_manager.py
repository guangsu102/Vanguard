"""群组管理器 - 支持账号矩阵和群组分"""
from typing import Dict, List, Optional
from datetime import datetime
from dataclasses import dataclass, field
from enum import Enum
import json

from loguru import logger


class GroupGrade(Enum):
    """群组等级"""
    GRADE_A = "A"  # 可直接营销
    GRADE_B = "B"  # 观察后行动
    GRADE_C = "C"  # 仅监控
    GRADE_D = "D"  # 黑名单


class GroupStatus(Enum):
    """群组状态"""
    ACTIVE = "active"
    KICKED = "kicked"
    BLACKLIST = "blacklist"
    PENDING = "pending"


@dataclass
class GroupAccount:
    """群组中的账号"""
    account_name: str
    role: str  # main / aux1 / aux2
    joined_at: datetime = field(default_factory=datetime.now)
    last_active: datetime = field(default_factory=datetime.now)


@dataclass
class Group:
    """群组"""
    group_id: int
    group_name: str = ""
    group_username: str = ""
    grade: GroupGrade = GroupGrade.GRADE_C
    status: GroupStatus = GroupStatus.PENDING
    accounts: List[GroupAccount] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)

    def can_add_main_account(self) -> bool:
        """是否可以添加主号"""
        return not any(acc.role == "main" for acc in self.accounts)

    def can_add_aux_account(self) -> bool:
        """是否可以添加辅号"""
        return len(self.accounts) < 3 and self.can_add_main_account() is False

    def add_account(self, account_name: str, role: str) -> bool:
        """添加账号到群组"""
        if role == "main" and not self.can_add_main_account():
            logger.warning(f"群组 {self.group_id} 已达主号上限")
            return False

        if role.startswith("aux") and not self.can_add_aux_account():
            logger.warning(f"群组 {self.group_id} 已达账号上限")
            return False

        # 检查是否已存在
        if any(acc.account_name == account_name for acc in self.accounts):
            logger.warning(f"账号 {account_name} 已在群组 {self.group_id} 中")
            return False

        self.accounts.append(GroupAccount(
            account_name=account_name,
            role=role
        ))
        self.updated_at = datetime.now()
        return True

    def remove_account(self, account_name: str) -> bool:
        """移除账号"""
        for i, acc in enumerate(self.accounts):
            if acc.account_name == account_name:
                self.accounts.pop(i)
                self.updated_at = datetime.now()
                return True
        return False

    def get_main_account(self) -> Optional[GroupAccount]:
        """获取主号"""
        for acc in self.accounts:
            if acc.role == "main":
                return acc
        return None

    def get_all_accounts(self) -> List[str]:
        """获取所有账号名"""
        return [acc.account_name for acc in self.accounts]


class GroupManager:
    """群组管理器"""

    MAX_ACCOUNTS_PER_GROUP = 3

    def __init__(self, redis_client=None):
        self.redis = redis_client
        self._groups: Dict[int, Group] = {}
        self._account_groups: Dict[str, List[int]] = {}  # 账号 -> 群组列表

    async def initialize(self):
        """从存储加载"""
        if self.redis:
            data = await self.redis.get("groups:all")
            if data:
                try:
                    groups_data = json.loads(data)
                    for gid, gdata in groups_data.items():
                        group = Group(
                            group_id=int(gid),
                            group_name=gdata.get("name", ""),
                            group_username=gdata.get("username", ""),
                            grade=GroupGrade(gdata.get("grade", "C")),
                            status=GroupStatus(gdata.get("status", "pending"))
                        )
                        self._groups[group.group_id] = group
                except Exception as e:
                    logger.error(f"加载群组失败: {e}")

        logger.info(f"群组加载完成: {len(self._groups)} 个群组")

    async def _save_to_storage(self):
        """保存到存储"""
        if self.redis:
            data = {}
            for gid, group in self._groups.items():
                data[str(gid)] = {
                    "name": group.group_name,
                    "username": group.group_username,
                    "grade": group.grade.value,
                    "status": group.status.value,
                    "accounts": [
                        {"name": acc.account_name, "role": acc.role}
                        for acc in group.accounts
                    ]
                }
            await self.redis.set("groups:all", json.dumps(data))

    def add_group(
        self,
        group_id: int,
        group_name: str = "",
        group_username: str = "",
        grade: GroupGrade = GroupGrade.GRADE_C
    ) -> Group:
        """添加群组"""
        if group_id in self._groups:
            return self._groups[group_id]

        group = Group(
            group_id=group_id,
            group_name=group_name,
            group_username=group_username,
            grade=grade
        )
        self._groups[group_id] = group

        import asyncio
        asyncio.create_task(self._save_to_storage())

        logger.info(f"添加群组: {group_id} ({group_name})")
        return group

    def get_group(self, group_id: int) -> Optional[Group]:
        """获取群组"""
        return self._groups.get(group_id)

    def get_groups_by_account(self, account_name: str) -> List[Group]:
        """获取账号所在的群组"""
        return [
            g for g in self._groups.values()
            if any(acc.account_name == account_name for acc in g.accounts)
        ]

    def get_groups_by_grade(self, grade: GroupGrade) -> List[Group]:
        """获取指定等级的群组"""
        return [g for g in self._groups.values() if g.grade == grade]

    def assign_account(
        self,
        group_id: int,
        account_name: str,
        role: str = None
    ) -> bool:
        """分配账号到群组"""
        group = self.get_group(group_id)
        if not group:
            group = self.add_group(group_id)

        # 自动确定角色
        if role is None:
            if group.can_add_main_account():
                role = "main"
            elif group.can_add_aux_account():
                role = "aux1" if not any(acc.role == "aux1" for acc in group.accounts) else "aux2"
            else:
                logger.warning(f"群组 {group_id} 账号已满")
                return False

        # 添加账号
        if group.add_account(account_name, role):
            # 更新反向索引
            if account_name not in self._account_groups:
                self._account_groups[account_name] = []
            if group_id not in self._account_groups[account_name]:
                self._account_groups[account_name].append(group_id)

            import asyncio
            asyncio.create_task(self._save_to_storage())

            logger.info(f"账号 {account_name} 加入群组 {group_id} (角色: {role})")
            return True

        return False

    def unassign_account(self, group_id: int, account_name: str) -> bool:
        """移除群组中的账号"""
        group = self.get_group(group_id)
        if not group:
            return False

        if group.remove_account(account_name):
            # 更新反向索引
            if account_name in self._account_groups:
                if group_id in self._account_groups[account_name]:
                    self._account_groups[account_name].remove(group_id)

            import asyncio
            asyncio.create_task(self._save_to_storage())
            return True

        return False

    def set_grade(self, group_id: int, grade: GroupGrade):
        """设置群组等级"""
        group = self.get_group(group_id)
        if group:
            group.grade = grade
            group.updated_at = datetime.now()

            import asyncio
            asyncio.create_task(self._save_to_storage())

    def set_status(self, group_id: int, status: GroupStatus):
        """设置群组状态"""
        group = self.get_group(group_id)
        if group:
            group.status = status
            group.updated_at = datetime.now()

            import asyncio
            asyncio.create_task(self._save_to_storage())

    def mark_kicked(self, group_id: int):
        """标记为被踢"""
        self.set_status(group_id, GroupStatus.KICKED)

    def mark_blacklist(self, group_id: int):
        """加入黑名单"""
        self.set_status(group_id, GroupStatus.BLACKLIST)

    def get_statistics(self) -> Dict:
        """获取统计信息"""
        stats = {
            "total": len(self._groups),
            "by_grade": {},
            "by_status": {},
            "accounts": {}
        }

        for grade in GroupGrade:
            stats["by_grade"][grade.value] = len(self.get_groups_by_grade(grade))

        for status in GroupStatus:
            stats["by_status"][status.value] = sum(
                1 for g in self._groups.values() if g.status == status
            )

        for account_name, group_ids in self._account_groups.items():
            stats["accounts"][account_name] = len(group_ids)

        return stats
