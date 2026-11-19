// Package money formats cent amounts for display.
package money

import (
	"fmt"
	"strings"
	"unicode/utf8"
)

// Format renders cents as a dollar string such as "$1.25".
func Format(cents int) string {
	sign := ""
	if cents < 0 {
		sign = "-"
		cents = -cents
	}
	return fmt.Sprintf("%s$%d.%02d", sign, cents/100, cents%100)
}

// PadLeft right-aligns s in a column of the given width.
func PadLeft(s string, width int) string {
	if n := width - utf8.RuneCountInString(s); n > 0 {
		return strings.Repeat(" ", n) + s
	}
	return s
}

// PadRight left-aligns s in a column of the given width.
func PadRight(s string, width int) string {
	if n := width - utf8.RuneCountInString(s); n > 0 {
		return s + strings.Repeat(" ", n)
	}
	return s
}
