"""Prices and stock per slot."""

import copy

from . import config


class Shelves:
    def __init__(self):
        self._slots = copy.deepcopy(config.SLOTS)

    def known(self, code):
        return code in self._slots

    def price(self, code):
        return self._slots[code]["price"]

    def count(self, code):
        return self._slots[code]["stock"]

    def take_one(self, code):
        self._slots[code]["stock"] -= 1

    def fill(self, code, amount):
        self._slots[code]["stock"] += amount

    def listing(self):
        return {c: (s["name"], s["price"], s["stock"]) for c, s in self._slots.items()}
