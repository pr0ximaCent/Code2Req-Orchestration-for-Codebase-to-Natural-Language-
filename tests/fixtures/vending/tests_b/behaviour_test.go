// Package behaviour checks that implementation B (code_b/) does what SYSTEM.md says: the checks of
// tests/test_behaviour.py, ported to B's API. Run: go test ./... (from tests_b/).
package behaviour

import (
	"io"
	"log/slog"
	"path/filepath"
	"testing"

	"snackbox/config"
	"snackbox/controller"
	"snackbox/journal"
	"snackbox/motor"
)

func machine(t *testing.T, push motor.PushFunc, edit func(*config.Machine)) (*controller.Controller, string) {
	t.Helper()
	cfg, err := config.Default()
	if err != nil {
		t.Fatal(err)
	}
	cfg.JournalPath = filepath.Join(t.TempDir(), "events.jsonl")
	cfg.MotorPause = 0
	if edit != nil {
		edit(&cfg)
	}
	if push == nil {
		push = func(string) error { return nil } // no random jams
	}
	c, err := controller.New(cfg, controller.WithPush(push),
		controller.WithLogger(slog.New(slog.NewTextHandler(io.Discard, nil))))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = c.Close() })
	return c, cfg.JournalPath
}

func coins(c *controller.Controller, cents ...int) {
	for _, v := range cents {
		c.Handle(controller.Coin{Cents: v})
	}
}

func stock(t *testing.T, c *controller.Controller, slot string) int {
	n, ok := c.Stock(slot)
	if !ok {
		t.Fatalf("no slot %s", slot)
	}
	return n
}

func TestBadCoinReturned(t *testing.T) {
	c, _ := machine(t, nil, nil)
	r := c.Handle(controller.Coin{Cents: 1})
	if r.Returned != 1 || c.Credit() != 0 {
		t.Fatalf("got %+v, credit %d", r, c.Credit())
	}
}

func TestCreditAccumulatesAndSaleGivesChange(t *testing.T) {
	c, _ := machine(t, nil, nil)
	coins(c, 25, 25, 25, 25, 25, 25) // A1 costs 125
	r := c.Handle(controller.Select{Slot: "A1"})
	if r.Outcome != controller.Sold || r.Returned != 25 || c.Phase() != controller.Idle || stock(t, c, "A1") != 4 {
		t.Fatalf("got %+v, phase %v, stock %d", r, c.Phase(), stock(t, c, "A1"))
	}
}

func TestInsufficientCreditKeepsCredit(t *testing.T) {
	c, _ := machine(t, nil, nil)
	coins(c, 25)
	r := c.Handle(controller.Select{Slot: "A1"})
	if r.Outcome == controller.Sold || c.Credit() != 25 || c.Phase() != controller.Collecting {
		t.Fatalf("got %+v, credit %d, phase %v", r, c.Credit(), c.Phase())
	}
}

func TestSoldOut(t *testing.T) {
	c, _ := machine(t, nil, func(m *config.Machine) {
		for i := range m.Slots {
			if m.Slots[i].ID == "A2" {
				m.Slots[i].Stock = 0
			}
		}
	})
	coins(c, 25, 25, 25, 25, 25)
	r := c.Handle(controller.Select{Slot: "A2"})
	if r.Outcome != controller.SoldOut || c.Credit() != 125 {
		t.Fatalf("got %+v, credit %d", r, c.Credit())
	}
}

func TestMotorFailureRefunds(t *testing.T) {
	c, _ := machine(t, func(string) error { return motor.ErrJammed }, nil)
	coins(c, 25, 25, 25, 25, 25, 25)
	r := c.Handle(controller.Select{Slot: "A1"})
	if r.Returned != 150 || stock(t, c, "A1") != 5 || c.Phase() != controller.Idle {
		t.Fatalf("got %+v, stock %d, phase %v", r, stock(t, c, "A1"), c.Phase())
	}
}

func TestCancelRefunds(t *testing.T) {
	c, _ := machine(t, nil, nil)
	coins(c, 10)
	r := c.Handle(controller.Cancel{})
	if r.Returned != 10 || c.Credit() != 0 {
		t.Fatalf("got %+v, credit %d", r, c.Credit())
	}
}

func TestTemperatureFaultRefundsAndBlocksThenRecovers(t *testing.T) {
	c, _ := machine(t, nil, nil)
	coins(c, 25)
	c.Handle(controller.Temperature{Celsius: 12})
	if c.Phase() != controller.OutOfService || c.Credit() != 0 {
		t.Fatalf("phase %v, credit %d", c.Phase(), c.Credit())
	}
	if r := c.Handle(controller.Coin{Cents: 25}); r.Outcome == controller.CoinAccepted {
		t.Fatal("coin accepted while out of service")
	}
	if r := c.Handle(controller.Select{Slot: "A1"}); r.Outcome == controller.Sold {
		t.Fatal("sold while out of service")
	}
	c.Handle(controller.Temperature{Celsius: 5})
	if c.Phase() != controller.Idle {
		t.Fatalf("phase %v", c.Phase())
	}
}

func TestRestockRules(t *testing.T) {
	c, _ := machine(t, nil, nil)
	if r := c.Handle(controller.Refill{Slot: "A1", Count: 2}); r.Outcome != controller.Refilled {
		t.Fatalf("idle refill: %+v", r)
	}
	coins(c, 5)
	if r := c.Handle(controller.Refill{Slot: "A1", Count: 2}); r.Outcome == controller.Refilled {
		t.Fatal("refilled while holding credit")
	}
	c.Handle(controller.Temperature{Celsius: 20})
	if r := c.Handle(controller.Refill{Slot: "A1", Count: 2}); r.Outcome != controller.Refilled {
		t.Fatalf("out-of-service refill: %+v", r)
	}
}

func TestSaleAndRefundLogged(t *testing.T) {
	c, path := machine(t, nil, nil)
	coins(c, 25, 25, 25, 25, 25)
	c.Handle(controller.Select{Slot: "A1"})
	coins(c, 5)
	c.Handle(controller.Cancel{})
	entries, err := journal.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 2 {
		t.Fatalf("got %d entries", len(entries))
	}
}
