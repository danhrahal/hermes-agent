"""AFK alerting and pending-prompt registry helpers.

This module is intentionally small and dependency-light so CLI, gateway,
cron, and tools can share AFK behavior without importing the full TUI stack.
All delivery is best-effort: AFK failures must never break the primary action.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home
from utils import is_truthy_value

logger = logging.getLogger(__name__)

DEFAULT_AFK_CONFIG: dict[str, Any] = {
    "enabled_by_default": False,
    "target": "",
    "escalate_after_seconds": 120,
    "alert_on": ["unanswered_question", "blocker", "approval_needed", "job_done"],
}

_PENDING_FILE_NAME = "afk_pending_questions.json"


def _load_global_config() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _normalize_alert_on(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = [value]
    normalized: list[str] = []
    for item in items:
        text = str(item).strip().lower()
        if text:
            normalized.append(text)
    return normalized


def load_afk_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return normalized AFK config with non-sending defaults."""
    base = dict(DEFAULT_AFK_CONFIG)
    source = config if isinstance(config, dict) else _load_global_config()
    afk_cfg = source.get("afk", {}) if isinstance(source, dict) else {}
    if isinstance(afk_cfg, dict):
        base.update(afk_cfg)
    base["enabled_by_default"] = is_truthy_value(base.get("enabled_by_default", False))
    base["target"] = str(base.get("target", "") or "").strip()
    base["alert_on"] = _normalize_alert_on(base.get("alert_on", []))
    try:
        timeout = int(float(base.get("escalate_after_seconds", 120)))
    except (TypeError, ValueError):
        timeout = 120
    base["escalate_after_seconds"] = max(timeout, 1)
    return base


def afk_enabled(config: dict[str, Any] | None = None) -> bool:
    cfg = load_afk_config(config)
    return bool(cfg.get("enabled_by_default")) and bool(cfg.get("target"))


def afk_target(config: dict[str, Any] | None = None) -> str:
    return str(load_afk_config(config).get("target", "") or "").strip()


def afk_alert_enabled(kind: str, config: dict[str, Any] | None = None) -> bool:
    cfg = load_afk_config(config)
    return bool(cfg.get("enabled_by_default")) and str(kind).strip().lower() in set(cfg.get("alert_on") or [])


def afk_escalate_after_seconds(config: dict[str, Any] | None = None, fallback: int = 120) -> int:
    if afk_alert_enabled("unanswered_question", config):
        target = afk_target(config)
        if target:
            return int(load_afk_config(config).get("escalate_after_seconds", fallback))
    try:
        fallback_int = int(float(fallback))
    except (TypeError, ValueError):
        fallback_int = 120
    return max(fallback_int, 1)


def _clean_choices(choices: list[Any] | tuple[Any, ...] | None) -> list[str]:
    return [str(choice).strip() for choice in (choices or []) if str(choice).strip()]


def _truncate(text: Any, limit: int = 500) -> str:
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)] + "…"


def format_unanswered_question_alert(
    question: str,
    choices: list[Any] | tuple[Any, ...] | None,
    timeout_seconds: int,
    *,
    pending_id: str | None = None,
) -> str:
    lines = [
        "AFK question timeout from Hermes CLI.",
        "",
        f"Question: {_truncate(question, 1200)}",
    ]
    cleaned_choices = _clean_choices(choices)
    if cleaned_choices:
        lines.append("")
        lines.append("Options:")
        lines.extend(f"{idx}. {choice}" for idx, choice in enumerate(cleaned_choices, start=1))
    if pending_id:
        lines.extend([
            "",
            f"Pending ID: {pending_id}",
            f"Reply with: /afk answer {pending_id} <choice number or text>",
        ])
    lines.extend([
        "",
        f"No local answer arrived after {timeout_seconds}s.",
        "Safe default: Hermes will use best judgement and proceed unless the decision is unsafe or explicitly requires approval.",
        "Continue: answer in the CLI/TUI to unblock this exact session. A Discord reply starts/continues the Discord session unless this task was launched from Discord.",
    ])
    return "\n".join(lines)


def format_approval_needed_alert(
    command: str,
    description: str,
    choices: list[str],
    *,
    pending_id: str | None = None,
    timeout_seconds: int = 60,
) -> str:
    lines = [
        "AFK approval needed from Hermes CLI.",
        "",
        f"Description: {_truncate(description, 800)}",
        f"Command: {_truncate(command, 1000)}",
        "",
        "Allowed responses:",
    ]
    for choice in choices:
        if choice != "view":
            lines.append(f"- {choice}")
    if pending_id:
        lines.extend([
            "",
            f"Pending ID: {pending_id}",
            f"Reply with: /afk answer {pending_id} once|session|always|deny",
        ])
    lines.extend([
        "",
        f"If no answer arrives after {timeout_seconds}s, Hermes will deny the command.",
        "Safety: vague Discord replies are ignored; approvals require an exact allowed word.",
    ])
    return "\n".join(lines)


def send_afk_alert(
    kind: str,
    title: str,
    body_lines: list[str] | tuple[str, ...] | str,
    *,
    target: str | None = None,
    config: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Best-effort AFK alert delivery via the messaging tool."""
    cfg = load_afk_config(config)
    resolved_target = (target or cfg.get("target") or "").strip()
    if not resolved_target:
        return {"sent": False, "target": "", "error": "missing target"}
    if not cfg.get("enabled_by_default") or str(kind).strip().lower() not in set(cfg.get("alert_on") or []):
        return {"sent": False, "target": resolved_target, "error": "alert disabled"}

    if isinstance(body_lines, str):
        body = body_lines
    else:
        body = "\n".join(str(line) for line in body_lines)
    message = f"{title}\n{body}" if title and title not in body[:120] else body
    if metadata:
        compact_meta = {k: v for k, v in metadata.items() if v not in (None, "")}
        if compact_meta:
            message += "\n\nMetadata: " + json.dumps(compact_meta, ensure_ascii=False, sort_keys=True)

    try:
        from tools.send_message_tool import send_message_tool

        raw = send_message_tool({"action": "send", "target": resolved_target, "message": message})
        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            payload = {}
        if isinstance(payload, dict) and (payload.get("error") or payload.get("success") is False):
            return {"sent": False, "target": resolved_target, "error": payload.get("error") or payload.get("message") or "send failed"}
        return {"sent": True, "target": resolved_target, "error": None, "message_id": (payload or {}).get("message_id") if isinstance(payload, dict) else None}
    except Exception as exc:
        logger.warning("AFK %s alert failed: %s", kind, exc)
        return {"sent": False, "target": resolved_target, "error": str(exc)}


def pending_registry_path() -> Path:
    return get_hermes_home() / "state" / _PENDING_FILE_NAME


def _now_ts() -> float:
    return time.time()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _read_registry() -> dict[str, Any]:
    path = pending_registry_path()
    if not path.exists():
        return {"prompts": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("prompts"), dict):
            return data
    except Exception as exc:
        logger.warning("Failed to read AFK pending registry %s: %s", path, exc)
    return {"prompts": {}}


def _write_registry(data: dict[str, Any]) -> None:
    path = pending_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp_name, path)
    finally:
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except Exception:
            pass


def create_pending_prompt(
    kind: str,
    question: str,
    choices: list[Any] | tuple[Any, ...] | None = None,
    *,
    session_id: str | None = None,
    source: str = "cli",
    safe_default: str | None = None,
    ttl_seconds: int = 900,
    metadata: dict[str, Any] | None = None,
) -> str:
    now = _now_ts()
    try:
        ttl = max(int(float(ttl_seconds)), 1)
    except (TypeError, ValueError):
        ttl = 900
    prompt_id = "afkq_" + datetime.fromtimestamp(now).strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]
    data = _read_registry()
    prompts = data.setdefault("prompts", {})
    prompts[prompt_id] = {
        "id": prompt_id,
        "kind": str(kind).strip().lower(),
        "session_id": str(session_id or ""),
        "source": str(source or "cli"),
        "question": _truncate(question, 1600),
        "choices": _clean_choices(choices),
        "safe_default": _truncate(safe_default, 500),
        "created_at": _iso(now),
        "expires_at": _iso(now + ttl),
        "expires_ts": now + ttl,
        "status": "pending",
        "answer": None,
        "answer_source": None,
        "answered_at": None,
        "metadata": metadata or {},
    }
    _write_registry(data)
    return prompt_id


def get_pending_prompt(prompt_id: str) -> dict[str, Any] | None:
    prompt = _read_registry().get("prompts", {}).get(str(prompt_id).strip())
    return dict(prompt) if isinstance(prompt, dict) else None


def _normalize_answer_for_prompt(prompt: dict[str, Any], answer: Any) -> tuple[bool, str, str | None]:
    raw = str(answer or "").strip()
    if not raw:
        return False, "", "empty answer"
    choices = _clean_choices(prompt.get("choices") or [])
    kind = str(prompt.get("kind", "")).lower()
    if kind == "approval":
        allowed = {choice for choice in choices if choice in {"once", "session", "always", "deny"}}
        lowered = raw.lower()
        return (True, lowered, None) if lowered in allowed else (False, raw, "approval answer must be one of: " + ", ".join(sorted(allowed)))
    if choices:
        if raw.isdigit():
            idx = int(raw)
            if 1 <= idx <= len(choices):
                return True, choices[idx - 1], None
        lowered = raw.lower()
        for choice in choices:
            if lowered == choice.lower():
                return True, choice, None
        return False, raw, "answer must match a choice number or exact choice text"
    return True, raw, None


def answer_pending_prompt(prompt_id: str, answer: Any, answer_source: str = "gateway") -> dict[str, Any]:
    data = _read_registry()
    prompts = data.setdefault("prompts", {})
    key = str(prompt_id).strip()
    prompt = prompts.get(key)
    if not isinstance(prompt, dict):
        return {"ok": False, "error": "pending prompt not found", "id": key}
    if prompt.get("status") != "pending":
        return {"ok": False, "error": f"pending prompt is {prompt.get('status')}", "id": key, "prompt": dict(prompt)}
    if float(prompt.get("expires_ts") or 0) <= _now_ts():
        prompt["status"] = "expired"
        _write_registry(data)
        return {"ok": False, "error": "pending prompt expired", "id": key, "prompt": dict(prompt)}
    ok, normalized, error = _normalize_answer_for_prompt(prompt, answer)
    if not ok:
        return {"ok": False, "error": error or "invalid answer", "id": key, "prompt": dict(prompt)}
    prompt["answer"] = normalized
    prompt["answer_source"] = str(answer_source or "gateway")
    prompt["answered_at"] = _iso(_now_ts())
    prompt["status"] = "answered"
    _write_registry(data)
    return {"ok": True, "id": key, "answer": normalized, "prompt": dict(prompt)}


def close_pending_prompt(prompt_id: str | None, status: str = "closed") -> dict[str, Any]:
    if not prompt_id:
        return {"ok": False, "error": "missing id"}
    data = _read_registry()
    prompts = data.setdefault("prompts", {})
    key = str(prompt_id).strip()
    prompt = prompts.get(key)
    if not isinstance(prompt, dict):
        return {"ok": False, "error": "pending prompt not found", "id": key}
    prompt["status"] = str(status or "closed")
    prompt["closed_at"] = _iso(_now_ts())
    _write_registry(data)
    return {"ok": True, "id": key, "prompt": dict(prompt)}


def expire_old_pending_prompts(now: float | None = None) -> int:
    ts = _now_ts() if now is None else float(now)
    data = _read_registry()
    count = 0
    for prompt in data.setdefault("prompts", {}).values():
        if isinstance(prompt, dict) and prompt.get("status") == "pending" and float(prompt.get("expires_ts") or 0) <= ts:
            prompt["status"] = "expired"
            count += 1
    if count:
        _write_registry(data)
    return count


def list_pending_prompts(include_non_pending: bool = False) -> list[dict[str, Any]]:
    expire_old_pending_prompts()
    prompts = _read_registry().get("prompts", {})
    out = []
    for prompt in prompts.values():
        if not isinstance(prompt, dict):
            continue
        if include_non_pending or prompt.get("status") == "pending":
            out.append(dict(prompt))
    return sorted(out, key=lambda p: str(p.get("created_at", "")), reverse=True)


def poll_answer(prompt_id: str | None) -> str | None:
    if not prompt_id:
        return None
    prompt = get_pending_prompt(prompt_id)
    if isinstance(prompt, dict) and prompt.get("status") == "answered":
        answer = prompt.get("answer")
        return str(answer) if answer is not None else ""
    return None
