"""Display."""
from .money import Money


class Display:
    def show_message(self, text: str) -> None:
        """Shows the text."""

    def show_balance(self, balance: Money) -> None:
        """Shows the balance."""
