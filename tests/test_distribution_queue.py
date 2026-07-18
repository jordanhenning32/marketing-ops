import compliance
import distribution_queue


def isolate_distribution(tmp_path, monkeypatch):
    state = tmp_path / "state"
    config = tmp_path / "config"
    state.mkdir()
    config.mkdir()
    monkeypatch.setattr(distribution_queue, "STATE_DIR", state)
    monkeypatch.setattr(distribution_queue, "CONFIG_DIR", config)
    monkeypatch.setattr(distribution_queue, "QUEUE_PATH", state / "publish-queue.jsonl")
    monkeypatch.setattr(distribution_queue, "LOG_PATH", state / "publish-log.jsonl")
    monkeypatch.setattr(compliance, "CONFIG_DIR", config)
    return state, config


def write_campaign(folder, unsafe=False):
    folder.mkdir(parents=True)
    (folder / "01-x-thread.md").write_text(
        "Guaranteed profit if you use this." if unsafe else "Safe risk-control workflow language.",
        encoding="utf-8",
    )
    (folder / "06-email.md").write_text("Tools and education only. No promised outcomes.", encoding="utf-8")


def test_queue_campaign_refuses_failed_compliance(tmp_path, monkeypatch):
    isolate_distribution(tmp_path, monkeypatch)
    campaign = tmp_path / "content" / "bad-campaign"
    write_campaign(campaign, unsafe=True)

    try:
        distribution_queue.queue_campaign(campaign, "bad-campaign")
    except ValueError as exc:
        assert "refusing queue" in str(exc)
    else:
        raise AssertionError("unsafe campaign should not queue")


def test_queue_campaign_is_idempotent_and_manual_by_default(tmp_path, monkeypatch):
    isolate_distribution(tmp_path, monkeypatch)
    campaign = tmp_path / "content" / "good-campaign"
    write_campaign(campaign)

    first = distribution_queue.queue_campaign(campaign, "good-campaign")
    second = distribution_queue.queue_campaign(campaign, "good-campaign")

    assert len(first) == 2
    assert second == []
    assert len(distribution_queue.list_queue()) == 2
    assert {row["publish_mode"] for row in distribution_queue.list_queue()} == {"manual"}
    assert all(row["compliance_status"] == "pass" for row in distribution_queue.list_queue())


def test_tiktok_and_youtube_text_assets_require_short_video(tmp_path, monkeypatch):
    isolate_distribution(tmp_path, monkeypatch)
    campaign = tmp_path / "content" / "video-required-campaign"
    campaign.mkdir(parents=True)
    (campaign / "03-tiktok.md").write_text("Caption copy\n\nCTA: https://shadowedgetools.com/checklist?utm_source=tiktok", encoding="utf-8")
    (campaign / "05-youtube.md").write_text("Shorts outline\n\nCTA: https://shadowedgetools.com/checklist?utm_source=youtube", encoding="utf-8")

    queued = distribution_queue.queue_campaign(campaign, "video-required")
    rows = {row["platform"]: row for row in queued}

    assert rows["tiktok"]["status"] == "needs_video"
    assert rows["youtube"]["status"] == "needs_video"
    assert rows["tiktok"]["approval_status"] == "blocked"
    assert rows["youtube"]["video_status"] == "missing"

    try:
        distribution_queue.update_queue_item(rows["youtube"]["queue_key"], "approve")
    except PermissionError as exc:
        assert "short video" in str(exc)
    else:
        raise AssertionError("YouTube copy-only item should not approve without a short video")


def test_video_distribution_kits_queue_tiktok_and_youtube_with_mp4(tmp_path, monkeypatch):
    isolate_distribution(tmp_path, monkeypatch)
    campaign = tmp_path / "content" / "short-kit-campaign"
    clip = campaign / "clips" / "01-demo-short.mp4"
    clip.parent.mkdir(parents=True)
    clip.write_bytes(b"video bytes")

    for platform in ["tiktok", "youtube"]:
        kit = campaign / "distribution" / platform / "01"
        kit.mkdir(parents=True)
        (kit / "caption.txt").write_text(f"{platform} caption\n\nCTA: https://shadowedgetools.com/checklist?utm_source={platform}", encoding="utf-8")
        (kit / "title.txt").write_text("Demo Short", encoding="utf-8")
        (kit / "source-clip.txt").write_text(str(clip), encoding="utf-8")
        (kit / "metadata.json").write_text('{"clip_relpath": "clips/01-demo-short.mp4", "title": "Demo Short"}', encoding="utf-8")

    queued = distribution_queue.queue_campaign(campaign, "short-kit")

    assert {row["platform"] for row in queued} == {"tiktok", "youtube"}
    assert all(row["content_type"] == "short_video" for row in queued)
    assert all(row["video_status"] == "attached" for row in queued)
    assert all(row["video_file"].endswith("clips/01-demo-short.mp4") for row in queued)
    assert all(row["status"] == "queued" for row in queued)
    assert (campaign / "distribution" / "youtube" / "01" / "queue-metadata.json").exists()


def test_attach_video_unlocks_text_only_video_queue_item(tmp_path, monkeypatch):
    isolate_distribution(tmp_path, monkeypatch)
    monkeypatch.setattr(distribution_queue, "CONTENT_DIR", tmp_path / "content", raising=False)
    campaign = tmp_path / "content" / "attach-campaign"
    clip = campaign / "clips" / "01-existing-short.mp4"
    clip.parent.mkdir(parents=True)
    clip.write_bytes(b"video bytes")
    (campaign / "05-youtube.md").write_text("Shorts outline\n\nCTA: https://shadowedgetools.com/checklist?utm_source=youtube", encoding="utf-8")
    row = distribution_queue.queue_campaign(campaign, "attach-campaign")[0]

    attached = distribution_queue.attach_video(row["queue_key"], "content/attach-campaign/clips/01-existing-short.mp4")

    assert attached["status"] == "queued"
    assert attached["approval_status"] == "pending"
    assert attached["video_status"] == "attached"
    assert attached["video_file"] == "content/attach-campaign/clips/01-existing-short.mp4"
    assert distribution_queue.update_queue_item(row["queue_key"], "approve")["approval_status"] == "approved"
