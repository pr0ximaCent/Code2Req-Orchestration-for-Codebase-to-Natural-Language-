"""Drives the slot motor that pushes an item out."""

import random
import time

from . import config
from .eventlog import log


class JamError(Exception):
    pass


class SlotMotor:
    def _spin(self, code):
        # stand-in for the real hardware call
        if random.random() < 0.02:
            raise JamError(code)

    def push(self, code):
        """Returns True once the item has dropped, False if every try jammed."""
        for attempt in range(1, config.MOTOR_TRIES + 1):
            try:
                self._spin(code)
                return True
            except JamError:
                log.warning("motor jam on %s (try %d)", code, attempt)
                time.sleep(config.MOTOR_PAUSE_S)
        return False
