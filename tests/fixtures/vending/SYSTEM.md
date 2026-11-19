# Snack Vending Machine

A coin-operated machine that sells packaged snacks and keeps them cold.

## People who use it

- **Customer:** puts in coins, picks a snack, takes the snack and any change. Can cancel before choosing.
- **Operator:** opens the machine, refills the shelves, closes it again.

## What it contains

A coin slot that checks coins, a shelf controller that holds prices and stock, a motor that pushes the chosen snack out, a cooling unit with a temperature sensor, and a controller that decides what happens next.

## Settings

- Accepted coins: 5, 10 and 25 cents. Anything else is pushed back out.
- Safe temperature: between 2 and 8 °C.
- The motor is tried up to 3 times before giving up.
- Each slot has its own price and its own stock count.

## How it behaves

1. The machine starts idle with no credit.
2. Each accepted coin adds its value to the customer's credit. The credit builds up across several coins.
3. When the customer picks a slot, the machine sells it only if the credit covers the price and at least one item is left. Otherwise it sells nothing and keeps the credit so the customer can add more coins or pick something else.
4. After a sale the stock goes down by one, the machine gives back whatever credit is left over, and it returns to idle.
5. If the motor cannot push the item out after all attempts, the customer gets all their credit back and nothing is sold.
6. A customer can cancel at any time before a sale completes and gets all the credit back.
7. If the temperature leaves the safe range, the machine stops selling and shows "out of service". Any credit already in it is given back. It refuses coins and selections while out of service.
8. When the temperature is back in range, the machine returns to idle by itself.
9. The operator can refill stock only while the machine is idle with no credit, or out of service.
10. Every sale and every refund is written to an event log kept for the operator.
