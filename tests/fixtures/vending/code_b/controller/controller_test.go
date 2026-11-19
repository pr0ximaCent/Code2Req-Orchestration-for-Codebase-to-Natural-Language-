package controller

import (
	"errors"
	"io"
	"log/slog"
	"path/filepath"
	"testing"

	"snackbox/config"
	"snackbox/journal"
	"snackbox/motor"
)

type rig struct {
	c       *Controller
	journal string
	pushes  int
}

// newRig builds a controller from the shipped settings with a temp journal,
// no motor pause and a hardware call that jams the first jams attempts.
func newRig(t *testing.T, jams int) *rig {
	t.Helper()
	cfg, err := config.Default()
	if err != nil {
		t.Fatal(err)
	}
	r := &rig{journal: filepath.Join(t.TempDir(), "journal.jsonl")}
	cfg.JournalPath = r.journal
	cfg.MotorPause = 0
	push := func(string) error {
		r.pushes++
		if r.pushes <= jams {
			return motor.ErrJammed
		}
		return nil
	}
	quiet := slog.New(slog.NewTextHandler(io.Discard, nil))
	r.c, err = New(cfg, WithPush(push), WithLogger(quiet))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { r.c.Close() })
	return r
}

func (r *rig) coins(t *testing.T, cents ...int) {
	t.Helper()
	for _, v := range cents {
		if res := r.c.Handle(Coin{Cents: v}); res.Outcome != CoinAccepted {
			t.Fatalf("coin %d: got %v", v, res.Outcome)
		}
	}
}

func (r *rig) entries(t *testing.T) []journal.Entry {
	t.Helper()
	es, err := journal.ReadFile(r.journal)
	if err != nil {
		t.Fatal(err)
	}
	return es
}

func TestStartsIdleWithNoCredit(t *testing.T) {
	r := newRig(t, 0)
	if r.c.Phase() != Idle || r.c.Credit() != 0 {
		t.Fatalf("got phase %v credit %d", r.c.Phase(), r.c.Credit())
	}
}

func TestCoinsAccumulateCredit(t *testing.T) {
	r := newRig(t, 0)
	r.coins(t, 25, 10, 5)
	if r.c.Credit() != 40 || r.c.Phase() != Collecting {
		t.Fatalf("got credit %d phase %v", r.c.Credit(), r.c.Phase())
	}
}

func TestUnknownCoinIsReturned(t *testing.T) {
	r := newRig(t, 0)
	for _, v := range []int{1, 50, 100, 0, -5} {
		res := r.c.Handle(Coin{Cents: v})
		if res.Outcome != CoinRejected || res.Returned != v {
			t.Errorf("coin %d: got %+v", v, res)
		}
	}
	if r.c.Credit() != 0 || r.c.Phase() != Idle {
		t.Fatalf("credit changed to %d", r.c.Credit())
	}
}

func TestSaleGivesChangeAndDecrementsStock(t *testing.T) {
	r := newRig(t, 0)
	r.coins(t, 25, 25, 25, 25, 25, 10) // 135 for a 125 item
	res := r.c.Handle(Select{Slot: "A1"})
	if res.Outcome != Sold || res.Returned != 10 || res.Slot != "A1" {
		t.Fatalf("got %+v", res)
	}
	if s, _ := r.c.Stock("A1"); s != 4 {
		t.Fatalf("stock %d, want 4", s)
	}
	if r.c.Phase() != Idle || r.c.Credit() != 0 {
		t.Fatalf("phase %v credit %d", r.c.Phase(), r.c.Credit())
	}
	es := r.entries(t)
	if len(es) != 1 || es[0].Kind != journal.KindSale || es[0].Returned != 10 || es[0].Price != 125 {
		t.Fatalf("journal %+v", es)
	}
}

func TestExactCreditSaleHasNoChange(t *testing.T) {
	r := newRig(t, 0)
	r.coins(t, 25, 25, 25, 25)
	if res := r.c.Handle(Select{Slot: "A2"}); res.Outcome != Sold || res.Returned != 0 {
		t.Fatalf("got %+v", res)
	}
}

func TestShortCreditKeepsCreditAndStock(t *testing.T) {
	r := newRig(t, 0)
	r.coins(t, 25)
	res := r.c.Handle(Select{Slot: "A1"})
	if res.Outcome != NotEnoughCredit || res.Returned != 0 {
		t.Fatalf("got %+v", res)
	}
	if r.c.Credit() != 25 || r.c.Phase() != Collecting {
		t.Fatalf("credit %d phase %v", r.c.Credit(), r.c.Phase())
	}
	if s, _ := r.c.Stock("A1"); s != 5 {
		t.Fatalf("stock %d", s)
	}
	// Customer can top up and buy afterwards.
	r.coins(t, 25, 25, 25, 25)
	if res := r.c.Handle(Select{Slot: "A1"}); res.Outcome != Sold {
		t.Fatalf("after top-up got %+v", res)
	}
}

func TestEmptySlotSellsNothing(t *testing.T) {
	r := newRig(t, 0)
	r.coins(t, 25, 25, 25, 25)
	r.c.Handle(Select{Slot: "B2"})
	r.coins(t, 25, 25, 25, 25)
	r.c.Handle(Select{Slot: "B2"})
	r.coins(t, 25, 25, 25, 25)
	res := r.c.Handle(Select{Slot: "B2"})
	if res.Outcome != SoldOut || r.c.Credit() != 100 {
		t.Fatalf("got %+v credit %d", res, r.c.Credit())
	}
	// Credit stays available for another slot.
	if res := r.c.Handle(Select{Slot: "A2"}); res.Outcome != Sold {
		t.Fatalf("other slot got %+v", res)
	}
}

func TestUnknownSlot(t *testing.T) {
	r := newRig(t, 0)
	r.coins(t, 25)
	res := r.c.Handle(Select{Slot: "Z9"})
	if res.Outcome != UnknownSlot || r.c.Credit() != 25 {
		t.Fatalf("got %+v", res)
	}
	if _, ok := r.c.Stock("Z9"); ok {
		t.Fatal("Z9 should not exist")
	}
}

func TestMotorRecoversWithinTries(t *testing.T) {
	r := newRig(t, 2)
	r.coins(t, 25, 25, 25, 25)
	if res := r.c.Handle(Select{Slot: "A2"}); res.Outcome != Sold {
		t.Fatalf("got %+v", res)
	}
	if r.pushes != 3 {
		t.Fatalf("pushes %d", r.pushes)
	}
}

func TestMotorFailureRefundsEverything(t *testing.T) {
	r := newRig(t, 100)
	r.coins(t, 25, 25, 25, 25, 10)
	res := r.c.Handle(Select{Slot: "A2"})
	if res.Outcome != DispenseFailed || res.Returned != 110 || !errors.Is(res.Err, motor.ErrJammed) {
		t.Fatalf("got %+v", res)
	}
	if r.pushes != 3 {
		t.Fatalf("pushes %d, want 3", r.pushes)
	}
	if s, _ := r.c.Stock("A2"); s != 3 {
		t.Fatalf("stock %d, want 3", s)
	}
	if r.c.Credit() != 0 || r.c.Phase() != Idle {
		t.Fatalf("credit %d phase %v", r.c.Credit(), r.c.Phase())
	}
	es := r.entries(t)
	if len(es) != 1 || es[0].Kind != journal.KindRefund || es[0].Returned != 110 || es[0].Reason != "dispense_failed" {
		t.Fatalf("journal %+v", es)
	}
}

func TestCancelReturnsCredit(t *testing.T) {
	r := newRig(t, 0)
	r.coins(t, 10, 5)
	res := r.c.Handle(Cancel{})
	if res.Outcome != Cancelled || res.Returned != 15 {
		t.Fatalf("got %+v", res)
	}
	if r.c.Credit() != 0 || r.c.Phase() != Idle {
		t.Fatalf("credit %d phase %v", r.c.Credit(), r.c.Phase())
	}
	es := r.entries(t)
	if len(es) != 1 || es[0].Kind != journal.KindRefund || es[0].Returned != 15 {
		t.Fatalf("journal %+v", es)
	}
}

func TestCancelWithoutCreditLogsNothing(t *testing.T) {
	r := newRig(t, 0)
	if res := r.c.Handle(Cancel{}); res.Returned != 0 {
		t.Fatalf("got %+v", res)
	}
	if es := r.entries(t); len(es) != 0 {
		t.Fatalf("journal %+v", es)
	}
}

func TestOutOfRangeTemperatureStopsSales(t *testing.T) {
	for _, temp := range []float64{8.5, 1.9, 30} {
		r := newRig(t, 0)
		r.coins(t, 25, 10)
		res := r.c.Handle(Temperature{Celsius: temp})
		if res.Outcome != WentOutOfService || res.Returned != 35 {
			t.Fatalf("%.1f: got %+v", temp, res)
		}
		if r.c.Phase() != OutOfService || r.c.Credit() != 0 || r.c.Display() != "out of service" {
			t.Fatalf("%.1f: phase %v credit %d display %q", temp, r.c.Phase(), r.c.Credit(), r.c.Display())
		}
		if res := r.c.Handle(Coin{Cents: 25}); res.Outcome != Refused || res.Returned != 25 || r.c.Credit() != 0 {
			t.Fatalf("coin while out of service: %+v", res)
		}
		if res := r.c.Handle(Select{Slot: "A1"}); res.Outcome != Refused || !errors.Is(res.Err, ErrOutOfService) {
			t.Fatalf("select while out of service: %+v", res)
		}
		es := r.entries(t)
		if len(es) != 1 || es[0].Reason != "out_of_service" || es[0].Returned != 35 {
			t.Fatalf("journal %+v", es)
		}
	}
}

func TestBoundaryTemperaturesAreSafe(t *testing.T) {
	r := newRig(t, 0)
	for _, temp := range []float64{2, 8, 5} {
		if res := r.c.Handle(Temperature{Celsius: temp}); res.Outcome != NoChange {
			t.Fatalf("%.1f: got %+v", temp, res)
		}
	}
	if r.c.Phase() != Idle {
		t.Fatalf("phase %v", r.c.Phase())
	}
}

func TestReturnsToIdleWhenCoolAgain(t *testing.T) {
	r := newRig(t, 0)
	r.c.Handle(Temperature{Celsius: 12})
	if res := r.c.Handle(Temperature{Celsius: 10}); res.Outcome != NoChange || r.c.Phase() != OutOfService {
		t.Fatalf("still warm: %+v", res)
	}
	if res := r.c.Handle(Temperature{Celsius: 6}); res.Outcome != BackInService {
		t.Fatalf("got %+v", res)
	}
	if r.c.Phase() != Idle || r.c.Credit() != 0 {
		t.Fatalf("phase %v", r.c.Phase())
	}
	r.coins(t, 25)
}

func TestRefillWhenIdle(t *testing.T) {
	r := newRig(t, 0)
	if res := r.c.Handle(Refill{Slot: "B2", Count: 6}); res.Outcome != Refilled {
		t.Fatalf("got %+v", res)
	}
	if s, _ := r.c.Stock("B2"); s != 8 {
		t.Fatalf("stock %d", s)
	}
}

func TestRefillWhenOutOfService(t *testing.T) {
	r := newRig(t, 0)
	r.c.Handle(Temperature{Celsius: 15})
	if res := r.c.Handle(Refill{Slot: "A1", Count: 1}); res.Outcome != Refilled {
		t.Fatalf("got %+v", res)
	}
	if s, _ := r.c.Stock("A1"); s != 6 {
		t.Fatalf("stock %d", s)
	}
}

func TestRefillRefusedWhileCreditHeld(t *testing.T) {
	r := newRig(t, 0)
	r.coins(t, 5)
	res := r.c.Handle(Refill{Slot: "A1", Count: 3})
	if res.Outcome != Refused || !errors.Is(res.Err, ErrCreditHeld) {
		t.Fatalf("got %+v", res)
	}
	if s, _ := r.c.Stock("A1"); s != 5 {
		t.Fatalf("stock %d", s)
	}
	r.c.Handle(Cancel{})
	if res := r.c.Handle(Refill{Slot: "A1", Count: 3}); res.Outcome != Refilled {
		t.Fatalf("after cancel got %+v", res)
	}
}

func TestRefillRejectsBadInput(t *testing.T) {
	r := newRig(t, 0)
	if res := r.c.Handle(Refill{Slot: "A1", Count: 0}); res.Outcome != Refused || !errors.Is(res.Err, ErrBadCount) {
		t.Fatalf("zero count: %+v", res)
	}
	if res := r.c.Handle(Refill{Slot: "Q1", Count: 2}); res.Outcome != UnknownSlot {
		t.Fatalf("unknown slot: %+v", res)
	}
}

func TestJournalRecordsSalesAndRefundsInOrder(t *testing.T) {
	r := newRig(t, 0)
	r.coins(t, 25, 25, 25, 25)
	r.c.Handle(Select{Slot: "A2"})
	r.coins(t, 10)
	r.c.Handle(Cancel{})
	es := r.entries(t)
	if len(es) != 2 || es[0].Kind != journal.KindSale || es[1].Kind != journal.KindRefund {
		t.Fatalf("journal %+v", es)
	}
	if es[0].Time.IsZero() {
		t.Fatal("entry has no timestamp")
	}
}

func TestDisplay(t *testing.T) {
	r := newRig(t, 0)
	if got := r.c.Display(); got != "insert coins" {
		t.Fatalf("idle display %q", got)
	}
	r.coins(t, 25, 25, 25, 25, 25)
	if got := r.c.Display(); got != "credit $1.25" {
		t.Fatalf("credit display %q", got)
	}
}
