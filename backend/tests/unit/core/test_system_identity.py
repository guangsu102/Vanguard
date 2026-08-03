from app.core.account.system_identity import bot_risk_identity


def test_bot_risk_identity_is_stable_and_system_scoped():
    first = bot_risk_identity("scheduler")
    second = bot_risk_identity("scheduler")
    other = bot_risk_identity("guardian")

    assert first.account_id == second.account_id
    assert first.account_id < 0
    assert first.account_id != other.account_id
    assert first.fingerprint_id == "system:scheduler"
