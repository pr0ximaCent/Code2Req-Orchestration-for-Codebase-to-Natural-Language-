package coinslot

import (
	"slices"
	"testing"
)

func TestAccepts(t *testing.T) {
	s := New([]int{25, 5, 10})
	for _, v := range []int{5, 10, 25} {
		if !s.Accepts(v) {
			t.Errorf("%d should be accepted", v)
		}
	}
	for _, v := range []int{0, 1, 50, 100, -5} {
		if s.Accepts(v) {
			t.Errorf("%d should be rejected", v)
		}
	}
	if got := s.Values(); !slices.Equal(got, []int{5, 10, 25}) {
		t.Fatalf("values %v", got)
	}
}
