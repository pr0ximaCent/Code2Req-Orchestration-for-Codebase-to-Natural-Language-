"""Checks that the code does what SYSTEM.md says. Run: python -m pytest tests -q"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "code"))

from vending import config  # noqa: E402
from vending.machine import Machine, Mode  # noqa: E402


def machine(tmp_path=None):
    config.EVENT_LOG_PATH = os.path.join(tempfile.mkdtemp(), "events.jsonl")
    m = Machine()
    m.motor._spin = lambda code: None  # no random jams
    return m


def test_bad_coin_returned():
    m = machine()
    assert m.insert_coin(1)["returned"] == 1 and m.credit == 0


def test_credit_accumulates_and_sale_gives_change():
    m = machine()
    for c in (25, 25, 25, 25):
        m.insert_coin(c)
    r = m.select("A1")  # 75
    assert r["ok"] and r["change"] == 25 and m.mode == Mode.IDLE
    assert m.shelves.count("A1") == 5


def test_insufficient_credit_keeps_credit():
    m = machine()
    m.insert_coin(25)
    assert not m.select("A1")["ok"] and m.credit == 25 and m.mode == Mode.CREDITED


def test_sold_out():
    m = machine()
    m.shelves._slots["A2"]["stock"] = 0
    for _ in range(3):
        m.insert_coin(25)
    assert m.select("A2")["why"] == "sold out" and m.credit == 75


def test_motor_failure_refunds():
    m = machine()
    m.motor._spin = lambda code: (_ for _ in ()).throw(__import__("vending.motor", fromlist=["x"]).JamError(code))
    config.MOTOR_PAUSE_S = 0
    for _ in range(3):
        m.insert_coin(25)
    r = m.select("A1")
    assert r["returned"] == 75 and m.shelves.count("A1") == 6 and m.mode == Mode.IDLE


def test_cancel_refunds():
    m = machine()
    m.insert_coin(10)
    assert m.cancel()["returned"] == 10 and m.credit == 0


def test_temperature_fault_refunds_and_blocks_then_recovers():
    m = machine()
    m.insert_coin(25)
    m.on_temperature(12.0)
    assert m.mode == Mode.FAULT and m.credit == 0
    assert not m.insert_coin(25)["ok"] and not m.select("A1")["ok"]
    m.on_temperature(5.0)
    assert m.mode == Mode.IDLE


def test_restock_rules():
    m = machine()
    assert m.restock("A1", 2)
    m.insert_coin(5)
    assert not m.restock("A1", 2)
    m.on_temperature(20.0)
    assert m.restock("A1", 2)


def test_sale_and_refund_logged():
    m = machine()
    for _ in range(3):
        m.insert_coin(25)
    m.select("A1")
    m.insert_coin(5)
    m.cancel()
    lines = open(config.EVENT_LOG_PATH).read().strip().splitlines()
    assert len(lines) == 2
