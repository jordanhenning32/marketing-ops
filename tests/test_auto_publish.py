import json
from types import SimpleNamespace


def isolate_auto_publish(tmp_path, monkeypatch):
    import auto_publish
    import distribution_queue

    state = tmp_path / "state"
    content = tmp_path / "content"
    config = tmp_path / "config"
    state.mkdir()
    content.mkdir()
    config.mkdir()
    monkeypatch.setattr(distribution_queue, "STATE_DIR", state)
    monkeypatch.setattr(distribution_queue, "QUEUE_PATH", state / "publish-queue.jsonl")
    monkeypatch.setattr(distribution_queue, "LOG_PATH", state / "publish-log.jsonl")
    monkeypatch.setattr(distribution_queue, "CONTENT_DIR", content)
    monkeypatch.setattr(auto_publish, "CONTENT_DIR", content)
    monkeypatch.setattr(auto_publish, "DISTRIBUTION_CONFIG", config / "distribution.yaml")
    return state, content, config


def test_unsupported_platform_records_auto_publish_blocker(tmp_path, monkeypatch):
    import auto_publish
    import distribution_queue

    isolate_auto_publish(tmp_path, monkeypatch)
    row = {
        "queue_key": "x1",
        "platform": "x",
        "campaign_id": "c1",
        "status": "queued",
        "approval_status": "approved",
        "compliance_status": "pass",
    }
    distribution_queue.write_jsonl(distribution_queue.QUEUE_PATH, [row])

    result = auto_publish.try_auto_publish_after_approval(row)

    assert result["status"] == "blocked"
    saved = distribution_queue.list_queue()[0]
    assert saved["auto_publish_status"] == "blocked"
    assert "X auto-posting needs" in saved["auto_publish_message"]


def test_youtube_approval_starts_upload_job(tmp_path, monkeypatch):
    import auto_publish
    import distribution_queue

    state, content, config = isolate_auto_publish(tmp_path, monkeypatch)
    (config / "distribution.yaml").write_text(
        "platforms:\n  youtube:\n    enabled: true\n    auto_upload: true\n    default_privacy: unlisted\n",
        encoding="utf-8",
    )
    kit = content / "run" / "distribution" / "youtube" / "01"
    kit.mkdir(parents=True)
    (kit / "metadata.json").write_text("{}", encoding="utf-8")
    row = {
        "queue_key": "yt1",
        "platform": "youtube",
        "campaign_id": "run",
        "status": "queued",
        "approval_status": "approved",
        "compliance_status": "pass",
        "kit_dir": "content/run/distribution/youtube/01",
    }
    distribution_queue.write_jsonl(distribution_queue.QUEUE_PATH, [row])
    monkeypatch.setattr(auto_publish.youtube_auth, "get_authenticated_credentials", lambda: object())
    created = SimpleNamespace(id="job123")
    calls = {}
    def fake_create_job(**kwargs):
        calls["create"] = kwargs
        return created
    monkeypatch.setattr(auto_publish.jobs, "create_job", fake_create_job)
    monkeypatch.setattr(auto_publish.jobs, "run_job", lambda *args, **kwargs: calls.setdefault("run", {"args": args, "kwargs": kwargs}))

    result = auto_publish.try_auto_publish_after_approval(row)

    assert result == {"status": "started", "message": "YouTube upload started.", "job_id": "job123"}
    assert calls["create"]["kind"] == "youtube-upload"
    assert calls["run"]["args"][0] == "job123"
    saved = distribution_queue.list_queue()[0]
    assert saved["auto_publish_status"] == "started"
    assert saved["auto_publish_job_id"] == "job123"


def test_youtube_upload_wrapper_marks_queue_item_published(tmp_path, monkeypatch):
    import auto_publish
    import distribution_queue

    isolate_auto_publish(tmp_path, monkeypatch)
    row = {
        "queue_key": "yt1",
        "platform": "youtube",
        "campaign_id": "run",
        "status": "queued",
        "approval_status": "approved",
        "compliance_status": "pass",
    }
    distribution_queue.write_jsonl(distribution_queue.QUEUE_PATH, [row])
    monkeypatch.setattr(auto_publish.youtube_upload, "upload_clip_job", lambda *args, **kwargs: None)
    monkeypatch.setattr(auto_publish.jobs, "get_job", lambda _job_id: SimpleNamespace(meta={"platform_url": "https://www.youtube.com/shorts/abc"}))

    auto_publish._youtube_upload_and_record("job123", "yt1", "run", 1)

    saved = distribution_queue.list_queue()[0]
    assert saved["status"] == "published_auto"
    assert saved["platform_url"] == "https://www.youtube.com/shorts/abc"
    assert saved["auto_publish_status"] == "published"
