from app.core.account.bot_credentials import (
    encrypt_guardian_bot_token,
    resolve_guardian_bot_token,
)


def test_guardian_bot_token_round_trip_and_legacy_compatibility():
    plaintext = "123456789:managed-bot-secret"

    encrypted = encrypt_guardian_bot_token(plaintext)

    assert encrypted is not None
    assert encrypted.startswith("vge1:")
    assert plaintext not in encrypted
    assert resolve_guardian_bot_token(encrypted) == plaintext
    assert resolve_guardian_bot_token(plaintext) == plaintext
    assert encrypt_guardian_bot_token(encrypted) == encrypted
