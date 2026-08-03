from app.core.account.session_crypto import SESSION_CIPHER_PREFIX, SessionCryptoService


def test_session_crypto_encrypts_and_decrypts_string_session():
    service = SessionCryptoService("unit-test-session-key")

    encrypted = service.encrypt("telegram-string-session")

    assert encrypted is not None
    assert encrypted.startswith(SESSION_CIPHER_PREFIX)
    assert "telegram-string-session" not in encrypted
    assert service.decrypt(encrypted) == "telegram-string-session"


def test_session_crypto_keeps_legacy_plaintext_readable():
    service = SessionCryptoService("unit-test-session-key")

    assert service.decrypt("legacy-session") == "legacy-session"
    encrypted = service.encrypt("session")
    assert service.encrypt(encrypted) == encrypted

