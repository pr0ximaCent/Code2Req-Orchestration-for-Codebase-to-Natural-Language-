package inventory

import (
	"errors"
	"testing"
)

func TestTakeAndRefill(t *testing.T) {
	inv := New()
	inv.Add("A1", "Crisps", 125, 1)
	if err := inv.Take("A1"); err != nil {
		t.Fatal(err)
	}
	if err := inv.Take("A1"); !errors.Is(err, ErrSoldOut) {
		t.Fatalf("got %v", err)
	}
	if err := inv.Refill("A1", 4); err != nil {
		t.Fatal(err)
	}
	it, _ := inv.Lookup("A1")
	if it.Stock != 4 || it.Price != 125 {
		t.Fatalf("got %+v", it)
	}
}

func TestUnknownSlot(t *testing.T) {
	inv := New()
	if _, err := inv.Lookup("X"); !errors.Is(err, ErrUnknownSlot) {
		t.Fatal(err)
	}
	if err := inv.Take("X"); !errors.Is(err, ErrUnknownSlot) {
		t.Fatal(err)
	}
	if err := inv.Refill("X", 1); !errors.Is(err, ErrUnknownSlot) {
		t.Fatal(err)
	}
}

func TestAllKeepsOrder(t *testing.T) {
	inv := New()
	inv.Add("B1", "b", 1, 1)
	inv.Add("A1", "a", 1, 1)
	inv.Add("B1", "b2", 2, 2)
	all := inv.All()
	if len(all) != 2 || all[0].ID != "B1" || all[0].Name != "b2" || all[1].ID != "A1" {
		t.Fatalf("got %+v", all)
	}
}
