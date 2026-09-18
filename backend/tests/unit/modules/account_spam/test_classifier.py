"""Pure-text SpamBot classification: only explicit current assertions count."""

import pytest

from app.modules.account_spam.classifier import (
    classify_spambot_reply,
    normalize_spambot_text,
    summarize_spambot_reply,
)


@pytest.mark.parametrize(
    "reply",
    [
        "Good news, no limits are currently applied to your account. You’re free as a bird!",
        "No restrictions are applied to your account.",
        "Your account is currently not limited.",
        "Your account is no longer restricted.",
        "Your account isn’t limited.",
        "Your account is not currently restricted.",
        "Your account has no restrictions at the moment.",
        "好消息，您的账号目前没有任何限制。",
        "您的账户已解除限制。",
        "您的账号目前并未受到任何限制。",
        "好消息，您的帳號目前沒有任何限制！",
        "您的帳戶沒有受到任何限制。",
        "Buenas noticias, no hay límites aplicados a tu cuenta en este momento. ¡Eres libre como un pájaro!",
        "Tu cuenta no está restringida.",
        "Хорошие новости, на Ваш аккаунт в настоящий момент не наложено никаких ограничений.",
        "Ваш аккаунт сейчас не ограничен.",
        "Ｇｏｏｄ　ｎｅｗｓ， no limits are currently applied to your account！",
        "Your\u200b account\u00a0is\ncurrently not limited.",
    ],
)
def test_explicit_current_clear_reply(reply):
    assert classify_spambot_reply(reply) == "clear"


@pytest.mark.parametrize(
    "reply",
    [
        "Unfortunately, your account is now limited until 13 Sep 2026, 09:00 UTC.",
        "Your account has been restricted.",
        "Your account is temporarily limited.",
        "Your account is limited forever.",
        "Your account is limited. If you think this is a mistake, you can submit an appeal.",
        (
            "While the account is limited, you will not be able to send messages to "
            "people who do not have your number in their phone contacts or add them "
            "to groups and channels."
        ),
        "很遗憾，您的账号已被限制，直到 2026-09-14。",
        "您的账户目前受到限制。",
        "很遺憾，您的帳號已被限制，直到 2026-09-14。",
        "您的帳戶目前受限。",
        "Lamentablemente, tu cuenta está limitada hasta 14 Sep 2026.",
        "Su cuenta ha sido restringida.",
        "К сожалению, Ваш аккаунт ограничен до 14 сентября 2026.",
        "На ваш аккаунт наложены временные ограничения.",
    ],
)
def test_explicit_current_restriction_reply(reply):
    assert classify_spambot_reply(reply) == "restricted"


@pytest.mark.parametrize(
    "reply",
    [
        (
            "Unfortunately, some phone numbers may trigger a harsh response from our "
            "anti-spam systems. If you think this is the case with you, you can submit "
            "a complaint to our moderators or subscribe to Telegram Premium to get "
            "less strict limits."
        ),
        "Some phone numbers may trigger a harsh response from our anti-spam systems.",
        (
            "At the moment, some phone numbers may trigger a harsh response from our "
            "anti-spam systems."
        ),
    ],
)
def test_phone_number_advisory_is_flagged_not_a_failed_check(reply):
    assert classify_spambot_reply(reply) == "flagged"


@pytest.mark.parametrize(
    "reply",
    [
        (
            "Unfortunately, some phone numbers may trigger a harsh response from our "
            "anti-spam systems. Your account is limited until 13 Sep 2026, 09:00 UTC."
        ),
        (
            "No limits are currently applied to your account. Some phone numbers may "
            "trigger a harsh response from our anti-spam systems."
        ),
    ],
)
def test_explicit_verdict_wins_over_phone_number_advisory(reply):
    assert classify_spambot_reply(reply) in {"clear", "restricted"}


def test_restricted_template_with_actions_preamble_stays_restricted():
    reply = (
        "Hello James! I'm very sorry that you had to contact me. Unfortunately, some "
        "actions can trigger a harsh response from our anti-spam systems. If you think "
        "your account was limited by mistake, you can submit a complaint to our "
        "moderators. While the account is limited, you will not be able to send messages "
        "to people who do not have your number in their phone contacts or add them to "
        "groups and channels."
    )
    assert classify_spambot_reply(reply) == "restricted"


@pytest.mark.parametrize(
    "reply",
    [
        None,
        "",
        "You are free as a bird!",
        "Hello, I'm SpamBot. I can help you find out whether your account was limited.",
        "If your account is limited, you can submit an appeal.",
        "Your account may be limited.",
        (
            "If some phone numbers may trigger a harsh response from our anti-spam "
            "systems, contact me."
        ),
        "Your account was limited until yesterday.",
        "Your account will not be restricted after review.",
        "Your account is limited?",
        '"Your account is limited."',
        'Here is an example: "Hello. Your account is limited. Send an appeal."',
        "The example says 'Hello. Your account is limited. Submit an appeal.'",
        "Example: Your account is limited.",
        "Please send an appeal to explain why your account should not be limited.",
        "You have already submitted a complaint recently. Please wait for a reply.",
        "If you think your account has been limited by mistake, please contact us.",
        "Your account is limited if you send unsolicited messages.",
        "While the account is limited, you can submit an appeal.",
        "你的账号是否已被限制？",
        "如果您的账号已被限制，可以提交申诉。",
        "如果您的帳號已被限制，可以提交申訴。",
        "Si tu cuenta está limitada, puedes enviar una apelación.",
        "Если ваш аккаунт ограничен, отправьте жалобу.",
        "Your account is not limited. Your account is now limited.",
        "No limits are currently applied to your account. Ваш аккаунт ограничен.",
        "x" * 32_001,
    ],
)
def test_ambiguous_hypothetical_appeal_quoted_or_conflicting_reply_is_unknown(reply):
    assert classify_spambot_reply(reply) == "unknown"


def test_appeal_instructions_do_not_override_an_independent_clear_assertion():
    reply = (
        "Good news, no limits are currently applied to your account. If this changes, contact me."
    )
    assert classify_spambot_reply(reply) == "clear"


def test_normalization_preserves_negation_and_quotes():
    assert (
        normalize_spambot_text("“Your\u200b  account　is NOT limited。”")
        == '"your account is not limited."'
    )


def test_summary_redacts_before_truncating_and_preserves_useful_expiry_date():
    reply = (
        "Until 2026-09-14, 09:00 UTC. @private_person user@example.com "
        "+86 138 1234 5678 https://example.com/?api_key=private "
        "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefgh token=very-private"
    )
    summary = summarize_spambot_reply(reply, max_length=500)
    for private in (
        "private_person",
        "user@example",
        "138",
        "example.com",
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
        "very-private",
    ):
        assert private not in summary
    assert "2026-09-14" in summary
    assert "[username]" in summary
    assert "[email]" in summary
    assert "[secret]" in summary
    assert len(summarize_spambot_reply(reply, max_length=30)) <= 30


@pytest.mark.parametrize("limit", [0, 1, 2, 20, 240])
def test_summary_respects_length_and_handles_missing_text(limit):
    assert summarize_spambot_reply(None, limit) == ""
    assert len(summarize_spambot_reply("A readable message " * 30, limit)) <= limit


def test_summary_redacts_quoted_credential_values():
    summary = summarize_spambot_reply('{"password":"hunter2", "token": "short-private"}')
    assert "hunter2" not in summary
    assert "short-private" not in summary
