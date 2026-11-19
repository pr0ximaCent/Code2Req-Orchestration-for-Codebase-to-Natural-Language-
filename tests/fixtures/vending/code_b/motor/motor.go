// Package motor drives the spiral that pushes a snack out of its slot.
package motor

import (
	"errors"
	"fmt"
	"log/slog"
	"math/rand/v2"
	"time"
)

// ErrJammed is what the hardware reports when the spiral does not turn.
var ErrJammed = errors.New("motor jammed")

// PushFunc performs one hardware attempt to release an item from slot.
type PushFunc func(slot string) error

// Driver retries the hardware call with a pause between attempts.
type Driver struct {
	push  PushFunc
	tries int
	pause time.Duration
	log   *slog.Logger
}

// New builds a driver. tries below 1 is treated as 1.
func New(push PushFunc, tries int, pause time.Duration, log *slog.Logger) *Driver {
	if tries < 1 {
		tries = 1
	}
	if log == nil {
		log = slog.Default()
	}
	return &Driver{push: push, tries: tries, pause: pause, log: log}
}

// Dispense pushes the item out, returning an error if every attempt fails.
func (d *Driver) Dispense(slot string) error {
	var err error
	for attempt := 1; attempt <= d.tries; attempt++ {
		if err = d.push(slot); err == nil {
			if attempt > 1 {
				d.log.Info("motor freed after retry", "slot", slot, "attempt", attempt)
			}
			return nil
		}
		d.log.Warn("motor attempt failed", "slot", slot, "attempt", attempt, "max", d.tries, "err", err)
		if attempt < d.tries && d.pause > 0 {
			time.Sleep(d.pause)
		}
	}
	return fmt.Errorf("slot %s: gave up after %d tries: %w", slot, d.tries, err)
}

// Simulated stands in for the real hardware; it jams with the given probability.
func Simulated(jamRate float64) PushFunc {
	return func(string) error {
		if rand.Float64() < jamRate {
			return ErrJammed
		}
		return nil
	}
}
