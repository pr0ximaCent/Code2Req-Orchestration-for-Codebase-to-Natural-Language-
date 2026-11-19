// Package coinslot validates coins dropped into the machine.
package coinslot

import "slices"

// Slot knows which coin values the machine takes.
type Slot struct {
	accepted map[int]bool
}

// New returns a coin slot that accepts the given values in cents.
func New(values []int) *Slot {
	s := &Slot{accepted: make(map[int]bool, len(values))}
	for _, v := range values {
		s.accepted[v] = true
	}
	return s
}

// Accepts reports whether a coin of the given value is kept.
func (s *Slot) Accepts(cents int) bool {
	return s.accepted[cents]
}

// Values lists accepted coin values in ascending order.
func (s *Slot) Values() []int {
	out := make([]int, 0, len(s.accepted))
	for v := range s.accepted {
		out = append(out, v)
	}
	slices.Sort(out)
	return out
}
