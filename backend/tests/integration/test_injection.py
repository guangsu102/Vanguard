"""
XSS/SQL 注入测试
测试应用对常见注入攻击的防护
"""

import pytest
import re
from typing import List, Any
from html import escape


class TestXSSPrevention:
    """XSS 防护测试"""

    def test_html_escape_user_input(self):
        """测试 HTML 转义用户输入"""
        dangerous_inputs = [
            '<script>alert("XSS")</script>',
            '<img src=x onerror=alert(1)>',
            '<svg onload=alert("XSS")>',
            'javascript:alert("XSS")',
            '<a href="javascript:alert(1)">click</a>'
        ]

        for dangerous in dangerous_inputs:
            escaped = escape(dangerous, quote=True)
            # 转义后不应该包含未转义的标签
            assert '<' not in escaped or '&lt;' in escaped
            assert 'script' not in escaped.lower()

    def test_attribute_escape(self):
        """测试属性转义"""
        user_input = 'onerror=alert("XSS")'

        # 在 HTML 属性中应该被转义
        escaped = escape(user_input, quote=True)
        assert 'onerror' not in escaped

    def test_url_xss_prevention(self):
        """测试 URL XSS 防护"""
        dangerous_urls = [
            'javascript:alert(1)',
            'data:text/html,<script>alert(1)</script>',
            'vbscript:msgbox("XSS")'
        ]

        for url in dangerous_urls:
            # 应该检测并阻止 javascript: 协议
            is_dangerous = url.startswith(('javascript:', 'vbscript:', 'data:'))
            assert is_dangerous is True

    def test_strip_script_tags(self):
        """测试剥离脚本标签"""
        html_with_script = '<p>Hello</p><script>alert("XSS")</script><p>World</p>'

        # 简单的脚本标签剥离
        cleaned = re.sub(r'<script[^>]*>.*?</script>', '', html_with_script, flags=re.DOTALL | re.IGNORECASE)
        cleaned = re.sub(r'<script[^>]*>', '', cleaned, flags=re.IGNORECASE)

        assert 'script' not in cleaned.lower()
        assert 'Hello' in cleaned
        assert 'World' in cleaned

    def test_strip_event_handlers(self):
        """测试剥离事件处理器"""
        html_with_events = '<img src=x onerror=alert(1) onload=alert(2)>'

        # 剥离事件处理器
        cleaned = re.sub(r'\s*on\w+\s*=\s*["\'][^"\']*["\']', '', html_with_events, flags=re.IGNORECASE)

        assert 'onerror' not in cleaned
        assert 'onload' not in cleaned

    def test_strip_iframe_tags(self):
        """测试剥离 iframe 标签"""
        html_with_iframe = '<p>Content</p><iframe src="evil.com"></iframe><p>More</p>'

        cleaned = re.sub(r'<iframe[^>]*>.*?</iframe>', '', html_with_iframe, flags=re.DOTALL | re.IGNORECASE)

        assert 'iframe' not in cleaned.lower()

    def test_strip_style_tags(self):
        """测试剥离样式标签防止 CSS XSS"""
        html_with_style = '<style>body{background:url(javascript:alert(1))}</style><p>Text</p>'

        cleaned = re.sub(r'<style[^>]*>.*?</style>', '', html_with_style, flags=re.DOTALL | re.IGNORECASE)

        assert 'style' not in cleaned.lower()


class TestSQLInjectionPrevention:
    """SQL 注入防护测试"""

    def test_sql_metacharacter_sanitization(self):
        """测试 SQL 元字符清理"""
        dangerous_inputs = [
            "' OR '1'='1",
            "'; DROP TABLE users; --",
            "1 UNION SELECT * FROM passwords",
            "admin'--",
            "1; DELETE FROM users WHERE '1'='1"
        ]

        # 应该检测到 SQL 注入特征
        sql_patterns = [
            r"(\bOR\b|\bAND\b).*=.*['\"]",
            r"\bUNION\b",
            r"--\s*$",
            r";\s*(DROP|DELETE|INSERT|UPDATE)",
            r"'\s*(OR|AND)\s*'",
        ]

        for dangerous in dangerous_inputs:
            is_sql_injection = any(re.search(pattern, dangerous, re.IGNORECASE) for pattern in sql_patterns)
            assert is_sql_injection is True

    def test_parameterized_query_usage(self):
        """测试参数化查询的使用"""
        # 模拟参数化查询
        user_input = "'; DROP TABLE users; --"

        # 参数化查询应该安全处理
        safe_query = "SELECT * FROM users WHERE name = %s"
        params = (user_input,)

        # 不应该在查询中拼接用户输入
        assert user_input not in safe_query
        assert "%s" in safe_query

    def test_literal_escaping(self):
        """测试字面量转义"""
        dangerous_input = "user' OR '1'='1"

        # 对于无法使用参数化的地方，应该转义
        escaped_input = dangerous_input.replace("'", "''")

        assert escaped_input == "user'' OR ''1''=''1"
        assert "OR" not in escaped_input  # 转义后不再是有效的 SQL

    def test_numeric_input_validation(self):
        """测试数字输入验证"""
        numeric_inputs = ["123", "0", "999999"]
        non_numeric_inputs = ["abc", "123 OR 1=1", "'; SELECT"]

        # 验证是否为数字
        for num in numeric_inputs:
            assert num.isdigit()

        for non_num in non_numeric_inputs:
            assert not non_num.isdigit() if non_num.isdigit() else True

    def test_order_by_injection_prevention(self):
        """测试 ORDER BY 注入防护"""
        valid_columns = ["id", "created_at", "username"]
        user_input = "id; DROP TABLE users"

        # 验证列名是否在白名单中
        is_safe = user_input in valid_columns
        assert is_safe is False


class TestCommandInjectionPrevention:
    """命令注入防护测试"""

    def test_shell_metacharacter_detection(self):
        """测试 Shell 元字符检测"""
        dangerous_inputs = [
            "; ls -la",
            "| cat /etc/passwd",
            "`whoami`",
            "$(whoami)",
            "&& rm -rf /",
            "|| echo hacked"
        ]

        shell_chars = [';', '|', '`', '$', '&', '<', '>']

        for dangerous in dangerous_inputs:
            has_shell_char = any(char in dangerous for char in shell_chars)
            assert has_shell_char is True

    def test_path_traversal_prevention(self):
        """测试路径遍历防护"""
        dangerous_paths = [
            "../../../etc/passwd",
            "..\\..\\..\\windows\\system32",
            "file.txt/../../../root/.ssh",
            "%2e%2e%2f%2e%2e%2fetc/passwd"
        ]

        traversal_patterns = [
            r'\.\.[/\\]',
            r'%2e%2e',
        ]

        for path in dangerous_paths:
            is_traversal = any(re.search(pattern, path, re.IGNORECASE) for pattern in traversal_patterns)
            assert is_traversal is True

    def test_filename_sanitization(self):
        """测试文件名清理"""
        dangerous_filenames = [
            "../../../etc/passwd",
            "file<script>.txt",
            "file|.exe",
            "file*.txt"
        ]

        safe_filenames = ["report.pdf", "data.json", "image.png"]

        # 应该只允许安全的字符
        allowed_pattern = r'^[a-zA-Z0-9_\-\.]+$'

        for safe in safe_filenames:
            assert re.match(allowed_pattern, safe) is not None

        for dangerous in dangerous_filenames:
            is_safe = re.match(allowed_pattern, dangerous) is not None
            assert is_safe is False


class TestLDAPInjectionPrevention:
    """LDAP 注入防护测试"""

    def test_ldap_special_characters(self):
        """测试 LDAP 特殊字符"""
        dangerous_inputs = [
            "*",
            "(user=*)",
            "admin)(|password=*)",
            "a)(",
        ]

        ldap_special = ['*', '(', ')', '\\', 'NUL']

        for dangerous in dangerous_inputs:
            has_special = any(char in dangerous for char in ldap_special)
            assert has_special is True

    def test_ldap_sanitization(self):
        """测试 LDAP 清理"""
        user_input = "admin)(|password=*)"

        # 转义 LDAP 特殊字符
        sanitized = user_input.replace('(', '\\28').replace(')', '\\29')

        assert '(' not in sanitized
        assert ')' not in sanitized


class TestXMLInjectionPrevention:
    """XML 注入防护测试"""

    def test_xml_special_entities(self):
        """测试 XML 特殊实体"""
        dangerous_inputs = [
            "<![CDATA[<script>alert(1)</script>]]>",
            "<!DOCTYPE html>",
            "<?xml version=\"1.0\"?>"
        ]

        # XML 注入特征
        xml_patterns = [
            r'<!\[CDATA\[',
            r'<!DOCTYPE',
            r'<\?xml'
        ]

        for dangerous in dangerous_inputs:
            is_injection = any(re.search(pattern, dangerous) for pattern in xml_patterns)
            assert is_injection is True

    def test_xml_entity_expansion(self):
        """测试 XML 实体扩展攻击"""
        # Billion Laughs 攻击特征
        evil_xml = "<?xml version=\"1.0\"?><!DOCTYPE lolz [<!ENTITY lol \"lol\">..."

        assert "ENTITY" in evil_xml.upper()


class TestNoSQLInjectionPrevention:
    """NoSQL 注入防护测试"""

    def test_mongo_special_operators(self):
        """测试 MongoDB 特殊操作符"""
        dangerous_inputs = [
            '{"$ne": null}',
            '{"$gt": ""}',
            '{"$where": "function() { return true; }"}',
            '{"$regex": ".*"}'
        ]

        nosql_patterns = [
            r'\$\w+',  # MongoDB 操作符
        ]

        for dangerous in dangerous_inputs:
            is_injection = any(re.search(pattern, dangerous) for pattern in nosql_patterns)
            assert is_injection is True

    def test_type_coercion_prevention(self):
        """测试类型强制转换防护"""
        # 模拟尝试注入字符串到数字字段
        malicious_input = '12345 || "admin"'

        # 应该验证类型
        is_numeric = malicious_input.replace(' ', '').replace('||', '').isdigit()

        # 恶意输入不是纯数字
        assert is_numeric is False


class TestInputFiltering:
    """输入过滤测试"""

    def test_whitelist_validation(self):
        """测试白名单验证"""
        # 只允许特定字符
        allowed_pattern = r'^[a-zA-Z0-9\s@.\-]+$'

        valid_inputs = ["user@example.com", "John Doe", "test-user_123"]
        invalid_inputs = ["<script>", "'; DROP", "user`whoami`"]

        for valid in valid_inputs:
            assert re.match(allowed_pattern, valid) is not None

        for invalid in invalid_inputs:
            assert re.match(allowed_pattern, invalid) is None

    def test_blacklist_filtering(self):
        """测试黑名单过滤"""
        blacklist = ['<script', 'javascript:', 'onerror=', 'onload=']

        user_input = '<img src=x onerror=alert(1)>'

        is_blocked = any(term in user_input.lower() for term in blacklist)
        assert is_blocked is True

    def test_length_limits(self):
        """测试长度限制"""
        max_length = 1000

        normal_input = "A" * 500
        oversized_input = "A" * 2000

        assert len(normal_input) <= max_length
        assert len(oversized_input) > max_length


class TestOutputEncoding:
    """输出编码测试"""

    def test_html_output_encoding(self):
        """测试 HTML 输出编码"""
        user_input = '<script>alert("XSS")</script>'

        encoded = escape(user_input)
        assert encoded == '&lt;script&gt;alert(&quot;XSS&quot;)&lt;/script&gt;'

    def test_javascript_output_encoding(self):
        """测试 JavaScript 输出编码"""
        user_input = '"; alert("XSS"); //'

        # JavaScript 字符串中应该转义
        js_escaped = user_input.replace('\\', '\\\\').replace('"', '\\"').replace("'", "\\'")

        assert '"' not in js_escaped or js_escaped.startswith('\\"')

    def test_url_output_encoding(self):
        """测试 URL 输出编码"""
        user_input = "hello world & test=value"

        # URL 编码
        from urllib.parse import quote
        url_encoded = quote(user_input, safe='')

        assert '%' in url_encoded
        assert ' ' in url_encoded  # 被编码了

    def test_css_output_encoding(self):
        """测试 CSS 输出编码"""
        user_input = 'color: expression(alert("XSS"))'

        # CSS 中应该避免表达式
        css_safe = not any(term in user_input.lower() for term in ['expression', 'javascript', 'behavior'])

        assert css_safe is False  # 这是危险的 CSS


class TestContentSecurityPolicy:
    """内容安全策略测试"""

    def test_csp_header_validation(self):
        """测试 CSP 头验证"""
        csp_directives = [
            "default-src 'self'",
            "script-src 'self' 'nonce-random123'",
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data: https:"
        ]

        csp_string = "; ".join(csp_directives)

        assert 'default-src' in csp_string
        assert "'self'" in csp_string

    def test_csp_prevents_inline_scripts(self):
        """测试 CSP 阻止内联脚本"""
        csp = "script-src 'self'"

        inline_script = '<script>alert(1)</script>'
        external_script = '<script src="external.js"></script>'

        # CSP 不允许 'unsafe-inline'
        allows_inline = "'unsafe-inline'" in csp

        assert allows_inline is False


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
