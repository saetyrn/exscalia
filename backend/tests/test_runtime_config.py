from app.overseer.runtime_config import api_key_matches


def test_google_accepts_current_and_legacy_key_formats():
    assert api_key_matches("google", "AQ.example-auth-key")
    assert api_key_matches("google", "AIza-example-standard-key")


def test_google_rejects_other_provider_key_formats():
    assert not api_key_matches("google", "sk-example")


def test_other_provider_prefixes_are_unchanged():
    assert api_key_matches("anthropic", "sk-ant-example")
    assert api_key_matches("openai", "sk-example")
