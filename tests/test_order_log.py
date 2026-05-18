"""Unit tests for ``coffee_agent.order_log.OrderLog``.

Per design 12.5 / tasks.md task 28. Covers append + read_all round-trip,
parent-dir auto-create on first write, multiple records preserved in
order, and tolerant skipping of malformed lines (clause 2.17).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from coffee_agent.order_log import OrderLog
from coffee_agent.state import CartItem, OrderRecord


def _record(order_id: str, total: int = 10000) -> OrderRecord:
    return OrderRecord(
        order_id=order_id,
        session_id="s1",
        items=[CartItem(id="x", name="A", type="dish", price=10000, quantity=1)],
        total=total,
        qr_url="https://img.vietqr.io/image/MB-669699669-compact.png?amount=10000",
        created_at=datetime.now(timezone.utc),
    )


def test_append_creates_parent_dir(tmp_path):
    p = tmp_path / "logs" / "nested" / "orders.jsonl"
    assert not p.parent.exists()
    log = OrderLog(p)
    log.append(_record("a"))
    assert p.exists()
    assert p.parent.is_dir()


def test_append_then_read_all_round_trips_one_record(tmp_path):
    p = tmp_path / "orders.jsonl"
    log = OrderLog(p)
    log.append(_record("a"))
    records = list(log.read_all())
    assert len(records) == 1
    assert records[0].order_id == "a"


def test_multiple_records_preserve_append_order(tmp_path):
    p = tmp_path / "orders.jsonl"
    log = OrderLog(p)
    log.append(_record("a"))
    log.append(_record("b"))
    log.append(_record("c"))
    ids = [r.order_id for r in log.read_all()]
    assert ids == ["a", "b", "c"]


def test_read_all_on_missing_file_yields_nothing(tmp_path):
    p = tmp_path / "does_not_exist.jsonl"
    log = OrderLog(p)
    assert list(log.read_all()) == []


def test_read_all_skips_malformed_lines(tmp_path):
    p = tmp_path / "orders.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    # Pre-seed the file with one malformed payload (missing required fields).
    p.write_text(json.dumps({"bad": "data"}) + "\n", encoding="utf-8")
    log = OrderLog(p)
    log.append(_record("good"))
    ids = [r.order_id for r in log.read_all()]
    assert ids == ["good"]


def test_read_all_skips_blank_lines_and_invalid_json(tmp_path):
    p = tmp_path / "orders.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\nnot-json\n\n", encoding="utf-8")
    log = OrderLog(p)
    log.append(_record("ok"))
    ids = [r.order_id for r in log.read_all()]
    assert ids == ["ok"]


def test_clear_removes_existing_log(tmp_path):
    p = tmp_path / "orders.jsonl"
    log = OrderLog(p)
    log.append(_record("a"))
    assert p.exists()
    log.clear()
    assert not p.exists()
    assert list(log.read_all()) == []


def test_clear_when_missing_is_noop(tmp_path):
    p = tmp_path / "orders.jsonl"
    log = OrderLog(p)
    log.clear()  # should not raise
    assert not p.exists()


def test_path_property_returns_underlying_path(tmp_path):
    p = tmp_path / "logs" / "orders.jsonl"
    log = OrderLog(p)
    assert log.path == p


def test_each_appended_record_is_one_jsonl_line(tmp_path):
    p = tmp_path / "orders.jsonl"
    log = OrderLog(p)
    log.append(_record("a", total=10000))
    log.append(_record("b", total=20000))
    raw = p.read_text(encoding="utf-8").splitlines()
    assert len(raw) == 2
    parsed = [json.loads(line) for line in raw]
    assert [r["order_id"] for r in parsed] == ["a", "b"]
    assert [r["total"] for r in parsed] == [10000, 20000]
