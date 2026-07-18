import pytest

import llm_router


CFG = {
    "providers": {
        "deepseek": {"kind": "openai", "key_env": "DEEPSEEK_API_KEY", "base_url": "https://ds"},
        "gemini": {"kind": "gemini", "key_env": "GEMINI_API_KEY", "base_url": "https://gm"},
        "anthropic": {"kind": "anthropic", "key_env": "ANTHROPIC_API_KEY", "base_url": "https://an"},
    },
    "roles": {
        "triage": [
            {"provider": "deepseek", "model": "deepseek-chat"},
            {"provider": "gemini", "model": "gemini-2.0-flash"},
            {"provider": "anthropic", "model": "claude-haiku-4-5"},
        ],
    },
    "defaults": {"max_tokens": 100, "timeout_seconds": 5},
}


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    # All keys present by default; individual tests remove some.
    monkeypatch.setenv("DEEPSEEK_API_KEY", "x")
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")


def _patch_calls(monkeypatch, behavior):
    """behavior: {kind: callable(...)->str or raises}."""
    def make(kind):
        def _call(base_url, key, model, system, user, max_tokens, timeout):
            return behavior[kind](model)
        return _call
    monkeypatch.setattr(llm_router, "_PROVIDER_CALLS", {k: make(k) for k in behavior})


def test_primary_wins_when_it_succeeds(monkeypatch):
    _patch_calls(monkeypatch, {
        "openai": lambda m: f"deepseek says hi ({m})",
        "gemini": lambda m: "should not be used",
        "anthropic": lambda m: "should not be used",
    })
    res = llm_router.complete("triage", system="s", user="u", cfg=CFG)
    assert res.provider == "deepseek"
    assert "deepseek" in res.text
    assert res.attempts == []


def test_falls_through_on_error_to_next_provider(monkeypatch):
    def boom(m):
        raise RuntimeError("503 upstream")
    _patch_calls(monkeypatch, {
        "openai": boom,                       # deepseek fails
        "gemini": lambda m: "gemini backup",  # gemini succeeds
        "anthropic": lambda m: "claude last",
    })
    res = llm_router.complete("triage", system="s", user="u", cfg=CFG)
    assert res.provider == "gemini"
    assert res.text == "gemini backup"
    assert res.attempts[0][0] == "deepseek"
    assert "503" in res.attempts[0][2]


def test_missing_key_is_skipped_not_fatal(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    _patch_calls(monkeypatch, {
        "openai": lambda m: "should not run, no key",
        "gemini": lambda m: "gemini backup",
        "anthropic": lambda m: "claude last",
    })
    res = llm_router.complete("triage", system="s", user="u", cfg=CFG)
    assert res.provider == "gemini"
    assert res.attempts[0] == ("deepseek", "deepseek-chat", "missing DEEPSEEK_API_KEY")


def test_all_fail_raises_with_reasons(monkeypatch):
    def boom(m):
        raise RuntimeError("down")
    _patch_calls(monkeypatch, {"openai": boom, "gemini": boom, "anthropic": boom})
    with pytest.raises(llm_router.LLMRouterError) as exc:
        llm_router.complete("triage", system="s", user="u", cfg=CFG)
    msg = str(exc.value)
    assert "deepseek" in msg and "gemini" in msg and "anthropic" in msg


def test_empty_response_falls_through(monkeypatch):
    _patch_calls(monkeypatch, {
        "openai": lambda m: "",                # empty -> skip
        "gemini": lambda m: "real answer",
        "anthropic": lambda m: "x",
    })
    res = llm_router.complete("triage", system="s", user="u", cfg=CFG)
    assert res.provider == "gemini"
    assert res.attempts[0][2] == "empty response"


def test_unknown_role_raises():
    with pytest.raises(llm_router.LLMRouterError):
        llm_router.resolve_chain(CFG, "nope")


def test_complete_json_strips_fence(monkeypatch):
    _patch_calls(monkeypatch, {"openai": lambda m: '```json\n{"ok": true}\n```'})
    data, res = llm_router.complete_json("triage", system="s", user="u", cfg=CFG)
    assert data == {"ok": True}
    assert res.provider == "deepseek"
