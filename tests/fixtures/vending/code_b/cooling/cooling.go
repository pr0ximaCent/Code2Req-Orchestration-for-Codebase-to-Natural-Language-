// Package cooling watches the cabinet temperature sensor.
package cooling

// Change is what a new reading means for the machine.
type Change int

const (
	// Steady means the reading did not cross the safe boundary.
	Steady Change = iota
	// WentUnsafe means the cabinet just left the safe range.
	WentUnsafe
	// Recovered means the cabinet just came back into the safe range.
	Recovered
)

func (c Change) String() string {
	switch c {
	case WentUnsafe:
		return "went unsafe"
	case Recovered:
		return "recovered"
	default:
		return "steady"
	}
}

// Watcher turns raw readings into boundary crossings.
type Watcher struct {
	min, max float64
	safe     bool
	last     float64
}

// New returns a watcher for the inclusive range [min, max]. It starts safe.
func New(min, max float64) *Watcher {
	return &Watcher{min: min, max: max, safe: true}
}

// InRange reports whether celsius is within the safe range.
func (w *Watcher) InRange(celsius float64) bool {
	return celsius >= w.min && celsius <= w.max
}

// Observe records a reading and reports whether it crossed the boundary.
func (w *Watcher) Observe(celsius float64) Change {
	w.last = celsius
	now := w.InRange(celsius)
	switch {
	case w.safe && !now:
		w.safe = false
		return WentUnsafe
	case !w.safe && now:
		w.safe = true
		return Recovered
	}
	return Steady
}

// Safe reports whether the latest reading was in range.
func (w *Watcher) Safe() bool { return w.safe }

// Last returns the latest reading.
func (w *Watcher) Last() float64 { return w.last }
