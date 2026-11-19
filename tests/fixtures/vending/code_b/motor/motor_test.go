package motor

import (
	"errors"
	"io"
	"log/slog"
	"testing"
	"time"
)

var quiet = slog.New(slog.NewTextHandler(io.Discard, nil))

func jamming(n int, calls *int) PushFunc {
	return func(string) error {
		*calls++
		if *calls <= n {
			return ErrJammed
		}
		return nil
	}
}

func TestSucceedsFirstTry(t *testing.T) {
	var calls int
	if err := New(jamming(0, &calls), 3, 0, quiet).Dispense("A1"); err != nil || calls != 1 {
		t.Fatalf("err %v calls %d", err, calls)
	}
}

func TestRetriesWithPause(t *testing.T) {
	var calls int
	start := time.Now()
	err := New(jamming(2, &calls), 3, 10*time.Millisecond, quiet).Dispense("A1")
	if err != nil || calls != 3 {
		t.Fatalf("err %v calls %d", err, calls)
	}
	if time.Since(start) < 20*time.Millisecond {
		t.Fatal("expected a pause between attempts")
	}
}

func TestGivesUpAfterTries(t *testing.T) {
	var calls int
	err := New(jamming(10, &calls), 3, 0, quiet).Dispense("A1")
	if !errors.Is(err, ErrJammed) || calls != 3 {
		t.Fatalf("err %v calls %d", err, calls)
	}
}

func TestSimulatedRates(t *testing.T) {
	if Simulated(0)("A1") != nil {
		t.Fatal("rate 0 should never jam")
	}
	if !errors.Is(Simulated(1)("A1"), ErrJammed) {
		t.Fatal("rate 1 should always jam")
	}
}
