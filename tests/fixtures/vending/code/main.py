"""Console front panel for trying the machine out."""

import logging

from vending.machine import Machine
from vending.money import dollars, pad


def show(machine):
    for code, (name, price, stock) in sorted(machine.shelves.listing().items()):
        print(pad(code, 4), pad(name, 16), pad(dollars(price), 8), stock)
    print("credit:", dollars(machine.credit), "mode:", machine.mode.value)


def main():
    logging.basicConfig(level=logging.INFO)
    m = Machine()
    print("commands: coin <cents> | pick <slot> | cancel | temp <c> | fill <slot> <n> | quit")
    while True:
        show(m)
        try:
            parts = input("> ").split()
        except EOFError:
            break
        if not parts:
            continue
        cmd, args = parts[0], parts[1:]
        if cmd == "coin" and args:
            print(m.insert_coin(int(args[0])))
        elif cmd == "pick" and args:
            print(m.select(args[0]))
        elif cmd == "cancel":
            print(m.cancel())
        elif cmd == "temp" and args:
            m.on_temperature(float(args[0]))
        elif cmd == "fill" and len(args) == 2:
            print(m.restock(args[0], int(args[1])))
        elif cmd == "quit":
            break


if __name__ == "__main__":
    main()
