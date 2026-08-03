"""
Regex Guard Module

Provides ReDoS (Regular Expression Denial of Service) protection for
keyword compilation and execution.

Features:
- Static regex complexity analysis before compilation
- Runtime execution timeout via thread-pool isolation
- Rejection of known catastrophic patterns
"""

import asyncio
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

# Thread pool for executing regex with timeout
_regex_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="regex_guard")

# Maximum allowed regex length to prevent obfuscated attacks
MAX_REGEX_LENGTH = 500

# Catastrophic patterns that cause exponential backtracking
# These are patterns known to be exploited in ReDoS attacks
CATASTROPHIC_PATTERNS = [
    # Nested quantifiers: (a+)+, (a*)*, (a+)*, (a*)+
    re.compile(r'\([^)]*\+\)\+'),
    re.compile(r'\([^)]*\*\)\*'),
    re.compile(r'\([^)]*\+\)\*'),
    re.compile(r'\([^)]*\*\)\+'),
    # Optional inside repetition: (a?)*, (a?)+, (a?){n,}
    re.compile(r'\([^)]*\?\)\*'),
    re.compile(r'\([^)]*\?\)\+'),
    re.compile(r'\([^)]*\?\)\{'),
    # Nested groups with quantifiers: ((a+))+
    re.compile(r'\((?:\?[^:]+:)?\([^)]*\+\)\)\+'),
    re.compile(r'\((?:\?[^:]+:)?\([^)]*\*\)\)\*'),
    # Alternation + nested quantifier: (a|aa)+, (a|b)* followed by +
    re.compile(r'\([^)]*\|[^)]*\)[\*\+]'),
    # Excessive backreferences or lookarounds (less common but risky)
    re.compile(r'\(\?[=!<]'),  # lookahead/lookbehind
]


def _count_quantifier_depth(pattern: str) -> int:
    """Count nested quantifier depth as a proxy for risk."""
    depth = 0
    max_depth = 0
    in_group = False
    for i, ch in enumerate(pattern):
        if ch == '(':
            in_group = True
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth < 0:
                depth = 0
            if depth == 0:
                in_group = False
        # Check if next char is a quantifier applied to a group
        if depth > 0 and i + 1 < len(pattern):
            next_ch = pattern[i + 1]
            if next_ch in '*+?{':
                max_depth = max(max_depth, depth)
    return max_depth


def analyze_regex_complexity(pattern: str) -> tuple[bool, str, int]:
    """
    Analyze regex for ReDoS vulnerability.

    Returns:
        (is_safe, reason, risk_score)
    """
    if not pattern:
        return False, "Empty pattern", 0

    if len(pattern) > MAX_REGEX_LENGTH:
        return False, f"Pattern too long ({len(pattern)} > {MAX_REGEX_LENGTH})", 10

    risk_score = 0

    # Check for known catastrophic patterns
    for cata in CATASTROPHIC_PATTERNS:
        if cata.search(pattern):
            risk_score += 5
            return False, f"Pattern contains catastrophic ReDoS construct", risk_score

    # Check nested quantifier depth
    depth = _count_quantifier_depth(pattern)
    if depth >= 3:
        risk_score += depth
        return False, f"Excessive nested quantifier depth ({depth})", risk_score

    # Count individual risk factors
    # Alternation + quantifier combinations
    alt_groups = pattern.count('|')
    quantifiers = pattern.count('*') + pattern.count('+') + pattern.count('?')
    if alt_groups > 0 and quantifiers > 3:
        risk_score += alt_groups + quantifiers

    # Groups with quantifiers
    group_count = pattern.count('(')
    if group_count > 5 and quantifiers > 5:
        risk_score += (group_count + quantifiers) // 2

    # Limit acceptable risk
    if risk_score > 8:
        return False, f"Regex risk score too high ({risk_score})", risk_score

    return True, "safe", risk_score


def safe_compile(pattern: str, flags: int = 0) -> Optional[re.Pattern]:
    """
    Compile regex with ReDoS safety check.

    Args:
        pattern: Regex pattern string
        flags: Regex flags

    Returns:
        Compiled pattern, or raises ValueError if unsafe
    """
    is_safe, reason, score = analyze_regex_complexity(pattern)
    if not is_safe:
        raise ValueError(f"Unsafe regex rejected (score={score}): {reason}")
    return re.compile(pattern, flags)


async def safe_search(
    compiled_pattern: re.Pattern,
    text: str,
    timeout: float = 0.5,
) -> Optional[re.Match]:
    """
    Execute regex search with timeout protection.

    Uses a thread pool + asyncio timeout to prevent blocking
    the event loop on catastrophic backtracking.

    Args:
        compiled_pattern: Pre-compiled regex pattern
        text: Text to search
        timeout: Maximum seconds to wait for match

    Returns:
        Match object or None

    Raises:
        asyncio.TimeoutError: If regex execution exceeds timeout
    """
    if not text:
        return None

    # Truncate extremely long text to prevent abuse
    max_text_len = 10000
    if len(text) > max_text_len:
        text = text[:max_text_len]

    loop = asyncio.get_running_loop()

    def _search() -> Optional[re.Match]:
        return compiled_pattern.search(text)

    try:
        return await asyncio.wait_for(
            loop.run_in_executor(_regex_executor, _search),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        # Log and treat as no-match to avoid leaking ReDoS
        raise RegexTimeoutError(
            f"Regex search timed out after {timeout}s"
        ) from None


class RegexTimeoutError(TimeoutError):
    """Raised when regex execution exceeds safe timeout."""
    pass
