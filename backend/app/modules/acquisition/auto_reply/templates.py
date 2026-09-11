"""
Message Templates Module

Template management for auto发言 and reply messages.
"""

import asyncio
import random
from dataclasses import dataclass
from typing import Optional

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.acquisition.models import MessageTemplate, MessageType

logger = structlog.get_logger()


@dataclass
class TemplateData:
    """Template data for rendering."""
    user_name: str = ""
    group_name: str = ""
    bot_name: str = ""
    register_link: str = ""
    keyword: str = ""


class TemplateEngine:
    """
    Message template engine for generating dynamic content.

    Manages templates with variable substitution and caching.
    """

    def __init__(
        self,
        db: Optional[AsyncSession] = None,
        *,
        scope: str = "acquisition",
        owned_group_asset_id: int | None = None,
    ):
        """
        Initialize TemplateEngine.

        Args:
            db: Optional database session for persistent templates
        """
        if scope not in {"acquisition", "owned_group"}:
            raise ValueError("template scope must be acquisition or owned_group")
        if scope == "acquisition" and owned_group_asset_id is not None:
            raise ValueError("acquisition templates cannot have an owned-group asset")
        if scope == "owned_group" and owned_group_asset_id is None:
            raise ValueError("owned_group template scope requires owned_group_asset_id")
        self.db = db
        self.scope = scope
        self.owned_group_asset_id = owned_group_asset_id
        self._cache: dict[tuple[str, int | None, int], MessageTemplate] = {}
        self._lock = asyncio.Lock()
        self.logger = logger.bind(module="template_engine")

    def _scope_conditions(self) -> tuple:
        return (
            MessageTemplate.scope == self.scope,
            MessageTemplate.owned_group_asset_id
            == (self.owned_group_asset_id if self.scope == "owned_group" else None),
        )

    def _cache_key(self, template_id: int) -> tuple[str, int | None, int]:
        return (self.scope, self.owned_group_asset_id, int(template_id))

    async def load_templates(self) -> int:
        """
        Load templates from database.

        Returns:
            Number of templates loaded
        """
        if not self.db:
            return 0

        async with self._lock:
            result = await self.db.execute(
                select(MessageTemplate).where(
                    *self._scope_conditions(),
                    MessageTemplate.enabled.is_(True),
                )
            )
            templates = list(result.scalars().all())

            self._cache.clear()
            for tmpl in templates:
                self._cache[self._cache_key(tmpl.id)] = tmpl

            self.logger.debug("templates_loaded", count=len(self._cache))
            return len(self._cache)

    async def get_template(self, template_id: int) -> Optional[MessageTemplate]:
        """
        Get template by ID.

        Args:
            template_id: Template ID

        Returns:
            MessageTemplate or None
        """
        cache_key = self._cache_key(template_id)
        if cache_key in self._cache:
            return self._cache[cache_key]

        if self.db:
            result = await self.db.execute(
                select(MessageTemplate).where(
                    MessageTemplate.id == template_id,
                    *self._scope_conditions(),
                )
            )
            template = result.scalar_one_or_none()
            if template is not None:
                self._cache[cache_key] = template
            return template

        return None

    async def get_template_by_keyword(
        self,
        keyword_id: int,
    ) -> Optional[MessageTemplate]:
        """
        Get template associated with a keyword.

        Args:
            keyword_id: Keyword ID

        Returns:
            Associated MessageTemplate or None
        """
        if self.db:
            result = await self.db.execute(
                select(MessageTemplate)
                .join_from(MessageTemplate, MessageTemplate)
                .where(
                    MessageTemplate.id == keyword_id,
                    *self._scope_conditions(),
                )  # 简化：假设 keyword_id 对应 template_id
            )
            return result.scalar_one_or_none()
        return None

    async def get_random_template(
        self,
        message_type: MessageType,
    ) -> Optional[MessageTemplate]:
        """
        Get a random template of specified type.

        Args:
            message_type: Type of message

        Returns:
            Random MessageTemplate or None
        """
        type_templates = [
            tmpl for tmpl in self._cache.values()
            if tmpl.message_type == message_type and tmpl.enabled
        ]

        if not type_templates and self.db:
            result = await self.db.execute(
                select(MessageTemplate).where(
                    *self._scope_conditions(),
                    MessageTemplate.message_type == message_type,
                    MessageTemplate.enabled.is_(True),
                )
            )
            type_templates = list(result.scalars().all())

        if not type_templates:
            return None

        return random.choice(type_templates)

    def render(
        self,
        template: MessageTemplate,
        **kwargs,
    ) -> str:
        """
        Render template with provided variables.

        Args:
            template: Template to render
            **kwargs: Variables for substitution

        Returns:
            Rendered content
        """
        content = template.content

        if self.scope == "owned_group":
            replacements = {
                "user_name": kwargs.get("user_name", "朋友"),
                "group_name": kwargs.get("group_name", ""),
                "account_name": kwargs.get("account_name", ""),
                "current_date": kwargs.get("current_date", ""),
                "current_time": kwargs.get("current_time", ""),
                "promotion_url": kwargs.get("promotion_url", ""),
                "promotion_cta": kwargs.get("promotion_cta", ""),
            }
        else:
            # Growth templates retain their historical variable contract.
            replacements = {
                "user_name": kwargs.get("user_name", "朋友"),
                "group_name": kwargs.get("group_name", ""),
                "bot_name": kwargs.get("bot_name", "XBoard"),
                "register_link": kwargs.get("register_link", ""),
                "keyword": kwargs.get("keyword", ""),
            }

        for var, value in replacements.items():
            placeholder = f"{{{{{var}}}}}"
            content = content.replace(placeholder, str(value))

        return content

    def render_string(
        self,
        content: str,
        **kwargs,
    ) -> str:
        """
        Render a string template with variables.

        Args:
            content: Template content string
            **kwargs: Variables for substitution

        Returns:
            Rendered content
        """
        if self.scope == "owned_group":
            replacements = {
                "user_name": kwargs.get("user_name", "朋友"),
                "group_name": kwargs.get("group_name", ""),
                "account_name": kwargs.get("account_name", ""),
                "current_date": kwargs.get("current_date", ""),
                "current_time": kwargs.get("current_time", ""),
                "promotion_url": kwargs.get("promotion_url", ""),
                "promotion_cta": kwargs.get("promotion_cta", ""),
            }
        else:
            replacements = {
                "user_name": kwargs.get("user_name", "朋友"),
                "group_name": kwargs.get("group_name", ""),
                "bot_name": kwargs.get("bot_name", "XBoard"),
                "register_link": kwargs.get("register_link", ""),
            }

        for var, value in replacements.items():
            placeholder = f"{{{{{var}}}}}"
            content = content.replace(placeholder, str(value))

        return content


class MessageTemplateStore:
    """
    In-memory template store with default templates.

    Provides fallback templates when database is unavailable.
    """

    DEFAULT_TEMPLATES = {
        MessageType.INTERACTION: [
            "大家平时都用哪些节点呀？感觉速度怎么样？",
            "有人用过这个吗？效果怎么样？",
            "话说这个功能怎么用啊？",
            "有推荐的节点吗？",
        ],
        MessageType.SHARE: [
            "用了一段时间了，整体还不错，推荐给大家试试",
            "说实话挺好用的，速度稳定",
            "个人感觉挺不错的，值得一试",
            "用了这么久没出过什么问题，推荐",
        ],
        MessageType.GUIDE: [
            "有需要可以试试这个：{{register_link}}",
            "新用户有优惠，点击注册：{{register_link}}",
            "需要的可以了解一下：{{register_link}}",
        ],
        MessageType.QA: [
            "问一下，有人知道怎么设置吗？",
            "求教，这个怎么操作的？",
            "请问有了解的吗？",
        ],
    }

    def __init__(self):
        """Initialize template store with defaults."""
        self.logger = logger.bind(module="template_store")

    def get_random(self, message_type: MessageType) -> str:
        """
        Get a random template of specified type.

        Args:
            message_type: Type of message

        Returns:
            Random template string
        """
        templates = self.DEFAULT_TEMPLATES.get(message_type, [])
        if not templates:
            return "有需要可以试试~"

        return random.choice(templates)

    def get_all(self, message_type: MessageType) -> list[str]:
        """Get all templates of specified type."""
        return self.DEFAULT_TEMPLATES.get(message_type, [])

    def get_by_keyword_type(self, keyword_type: str) -> list[str]:
        """
        Get templates suitable for keyword type.

        Args:
            keyword_type: Keyword type

        Returns:
            List of suitable template strings
        """
        type_mapping = {
            "demand": [MessageType.GUIDE],
            "inquiry": [MessageType.QA, MessageType.SHARE],
            "price": [MessageType.GUIDE],
            "competitor": [],
        }

        message_types = type_mapping.get(keyword_type, [MessageType.INTERACTION])
        templates = []

        for msg_type in message_types:
            templates.extend(self.get_all(msg_type))

        return templates or ["有需要可以试试~"]
