"""Small helpers for showing money."""


def dollars(cents):
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}${cents // 100}.{cents % 100:02d}"


def pad(text, width):
    return str(text).ljust(width)[:width]
