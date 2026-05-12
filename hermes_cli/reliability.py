"""Local-first Hermes reliability summaries and event helpers.

This module intentionally derives read-only observability from local Hermes state:
state.db, cron run history JSONL, and logs. It is a lightweight local truth
layer for Personal OS reliability; external tracing systems can be layered on
later.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sqlite3
import uuid
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from hermes_constants import get_hermes_home

ERROR_WORDS = ("critical", "error", "exception", "traceback", "failed", "failure", "warning", "timeout")
LOG_FILES = (
    "agent.log",
    "errors.log",
    "gateway.log",
    "gateway.error.log",
    "mcp-stderr.log",
    "tui_gateway_crash.log",
)


def _home() -> Path:
    return get_hermes_home().resolve()


def observability_dir(home: Path | None = None) -> Path:
    return (home or _home()) / "observability"


def events_file(home: Path | None = None) -> Path:
    return observability_dir(home) / "events.jsonl"


def _now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat()


def append_event(event: dict[str, Any], *, home: Path | None = None) -> dict[str, Any]:
    """Append one structured local observability event.

    Event append is deliberately simple and local. Callers should keep payloads
    bounded and avoid raw prompt/output bodies.
    """
    record = dict(event)
    record.setdefault("schema_version", 1)
    record.setdefault("event_id", uuid.uuid4().hex)
    record.setdefault("recorded_at", _now_iso())
    path = events_file(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return record


def read_jsonl(path: Path, *, limit: int | None = None) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    if limit is not None and limit > 0:
        lines = lines[-limit:]
    for line in lines:
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            rows.append(data)
    return rows


def read_events(*, home: Path | None = None, limit: int = 200) -> list[dict[str, Any]]:
    return read_jsonl(events_file(home), limit=limit)


def _event_status_from_result(result: Any) -> str:
    if isinstance(result, str):
        try:
            decoded = json.loads(result)
        except Exception:
            return "ok"
    else:
        decoded = result
    if isinstance(decoded, dict):
        if decoded.get("error") or decoded.get("success") is False:
            return "error"
    return "ok"


def _bounded_error(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).replace("\n", " ").strip()
    return text[:500] if text else None


def _usage_int(usage: dict[str, Any] | None, *keys: str) -> int:
    if not isinstance(usage, dict):
        return 0
    for key in keys:
        if usage.get(key) is not None:
            try:
                return int(usage.get(key) or 0)
            except Exception:
                return 0
    return 0


def _usage_float(usage: dict[str, Any] | None, *keys: str) -> float:
    if not isinstance(usage, dict):
        return 0.0
    for key in keys:
        if usage.get(key) is not None:
            try:
                return float(usage.get(key) or 0)
            except Exception:
                return 0.0
    return 0.0


def record_tool_call_event(
    *,
    tool_name: str,
    args: dict[str, Any] | None = None,
    result: Any = None,
    duration_ms: int | None = None,
    task_id: str | None = None,
    session_id: str | None = None,
    tool_call_id: str | None = None,
    status: str | None = None,
    error: str | None = None,
    home: Path | None = None,
) -> dict[str, Any]:
    """Record a bounded tool-call event without raw args/result bodies."""
    safe_args = args if isinstance(args, dict) else {}
    detected_status = status or _event_status_from_result(result)
    detected_error = error
    if detected_error is None and detected_status == "error":
        try:
            decoded = json.loads(result) if isinstance(result, str) else result
            if isinstance(decoded, dict):
                detected_error = decoded.get("error") or decoded.get("message")
        except Exception:
            detected_error = None
    return append_event(
        {
            "event_type": "tool_call",
            "tool_name": str(tool_name),
            "status": detected_status,
            "duration_ms": duration_ms,
            "task_id": task_id or None,
            "session_id": session_id or None,
            "tool_call_id": tool_call_id or None,
            "args_keys": sorted(str(k) for k in safe_args.keys()),
            "result_chars": len(result) if isinstance(result, str) else None,
            "error": _bounded_error(detected_error),
        },
        home=home,
    )


def record_model_call_event(
    *,
    model: str | None = None,
    provider: str | None = None,
    api_mode: str | None = None,
    duration_ms: int | None = None,
    duration_s: float | None = None,
    status: str = "ok",
    error: str | None = None,
    usage: dict[str, Any] | None = None,
    finish_reason: str | None = None,
    response_model: str | None = None,
    session_id: str | None = None,
    task_id: str | None = None,
    platform: str | None = None,
    api_call_count: int | None = None,
    message_count: int | None = None,
    assistant_content_chars: int | None = None,
    assistant_tool_call_count: int | None = None,
    home: Path | None = None,
) -> dict[str, Any]:
    if duration_ms is None and duration_s is not None:
        duration_ms = int(duration_s * 1000)
    usage = usage if isinstance(usage, dict) else {}
    return append_event(
        {
            "event_type": "model_call",
            "status": status,
            "model": model or response_model or "unknown",
            "response_model": response_model,
            "provider": provider or "unknown",
            "api_mode": api_mode,
            "duration_ms": duration_ms,
            "finish_reason": finish_reason,
            "session_id": session_id or None,
            "task_id": task_id or None,
            "platform": platform or None,
            "api_call_count": api_call_count,
            "message_count": message_count,
            "assistant_content_chars": assistant_content_chars,
            "assistant_tool_call_count": assistant_tool_call_count,
            "input_tokens": _usage_int(usage, "input_tokens", "prompt_tokens", "input"),
            "output_tokens": _usage_int(usage, "output_tokens", "completion_tokens", "output"),
            "cache_read_tokens": _usage_int(usage, "cache_read_tokens", "cache_read_input_tokens"),
            "cache_write_tokens": _usage_int(usage, "cache_write_tokens", "cache_creation_input_tokens"),
            "reasoning_tokens": _usage_int(usage, "reasoning_tokens"),
            "total_tokens": _usage_int(usage, "total_tokens", "total"),
            "estimated_cost_usd": _usage_float(usage, "estimated_cost_usd", "cost_usd", "cost"),
            "error": _bounded_error(error),
        },
        home=home,
    )


def record_gateway_delivery_event(
    *,
    platform: str,
    target: str,
    status: str,
    duration_ms: int | None = None,
    error: str | None = None,
    content_chars: int | None = None,
    job_id: str | None = None,
    job_name: str | None = None,
    session_id: str | None = None,
    message_id: str | None = None,
    home: Path | None = None,
) -> dict[str, Any]:
    return append_event(
        {
            "event_type": "gateway_delivery",
            "platform": platform,
            "target": target,
            "status": status,
            "duration_ms": duration_ms,
            "content_chars": content_chars,
            "job_id": job_id or None,
            "job_name": job_name or None,
            "session_id": session_id or None,
            "message_id": message_id or None,
            "error": _bounded_error(error),
        },
        home=home,
    )


def _state_db(home: Path) -> Path:
    return home / "state.db"


def _connect_state(home: Path) -> sqlite3.Connection | None:
    db = _state_db(home)
    if not db.exists():
        return None
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    row = con.execute("select 1 from sqlite_master where type='table' and name=?", (table,)).fetchone()
    return row is not None


def _since_ts(days: int) -> float:
    return dt.datetime.now().timestamp() - max(1, days) * 86400


def _parse_tool_calls(raw: Any) -> Iterable[tuple[str, dict[str, Any]]]:
    if not raw:
        return
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return
    calls = data if isinstance(data, list) else [data]
    for call in calls:
        if not isinstance(call, dict):
            continue
        name = call.get("name") or (call.get("function") or {}).get("name")
        raw_args = call.get("arguments") or (call.get("function") or {}).get("arguments") or call.get("args")
        args: dict[str, Any] = {}
        if isinstance(raw_args, str):
            try:
                decoded = json.loads(raw_args)
                if isinstance(decoded, dict):
                    args = decoded
            except Exception:
                args = {}
        elif isinstance(raw_args, dict):
            args = raw_args
        if name:
            yield str(name), args


def summarize_tools(home: Path, days: int, limit: int) -> dict[str, Any]:
    con = _connect_state(home)
    if con is None:
        return {"ok": False, "error": "state.db missing", "tools": []}
    since = _since_ts(days)
    counts: Counter[str] = Counter()
    skill_counts: Counter[str] = Counter()
    try:
        if not _table_exists(con, "messages"):
            return {"ok": False, "error": "messages table missing", "tools": []}
        for row in con.execute("select tool_calls from messages where timestamp >= ? and tool_calls is not null", (since,)):
            for name, args in _parse_tool_calls(row["tool_calls"]):
                counts[name] += 1
                if name == "skill_view" or name.endswith("skill_view"):
                    skill_name = args.get("name")
                    if skill_name:
                        skill_counts[str(skill_name)] += 1
    finally:
        con.close()
    return {
        "ok": True,
        "window_days": days,
        "tools": [{"name": name, "count": count} for name, count in counts.most_common(limit)],
        "skills": [{"name": name, "count": count} for name, count in skill_counts.most_common(limit)],
        "instrumented_calls": summarize_tool_events(home, limit),
        "note": "Tool usage is derived from assistant tool_calls in state.db; instrumented_calls comes from bounded local observability/tool_call events with latency/status.",
    }


def summarize_tool_events(home: Path, limit: int) -> dict[str, Any]:
    rows = [row for row in read_events(home=home, limit=5000) if row.get("event_type") == "tool_call"]
    grouped: dict[str, dict[str, Any]] = {}
    status_counts: Counter[str] = Counter()
    for row in rows:
        name = str(row.get("tool_name") or "unknown")
        status = str(row.get("status") or "unknown")
        status_counts[status] += 1
        item = grouped.setdefault(name, {
            "name": name,
            "calls": 0,
            "errors": 0,
            "avg_duration_ms": None,
            "_duration_total": 0,
            "_duration_count": 0,
            "last_status": None,
            "last_recorded_at": None,
        })
        item["calls"] += 1
        if status != "ok":
            item["errors"] += 1
        item["last_status"] = status
        item["last_recorded_at"] = row.get("recorded_at")
        if row.get("duration_ms") is not None:
            try:
                item["_duration_total"] += int(row.get("duration_ms") or 0)
                item["_duration_count"] += 1
            except Exception:
                pass
    tools = list(grouped.values())
    for item in tools:
        if item["_duration_count"]:
            item["avg_duration_ms"] = int(item["_duration_total"] / item["_duration_count"])
        item.pop("_duration_total", None)
        item.pop("_duration_count", None)
    tools.sort(key=lambda item: (item["calls"], item.get("last_recorded_at") or ""), reverse=True)
    return {"ok": True, "events_count": len(rows), "status_counts": dict(status_counts), "tools": tools[:max(1, limit)]}


def summarize_model_events(home: Path, limit: int) -> dict[str, Any]:
    rows = [row for row in read_events(home=home, limit=5000) if row.get("event_type") == "model_call"]
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    status_counts: Counter[str] = Counter()
    for row in rows:
        provider = str(row.get("provider") or "unknown")
        model = str(row.get("model") or row.get("response_model") or "unknown")
        status = str(row.get("status") or "unknown")
        status_counts[status] += 1
        item = grouped.setdefault((provider, model), {
            "provider": provider,
            "model": model,
            "api_calls": 0,
            "errors": 0,
            "avg_duration_ms": None,
            "_duration_total": 0,
            "_duration_count": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "reasoning_tokens": 0,
            "total_tokens": 0,
            "estimated_cost_usd": 0.0,
            "last_status": None,
            "last_recorded_at": None,
        })
        item["api_calls"] += 1
        if status != "ok":
            item["errors"] += 1
        item["last_status"] = status
        item["last_recorded_at"] = row.get("recorded_at")
        if row.get("duration_ms") is not None:
            try:
                item["_duration_total"] += int(row.get("duration_ms") or 0)
                item["_duration_count"] += 1
            except Exception:
                pass
        for field in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens", "total_tokens"):
            try:
                item[field] += int(row.get(field) or 0)
            except Exception:
                pass
        try:
            item["estimated_cost_usd"] += float(row.get("estimated_cost_usd") or 0)
        except Exception:
            pass
    models = list(grouped.values())
    for item in models:
        if not item["total_tokens"]:
            item["total_tokens"] = sum(int(item[k] or 0) for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens"))
        if item["_duration_count"]:
            item["avg_duration_ms"] = int(item["_duration_total"] / item["_duration_count"])
        item.pop("_duration_total", None)
        item.pop("_duration_count", None)
    models.sort(key=lambda item: (item["api_calls"], item.get("last_recorded_at") or ""), reverse=True)
    return {"ok": True, "events_count": len(rows), "status_counts": dict(status_counts), "models": models[:max(1, limit)]}


def summarize_gateway_events(home: Path, limit: int) -> dict[str, Any]:
    rows = [row for row in read_events(home=home, limit=5000) if row.get("event_type") == "gateway_delivery"]
    grouped: dict[str, dict[str, Any]] = {}
    status_counts: Counter[str] = Counter()
    recent_errors: list[dict[str, Any]] = []
    for row in rows:
        platform = str(row.get("platform") or "unknown")
        status = str(row.get("status") or "unknown")
        status_counts[status] += 1
        item = grouped.setdefault(platform, {
            "platform": platform,
            "deliveries": 0,
            "delivery_errors": 0,
            "avg_duration_ms": None,
            "_duration_total": 0,
            "_duration_count": 0,
            "last_status": None,
            "last_recorded_at": None,
        })
        item["deliveries"] += 1
        if status not in {"delivered", "saved_local"}:
            item["delivery_errors"] += 1
            if row.get("error"):
                recent_errors.append({"platform": platform, "target": row.get("target"), "error": row.get("error"), "recorded_at": row.get("recorded_at")})
        item["last_status"] = status
        item["last_recorded_at"] = row.get("recorded_at")
        if row.get("duration_ms") is not None:
            try:
                item["_duration_total"] += int(row.get("duration_ms") or 0)
                item["_duration_count"] += 1
            except Exception:
                pass
    platforms = list(grouped.values())
    for item in platforms:
        if item["_duration_count"]:
            item["avg_duration_ms"] = int(item["_duration_total"] / item["_duration_count"])
        item.pop("_duration_total", None)
        item.pop("_duration_count", None)
    platforms.sort(key=lambda item: (item["deliveries"], item.get("last_recorded_at") or ""), reverse=True)
    return {"ok": True, "events_count": len(rows), "status_counts": dict(status_counts), "platforms": platforms[:max(1, limit)], "recent_errors": recent_errors[-max(1, limit):]}


def summarize_models(home: Path, days: int, limit: int) -> dict[str, Any]:
    con = _connect_state(home)
    if con is None:
        base = {"ok": False, "error": "state.db missing", "models": []}
    else:
        since = _since_ts(days)
        try:
            if not _table_exists(con, "sessions"):
                base = {"ok": False, "error": "sessions table missing", "models": []}
            else:
                rows = list(con.execute("select * from sessions where started_at >= ?", (since,)))
                grouped: dict[tuple[str, str], dict[str, Any]] = {}
                for row in rows:
                    data = dict(row)
                    provider = str(data.get("billing_provider") or data.get("provider") or "unknown")
                    model = str(data.get("model") or "unknown")
                    item = grouped.setdefault((provider, model), {
                        "provider": provider,
                        "model": model,
                        "sessions": 0,
                        "api_calls": 0,
                        "tool_calls": 0,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cache_read_tokens": 0,
                        "cache_write_tokens": 0,
                        "reasoning_tokens": 0,
                        "estimated_cost_usd": 0.0,
                        "actual_cost_usd": 0.0,
                    })
                    item["sessions"] += 1
                    item["api_calls"] += int(data.get("api_call_count") or 0)
                    item["tool_calls"] += int(data.get("tool_call_count") or 0)
                    for field in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens"):
                        item[field] += int(data.get(field) or 0)
                    for field in ("estimated_cost_usd", "actual_cost_usd"):
                        item[field] += float(data.get(field) or 0)
                models = list(grouped.values())
                for item in models:
                    item["total_tokens"] = sum(int(item[k] or 0) for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens"))
                models.sort(key=lambda item: (item["sessions"], item["api_calls"]), reverse=True)
                base = {"ok": True, "window_days": days, "models": models[: max(1, limit)]}
        finally:
            con.close()
    base["instrumented_calls"] = summarize_model_events(home, limit)
    return base


def summarize_cron(home: Path, limit: int) -> dict[str, Any]:
    # Prefer the new cron helper when available so CLI and report semantics match.
    rows: list[dict[str, Any]] = []
    try:
        from cron.jobs import list_run_history

        rows = list_run_history(limit=limit)
    except Exception:
        rows = read_jsonl(home / "cron" / "runs.jsonl", limit=limit)

    counts: Counter[str] = Counter(str(row.get("status") or "unknown") for row in rows)
    by_job: dict[str, dict[str, Any]] = {}
    for row in sorted(rows, key=lambda r: str(r.get("started_at") or ""), reverse=True):
        job_id = str(row.get("job_id") or "unknown")
        info = by_job.setdefault(job_id, {
            "job_id": job_id,
            "name": row.get("job_name"),
            "runs": 0,
            "failures": 0,
            "delivery_errors": 0,
            "silent_runs": 0,
            "last_status": None,
            "last_started_at": None,
            "avg_duration_ms": None,
            "_duration_total": 0,
            "_duration_count": 0,
        })
        info["runs"] += 1
        status = str(row.get("status") or "unknown")
        if info["last_status"] is None:
            info["last_status"] = status
            info["last_started_at"] = row.get("started_at")
        if status not in {"ok", "silent"}:
            info["failures"] += 1
        if status == "delivery_error" or row.get("delivery_status") == "delivery_error":
            info["delivery_errors"] += 1
        if status == "silent" or row.get("silent"):
            info["silent_runs"] += 1
        if row.get("duration_ms") is not None:
            try:
                info["_duration_total"] += int(row.get("duration_ms") or 0)
                info["_duration_count"] += 1
            except Exception:
                pass
    jobs = []
    for info in by_job.values():
        if info["_duration_count"]:
            info["avg_duration_ms"] = int(info["_duration_total"] / info["_duration_count"])
        info.pop("_duration_total", None)
        info.pop("_duration_count", None)
        jobs.append(info)
    jobs.sort(key=lambda j: str(j.get("last_started_at") or ""), reverse=True)
    return {
        "ok": True,
        "history_file": str(home / "cron" / "runs.jsonl"),
        "history_count": len(rows),
        "status_counts": dict(counts),
        "recent_runs": rows[: min(10, len(rows))],
        "jobs": jobs[:limit],
    }


def _normalize_error_line(line: str) -> str:
    line = line.strip()
    line = re.sub(r"\b\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b", "<ts>", line)
    line = re.sub(r"\b\d+(?:\.\d+)?\b", "<num>", line)
    return line[:220]


def summarize_errors(home: Path, limit: int) -> dict[str, Any]:
    logs = home / "logs"
    if not logs.exists():
        return {"ok": False, "error": "logs directory missing", "fingerprints": []}
    counts: Counter[str] = Counter()
    examples: list[dict[str, str]] = []
    for name in LOG_FILES:
        path = logs / name
        if not path.exists():
            continue
        try:
            text = path.read_text(errors="replace")[-512_000:]
        except OSError:
            continue
        for line in text.splitlines()[-3000:]:
            if any(word in line.lower() for word in ERROR_WORDS):
                fp = _normalize_error_line(line)
                counts[fp] += 1
                if len(examples) < limit:
                    examples.append({"file": name, "line": line[-500:]})
    return {
        "ok": True,
        "fingerprints": [{"fingerprint": fp, "count": count} for fp, count in counts.most_common(limit)],
        "examples": examples[-limit:],
    }


def build_snapshot(*, days: int = 30, limit: int = 20, home: Path | None = None) -> dict[str, Any]:
    root = (home or _home()).resolve()
    return {
        "schema_version": 1,
        "generated_at": _now_iso(),
        "home": str(root),
        "window_days": max(1, days),
        "sources": {
            "cron": summarize_cron(root, limit),
            "tools": summarize_tools(root, days, limit),
            "models": summarize_models(root, days, limit),
            "gateway": summarize_gateway_events(root, limit),
            "errors": summarize_errors(root, limit),
            "events": {"path": str(events_file(root)), "recent": read_events(home=root, limit=min(limit, 50))},
        },
        "local_truth_note": "Local reliability is derived from state.db, cron/runs.jsonl, logs, and optional observability/events.jsonl. Langfuse/Sentry/Otel are optional external layers, not the source of truth.",
    }


def export_snapshot_event(snapshot: dict[str, Any], *, home: Path | None = None) -> dict[str, Any]:
    cron = snapshot.get("sources", {}).get("cron", {})
    tools = snapshot.get("sources", {}).get("tools", {})
    models = snapshot.get("sources", {}).get("models", {})
    errors = snapshot.get("sources", {}).get("errors", {})
    return append_event(
        {
            "event_type": "reliability_snapshot",
            "status": "ok",
            "window_days": snapshot.get("window_days"),
            "cron_status_counts": cron.get("status_counts", {}),
            "cron_history_count": cron.get("history_count", 0),
            "top_tools": (tools.get("tools") or [])[:10],
            "top_models": (models.get("models") or [])[:5],
            "error_fingerprint_count": len(errors.get("fingerprints") or []),
        },
        home=home,
    )


def _print_kv(label: str, value: Any) -> None:
    print(f"{label}: {value if value not in (None, '') else '-'}")


def print_terminal(snapshot: dict[str, Any], view: str) -> None:
    sources = snapshot.get("sources", {})
    if view in {"summary", "cron"}:
        cron = sources.get("cron", {})
        print("Cron reliability")
        _print_kv("history_file", cron.get("history_file"))
        _print_kv("history_count", cron.get("history_count"))
        _print_kv("status_counts", cron.get("status_counts"))
        for job in (cron.get("jobs") or [])[:10]:
            print(f"- {job.get('name') or job.get('job_id')} ({job.get('job_id')}): last={job.get('last_status')} runs={job.get('runs')} failures={job.get('failures')} avg_ms={job.get('avg_duration_ms')}")
    if view in {"summary", "tools"}:
        tools = sources.get("tools", {})
        print("\nTool usage")
        print(tools.get("note") or "")
        for item in (tools.get("tools") or [])[:20]:
            print(f"- {item.get('name')}: {item.get('count')}")
        instrumented = tools.get("instrumented_calls") or {}
        if instrumented.get("events_count"):
            print("Instrumented tool calls")
            _print_kv("event_count", instrumented.get("events_count"))
            _print_kv("status_counts", instrumented.get("status_counts"))
            for item in (instrumented.get("tools") or [])[:20]:
                print(f"- {item.get('name')}: calls={item.get('calls')} errors={item.get('errors')} avg_ms={item.get('avg_duration_ms')} last={item.get('last_status')}")
    if view in {"summary", "models"}:
        models = sources.get("models", {})
        print("\nModel usage")
        for item in (models.get("models") or [])[:20]:
            print(f"- {item.get('provider')} / {item.get('model')}: sessions={item.get('sessions')} api_calls={item.get('api_calls')} tokens={item.get('total_tokens')} est=${float(item.get('estimated_cost_usd') or 0):.4f}")
        instrumented = models.get("instrumented_calls") or {}
        if instrumented.get("events_count"):
            print("Instrumented model calls")
            _print_kv("event_count", instrumented.get("events_count"))
            _print_kv("status_counts", instrumented.get("status_counts"))
            for item in (instrumented.get("models") or [])[:20]:
                print(f"- {item.get('provider')} / {item.get('model')}: api_calls={item.get('api_calls')} errors={item.get('errors')} tokens={item.get('total_tokens')} avg_ms={item.get('avg_duration_ms')} est=${float(item.get('estimated_cost_usd') or 0):.4f}")
    if view in {"summary", "gateway"}:
        gateway = sources.get("gateway", {})
        print("\nGateway delivery events")
        _print_kv("event_count", gateway.get("events_count"))
        _print_kv("status_counts", gateway.get("status_counts"))
        for item in (gateway.get("platforms") or [])[:20]:
            print(f"- {item.get('platform')}: deliveries={item.get('deliveries')} errors={item.get('delivery_errors')} avg_ms={item.get('avg_duration_ms')} last={item.get('last_status')}")
        if gateway.get("recent_errors"):
            print("Recent delivery errors")
            for item in (gateway.get("recent_errors") or [])[:10]:
                print(f"- {item.get('platform')} {item.get('target')}: {item.get('error')}")
    if view in {"summary", "errors"}:
        errors = sources.get("errors", {})
        print("\nError fingerprints")
        for item in (errors.get("fingerprints") or [])[:20]:
            print(f"- {item.get('count')}x {item.get('fingerprint')}")


def run_cli(args: Any) -> int:
    view = getattr(args, "reliability_command", None) or "summary"
    if view == "export":
        view = "summary"
    snapshot = build_snapshot(days=getattr(args, "days", 30), limit=getattr(args, "limit", 20))
    if getattr(args, "export_events", False):
        snapshot["exported_event"] = export_snapshot_event(snapshot)
    if getattr(args, "json", False):
        print(json.dumps(snapshot if view == "summary" else snapshot.get("sources", {}).get(view, {}), indent=2, ensure_ascii=False))
    else:
        print_terminal(snapshot, view)
        print("\nLocal truth note:")
        print(snapshot["local_truth_note"])
    return 0
