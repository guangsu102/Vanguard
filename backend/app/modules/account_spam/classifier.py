"""Conservative, offline classification of replies from the verified SpamBot.

These functions classify text only. The caller must verify the sender and correlate
the reply to its own check; a matching phrase from another chat is not evidence.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Literal

SpamStatus = Literal["clear", "restricted", "flagged", "unknown"]

_PUNCTUATION = str.maketrans(
    {
        "。": ".",
        "！": "!",
        "？": "?",
        "；": ";",
        "：": ":",
        "，": ",",
        "’": "'",
        "‘": "'",
        "“": '"',
        "”": '"',
        "–": "-",
        "—": "-",
    }
)
_MAX_REPLY_LENGTH = 32_000


def _clean_unicode(text: str | None) -> str:
    if not isinstance(text, str):
        return ""
    value = unicodedata.normalize("NFKC", text).translate(_PUNCTUATION)
    # Discard directional and zero-width formatting without dropping visible text.
    value = "".join(char for char in value if unicodedata.category(char) != "Cf")
    return re.sub(r"\s+", " ", value).strip()


def normalize_spambot_text(text: str | None) -> str:
    """Normalize Unicode, whitespace and common punctuation for phrase matching."""
    return _clean_unicode(text).casefold()


# Match assertions at the beginning of a sentence, never an arbitrary substring
# inside an appeal, quotation, question, or example. Intentionally omit generic
# reassurances such as "free as a bird" and historical/future account states.
_INTRO = (
    r"(?:(?:good news|unfortunately|sorry|therefore|at the moment|currently|"
    r"i'm afraid|as a result|好消息|很遗憾|很遺憾|很抱歉|遗憾的是|遺憾的是|"
    r"buenas noticias|lamentablemente|desafortunadamente|actualmente|"
    r"хорошие новости|к сожалению|в настоящее время)[,: -]*\s*)?"
)
_ACCOUNT_ZH = r"[你您]的\s*(?:telegram\s*)?(?:账号|帐号|账户|帐户|帳號|帳戶)\s*"
_NOW_ZH = r"(?:目前|当前|當前|现在|現在)?\s*"
_UNTIL = r"(?:\s+(?:until|forever|hasta|до)\b[^!?]*)?"

_CLEAR_PATTERNS = (
    r"no (?:limits|restrictions) are (?:currently )?applied to your account",
    r"your account is (?:currently )?(?:not|no longer) (?:limited|restricted)"
    r"(?: (?:at the moment|anymore|any more))?",
    r"your account is not currently (?:limited|restricted)",
    r"your account isn't (?:currently )?(?:limited|restricted)",
    r"your account (?:has no|does not have any) (?:limits|limitations|restrictions)"
    r"(?: (?:currently|at the moment))?",
    r"your account is (?:currently )?free (?:of|from) (?:any )?(?:limits|restrictions)",
    _ACCOUNT_ZH
    + _NOW_ZH
    + r"(?:没有任何限制|沒有任何限制|没有限制|沒有限制|未受到任何限制|没有受到任何限制|"
    r"沒有受到任何限制|并未受到任何限制|並未受到任何限制|未受到限制|未被限制|"
    r"没有被限制|沒有被限制|不受任何限制|已解除限制|已不再受限)",
    r"no hay (?:límites|restricciones) (?:actualmente )?aplicad[oa]s a tu cuenta"
    r"(?: en este momento)?",
    r"no se aplican (?:límites|restricciones) (?:actualmente )?a tu cuenta",
    r"(?:tu|su) cuenta (?:actualmente )?no (?:está (?:limitada|restringida)|"
    r"tiene (?:ninguna limitación|limitaciones|restricciones|límites))"
    r"(?: (?:actualmente|en este momento))?",
    r"на ваш аккаунт (?:(?:сейчас|в настоящее время|в настоящий момент) )?"
    r"не наложено (?:никаких )?ограничений",
    r"ваш аккаунт (?:сейчас )?не ограничен",
)
_RESTRICTED_PATTERNS = (
    r"your account (?:is|has been) (?:(?:now|currently|temporarily|permanently) )?"
    r"(?:limited|restricted)" + _UNTIL,
    # Official @SpamBot template. Keep this deliberately exact: generic
    # ``while ... limited`` wording is conditional and is not itself proof.
    r"while the account is limited,? you will not be able to send messages to people "
    r"who do not have your number in their phone contacts or add them to groups and channels",
    _ACCOUNT_ZH
    + _NOW_ZH
    + r"(?:已被限制|已经被限制|已經被限制|被限制|受到了限制|受到限制|已受限|受限|"
    r"暂时被限制|暫時被限制|永久受限)"
    r"(?:\s*[, ]?\s*(?:直到|至|到|限制至)[^!?]*)?",
    r"(?:tu|su) cuenta (?:está|ha sido) (?:(?:actualmente|temporalmente|permanentemente) )?"
    r"(?:limitada|restringida)" + _UNTIL,
    r"ваш аккаунт (?:(?:сейчас|временно|навсегда) )?ограничен" + _UNTIL,
    r"на ваш аккаунт (?:сейчас )?наложены (?:(?:временные|постоянные) )?ограничения",
)
# Soft anti-spam advisory about the phone number itself. It states neither a
# current account limit nor a clean bill, so it must stay its own outcome and
# must never mask an explicit clear/restricted assertion in the same reply.
# The sibling sentence "some actions can trigger a harsh response" is
# deliberately absent: it opens the restricted-account template and would
# downgrade a real restriction verdict.
_FLAGGED_PATTERNS = (r"some phone numbers may trigger a harsh response from our anti-spam systems",)
_CLEAR = tuple(re.compile(_INTRO + pattern + r"\s*", re.IGNORECASE) for pattern in _CLEAR_PATTERNS)
_RESTRICTED = tuple(
    re.compile(_INTRO + pattern + r"\s*", re.IGNORECASE) for pattern in _RESTRICTED_PATTERNS
)
_FLAGGED = tuple(
    re.compile(_INTRO + pattern + r"\s*", re.IGNORECASE) for pattern in _FLAGGED_PATTERNS
)
_NON_ASSERTION = re.compile(
    r"\b(?:if|unless|suppose|example|would|could|may|might|whether|si|ejemplo|если|пример)\b"
    r"|如果|假如|假设|假設|例如|示例|是否|可能|曾经|曾經",
    re.IGNORECASE,
)


def classify_spambot_reply(text: str | None) -> SpamStatus:
    """Return a current, explicit account state; uncertainty/conflicts stay unknown."""
    if not isinstance(text, str) or len(text) > _MAX_REPLY_LENGTH:
        return "unknown"
    normalized = normalize_spambot_text(text)
    found: set[SpamStatus] = set()
    # A quotation may span several sentences; apostrophes inside words do not.
    quotes = iter(re.finditer(r"\"|(?<!\w)'|'(?!\w)", normalized))
    next_quote = next(quotes, None)
    active_quote: str | None = None
    # Keep the terminator to reject questions instead of reading them as assertions.
    for match in re.finditer(r"([^.!?;]+)([.!?;]|$)", normalized):
        while next_quote is not None and next_quote.start() < match.start():
            mark = next_quote.group()
            if active_quote == mark:
                active_quote = None
            elif active_quote is None:
                active_quote = mark
            next_quote = next(quotes, None)
        sentence, terminator = match.groups()
        sentence = sentence.strip()
        if active_quote or not sentence or terminator == "?":
            continue
        # Spanish opening punctuation is formatting; quotation marks are retained.
        sentence = sentence.removeprefix("¡").strip()
        # The phone-number advisory is long, exact and contains "may" purely as
        # part of the official wording, so it is matched before the hypothetical
        # guard below rather than being discarded by it.
        if any(pattern.fullmatch(sentence) for pattern in _FLAGGED):
            found.add("flagged")
            continue
        if _NON_ASSERTION.search(sentence):
            continue
        if any(pattern.fullmatch(sentence) for pattern in _CLEAR):
            found.add("clear")
        if any(pattern.fullmatch(sentence) for pattern in _RESTRICTED):
            found.add("restricted")
    if len(found) == 1:
        return next(iter(found))
    # A phone-number advisory is context, not a verdict: an explicit
    # clear/restricted assertion elsewhere in the reply wins over it.
    found.discard("flagged")
    return next(iter(found)) if len(found) == 1 else "unknown"


_REDACTIONS = (
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b"), "[secret]"),
    (re.compile(r"\b(?:https?://|tg://|www\.|t\.me/)[^\s<>]+", re.IGNORECASE), "[link]"),
    (re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}"), "[email]"),
    (re.compile(r"(?<!\w)@[A-Za-z0-9_]{3,}"), "[username]"),
    (
        re.compile(
            r"\b(?:bearer\s+|(?:api[_ -]?key|token|password)[\x22\x27]?\s*[:=]\s*[\x22\x27]?)[^\s,;\x22\x27]+",
            re.IGNORECASE,
        ),
        "[secret]",
    ),
    (re.compile(r"(?<!\w)\+\d[\d ()-]{6,}\d\b|(?<!\w)\d{8,}(?!\w)"), "[number]"),
    (re.compile(r"(?<!\w)[A-Za-z0-9_-]{32,}(?!\w)"), "[secret]"),
)


def summarize_spambot_reply(text: str | None, max_length: int = 240) -> str:
    """Produce a bounded display summary, redacting before truncating any text."""
    limit = max(0, int(max_length))
    if not limit:
        return ""
    summary = _clean_unicode(text)
    for pattern, replacement in _REDACTIONS:
        summary = pattern.sub(replacement, summary)
    if len(summary) <= limit:
        return summary
    return summary[: max(0, limit - 1)].rstrip() + "…"
