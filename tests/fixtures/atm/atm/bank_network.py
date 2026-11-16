"""Bank network.

The bank answers asynchronously: verify_pin is answered with the ATM's
pin_result, authorize_withdrawal with withdrawal_result, and get_balance with
balance_result.
"""
from .money import Money


class BankNetwork:
    def verify_pin(self, card_number: str, pin: str) -> None:
        """Checks the PIN; answers with pin_result."""

    def authorize_withdrawal(self, card_number: str, amount: Money) -> None:
        """Checks the account; answers with withdrawal_result."""

    def get_balance(self, card_number: str) -> None:
        """Looks up the balance; answers with balance_result."""

    def report_low_cash(self) -> None:
        """Records that the ATM needs a refill."""
