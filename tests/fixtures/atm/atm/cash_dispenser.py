"""Cash dispenser."""
from .money import Money


class CashDispenser:
    def dispense_notes(self, amount: Money) -> None:
        """Pays the amount out in notes."""
