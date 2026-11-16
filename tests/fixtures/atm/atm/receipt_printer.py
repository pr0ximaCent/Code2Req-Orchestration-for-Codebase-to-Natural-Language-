"""Receipt printer."""
from .money import Money


class ReceiptPrinter:
    def print_receipt(self, amount: Money) -> None:
        """Prints a receipt for the amount."""
