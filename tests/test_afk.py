"""Unit tests for shared AFK helpers."""

from __future__ import annotations

import json

import afk


def test_load_afk_config_normalizes_defaults_and_alerts():
    cfg = afk.load_afk_config({"afk": {"enabled_by_default": "yes", "target": " discord:#main-hermes ", "alert_on": "unanswered_question, job_done", "escalate_after_seconds": "7"}})

    assert cfg["enabled_by_default"] is True
    assert cfg["target"] == "discord:#main-hermes"
    assert cfg["alert_on"] == ["unanswered_question", "job_done"]
    assert cfg["escalate_after_seconds"] == 7


def test_afk_escalate_falls_back_when_disabled_or_missing_target():
    assert afk.afk_escalate_after_seconds({"clarify": {}, "afk": {"enabled_by_default": False, "escalate_after_seconds": 7}}, fallback=300) == 300
    assert afk.afk_escalate_after_seconds({"afk": {"enabled_by_default": True, "target": "", "alert_on": ["unanswered_question"], "escalate_after_seconds": 7}}, fallback=300) == 300


def test_pending_prompt_registry_round_trip(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    prompt_id = afk.create_pending_prompt(
        "clarify",
        "Pick a source?",
        ["Roam", "Dropbox"],
        session_id="sess1",
        ttl_seconds=60,
    )

    prompt = afk.get_pending_prompt(prompt_id)
    assert prompt is not None
    assert prompt["status"] == "pending"
    assert prompt["choices"] == ["Roam", "Dropbox"]

    result = afk.answer_pending_prompt(prompt_id, "2", "discord:test")
    assert result["ok"] is True
    assert result["answer"] == "Dropbox"
    assert afk.poll_answer(prompt_id) == "Dropbox"

    path = afk.pending_registry_path()
    assert path == tmp_path / "state" / "afk_pending_questions.json"
    data = json.loads(path.read_text())
    assert data["prompts"][prompt_id]["answer_source"] == "discord:test"


def test_pending_prompt_rejects_invalid_choice(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    prompt_id = afk.create_pending_prompt("clarify", "Pick?", ["A", "B"], ttl_seconds=60)

    result = afk.answer_pending_prompt(prompt_id, "C", "discord:test")

    assert result["ok"] is False
    assert "choice" in result["error"]
    assert afk.get_pending_prompt(prompt_id)["status"] == "pending"


def test_approval_prompt_requires_exact_allowed_word(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    prompt_id = afk.create_pending_prompt("approval", "Approve?", ["once", "session", "deny"], ttl_seconds=60)

    vague = afk.answer_pending_prompt(prompt_id, "sure", "discord:test")
    assert vague["ok"] is False

    explicit = afk.answer_pending_prompt(prompt_id, "deny", "discord:test")
    assert explicit["ok"] is True
    assert explicit["answer"] == "deny"
