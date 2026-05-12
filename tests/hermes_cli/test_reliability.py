from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path

from hermes_cli import reliability


def test_read_jsonl_skips_malformed(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    path.write_text('{"a": 1}\nnot-json\n{"b": 2}\n', encoding="utf-8")

    assert reliability.read_jsonl(path) == [{"a": 1}, {"b": 2}]


def test_append_event_writes_local_event(tmp_path: Path):
    event = reliability.append_event({"event_type": "test", "status": "ok"}, home=tmp_path)

    rows = reliability.read_events(home=tmp_path, limit=10)
    assert rows == [event]
    assert rows[0]["schema_version"] == 1
    assert rows[0]["event_id"]
    assert rows[0]["recorded_at"]


def test_observability_event_retention_and_rotation(tmp_path: Path):
    path = reliability.events_file(tmp_path)
    path.parent.mkdir(parents=True)
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=40)).isoformat()
    recent = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).isoformat()
    rows = [
        {"event_type": "tool_call", "recorded_at": old, "n": 1},
        {"event_type": "tool_call", "recorded_at": recent, "n": 2},
        {"event_type": "model_call", "recorded_at": recent, "n": 3},
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    stats = reliability.event_stream_stats(home=tmp_path)
    assert stats["line_count"] == 3
    result = reliability.prune_observability_events(home=tmp_path, retention_days=30, max_events=1)
    assert result["before"] == 3
    assert result["after"] == 1
    assert reliability.read_events(home=tmp_path, limit=10)[0]["n"] == 3

    rotation = reliability.rotate_observability_events(home=tmp_path, max_bytes=1, keep=2)
    assert rotation["rotated"] is True
    assert (path.parent / "events.jsonl.1").exists()
    assert path.exists()


def test_summarize_cron_reads_runs_jsonl(tmp_path: Path, monkeypatch):
    runs = tmp_path / "cron" / "runs.jsonl"
    runs.parent.mkdir()
    runs.write_text(
        json.dumps({
            "job_id": "abc",
            "job_name": "demo",
            "status": "ok",
            "started_at": "2026-01-01T00:00:00Z",
            "duration_ms": 25,
        }) + "\n",
        encoding="utf-8",
    )

    def fail_helper(*args, **kwargs):
        raise RuntimeError("helper unavailable")

    monkeypatch.setattr("cron.jobs.list_run_history", fail_helper)
    summary = reliability.summarize_cron(tmp_path, limit=10)

    assert summary["history_count"] == 1
    assert summary["status_counts"] == {"ok": 1}
    assert summary["jobs"][0]["job_id"] == "abc"
    assert summary["jobs"][0]["avg_duration_ms"] == 25


def test_summarize_tools_from_state_db(tmp_path: Path):
    con = sqlite3.connect(tmp_path / "state.db")
    con.execute("create table messages (timestamp real, tool_calls text)")
    con.execute(
        "insert into messages values (?, ?)",
        (9999999999, json.dumps([{"name": "terminal", "arguments": "{}"}, {"name": "skill_view", "arguments": json.dumps({"name": "hermes-agent"})}])),
    )
    con.commit()
    con.close()

    summary = reliability.summarize_tools(tmp_path, days=1, limit=10)

    assert summary["ok"] is True
    assert {item["name"]: item["count"] for item in summary["tools"]} == {"terminal": 1, "skill_view": 1}
    assert summary["skills"] == [{"name": "hermes-agent", "count": 1}]


def test_record_tool_call_event_is_bounded_and_summarized(tmp_path: Path):
    reliability.record_tool_call_event(
        tool_name="terminal",
        args={"command": "echo secret-ish payload that should not be stored"},
        result=json.dumps({"output": "x"}),
        duration_ms=42,
        task_id="task-1",
        session_id="session-1",
        tool_call_id="call-1",
        home=tmp_path,
    )

    rows = reliability.read_events(home=tmp_path, limit=10)
    assert len(rows) == 1
    row = rows[0]
    assert row["event_type"] == "tool_call"
    assert row["tool_name"] == "terminal"
    assert row["status"] == "ok"
    assert row["duration_ms"] == 42
    assert row["args_keys"] == ["command"]
    assert "secret-ish payload" not in json.dumps(row)

    summary = reliability.summarize_tool_events(tmp_path, limit=10)
    assert summary["events_count"] == 1
    assert summary["tools"][0]["name"] == "terminal"
    assert summary["tools"][0]["avg_duration_ms"] == 42


def test_record_model_call_event_summarizes_latency_tokens_and_cost(tmp_path: Path):
    reliability.record_model_call_event(
        model="gpt-test",
        provider="openai",
        api_mode="chat_completions",
        duration_ms=1200,
        status="ok",
        usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15, "estimated_cost_usd": 0.0123},
        session_id="session-1",
        task_id="task-1",
        home=tmp_path,
    )

    summary = reliability.summarize_model_events(tmp_path, limit=10)
    assert summary["events_count"] == 1
    item = summary["models"][0]
    assert item["provider"] == "openai"
    assert item["model"] == "gpt-test"
    assert item["api_calls"] == 1
    assert item["avg_duration_ms"] == 1200
    assert item["input_tokens"] == 10
    assert item["output_tokens"] == 5
    assert item["estimated_cost_usd"] == 0.0123


def test_record_gateway_event_summarizes_delivery_status(tmp_path: Path):
    reliability.record_gateway_delivery_event(
        platform="telegram",
        target="telegram:123",
        status="delivery_error",
        duration_ms=9,
        error="boom",
        content_chars=1234,
        job_id="job-1",
        home=tmp_path,
    )

    summary = reliability.summarize_gateway_events(tmp_path, limit=10)
    assert summary["events_count"] == 1
    assert summary["status_counts"] == {"delivery_error": 1}
    item = summary["platforms"][0]
    assert item["platform"] == "telegram"
    assert item["deliveries"] == 1
    assert item["delivery_errors"] == 1
    assert item["avg_duration_ms"] == 9
    assert "boom" in summary["recent_errors"][0]["error"]


def test_build_doctor_summary_warns_when_history_and_gateway_events_absent(monkeypatch):
    class Job:
        enabled = True

    monkeypatch.setattr("cron.jobs.load_jobs", lambda: [Job()])
    summary = reliability.build_doctor_summary({
        "cron": {"history_count": 0},
        "tools": {"instrumented_calls": {"events_count": 3, "status_counts": {"ok": 3}}},
        "models": {"instrumented_calls": {"events_count": 2, "status_counts": {"ok": 2}}},
        "gateway": {"events_count": 0, "status_counts": {}},
        "errors": {"fingerprints": []},
    })

    assert summary["status"] == "warn"
    checks = {item["id"]: item for item in summary["checks"]}
    assert checks["cron_history_present"]["status"] == "warn"
    assert checks["gateway_delivery_events_present"]["status"] == "warn"
    assert checks["tool_events_present"]["status"] == "ok"
    assert checks["model_events_present"]["status"] == "ok"


def test_build_doctor_summary_flags_repeated_error_fingerprints(monkeypatch):
    monkeypatch.setattr("cron.jobs.load_jobs", lambda: [])
    summary = reliability.build_doctor_summary({
        "cron": {"history_count": 1},
        "tools": {"instrumented_calls": {"events_count": 1, "status_counts": {"ok": 1}}},
        "models": {"instrumented_calls": {"events_count": 1, "status_counts": {"ok": 1}}},
        "gateway": {"events_count": 1, "status_counts": {"delivered": 1}},
        "errors": {"fingerprints": [{"fingerprint": "boom", "count": 4}]},
    })

    checks = {item["id"]: item for item in summary["checks"]}
    assert summary["status"] == "warn"
    assert checks["recent_error_fingerprints"]["status"] == "warn"
    assert checks["gateway_delivery_events_present"]["status"] == "ok"
