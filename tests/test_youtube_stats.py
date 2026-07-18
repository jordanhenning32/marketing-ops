"""Tests for the YouTube performance feedback loop.

The API is mocked at the client boundary (`youtube_stats._client`) so no
network or credentials are touched. Paths are redirected into tmp_path.
"""
import json

import pytest

import youtube_stats


# --- a tiny fake of the YouTube Data API client -----------------------------

class _Req:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


class _Resource:
    def __init__(self, fn):
        self._fn = fn

    def list(self, **kwargs):
        return _Req(self._fn(**kwargs))


class FakeYT:
    """Supports channels().list, playlistItems().list, videos().list."""

    def __init__(self, *, subscribers, channel_views, videos, upload_ids):
        self._channel_items = [{
            "id": "UC_test",
            "snippet": {"title": "Test Channel"},
            "statistics": {
                "subscriberCount": str(subscribers),
                "viewCount": str(channel_views),
                "videoCount": str(len(upload_ids)),
            },
            "contentDetails": {"relatedPlaylists": {"uploads": "UU_test"}},
        }]
        self._videos = videos          # id -> {views, likes, comments, title, privacy}
        self._upload_ids = upload_ids

    def channels(self):
        return _Resource(lambda **kw: {"items": self._channel_items})

    def playlistItems(self):
        def fn(**kw):
            return {
                "items": [{"contentDetails": {"videoId": v}} for v in self._upload_ids],
                "nextPageToken": None,
            }
        return _Resource(fn)

    def videos(self):
        def fn(**kw):
            ids = (kw.get("id") or "").split(",")
            items = []
            for vid in ids:
                v = self._videos.get(vid)
                if not v:
                    continue
                items.append({
                    "id": vid,
                    "snippet": {"title": v.get("title", ""), "publishedAt": "2026-06-12T00:00:00Z"},
                    "statistics": {
                        "viewCount": str(v["views"]),
                        "likeCount": str(v.get("likes", 0)),
                        "commentCount": str(v.get("comments", 0)),
                        "favoriteCount": "0",
                    },
                    "status": {"privacyStatus": v.get("privacy", "public")},
                })
            return {"items": items}
        return _Resource(fn)


# --- fixtures ---------------------------------------------------------------

def _isolate(tmp_path, monkeypatch):
    content = tmp_path / "content"
    state = tmp_path / "state"
    content.mkdir()
    state.mkdir()
    monkeypatch.setattr(youtube_stats, "CONTENT_DIR", content)
    monkeypatch.setattr(youtube_stats, "STATE_DIR", state)
    monkeypatch.setattr(youtube_stats, "VIDEO_STATS_PATH", state / "youtube-video-stats.jsonl")
    monkeypatch.setattr(youtube_stats, "CHANNEL_STATS_PATH", state / "youtube-channel-stats.jsonl")
    return content, state


def _make_tracked_clip(content, slug, clip_index, video_id, *, title="Tracked Clip"):
    folder = content / slug / "distribution" / "youtube" / f"{clip_index:02d}"
    folder.mkdir(parents=True)
    meta = {
        "video_id": video_id,
        "platform_url": f"https://www.youtube.com/shorts/{video_id}",
        "uploaded_at": "2026-06-13T22:00:00",
        "title_used": title,
        "privacy": "public",
    }
    (folder / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    return folder / "metadata.json"


def _fake_client(monkeypatch, fake):
    monkeypatch.setattr(youtube_stats, "_client", lambda: fake)


# --- tests ------------------------------------------------------------------

def test_local_video_index_discovers_uploads(tmp_path, monkeypatch):
    content, _ = _isolate(tmp_path, monkeypatch)
    _make_tracked_clip(content, "run-a", 5, "VID_A", title="Winner")
    _make_tracked_clip(content, "run-b", 1, "VID_B")

    idx = youtube_stats.local_video_index()

    assert set(idx) == {"VID_A", "VID_B"}
    assert idx["VID_A"]["slug"] == "run-a"
    assert idx["VID_A"]["clip_index"] == 5
    assert idx["VID_A"]["title_used"] == "Winner"


def test_pull_stores_snapshot_and_writes_back(tmp_path, monkeypatch):
    content, state = _isolate(tmp_path, monkeypatch)
    meta_path = _make_tracked_clip(content, "run-a", 5, "VID_A")

    fake = FakeYT(
        subscribers=3, channel_views=128,
        videos={
            "VID_A": {"views": 46, "likes": 1, "comments": 0, "title": "Tracked Clip"},
            "VID_U": {"views": 43, "likes": 2, "comments": 1, "title": "Untracked"},
        },
        upload_ids=["VID_A", "VID_U"],
    )
    _fake_client(monkeypatch, fake)

    result = youtube_stats.pull(record_analytics=False)

    # whole-channel coverage: both the tracked and untracked video are captured
    assert result["video_count"] == 2
    assert result["tracked_count"] == 1
    # hottest first
    assert result["videos"][0]["video_id"] == "VID_A"
    assert result["videos"][0]["views"] == 46

    # time-series files written
    video_rows = youtube_stats._read_jsonl(youtube_stats.VIDEO_STATS_PATH)
    channel_rows = youtube_stats._read_jsonl(youtube_stats.CHANNEL_STATS_PATH)
    assert len(video_rows) == 2
    assert len(channel_rows) == 1
    assert channel_rows[0]["subscribers"] == 3
    assert channel_rows[0]["views"] == 128

    # write-back into the tracked clip metadata; untracked has no local file to touch
    md = json.loads(meta_path.read_text(encoding="utf-8"))
    assert md["stats"]["views"] == 46
    assert md["stats"]["delta_views"] is None  # first pull, no prior


def test_pull_computes_deltas_on_second_pull(tmp_path, monkeypatch):
    content, _ = _isolate(tmp_path, monkeypatch)
    _make_tracked_clip(content, "run-a", 5, "VID_A")

    fake = FakeYT(subscribers=3, channel_views=100,
                  videos={"VID_A": {"views": 40, "title": "A"}}, upload_ids=["VID_A"])
    _fake_client(monkeypatch, fake)
    youtube_stats.pull(record_analytics=False)

    # views climb, channel grows
    fake._videos["VID_A"]["views"] = 46
    fake._channel_items[0]["statistics"]["viewCount"] = "112"
    second = youtube_stats.pull(record_analytics=False)

    assert second["videos"][0]["delta_views"] == 6
    assert youtube_stats.channel_summary()["delta_views"] == 12


def test_video_summary_sorts_and_computes_delta(tmp_path, monkeypatch):
    _, state = _isolate(tmp_path, monkeypatch)
    rows = [
        {"pulled_at": "2026-06-16T10:00:00", "video_id": "A", "views": 10, "likes": 0, "title": "A", "tracked": True},
        {"pulled_at": "2026-06-16T11:00:00", "video_id": "A", "views": 18, "likes": 0, "title": "A", "tracked": True},
        {"pulled_at": "2026-06-16T11:00:00", "video_id": "B", "views": 30, "likes": 0, "title": "B", "tracked": True},
    ]
    for r in rows:
        youtube_stats._append_jsonl(youtube_stats.VIDEO_STATS_PATH, r)

    summary = youtube_stats.video_summary()

    assert [v["video_id"] for v in summary] == ["B", "A"]  # B hotter
    a = next(v for v in summary if v["video_id"] == "A")
    assert a["views"] == 18 and a["delta_views"] == 8
    b = next(v for v in summary if v["video_id"] == "B")
    assert b["delta_views"] is None  # only one snapshot for B


def test_pull_records_analytics_rollup(tmp_path, monkeypatch):
    content, _ = _isolate(tmp_path, monkeypatch)
    _make_tracked_clip(content, "run-a", 5, "VID_A")
    fake = FakeYT(subscribers=3, channel_views=128,
                  videos={"VID_A": {"views": 46, "likes": 1, "title": "A"}}, upload_ids=["VID_A"])
    _fake_client(monkeypatch, fake)

    import analytics
    calls = []
    monkeypatch.setattr(analytics, "record_metric", lambda *a, **k: calls.append((a, k)))

    youtube_stats.pull(record_analytics=True)

    assert len(calls) == 1
    metric_name = calls[0][0][0]
    value = calls[0][0][1]
    assert metric_name == "youtube_watch_metrics"
    assert "views=128" in value and calls[0][1]["status"] == "measured"


def test_pull_raises_when_not_connected(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(youtube_stats.youtube_auth, "get_authenticated_credentials", lambda: None)
    with pytest.raises(RuntimeError, match="not connected"):
        youtube_stats.pull(record_analytics=False)
