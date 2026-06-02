"""
Guardian Rule Engine

Message evaluation and rule matching for group moderation.
"""

import asyncio
import re
import re as regex_module
from dataclasses import dataclass, field
from typing import Optional

import structlog
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.guardian.models import (
    ModerationSensitiveKeyword,
    ModerationRule,
    RuleType,
    ViolationAction,
    ViolationLevel,
    Whitelist,
)

logger = structlog.get_logger()


@dataclass
class MatchedRule:
    """Matched moderation rule result."""
    rule_id: int
    rule_type: RuleType
    pattern: str
    matched_content: str
    level: ViolationLevel
    action: ViolationAction


@dataclass
class EvaluationResult:
    """Result of message evaluation."""
    is_violation: bool
    matched_rules: list[MatchedRule] = field(default_factory=list)
    recommended_action: ViolationAction = ViolationAction.WARN
    severity: ViolationLevel = ViolationLevel.LOW
    should_delete: bool = False
    should_warn: bool = True
    reason: str = ""

    @property
    def has_matched(self) -> bool:
        return len(self.matched_rules) > 0


class GuardianRuleEngine:
    """
    Guardian rule engine for message moderation.
    
    Evaluates messages against configured rules and returns evaluation results.
    """
    
    def __init__(self, db: AsyncSession, keyword_engine):
        """
        Initialize GuardianRuleEngine.
        
        Args:
            db: Database session
            keyword_engine: Keyword engine for matching
        """
        self.db = db
        self.keyword_engine = keyword_engine
        self._rules_cache: dict[int, list[ModerationRule]] = {}
        self._whitelist_cache: dict[int, set[str]] = {}
        self._global_rules: list[ModerationRule] = []
        self._lock = asyncio.Lock()
        self.logger = logger.bind(module="guardian_rule_engine")
    
    async def load_rules(self) -> int:
        """
        Load all active rules into memory cache.
        
        Returns:
            Number of rules loaded
        """
        async with self._lock:
            result = await self.db.execute(
                select(ModerationRule).where(ModerationRule.enabled == True)
            )
            rules = list(result.scalars().all())
            
            self._global_rules = [r for r in rules if r.group_id is None]
            
            self._rules_cache.clear()
            for rule in rules:
                if rule.group_id is not None:
                    if rule.group_id not in self._rules_cache:
                        self._rules_cache[rule.group_id] = []
                    self._rules_cache[rule.group_id].append(rule)
            
            total = len(self._global_rules) + sum(len(v) for v in self._rules_cache.values())
            self.logger.info("guardian_rules_loaded", count=total)
            return total
    
    async def load_whitelist(self, group_id: Optional[int] = None) -> set[str]:
        """
        Load whitelist for a group.
        
        Args:
            group_id: Group ID (None for global whitelist)
            
        Returns:
            Set of whitelisted values
        """
        async with self._lock:
            query = select(Whitelist).where(Whitelist.expires_at.is_(None))
            
            if group_id:
                query = query.where(
                    (Whitelist.group_id == group_id) | (Whitelist.group_id.is_(None))
                )
            
            result = await self.db.execute(query)
            whitelist = list(result.scalars().all())
            
            whitelist_set = {w.value.lower() for w in whitelist}
            
            if group_id:
                self._whitelist_cache[group_id] = whitelist_set
            
            return whitelist_set
    
    def _compile_pattern(self, pattern: str, rule_type: RuleType) -> Optional[regex_module.Pattern]:
        """
        Compile rule pattern to regex.
        
        Args:
            pattern: Pattern string
            rule_type: Type of rule
            
        Returns:
            Compiled regex pattern or None if invalid
        """
        try:
            if rule_type == RuleType.KEYWORD:
                escaped = re.escape(pattern)
                return re.compile(escaped, re.IGNORECASE)
            elif rule_type == RuleType.DOMAIN:
                return re.compile(pattern, re.IGNORECASE)
            elif rule_type == RuleType.REGEX:
                return re.compile(pattern, re.IGNORECASE)
            else:
                return None
        except re.error as e:
            self.logger.warning("invalid_rule_pattern", pattern=pattern, error=str(e))
            return None
    
    async def evaluate_message(
        self,
        text: str,
        user_id: int,
        group_id: int,
        sender_username: Optional[str] = None
    ) -> EvaluationResult:
        """
        Evaluate a message against all applicable rules.
        
        Args:
            text: Message text
            user_id: Sender user ID
            group_id: Group ID
            sender_username: Sender username (for whitelist check)
            
        Returns:
            EvaluationResult with matched rules and recommended actions
        """
        if not text:
            return EvaluationResult(is_violation=False)
        
        matched_rules = []
        
        if sender_username and await self._is_whitelisted(sender_username, group_id):
            self.logger.debug("user_whitelisted", user_id=user_id, username=sender_username)
            return EvaluationResult(is_violation=False, reason="whitelisted")
        
        if await self._is_user_whitelisted(user_id, group_id):
            return EvaluationResult(is_violation=False, reason="user_whitelisted")
        
        keyword_matches = await self._check_keyword_rules(text, group_id)
        matched_rules.extend(keyword_matches)
        
        domain_matches = await self._check_domain_rules(text, group_id)
        matched_rules.extend(domain_matches)
        
        if not matched_rules:
            keyword_engine_matches = await self._check_keyword_engine(text, group_id)
            matched_rules.extend(keyword_engine_matches)
        
        if not matched_rules:
            return EvaluationResult(
                is_violation=False,
                reason="no_matched_rules"
            )
        
        highest_level = self._get_highest_severity(matched_rules)
        recommended_action = self._get_recommended_action(matched_rules, highest_level)
        
        return EvaluationResult(
            is_violation=True,
            matched_rules=matched_rules,
            recommended_action=recommended_action,
            severity=highest_level,
            should_delete=highest_level in [ViolationLevel.HIGH, ViolationLevel.MEDIUM],
            should_warn=True,
            reason=f"matched_{len(matched_rules)}_rules"
        )
    
    async def _check_keyword_rules(
        self,
        text: str,
        group_id: int
    ) -> list[MatchedRule]:
        """Check keyword rules."""
        matched = []
        rules = await self._get_applicable_rules(group_id, RuleType.KEYWORD)
        
        for rule in rules:
            pattern = self._compile_pattern(rule.pattern, RuleType.KEYWORD)
            if not pattern:
                continue
            
            match = pattern.search(text)
            if match:
                matched.append(MatchedRule(
                    rule_id=rule.id,
                    rule_type=RuleType.KEYWORD,
                    pattern=rule.pattern,
                    matched_content=match.group(),
                    level=rule.level,
                    action=rule.action
                ))
                self.logger.info(
                    "keyword_rule_matched",
                    rule_id=rule.id,
                    pattern=rule.pattern,
                    matched=match.group()
                )
        
        return matched
    
    async def _check_domain_rules(
        self,
        text: str,
        group_id: int
    ) -> list[MatchedRule]:
        """Check domain rules."""
        matched = []
        rules = await self._get_applicable_rules(group_id, RuleType.DOMAIN)
        
        url_pattern = re.compile(
            r'(?:https?://)?(?:[\w-]+\.)*(?:[\w-]+)\.(?:com|net|org|io|co|me|app|xyz|top|vip|cc|ru|cn|info|biz|tv|online|site|website|space|club|live|pro|work|tech|tools| dev|ai|cloud|host|site|store|shop|fun|tech|pro|app|link|in|io|cc|tv|me|fm|fr|de|uk|au|ru|cn|jp|kr|sg|hk|tw|mobi|la|mn|su|ws|sh|ac|ag|ar|at|be|bg|br|bz|ca|ch|cl|cm|cx|cz|dk|ee|eu|fi|fm|ge|gl|gm|gr|gs|gy|hk|hn|hr|hu|id|ie|il|im|in|info|iq|ir|is|it|je|jo|jp|ke|kg|kh|ki|km|kn|kp|kr|kz|la|lc|li|lk|ls|lt|lu|lv|ly|ma|mc|md|mg|mh|mk|ml|mm|mn|mo|mp|mq|mr|ms|mt|mu|mv|mw|mx|my|mz|na|nc|ne|nf|ng|ni|nl|no|nr|nu|nz|om|pa|pe|pf|ph|pk|pl|pn|pr|ps|pt|pw|py|qa|re|ro|rs|ru|rw|sa|sb|sc|sd|se|sg|sh|si|sj|sk|sl|sm|sn|so|sr|ss|st|su|sv|sx|sy|sz|tc|td|tf|tg|th|tj|tk|tl|tm|tn|to|tr|tt|tv|tw|tz|ua|ug|uk|us|uy|uz|va|vc|ve|vg|vi|vn|vu|wf|ws|ye|yt|yu|za|zm|zw)',
            re.IGNORECASE
        )
        
        urls = url_pattern.findall(text)
        
        for rule in rules:
            pattern = self._compile_pattern(rule.pattern, RuleType.DOMAIN)
            if not pattern:
                continue
            
            for url in urls:
                if pattern.search(url):
                    matched.append(MatchedRule(
                        rule_id=rule.id,
                        rule_type=RuleType.DOMAIN,
                        pattern=rule.pattern,
                        matched_content=url,
                        level=rule.level,
                        action=rule.action
                    ))
                    self.logger.info(
                        "domain_rule_matched",
                        rule_id=rule.id,
                        pattern=rule.pattern,
                        domain=url
                    )
        
        return matched
    
    async def _check_keyword_engine(
        self,
        text: str,
        group_id: int
    ) -> list[MatchedRule]:
        """Check against dedicated moderation sensitive keywords."""
        matched = []

        result = await self.db.execute(
            select(ModerationSensitiveKeyword).where(
                ModerationSensitiveKeyword.enabled == True,
                (ModerationSensitiveKeyword.group_id == group_id) | (ModerationSensitiveKeyword.group_id.is_(None)),
            )
        )

        for kw in result.scalars().all():
            if kw.text.lower() in text.lower():
                matched.append(
                    MatchedRule(
                        rule_id=kw.id,
                        rule_type=RuleType.KEYWORD,
                        pattern=kw.text,
                        matched_content=kw.text,
                        level=kw.level,
                        action=kw.action,
                    )
                )
        
        return matched
    
    async def _get_applicable_rules(
        self,
        group_id: int,
        rule_type: Optional[RuleType] = None
    ) -> list[ModerationRule]:
        """Get applicable rules for a group."""
        rules = list(self._global_rules)
        
        if group_id in self._rules_cache:
            rules.extend(self._rules_cache[group_id])
        
        if rule_type:
            rules = [r for r in rules if r.rule_type == rule_type]
        
        return [r for r in rules if r.enabled]
    
    async def _is_whitelisted(self, value: str, group_id: int) -> bool:
        """Check if value is in whitelist."""
        whitelist = await self.load_whitelist(group_id)
        return value.lower() in whitelist
    
    async def _is_user_whitelisted(self, user_id: int, group_id: int) -> bool:
        """Check if user is in whitelist."""
        whitelist = await self.load_whitelist(group_id)
        return str(user_id) in whitelist
    
    def _get_highest_severity(self, matched_rules: list[MatchedRule]) -> ViolationLevel:
        """Get the highest severity level from matched rules."""
        if not matched_rules:
            return ViolationLevel.LOW
        
        severity_order = {
            ViolationLevel.LOW: 0,
            ViolationLevel.MEDIUM: 1,
            ViolationLevel.HIGH: 2
        }
        
        return max(matched_rules, key=lambda r: severity_order.get(r.level, 0)).level
    
    def _get_recommended_action(
        self,
        matched_rules: list[MatchedRule],
        severity: ViolationLevel
    ) -> ViolationAction:
        """Get recommended action based on severity."""
        if severity == ViolationLevel.HIGH:
            return ViolationAction.BAN
        elif severity == ViolationLevel.MEDIUM:
            return ViolationAction.MUTE
        else:
            for rule in matched_rules:
                if rule.action != ViolationAction.WARN:
                    return rule.action
            return ViolationAction.WARN
    
    async def get_rule_by_id(self, rule_id: int) -> Optional[ModerationRule]:
        """Get a rule by ID."""
        result = await self.db.execute(
            select(ModerationRule).where(ModerationRule.id == rule_id)
        )
        return result.scalar_one_or_none()
    
    async def create_rule(
        self,
        rule_type: RuleType,
        pattern: str,
        level: ViolationLevel = ViolationLevel.MEDIUM,
        action: ViolationAction = ViolationAction.WARN,
        group_id: Optional[int] = None,
        enabled: bool = True
    ) -> ModerationRule:
        """Create a new moderation rule."""
        rule = ModerationRule(
            rule_type=rule_type,
            pattern=pattern,
            level=level,
            action=action,
            group_id=group_id,
            enabled=enabled
        )
        self.db.add(rule)
        await self.db.commit()
        await self.db.refresh(rule)
        
        await self.load_rules()
        
        self.logger.info(
            "rule_created",
            rule_id=rule.id,
            type=rule_type.value,
            pattern=pattern
        )
        return rule
    
    async def delete_rule(self, rule_id: int) -> bool:
        """Delete a rule."""
        rule = await self.get_rule_by_id(rule_id)
        if not rule:
            return False
        
        await self.db.delete(rule)
        await self.db.commit()
        
        await self.load_rules()
        
        self.logger.info("rule_deleted", rule_id=rule_id)
        return True
    
    async def reload(self) -> int:
        """Reload all rules and whitelist."""
        await self.load_rules()
        await self.load_whitelist()
        return len(self._global_rules) + sum(len(v) for v in self._rules_cache.values())
