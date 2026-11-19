"""The controller: ties coins, shelves, motor and temperature together."""

from enum import Enum

from . import config, eventlog
from .eventlog import log
from .motor import SlotMotor
from .shelves import Shelves


class Mode(Enum):
    IDLE = "idle"
    CREDITED = "credited"
    DISPENSING = "dispensing"
    FAULT = "fault"


class Machine:
    def __init__(self):
        self.mode = Mode.IDLE
        self.credit = 0
        self.shelves = Shelves()
        self.motor = SlotMotor()

    # --- customer side -------------------------------------------------

    def insert_coin(self, value):
        if self.mode == Mode.FAULT or self.mode == Mode.DISPENSING:
            return {"ok": False, "returned": value, "why": "unavailable"}
        if value not in config.COIN_VALUES:
            log.info("rejected coin %s", value)
            return {"ok": False, "returned": value, "why": "bad coin"}
        self.credit += value
        self.mode = Mode.CREDITED
        return {"ok": True, "credit": self.credit}

    def select(self, code):
        if self.mode != Mode.CREDITED:
            return {"ok": False, "why": "no credit" if self.mode == Mode.IDLE else "unavailable"}
        if not self.shelves.known(code):
            return {"ok": False, "why": "no such slot"}
        price = self.shelves.price(code)
        if self.credit < price:
            return {"ok": False, "why": "need more", "short": price - self.credit}
        if self.shelves.count(code) <= 0:
            return {"ok": False, "why": "sold out"}

        self.mode = Mode.DISPENSING
        if not self.motor.push(code):
            refund = self._pay_back()
            eventlog.write("refund", reason="motor", amount=refund)
            return {"ok": False, "why": "jam", "returned": refund}

        self.shelves.take_one(code)
        change = self.credit - price
        self.credit = 0
        self.mode = Mode.IDLE
        eventlog.write("sale", slot=code, price=price, change=change)
        return {"ok": True, "item": code, "change": change}

    def cancel(self):
        if self.mode != Mode.CREDITED:
            return {"ok": False, "returned": 0}
        refund = self._pay_back()
        eventlog.write("refund", reason="cancel", amount=refund)
        return {"ok": True, "returned": refund}

    # --- sensor side ---------------------------------------------------

    def on_temperature(self, celsius):
        too_far = celsius < config.TEMP_LOW_C or celsius > config.TEMP_HIGH_C
        if too_far and self.mode != Mode.FAULT:
            refund = self._pay_back(next_mode=Mode.FAULT)
            if refund:
                eventlog.write("refund", reason="temperature", amount=refund)
            log.error("temperature %.1f out of range", celsius)
        elif not too_far and self.mode == Mode.FAULT:
            self.mode = Mode.IDLE
            log.info("temperature back in range")

    # --- operator side -------------------------------------------------

    def restock(self, code, amount):
        if not (self.mode == Mode.FAULT or (self.mode == Mode.IDLE and self.credit == 0)):
            return False
        if not self.shelves.known(code) or amount <= 0:
            return False
        self.shelves.fill(code, amount)
        return True

    # --- internals -----------------------------------------------------

    def _pay_back(self, next_mode=Mode.IDLE):
        amount, self.credit = self.credit, 0
        self.mode = next_mode
        return amount
