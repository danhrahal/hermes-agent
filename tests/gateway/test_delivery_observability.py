from __future__ import annotations

import asyncio

from hermes_cli import reliability
from gateway.config import GatewayConfig, Platform
from gateway.delivery import DeliveryRouter, DeliveryTarget


class _Adapter:
    async def send(self, chat_id, content, metadata=None):
        return {"message_id": "m1", "chat_id": chat_id, "metadata": metadata}


class _FailingAdapter:
    async def send(self, chat_id, content, metadata=None):
        raise RuntimeError("send exploded")


def test_delivery_router_records_gateway_delivery_success(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    router = DeliveryRouter(GatewayConfig(), adapters={Platform.TELEGRAM: _Adapter()})

    result = asyncio.run(router.deliver(
        "hello",
        [DeliveryTarget(platform=Platform.TELEGRAM, chat_id="123")],
        job_id="job-1",
        job_name="Demo Job",
    ))

    assert result["telegram:123"]["success"] is True
    rows = reliability.read_events(home=tmp_path, limit=10)
    assert len(rows) == 1
    row = rows[0]
    assert row["event_type"] == "gateway_delivery"
    assert row["platform"] == "telegram"
    assert row["target"] == "telegram:123"
    assert row["status"] == "delivered"
    assert row["content_chars"] == 5
    assert row["job_id"] == "job-1"


def test_delivery_router_records_gateway_delivery_error(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    router = DeliveryRouter(GatewayConfig(), adapters={Platform.TELEGRAM: _FailingAdapter()})

    result = asyncio.run(router.deliver("hello", [DeliveryTarget(platform=Platform.TELEGRAM, chat_id="123")]))

    assert result["telegram:123"]["success"] is False
    rows = reliability.read_events(home=tmp_path, limit=10)
    assert len(rows) == 1
    row = rows[0]
    assert row["event_type"] == "gateway_delivery"
    assert row["status"] == "delivery_error"
    assert "send exploded" in row["error"]
