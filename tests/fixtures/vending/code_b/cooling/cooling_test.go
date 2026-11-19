package cooling

import "testing"

func TestObserveCrossings(t *testing.T) {
	w := New(2, 8)
	steps := []struct {
		c    float64
		want Change
	}{
		{5, Steady}, {2, Steady}, {8, Steady}, {8.1, WentUnsafe}, {12, Steady},
		{7, Recovered}, {1.5, WentUnsafe}, {2, Recovered},
	}
	for _, s := range steps {
		if got := w.Observe(s.c); got != s.want {
			t.Fatalf("%.1f: got %v want %v", s.c, got, s.want)
		}
		if w.Safe() != w.InRange(s.c) || w.Last() != s.c {
			t.Fatalf("%.1f: state out of sync", s.c)
		}
	}
}
