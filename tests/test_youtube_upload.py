import json


class FakeCredentials:
    valid = True


class FakeInsertRequest:
    def next_chunk(self):
        return None, {"id": "video123"}


class FakeVideos:
    def __init__(self):
        self.insert_calls = []

    def insert(self, **_kwargs):
        self.insert_calls.append(_kwargs)
        return FakeInsertRequest()


class FakeThumbnails:
    def __init__(self):
        self.calls = []

    def set(self, **kwargs):
        self.calls.append(kwargs)
        return self

    def execute(self):
        return {"items": []}


class FakeYouTube:
    def __init__(self):
        self.videos_resource = FakeVideos()
        self.thumbnails_resource = FakeThumbnails()

    def videos(self):
        return self.videos_resource

    def thumbnails(self):
        return self.thumbnails_resource


def test_upload_clip_sets_thumbnail_when_kit_has_thumbnail(tmp_path, monkeypatch):
    import youtube_upload

    slug = "run"
    kit = tmp_path / slug / "distribution" / "youtube" / "01"
    clips = tmp_path / slug / "clips"
    kit.mkdir(parents=True)
    clips.mkdir(parents=True)
    clip = clips / "clip.mp4"
    thumb = kit / "thumbnail.jpg"
    clip.write_bytes(b"video")
    thumb.write_bytes(b"jpg")
    (kit / "title.txt").write_text("Title", encoding="utf-8")
    (kit / "caption.txt").write_text("Caption", encoding="utf-8")
    (kit / "source-clip.txt").write_text(str(clip), encoding="utf-8")
    (kit / "metadata.json").write_text(
        json.dumps({"thumbnail_relpath": "distribution/youtube/01/thumbnail.jpg"}),
        encoding="utf-8",
    )

    fake_yt = FakeYouTube()
    monkeypatch.setattr(youtube_upload, "CONTENT_DIR", tmp_path)
    monkeypatch.setattr(youtube_upload.youtube_auth, "get_authenticated_credentials", lambda: FakeCredentials())
    monkeypatch.setattr(youtube_upload, "_yt_config", lambda: {"enabled": True, "default_privacy": "unlisted"})
    monkeypatch.setattr(youtube_upload, "build", lambda *_args, **_kwargs: fake_yt)
    monkeypatch.setattr(youtube_upload, "MediaFileUpload", lambda path, **kwargs: {"path": path, **kwargs})

    logs = []
    monkeypatch.setattr(youtube_upload.jobs, "log_job", lambda _job_id, line: logs.append(line))
    monkeypatch.setattr(youtube_upload.jobs, "update_job", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(youtube_upload.jobs, "get_job", lambda _job_id: None)

    youtube_upload.upload_clip_job("job1", slug, 1)

    assert fake_yt.thumbnails_resource.calls
    assert fake_yt.thumbnails_resource.calls[0]["videoId"] == "video123"
    body = fake_yt.videos_resource.insert_calls[0]["body"]
    assert body["snippet"]["title"] == "Title #Shorts"
    assert body["snippet"]["description"] == "Caption"
    metadata = json.loads((kit / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["thumbnail_uploaded"] is True
    assert metadata["thumbnail_error"] is None


def test_upload_clip_requires_thumbnail_before_video_insert(tmp_path, monkeypatch):
    import pytest
    import youtube_upload

    slug = "run"
    kit = tmp_path / slug / "distribution" / "youtube" / "01"
    clips = tmp_path / slug / "clips"
    kit.mkdir(parents=True)
    clips.mkdir(parents=True)
    clip = clips / "clip.mp4"
    clip.write_bytes(b"video")
    (kit / "title.txt").write_text("Title", encoding="utf-8")
    (kit / "caption.txt").write_text("Caption", encoding="utf-8")
    (kit / "source-clip.txt").write_text(str(clip), encoding="utf-8")
    (kit / "metadata.json").write_text("{}", encoding="utf-8")

    fake_yt = FakeYouTube()
    monkeypatch.setattr(youtube_upload, "CONTENT_DIR", tmp_path)
    monkeypatch.setattr(youtube_upload.youtube_auth, "get_authenticated_credentials", lambda: FakeCredentials())
    monkeypatch.setattr(youtube_upload, "_yt_config", lambda: {"enabled": True, "default_privacy": "unlisted"})
    monkeypatch.setattr(youtube_upload, "build", lambda *_args, **_kwargs: fake_yt)
    monkeypatch.setattr(youtube_upload.jobs, "log_job", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(youtube_upload.jobs, "update_job", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="requires thumbnail.jpg"):
        youtube_upload.upload_clip_job("job1", slug, 1)

    assert fake_yt.videos_resource.insert_calls == []


def test_youtube_description_keeps_shadow_edge_links_and_removes_other_urls():
    import youtube_upload

    description = youtube_upload._make_description(
        "\n".join([
            "Title",
            "",
            "Free risk-control checklist: https://shadowedgetools.com/checklist?utm_source=youtube&utm_medium=video&utm_campaign=launch&utm_content=short-01",
            "Legacy homepage survives sanitizer: https://shadowedgetools.com/?utm_source=youtube",
            "Bad: https://example.com",
            "Shortener: bit.ly/bad",
            "",
            "#Shorts",
        ]),
        "\nShadow Edge Tools - risk-management add-ons.\nshadowedgetools.com",
    )

    assert "https://shadowedgetools.com/checklist?utm_source=youtube&utm_medium=video&utm_campaign=launch&utm_content=short-01" in description
    assert "https://shadowedgetools.com/?utm_source=youtube" in description
    assert "https://shadowedgetools.com" in description
    assert "https://example.com" not in description
    assert "bit.ly/bad" not in description
    assert "Title" in description
    assert "risk-management add-ons" in description
    assert "#Shorts" in description


def test_youtube_title_gets_shorts_hashtag():
    import youtube_upload

    assert youtube_upload._ensure_shorts_title("Break Even Stop") == "Break Even Stop #Shorts"
    assert youtube_upload._ensure_shorts_title("Already #Shorts") == "Already #Shorts"


def test_upload_retries_with_minimal_description_on_invalid_description(tmp_path, monkeypatch):
    import youtube_upload

    slug = "run"
    kit = tmp_path / slug / "distribution" / "youtube" / "01"
    clips = tmp_path / slug / "clips"
    kit.mkdir(parents=True)
    clips.mkdir(parents=True)
    clip = clips / "clip.mp4"
    thumb = kit / "thumbnail.jpg"
    clip.write_bytes(b"video")
    thumb.write_bytes(b"jpg")
    (kit / "title.txt").write_text("Safe Title", encoding="utf-8")
    (kit / "caption.txt").write_text("Caption with https://shadowedgetools.com", encoding="utf-8")
    (kit / "source-clip.txt").write_text(str(clip), encoding="utf-8")
    (kit / "metadata.json").write_text(
        json.dumps({"thumbnail_relpath": "distribution/youtube/01/thumbnail.jpg"}),
        encoding="utf-8",
    )

    fake_yt = FakeYouTube()
    calls = {"wrap": 0}

    def fake_wrap(*_args, **_kwargs):
        calls["wrap"] += 1
        if calls["wrap"] == 1:
            raise RuntimeError("invalidDescription: invalid video description")
        return {"id": "video456"}

    monkeypatch.setattr(youtube_upload, "CONTENT_DIR", tmp_path)
    monkeypatch.setattr(youtube_upload.youtube_auth, "get_authenticated_credentials", lambda: FakeCredentials())
    monkeypatch.setattr(youtube_upload, "_yt_config", lambda: {"enabled": True, "default_privacy": "unlisted"})
    monkeypatch.setattr(youtube_upload, "build", lambda *_args, **_kwargs: fake_yt)
    monkeypatch.setattr(youtube_upload, "MediaFileUpload", lambda path, **kwargs: {"path": path, **kwargs})
    monkeypatch.setattr(youtube_upload, "_wrap_resumable", fake_wrap)
    monkeypatch.setattr(youtube_upload.jobs, "log_job", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(youtube_upload.jobs, "update_job", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(youtube_upload.jobs, "get_job", lambda _job_id: None)

    youtube_upload.upload_clip_job("job1", slug, 1)

    assert calls["wrap"] == 2
    assert len(fake_yt.videos_resource.insert_calls) == 2
    second_body = fake_yt.videos_resource.insert_calls[1]["body"]
    assert second_body["snippet"]["description"].startswith("Safe Title")
    assert "https://" not in second_body["snippet"]["description"]


def test_upload_long_form_uses_watch_url_and_keeps_link(tmp_path, monkeypatch):
    import youtube_upload

    slug = "run"
    kit = tmp_path / slug / "distribution" / "youtube" / "00"
    source = tmp_path / "source.mp4"
    kit.mkdir(parents=True)
    source.write_bytes(b"video")
    thumb = kit / "thumbnail.jpg"
    thumb.write_bytes(b"jpg")
    (kit / "title.txt").write_text("Full Discipline Score Walkthrough", encoding="utf-8")
    (kit / "caption.txt").write_text(
        "Free risk-control checklist: https://shadowedgetools.com/checklist?utm_source=youtube&utm_medium=video&utm_campaign=run&utm_content=long-form\n\nFull walkthrough.",
        encoding="utf-8",
    )
    (kit / "source-clip.txt").write_text(str(source), encoding="utf-8")
    (kit / "metadata.json").write_text(
        json.dumps({
            "content_type": "long_form",
            "thumbnail_relpath": "distribution/youtube/00/thumbnail.jpg",
        }),
        encoding="utf-8",
    )

    fake_yt = FakeYouTube()
    monkeypatch.setattr(youtube_upload, "CONTENT_DIR", tmp_path)
    monkeypatch.setattr(youtube_upload.youtube_auth, "get_authenticated_credentials", lambda: FakeCredentials())
    monkeypatch.setattr(youtube_upload, "_yt_config", lambda: {"enabled": True, "default_privacy": "unlisted"})
    monkeypatch.setattr(youtube_upload, "build", lambda *_args, **_kwargs: fake_yt)
    monkeypatch.setattr(youtube_upload, "MediaFileUpload", lambda path, **kwargs: {"path": path, **kwargs})
    monkeypatch.setattr(youtube_upload.jobs, "log_job", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(youtube_upload.jobs, "update_job", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(youtube_upload.jobs, "get_job", lambda _job_id: None)

    youtube_upload.upload_clip_job("job1", slug, 0)

    body = fake_yt.videos_resource.insert_calls[0]["body"]
    assert body["snippet"]["title"] == "Full Discipline Score Walkthrough"
    assert "#Shorts" not in body["snippet"]["title"]
    assert "https://shadowedgetools.com/checklist?utm_source=youtube&utm_medium=video&utm_campaign=run&utm_content=long-form" in body["snippet"]["description"]
    metadata = json.loads((kit / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["content_type"] == "long_form"
    assert metadata["platform_url"] == "https://www.youtube.com/watch?v=video123"


def test_upload_short_uses_shorts_url_and_checklist_link(tmp_path, monkeypatch):
    import youtube_upload

    slug = "run"
    kit = tmp_path / slug / "distribution" / "youtube" / "01"
    clips = tmp_path / slug / "clips"
    kit.mkdir(parents=True)
    clips.mkdir(parents=True)
    clip = clips / "clip.mp4"
    thumb = kit / "thumbnail.jpg"
    clip.write_bytes(b"video")
    thumb.write_bytes(b"jpg")
    (kit / "title.txt").write_text("Do Not Move That Stop", encoding="utf-8")
    (kit / "caption.txt").write_text(
        "Free risk-control checklist: https://shadowedgetools.com/checklist?utm_source=youtube&utm_medium=video&utm_campaign=run&utm_content=short-01\n\nShort walkthrough.",
        encoding="utf-8",
    )
    (kit / "source-clip.txt").write_text(str(clip), encoding="utf-8")
    (kit / "metadata.json").write_text(
        json.dumps({
            "thumbnail_relpath": "distribution/youtube/01/thumbnail.jpg",
        }),
        encoding="utf-8",
    )

    fake_yt = FakeYouTube()
    monkeypatch.setattr(youtube_upload, "CONTENT_DIR", tmp_path)
    monkeypatch.setattr(youtube_upload.youtube_auth, "get_authenticated_credentials", lambda: FakeCredentials())
    monkeypatch.setattr(youtube_upload, "_yt_config", lambda: {"enabled": True, "default_privacy": "unlisted"})
    monkeypatch.setattr(youtube_upload, "build", lambda *_args, **_kwargs: fake_yt)
    monkeypatch.setattr(youtube_upload, "MediaFileUpload", lambda path, **kwargs: {"path": path, **kwargs})
    monkeypatch.setattr(youtube_upload.jobs, "log_job", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(youtube_upload.jobs, "update_job", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(youtube_upload.jobs, "get_job", lambda _job_id: None)

    youtube_upload.upload_clip_job("job1", slug, 1)

    body = fake_yt.videos_resource.insert_calls[0]["body"]
    assert body["snippet"]["title"] == "Do Not Move That Stop #Shorts"
    assert "https://shadowedgetools.com/checklist?utm_source=youtube&utm_medium=video&utm_campaign=run&utm_content=short-01" in body["snippet"]["description"]
    metadata = json.loads((kit / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["content_type"] == "short"
    assert metadata["platform_url"] == "https://www.youtube.com/shorts/video123"
