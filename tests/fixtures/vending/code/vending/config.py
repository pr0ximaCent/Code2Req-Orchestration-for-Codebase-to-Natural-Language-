"""Static settings for the machine."""

COIN_VALUES = (5, 10, 25)          # cents
TEMP_LOW_C = 2.0
TEMP_HIGH_C = 8.0
MOTOR_TRIES = 3
MOTOR_PAUSE_S = 0.05

SLOTS = {
    "A1": {"name": "chips", "price": 75, "stock": 6},
    "A2": {"name": "pretzels", "price": 60, "stock": 4},
    "B1": {"name": "chocolate bar", "price": 90, "stock": 5},
    "B2": {"name": "cookies", "price": 110, "stock": 3},
}

EVENT_LOG_PATH = "events.jsonl"
