import importlib


def test_job_locks_are_pruned_for_terminal_and_deleted_jobs(tmp_path, monkeypatch):
    jobs = importlib.import_module("jobs")
    monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path / "jobs")
    jobs._JOB_LOCKS.clear()

    job = jobs.create_job(kind="video", source_filename="demo.mp4")
    assert job.id in jobs._JOB_LOCKS

    jobs.update_job(job.id, state="done", finished_at="2026-06-20T10:00:00")
    assert job.id not in jobs._JOB_LOCKS

    missing = "missing-job"
    assert jobs.delete_job(missing) is False
    assert missing not in jobs._JOB_LOCKS
