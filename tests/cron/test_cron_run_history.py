"""Tests for cron run-history storage and status summarization."""

import json
from argparse import Namespace

import pytest


@pytest.fixture()
def tmp_cron_dir(tmp_path, monkeypatch):
    cron_dir = tmp_path / "cron"
    monkeypatch.setattr("cron.jobs.CRON_DIR", cron_dir)
    monkeypatch.setattr("cron.jobs.JOBS_FILE", cron_dir / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", cron_dir / "output")
    monkeypatch.setattr("cron.jobs.RUNS_FILE", cron_dir / "runs.jsonl")
    return cron_dir


def test_append_and_list_run_history_filters_newest_first(tmp_cron_dir):
    from cron.jobs import RUNS_FILE, append_run_history, list_run_history

    append_run_history({"job_id": "job-a", "status": "ok", "started_at": "2026-05-11T01:00:00"})
    append_run_history({"job_id": "job-b", "status": "error", "started_at": "2026-05-11T02:00:00"})
    append_run_history({"job_id": "job-a", "status": "silent", "started_at": "2026-05-11T03:00:00"})

    assert RUNS_FILE.exists()
    assert oct(RUNS_FILE.stat().st_mode & 0o777) == "0o600"

    rows = list_run_history(limit=2)
    assert [row["started_at"] for row in rows] == ["2026-05-11T03:00:00", "2026-05-11T02:00:00"]
    assert rows[0]["schema_version"] == 1
    assert rows[0]["event_type"] == "run_attempt"

    assert [row["status"] for row in list_run_history(job_id="job-a")] == ["silent", "ok"]
    assert [row["job_id"] for row in list_run_history(status="error")] == ["job-b"]
    assert [row["job_id"] for row in list_run_history(since="2026-05-11T02:30:00")] == ["job-a"]


def test_list_run_history_skips_malformed_rows(tmp_cron_dir):
    from cron.jobs import RUNS_FILE, append_run_history, list_run_history

    append_run_history({"job_id": "good", "status": "ok", "started_at": "2026-05-11T01:00:00"})
    with RUNS_FILE.open("a", encoding="utf-8") as f:
        f.write("{not-json\n")
        f.write(json.dumps(["not", "an", "object"]) + "\n")

    rows = list_run_history()
    assert len(rows) == 1
    assert rows[0]["job_id"] == "good"


def test_build_cron_run_record_sanitizes_errors_and_body_metadata(tmp_path, monkeypatch):
    from cron.scheduler import _build_cron_run_record

    output_file = tmp_path / "output.txt"
    output_file.write_text("full body stays in output file", encoding="utf-8")
    monkeypatch.setattr("cron.scheduler.get_job", lambda job_id: {"next_run_at": "2026-05-11T04:00:00"})

    record = _build_cron_run_record(
        job={
            "id": "job-1",
            "name": "Nightly",
            "schedule": {"kind": "interval"},
            "schedule_display": "every 1h",
            "deliver": "telegram:-100123:456",
            "skills": ["maps", "maps", "qmd"],
            "no_agent": True,
            "script": "watchdog.py",
        },
        run_id="run-1",
        tick_id="tick-1",
        scheduled_for="2026-05-11T00:00:00",
        started_at="2026-05-11T00:00:02",
        ended_at="2026-05-11T00:00:03",
        duration_ms=1000,
        success=False,
        output="full body stays in output file",
        final_response="failure alert",
        error="RuntimeError: script failed\nstdout secret body\nstderr secret body",
        delivery_error="Failed to send to telegram:-100123:456",
        should_deliver=True,
        external_delivery_expected=True,
        silent=False,
        empty_final_response=False,
        processing_error=False,
        output_file=output_file,
    )

    assert record["status"] == "error"
    assert record["delivery_targets"] == ["telegram"]
    assert record["skills"] == ["maps", "qmd"]
    assert record["output_bytes"] == output_file.stat().st_size
    assert record["output_sha256"]
    assert "stdout secret body" not in record["error"]
    assert "stderr secret body" not in record["error"]
    assert "telegram:-100123:456" not in record["delivery_error"]
    assert "telegram:<redacted>" in record["delivery_error"]


def test_cron_status_snapshot_summarizes_job_health():
    from hermes_cli.cron import _build_cron_status_snapshot

    jobs = [
        {"id": "job-a", "name": "A", "state": "scheduled", "enabled": True, "next_run_at": "2026-05-11T04:00:00"},
        {"id": "job-b", "name": "B", "state": "paused", "enabled": True},
    ]
    rows = [
        {"job_id": "job-a", "status": "error", "started_at": "2026-05-11T03:00:00", "duration_ms": 300, "output_bytes": 10, "response_status": "empty"},
        {"job_id": "job-a", "status": "delivery_error", "started_at": "2026-05-11T02:00:00", "duration_ms": 200, "output_bytes": 20},
        {"job_id": "job-a", "status": "ok", "started_at": "2026-05-11T01:00:00", "duration_ms": 100, "output_bytes": 30},
    ]

    snapshot = _build_cron_status_snapshot(jobs, rows, [123])

    assert snapshot["gateway_running"] is True
    assert snapshot["active_jobs"] == 1
    assert snapshot["total_jobs"] == 2
    assert snapshot["next_run_at"] == "2026-05-11T04:00:00"
    assert snapshot["status_counts"] == {"error": 1, "delivery_error": 1, "ok": 1}
    job_a = next(job for job in snapshot["jobs"] if job["job_id"] == "job-a")
    assert job_a["last_status"] == "error"
    assert job_a["last_success_at"] == "2026-05-11T01:00:00"
    assert job_a["last_failure_at"] == "2026-05-11T03:00:00"
    assert job_a["consecutive_failures"] == 2
    assert job_a["avg_duration_ms"] == 200
    assert job_a["delivery_failures"] == 1
    assert job_a["empty_response_runs"] == 1


def test_cron_history_cli_json(tmp_cron_dir, capsys):
    from cron.jobs import append_run_history
    from hermes_cli.cron import cron_history

    append_run_history({"job_id": "job-a", "status": "ok", "started_at": "2026-05-11T01:00:00"})

    assert cron_history(Namespace(job_id=None, limit=10, status=None, since=None, days=None, json=True)) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["job_id"] == "job-a"


def test_cron_status_cli_json(tmp_cron_dir, monkeypatch, capsys):
    from cron.jobs import append_run_history, create_job
    from hermes_cli.cron import cron_status

    job = create_job(prompt="Check", schedule="every 1h", name="Check")
    append_run_history({"job_id": job["id"], "status": "ok", "started_at": "2026-05-11T01:00:00", "duration_ms": 50})
    monkeypatch.setattr("hermes_cli.gateway.find_gateway_pids", lambda: [999])

    assert cron_status(Namespace(history_limit=10, json=True)) == 0
    snapshot = json.loads(capsys.readouterr().out)
    assert snapshot["gateway_running"] is True
    assert snapshot["active_jobs"] == 1
    assert snapshot["status_counts"] == {"ok": 1}
    assert snapshot["jobs"][0]["last_status"] == "ok"
