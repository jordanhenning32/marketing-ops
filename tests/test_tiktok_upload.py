"""Tests for the TikTok Content Posting API client (mocked — no real network)."""
from __future__ import annotations

import pytest

import tiktok_auth
import tiktok_upload


class FakeResp:
    def __init__(self, json_data=None, status_code=200, text=""):
        self._json = json_data or {}
        self.status_code = status_code
        self.text = text

    def json(self):
        return self._json


class FakeClient:
    """Records POST/PUT calls; returns canned POST responses in order."""

    def __init__(self, *, post_results, put_status=200):
        self._post_results = list(post_results)
        self.post_calls = []
        self.put_calls = []
        self._put_status = put_status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, json=None, headers=None):
        self.post_calls.append({"url": url, "json": json, "headers": headers})
        return self._post_results.pop(0)

    def put(self, url, content=None, headers=None):
        self.put_calls.append({"url": url, "content": content, "headers": headers})
        return FakeResp(status_code=self._put_status, text="ok")


@pytest.fixture
def clip(tmp_path):
    p = tmp_path / "clip.mp4"
    p.write_bytes(b"\x00\x01\x02fakevideo")
    return p


def _patch(monkeypatch, fake, token="tok"):
    monkeypatch.setattr(tiktok_auth, "get_access_token", lambda: token)
    monkeypatch.setattr(tiktok_upload.httpx, "Client", lambda *a, **k: fake)


def test_oauth_scopes_gate_direct_post(monkeypatch):
    # Drafts scopes are always requested; video.publish (Direct Post) is opt-in via
    # TIKTOK_DIRECT_POST so it never breaks OAuth before the app is approved for it.
    monkeypatch.delenv("TIKTOK_DIRECT_POST", raising=False)
    base = tiktok_auth.oauth_scopes()
    assert "user.info.basic" in base
    assert "video.upload" in base
    assert "video.publish" not in base   # off by default

    monkeypatch.setenv("TIKTOK_DIRECT_POST", "1")
    assert "video.publish" in tiktok_auth.oauth_scopes()


def test_query_creator_info_returns_data(monkeypatch):
    fake = FakeClient(post_results=[
        FakeResp({"data": {"privacy_level_options": ["PUBLIC_TO_EVERYONE", "SELF_ONLY"]},
                  "error": {"code": "ok"}})
    ])
    _patch(monkeypatch, fake)
    data = tiktok_upload.query_creator_info()
    assert data["privacy_level_options"] == ["PUBLIC_TO_EVERYONE", "SELF_ONLY"]
    assert fake.post_calls[0]["url"] == tiktok_upload.CREATOR_INFO_URL


def test_query_creator_info_raises_on_error(monkeypatch):
    fake = FakeClient(post_results=[
        FakeResp({"error": {"code": "access_token_invalid", "message": "bad token"}})
    ])
    _patch(monkeypatch, fake)
    with pytest.raises(RuntimeError, match="creator_info failed"):
        tiktok_upload.query_creator_info()


def test_direct_post_happy_path(monkeypatch, clip):
    fake = FakeClient(post_results=[
        FakeResp({"data": {"publish_id": "pub_123", "upload_url": "https://upload.example"},
                  "error": {"code": "ok"}})
    ])
    _patch(monkeypatch, fake)
    result = tiktok_upload.direct_post(
        clip,
        title="The Lock keeps you disciplined",
        privacy_level="PUBLIC_TO_EVERYONE",
        brand_organic_toggle=True,
        creator_info={"privacy_level_options": ["PUBLIC_TO_EVERYONE", "SELF_ONLY"]},
    )
    assert result == {"publish_id": "pub_123"}

    init = fake.post_calls[0]
    assert init["url"] == tiktok_upload.DIRECT_POST_INIT_URL
    pi = init["json"]["post_info"]
    assert pi["title"] == "The Lock keeps you disciplined"
    assert pi["privacy_level"] == "PUBLIC_TO_EVERYONE"
    assert pi["brand_organic_toggle"] is True
    assert init["json"]["source_info"]["source"] == "FILE_UPLOAD"
    # the file was PUT exactly once
    assert len(fake.put_calls) == 1


def test_direct_post_refuses_disallowed_privacy(monkeypatch, clip):
    fake = FakeClient(post_results=[])  # should never reach the init POST
    _patch(monkeypatch, fake)
    with pytest.raises(RuntimeError, match="won't allow privacy"):
        tiktok_upload.direct_post(
            clip,
            title="x",
            privacy_level="PUBLIC_TO_EVERYONE",
            creator_info={"privacy_level_options": ["SELF_ONLY"]},  # public not allowed
        )
    assert fake.post_calls == []  # bailed before hitting the API


def test_direct_post_not_connected(monkeypatch, clip):
    monkeypatch.setattr(tiktok_auth, "get_access_token", lambda: None)
    with pytest.raises(RuntimeError, match="Not connected"):
        tiktok_upload.direct_post(clip, title="x", creator_info={"privacy_level_options": ["PUBLIC_TO_EVERYONE"]})


def test_upload_to_inbox_still_works(monkeypatch, clip):
    """Regression: the refactor kept the drafts/inbox path intact."""
    fake = FakeClient(post_results=[
        FakeResp({"data": {"publish_id": "draft_1", "upload_url": "https://upload.example"},
                  "error": {"code": "ok"}})
    ])
    _patch(monkeypatch, fake)
    result = tiktok_upload.upload_to_inbox(clip)
    assert result == {"publish_id": "draft_1"}
    init = fake.post_calls[0]
    assert init["url"] == tiktok_upload.INBOX_INIT_URL
    assert "post_info" not in init["json"]   # inbox upload carries no post_info
    assert init["json"]["source_info"]["source"] == "FILE_UPLOAD"
    assert len(fake.put_calls) == 1
