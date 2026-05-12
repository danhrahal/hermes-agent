from __future__ import annotations

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
