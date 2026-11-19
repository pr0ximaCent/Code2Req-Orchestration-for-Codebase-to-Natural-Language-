// Package inventory tracks price and stock for each shelf slot.
package inventory

import (
	"errors"
	"fmt"
)

var (
	// ErrUnknownSlot is returned for a slot id that does not exist.
	ErrUnknownSlot = errors.New("unknown slot")
	// ErrSoldOut is returned when taking from an empty slot.
	ErrSoldOut = errors.New("sold out")
)

// Item is a snapshot of one slot.
type Item struct {
	ID    string
	Name  string
	Price int
	Stock int
}

// Inventory holds all slots in the order they were added.
type Inventory struct {
	items map[string]*Item
	order []string
}

// New returns an empty inventory.
func New() *Inventory {
	return &Inventory{items: make(map[string]*Item)}
}

// Add registers a slot, replacing any slot with the same id.
func (inv *Inventory) Add(id, name string, price, stock int) {
	if _, ok := inv.items[id]; !ok {
		inv.order = append(inv.order, id)
	}
	inv.items[id] = &Item{ID: id, Name: name, Price: price, Stock: stock}
}

// Lookup returns a copy of the slot.
func (inv *Inventory) Lookup(id string) (Item, error) {
	it, ok := inv.items[id]
	if !ok {
		return Item{}, fmt.Errorf("%w: %s", ErrUnknownSlot, id)
	}
	return *it, nil
}

// Take removes one item from the slot.
func (inv *Inventory) Take(id string) error {
	it, ok := inv.items[id]
	if !ok {
		return fmt.Errorf("%w: %s", ErrUnknownSlot, id)
	}
	if it.Stock == 0 {
		return fmt.Errorf("%w: %s", ErrSoldOut, id)
	}
	it.Stock--
	return nil
}

// Refill adds n items to the slot.
func (inv *Inventory) Refill(id string, n int) error {
	it, ok := inv.items[id]
	if !ok {
		return fmt.Errorf("%w: %s", ErrUnknownSlot, id)
	}
	it.Stock += n
	return nil
}

// All returns every slot in display order.
func (inv *Inventory) All() []Item {
	out := make([]Item, 0, len(inv.order))
	for _, id := range inv.order {
		out = append(out, *inv.items[id])
	}
	return out
}
