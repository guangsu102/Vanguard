"""Shared content-safety primitives for owned-group messages and templates."""

from __future__ import annotations

import re
import unicodedata

_URL_TRAILING_PUNCTUATION = ".,，。!?！？;；:：、"
_URL_LIKE_RE = re.compile(
    r"(?<![A-Za-z0-9_@])(?:"
    r"(?:https?|tg|telegram)://[^\s<>()\[\]{}\"']+"
    r"|(?:www\.)[^\s<>()\[\]{}\"']+"
    r"|(?:[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?\.)+"
    r"(?:[a-z]{2,24}|xn--[a-z0-9-]{2,59})"
    r"(?::\d{1,5})?(?:[/?#][^\s<>()\[\]{}\"']*)?"
    r")",
    re.IGNORECASE,
)
_UNICODE_DOMAIN_RE = re.compile(
    r"(?<![\w@])(?P<host>[\w\-]+(?:\.[\w\-]+)+)"
    r"(?P<suffix>:\d{1,5}|[/?#][^\s<>()\[\]{}\"']*)?",
    re.UNICODE,
)


def _unicode_domain_tokens(text: str) -> list[tuple[int, int, str]]:
    matches: list[tuple[int, int, str]] = []
    for match in _UNICODE_DOMAIN_RE.finditer(text):
        token = match.group(0).rstrip(_URL_TRAILING_PUNCTUATION)
        host = match.group("host")
        if not token or not any(ord(char) > 127 for char in host):
            continue
        labels = host.split(".")
        if len(labels) < 2:
            continue
        try:
            encoded = [label.encode("idna").decode("ascii") for label in labels]
        except UnicodeError:
            continue
        if any(
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or "_" in label
            for label in encoded
        ):
            continue
        tld = labels[-1]
        if len(tld) < 2 or not any(unicodedata.category(char).startswith("L") for char in tld):
            continue
        matches.append((match.start(), match.start() + len(token), token))
    return matches


def find_url_like_tokens(text: str) -> tuple[str, ...]:
    """Return URL-like tokens, including Telegram links and bare domains."""

    normalized = unicodedata.normalize("NFKC", str(text or ""))
    matches = [
        (
            match.start(),
            match.start()
            + len(match.group(0).rstrip(_URL_TRAILING_PUNCTUATION)),
            match.group(0).rstrip(_URL_TRAILING_PUNCTUATION),
        )
        for match in _URL_LIKE_RE.finditer(normalized)
    ]
    matches.extend(_unicode_domain_tokens(normalized))
    matches.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    tokens: list[str] = []
    covered_until = -1
    for start, end, token in matches:
        if start < covered_until:
            continue
        tokens.append(token)
        covered_until = end
    return tuple(tokens)


def contains_url_like(text: str) -> bool:
    return bool(find_url_like_tokens(text))
