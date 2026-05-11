"""Tests for CLI AFK escalation behavior."""

from unittest.mock import patch

import cli


def _minimal_cli():
    shell = cli.HermesCLI.__new__(cli.HermesCLI)
    return shell


def test_clarify_timeout_uses_afk_escalation_when_enabled(monkeypatch):
    cfg = {
        "clarify": {"timeout": 300},
        "afk": {
            "enabled_by_default": True,
            "target": "discord:#main-hermes",
            "escalate_after_seconds": 7,
            "alert_on": ["unanswered_question"],
        },
    }
    monkeypatch.setattr(cli, "CLI_CONFIG", cfg)

    assert _minimal_cli()._clarify_timeout_seconds() == 7


def test_clarify_timeout_falls_back_when_afk_disabled(monkeypatch):
    cfg = {
        "clarify": {"timeout": 300},
        "afk": {
            "enabled_by_default": False,
            "target": "discord:#main-hermes",
            "escalate_after_seconds": 7,
            "alert_on": ["unanswered_question"],
        },
    }
    monkeypatch.setattr(cli, "CLI_CONFIG", cfg)

    assert _minimal_cli()._clarify_timeout_seconds() == 300


def test_afk_unanswered_question_alert_sends_to_configured_target(monkeypatch):
    cfg = {
        "afk": {
            "enabled_by_default": True,
            "target": "discord:#main-hermes",
            "escalate_after_seconds": 120,
            "alert_on": ["unanswered_question"],
        },
    }
    monkeypatch.setattr(cli, "CLI_CONFIG", cfg)
    sent = []

    def fake_send_message_tool(args):
        sent.append(args)
        return '{"success": true}'

    with patch("tools.send_message_tool.send_message_tool", fake_send_message_tool):
        _minimal_cli()._send_afk_unanswered_question_alert(
            "Which source should I use?",
            ["Roam", "Dropbox"],
            120,
        )

    assert len(sent) == 1
    assert sent[0]["action"] == "send"
    assert sent[0]["target"] == "discord:#main-hermes"
    message = sent[0]["message"]
    assert "AFK question timeout" in message
    assert "Question: Which source should I use?" in message
    assert "1. Roam" in message
    assert "2. Dropbox" in message
    assert "No local answer arrived after 120s" in message
    assert "Safe default" in message
    assert "answer in the CLI/TUI" in message


def test_afk_unanswered_question_alert_noops_without_target(monkeypatch):
    cfg = {
        "afk": {
            "enabled_by_default": True,
            "target": "",
            "escalate_after_seconds": 120,
            "alert_on": ["unanswered_question"],
        },
    }
    monkeypatch.setattr(cli, "CLI_CONFIG", cfg)

    with patch("tools.send_message_tool.send_message_tool") as send_message_tool:
        _minimal_cli()._send_afk_unanswered_question_alert("Q?", None, 120)

    send_message_tool.assert_not_called()
