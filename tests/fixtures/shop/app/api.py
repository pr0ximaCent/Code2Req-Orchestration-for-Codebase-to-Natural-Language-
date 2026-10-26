import os

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

MAX_CART_ITEMS = 50
SESSION_TIMEOUT_MS = 1500

app = FastAPI()
API_KEY = os.getenv("SHOP_API_KEY")


class CartItem(BaseModel):
    sku: str
    quantity: int


def validate_quantity(quantity: int) -> None:
    if quantity < 1:
        raise HTTPException(status_code=422, detail="quantity must be positive")


@app.post("/cart/items")
def add_item(item: CartItem, cart: list):
    validate_quantity(item.quantity)
    if len(cart) >= MAX_CART_ITEMS:
        raise HTTPException(status_code=409, detail="cart is full")
    cart.append(item)
    return {"count": len(cart)}


@app.get("/orders/{order_id}")
def get_order(order_id: str):
    # TODO: look up the order in the database
    raise NotImplementedError
