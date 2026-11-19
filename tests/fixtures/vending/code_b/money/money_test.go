package money

import "testing"

func TestFormat(t *testing.T) {
	cases := map[int]string{0: "$0.00", 5: "$0.05", 125: "$1.25", 1000: "$10.00", -35: "-$0.35"}
	for in, want := range cases {
		if got := Format(in); got != want {
			t.Errorf("Format(%d) = %q, want %q", in, got, want)
		}
	}
}

func TestPad(t *testing.T) {
	if got := PadLeft("$1.25", 7); got != "  $1.25" {
		t.Errorf("PadLeft = %q", got)
	}
	if got := PadRight("A1", 4); got != "A1  " {
		t.Errorf("PadRight = %q", got)
	}
	if got := PadLeft("toolong", 3); got != "toolong" {
		t.Errorf("PadLeft overflow = %q", got)
	}
}
