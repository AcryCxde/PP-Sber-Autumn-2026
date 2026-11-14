"""Receipt pages share I/O and preserve definitive outcomes atomically."""

import asyncio
import json
import sqlite3

import pytest

from ctrunner.gateway import DiagnosticAcks, GatewayClient, _record_diagnostic_acks
from ctrunner.harness_diagnostic import DiagnosticFeed, DiagnosticReceipts


def test_receipt_page_uses_one_connection_per_operation(tmp_path, monkeypatch):
    receipts = DiagnosticReceipts(tmp_path / "events.jsonl")
    connect = receipts._connect
    calls = []

    def counted():
        calls.append(1)
        return connect()

    monkeypatch.setattr(receipts, "_connect", counted)
    outcomes = {str(index): "accepted" for index in range(256)}
    receipts.record_many(outcomes)
    assert len(calls) == 1
    assert receipts.known_ids((*outcomes, "missing")) == set(outcomes)
    assert len(calls) == 2
    receipts.record_many(outcomes)
    with pytest.raises(ValueError, match="conflicting_diagnostic_receipt"):
        receipts.record_many({"new": "accepted", "0": "rejected"})
    assert not receipts.contains("new")


@pytest.mark.asyncio
async def test_ack_page_written_once_and_failed_write_retains_pending(tmp_path, monkeypatch):
    receipts = DiagnosticReceipts(tmp_path / "events.jsonl")
    pending = {str(index): {} for index in range(256)}
    attempts = {key: (1, 0.0) for key in pending}
    leases = dict.fromkeys(pending, 1.0)
    accepted = DiagnosticAcks()
    accepted.update(dict.fromkeys(pending, "accepted"))
    accepted.awaiting.update(pending)
    record = receipts.record_many
    calls = []

    def failed(_outcomes):
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(receipts, "record_many", failed)
    with pytest.raises(sqlite3.OperationalError):
        await _record_diagnostic_acks(accepted, receipts, pending, attempts, leases)
    assert len(pending) == 256
    assert not receipts.known_ids(tuple(pending))

    def counted(outcomes):
        calls.append(len(outcomes))
        record(outcomes)

    monkeypatch.setattr(receipts, "record_many", counted)
    accepted.update(dict.fromkeys(pending, "accepted"))
    await _record_diagnostic_acks(accepted, receipts, pending, attempts, leases)
    assert calls == [256]
    assert not pending and not leases and not attempts and not accepted.awaiting
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_reconnect_checks_historical_receipts_per_page(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    rows = [{"seq": 1, "ts": 1, "kind": "turn_started", "turn_id": "r", "payload": {}}]
    rows.extend({"seq": seq, "ts": seq, "kind": "sdk", "turn_id": "r", "payload": {
        "type": "result", "session_id": "s", "total_cost_usd": 1}} for seq in range(2, 1026))
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    feed = DiagnosticFeed(path, "p", "/workspace/project")
    receipts = DiagnosticReceipts(path)
    while not feed.caught_up():
        receipts.record_many({str(frame["diagnostic_id"]): "accepted" for frame in feed.poll()})
    connect = receipts._connect
    connections = []

    def counted():
        connections.append(1)
        return connect()

    monkeypatch.setattr(receipts, "_connect", counted)
    client = GatewayClient(
        url="ws://unused", token="test", project_id="p", runner_version="test",
        session_id=None, journal=path, health_file=tmp_path / "health",
        inbox_dir=tmp_path / "inbox", project_root="/workspace/project",
        last_seq=lambda: 1025, fork_questions=lambda _: None,
    )
    client.drain()
    client._public_flushed.set()

    class Socket:
        async def send(self, _raw):
            pytest.fail("durably received history must not be sent")

    await client._diagnostic_loop(Socket(), DiagnosticAcks(), receipts)
    assert len(connections) == 5  # 1024 results + one start, five bounded pages, not 1024 opens.
