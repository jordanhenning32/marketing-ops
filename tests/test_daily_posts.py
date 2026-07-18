import json
import sys
from datetime import date
from types import ModuleType, SimpleNamespace

import daily_posts


def test_generate_daily_posts_uses_optional_post_guidance(tmp_path, monkeypatch):
    captured = {}

    def fake_complete(_task, *, system, user, max_tokens):
        captured["system"] = system
        captured["user"] = user
        captured["max_tokens"] = max_tokens
        return SimpleNamespace(text="Hyped-but-useful post.", provider="fake", model="fake-model")

    fake_llm = ModuleType("llm_router")
    fake_llm.complete = fake_complete
    monkeypatch.setitem(sys.modules, "llm_router", fake_llm)
    monkeypatch.setattr(daily_posts, "POSTS_DIR", tmp_path / "daily-posts")
    monkeypatch.setattr(daily_posts, "GUIDANCE_PATH", tmp_path / "editor-guidance.json")
    daily_posts.GUIDANCE_PATH.write_text(json.dumps({"lead_theme": "discipline-score"}), encoding="utf-8")

    res = daily_posts.generate_daily_posts(
        platforms=["x"],
        d=date(2026, 6, 23),
        post_guidance="  Hype Drawdown Guardian lockouts and discipline score.  ",
    )

    assert res["generated"] == 1
    assert res["post_guidance_applied"] is True
    assert "Daily post direction from Jordan" in captured["user"]
    assert "Hype Drawdown Guardian lockouts and discipline score." in captured["user"]
    posts = daily_posts.load_posts(date(2026, 6, 23))
    assert posts[0]["post_guidance_applied"] is True
    assert posts[0]["post_guidance"] == "Hype Drawdown Guardian lockouts and discipline score."


def test_generate_daily_posts_omits_blank_post_guidance(tmp_path, monkeypatch):
    captured = {}

    def fake_complete(_task, *, system, user, max_tokens):
        captured["user"] = user
        return SimpleNamespace(text="Regular post.", provider="fake", model="fake-model")

    fake_llm = ModuleType("llm_router")
    fake_llm.complete = fake_complete
    monkeypatch.setitem(sys.modules, "llm_router", fake_llm)
    monkeypatch.setattr(daily_posts, "POSTS_DIR", tmp_path / "daily-posts")
    monkeypatch.setattr(daily_posts, "GUIDANCE_PATH", tmp_path / "missing-editor-guidance.json")

    res = daily_posts.generate_daily_posts(platforms=["x"], d=date(2026, 6, 23), post_guidance="  ")

    assert res["generated"] == 1
    assert res["post_guidance_applied"] is False
    assert "Daily post direction from Jordan" not in captured["user"]
    assert daily_posts.load_posts(date(2026, 6, 23))[0]["post_guidance_applied"] is False
