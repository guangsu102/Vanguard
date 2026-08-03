"""
Tests for Auto Reply Module
"""

from datetime import datetime

from app.modules.acquisition.auto_reply.templates import TemplateEngine, MessageTemplateStore
from app.modules.acquisition.auto_reply.scheduler import SpeakScheduler, SpeakTask, SpeakSchedule
from app.modules.acquisition.auto_reply.safety import has_ai_self_disclosure, sanitize_natural_group_reply
from app.modules.acquisition.models import MessageType


class TestMessageTemplateStore:
    """Tests for MessageTemplateStore."""

    def setup_method(self):
        """Set up test fixtures."""
        self.store = MessageTemplateStore()

    def test_get_random_interaction(self):
        """Test getting random interaction template."""
        template = self.store.get_random(MessageType.INTERACTION)

        assert isinstance(template, str)
        assert len(template) > 0

    def test_get_random_share(self):
        """Test getting random share template."""
        template = self.store.get_random(MessageType.SHARE)

        assert isinstance(template, str)
        assert len(template) > 0

    def test_get_random_guide(self):
        """Test getting random guide template."""
        template = self.store.get_random(MessageType.GUIDE)

        assert isinstance(template, str)
        assert "{{register_link}}" in template

    def test_get_random_qa(self):
        """Test getting random QA template."""
        template = self.store.get_random(MessageType.QA)

        assert isinstance(template, str)
        assert any(marker in template for marker in ("?", "？", "怎么", "问"))

    def test_get_all_by_type(self):
        """Test getting all templates of a type."""
        templates = self.store.get_all(MessageType.INTERACTION)

        assert isinstance(templates, list)
        assert len(templates) > 0

    def test_get_by_keyword_type(self):
        """Test getting templates by keyword type."""
        templates = self.store.get_by_keyword_type("demand")

        assert isinstance(templates, list)
        assert len(templates) > 0
        # Should contain guide templates
        assert any("{{register_link}}" in t for t in templates)

    def test_default_fallback(self):
        """Test default fallback message."""
        # Invalid type should use default
        template = self.store.get_random(MessageType.INTERACTION)
        assert template is not None


class TestTemplateEngine:
    """Tests for TemplateEngine."""

    def setup_method(self):
        """Set up test fixtures."""
        self.engine = TemplateEngine()

    def test_render_simple(self):
        """Test rendering a simple template."""
        class MockTemplate:
            content = "Hello {{user_name}}!"
            template_variables = "user_name"

        rendered = self.engine.render(MockTemplate(), user_name="John")

        assert rendered == "Hello John!"

    def test_render_with_link(self):
        """Test rendering template with register link."""
        class MockTemplate:
            content = "Click here: {{register_link}}"

        rendered = self.engine.render(MockTemplate(), register_link="https://xboard.com")

        assert rendered == "Click here: https://xboard.com"

    def test_render_all_variables(self):
        """Test rendering all supported variables."""
        class MockTemplate:
            content = "{{user_name}} in {{group_name}} via {{bot_name}}"

        rendered = self.engine.render(
            MockTemplate(),
            user_name="Alice",
            group_name="VPN Users",
            bot_name="XBoardBot",
        )

        assert "Alice" in rendered
        assert "VPN Users" in rendered
        assert "XBoardBot" in rendered

    def test_render_missing_variable(self):
        """Test rendering with missing variable uses default."""
        class MockTemplate:
            content = "Hello {{user_name}}!"

        rendered = self.engine.render(MockTemplate())

        assert "Hello" in rendered
        assert "朋友" in rendered  # Default value

    def test_render_string(self):
        """Test rendering a string template directly."""
        content = "Welcome {{user_name}} to {{group_name}}!"

        rendered = self.engine.render_string(
            content,
            user_name="Bob",
            group_name="Tech Group",
        )

        assert "Bob" in rendered
        assert "Tech Group" in rendered


class TestReplySafety:
    """Tests for group reply safety filters."""

    def test_detects_ai_self_disclosure(self):
        assert has_ai_self_disclosure("我是AI助手，可以帮你分析")
        assert has_ai_self_disclosure("As an AI language model, I can help")

    def test_sanitize_replaces_ai_self_disclosure(self):
        result = sanitize_natural_group_reply(
            "我是AI助手，可以帮你分析",
            {"blockAiSelfDisclosure": True, "replyMaxChars": 120},
            fallback="这个点可以再看看大家怎么说。",
        )

        assert result == "这个点可以再看看大家怎么说。"

    def test_sanitize_enforces_reply_length(self):
        result = sanitize_natural_group_reply(
            "这个问题挺实际的" * 20,
            {"blockAiSelfDisclosure": True, "replyMaxChars": 30},
        )

        assert len(result) <= 30


class TestSpeakScheduler:
    """Tests for SpeakScheduler."""

    def setup_method(self):
        """Set up test fixtures."""
        self.scheduler = SpeakScheduler()

    def test_create_schedule(self):
        """Test creating a speak schedule."""
        schedule = SpeakSchedule(
            name="test_schedule",
            tasks=[
                SpeakTask(group_id=123, message_type=MessageType.INTERACTION),
                SpeakTask(group_id=456, message_type=MessageType.SHARE),
            ],
        )

        self.scheduler.add_schedule(schedule)

        assert self.scheduler.get_schedule("test_schedule") is not None

    def test_remove_schedule(self):
        """Test removing a schedule."""
        schedule = SpeakSchedule(name="to_remove")
        self.scheduler.add_schedule(schedule)

        assert self.scheduler.remove_schedule("to_remove") is True
        assert self.scheduler.get_schedule("to_remove") is None

    def test_remove_nonexistent(self):
        """Test removing non-existent schedule."""
        assert self.scheduler.remove_schedule("nonexistent") is False

    def test_should_execute_task_enabled(self):
        """Test should_execute for enabled schedule."""
        schedule = SpeakSchedule(name="enabled", enabled=True)
        schedule.start_time = datetime.utcnow()
        schedule.end_time = datetime.utcnow()

        result = self.scheduler.should_execute_task(schedule)

        assert result is True

    def test_should_execute_task_disabled(self):
        """Test should_execute for disabled schedule."""
        schedule = SpeakSchedule(name="disabled", enabled=False)

        result = self.scheduler.should_execute_task(schedule)

        assert result is False

    def test_get_due_tasks(self):
        """Test getting due tasks."""
        schedule = SpeakSchedule(name="due", enabled=True, tasks=[
            SpeakTask(group_id=1, message_type=MessageType.INTERACTION),
            SpeakTask(group_id=2, message_type=MessageType.SHARE, priority=5),
            SpeakTask(group_id=3, message_type=MessageType.GUIDE, priority=1),
        ])
        self.scheduler.add_schedule(schedule)

        due_tasks = self.scheduler.get_due_tasks()

        assert len(due_tasks) == 3
        # Should be sorted by priority
        assert due_tasks[0].priority >= due_tasks[1].priority


class TestSpeakTask:
    """Tests for SpeakTask."""

    def test_create_task(self):
        """Test creating a speak task."""
        task = SpeakTask(
            group_id=123,
            message_type=MessageType.INTERACTION,
            priority=5,
        )

        assert task.group_id == 123
        assert task.message_type == MessageType.INTERACTION
        assert task.priority == 5
        assert task.scheduled_time is None

    def test_task_with_schedule(self):
        """Test task with scheduled time."""
        scheduled = datetime.utcnow()
        task = SpeakTask(
            group_id=456,
            message_type=MessageType.GUIDE,
            scheduled_time=scheduled,
        )

        assert task.scheduled_time == scheduled


class TestMessageType:
    """Tests for MessageType enum."""

    def test_all_types_exist(self):
        """Test all message types are defined."""
        assert MessageType.INTERACTION.value == "interaction"
        assert MessageType.SHARE.value == "share"
        assert MessageType.GUIDE.value == "guide"
        assert MessageType.QA.value == "qa"
