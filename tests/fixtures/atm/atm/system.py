"""The ATM and its state machine.

States: IDLE, AWAITING_PIN, VERIFYING_PIN, MENU, AUTHORIZING,
AWAITING_BALANCE, OUT_OF_SERVICE. The ATM starts in IDLE. The session
timeout runs in AWAITING_PIN and MENU, measured from entry to the state.
An event not handled by the current state is ignored: no transition, no effect.
"""
from __future__ import annotations

import threading
from enum import Enum
from typing import Optional

from .bank_network import BankNetwork
from .card_reader import CardReader
from .cash_dispenser import CashDispenser
from .display import Display
from .money import Money
from .receipt_printer import ReceiptPrinter


class State(str, Enum):
    IDLE = "IDLE"
    AWAITING_PIN = "AWAITING_PIN"
    VERIFYING_PIN = "VERIFYING_PIN"
    MENU = "MENU"
    AUTHORIZING = "AUTHORIZING"
    AWAITING_BALANCE = "AWAITING_BALANCE"
    OUT_OF_SERVICE = "OUT_OF_SERVICE"


_TIMED_STATES = (State.AWAITING_PIN, State.MENU)


class Atm:
    def __init__(self, bank_network: BankNetwork) -> None:
        self.current_card: str = ""
        self.pin_attempts: int = 0
        self.requested_amount: Money = 0
        self.cash_level: Money = 200000
        self.MAX_PIN_ATTEMPTS: int = 3
        self.LOW_CASH_LEVEL: Money = 20000
        self.SESSION_TIMEOUT: float = 30.0   # seconds

        self.card_reader = CardReader()
        self.cash_dispenser = CashDispenser()
        self.receipt_printer = ReceiptPrinter()
        self.display = Display()
        self.bank_network = bank_network

        self._lock = threading.RLock()
        self._timer: Optional[threading.Timer] = None
        self.state: State = State.IDLE
        self._enter(State.IDLE)

    # ---------- state handling ----------
    def _enter(self, state: State) -> None:
        self._cancel_timer()
        self.state = state
        if state in _TIMED_STATES:
            self._timer = threading.Timer(self.SESSION_TIMEOUT, self._timeout, args=(self._timer_token(),))
            self._timer.daemon = True
            self._timer.start()
        if state is State.IDLE and self.cash_level < self.LOW_CASH_LEVEL:
            self.bank_network.report_low_cash()
            self._enter(State.OUT_OF_SERVICE)

    def _timer_token(self) -> object:
        self._token = object()
        return self._token

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._token = None

    def _timeout(self, token: object) -> None:
        with self._lock:
            if token is not self._token or self.state not in _TIMED_STATES:
                return
            self.finish_session()
            self._enter(State.IDLE)

    # ---------- customer input ----------
    def insert_card(self, card_number: str) -> None:
        with self._lock:
            if self.state is State.IDLE:
                self.current_card = card_number
                self.pin_attempts = 0
                self._enter(State.AWAITING_PIN)

    def enter_pin(self, pin: str) -> None:
        with self._lock:
            if self.state is State.AWAITING_PIN:
                self.bank_network.verify_pin(self.current_card, pin)
                self._enter(State.VERIFYING_PIN)

    def select_withdrawal(self, amount: Money) -> None:
        with self._lock:
            if self.state is not State.MENU:
                return
            if amount <= self.cash_level:
                self.requested_amount = amount
                self.bank_network.authorize_withdrawal(self.current_card, amount)
                self._enter(State.AUTHORIZING)
            else:
                self.display.show_message("Amount not available")   # stays in MENU; timer keeps running

    def select_balance(self) -> None:
        with self._lock:
            if self.state is State.MENU:
                self.bank_network.get_balance(self.current_card)
                self._enter(State.AWAITING_BALANCE)

    def cancel(self) -> None:
        with self._lock:
            if self.state in (State.AWAITING_PIN, State.MENU):
                self.finish_session()
                self._enter(State.IDLE)

    # ---------- bank network replies ----------
    def pin_result(self, valid: bool) -> None:
        with self._lock:
            if self.state is not State.VERIFYING_PIN:
                return
            if valid:
                self.pin_attempts = 0
                self._enter(State.MENU)
            elif self.pin_attempts + 1 < self.MAX_PIN_ATTEMPTS:
                self.pin_attempts = self.pin_attempts + 1
                self._enter(State.AWAITING_PIN)
            else:
                self.card_reader.retain_card()
                self.pin_attempts = 0
                self.current_card = ""
                self._enter(State.IDLE)

    def withdrawal_result(self, approved: bool) -> None:
        with self._lock:
            if self.state is not State.AUTHORIZING:
                return
            if approved:
                self.dispense_cash()
                self._enter(State.IDLE)
            else:
                self.display.show_message("Withdrawal declined")
                self._enter(State.MENU)

    def balance_result(self, balance: Money) -> None:
        with self._lock:
            if self.state is State.AWAITING_BALANCE:
                self.display.show_balance(balance)
                self._enter(State.MENU)

    # ---------- service technician ----------
    def refill(self, amount: Money) -> None:
        with self._lock:
            if self.state is State.OUT_OF_SERVICE:
                self.cash_level = self.cash_level + amount
                self._enter(State.IDLE)

    # ---------- session actions ----------
    def dispense_cash(self) -> None:
        self.cash_dispenser.dispense_notes(self.requested_amount)
        self.cash_level = self.cash_level - self.requested_amount
        self.receipt_printer.print_receipt(self.requested_amount)
        self.card_reader.eject_card()
        self.current_card = ""
        self.requested_amount = 0

    def finish_session(self) -> None:
        self.card_reader.eject_card()
        self.current_card = ""
        self.requested_amount = 0
        self.pin_attempts = 0
