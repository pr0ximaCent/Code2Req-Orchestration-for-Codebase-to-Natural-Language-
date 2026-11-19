package journal

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestWriteAndRead(t *testing.T) {
	path := filepath.Join(t.TempDir(), "sub", "j.jsonl")
	j, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	j.Write(Entry{Kind: KindSale, Slot: "A1", Credit: 150, Price: 125, Returned: 25})
	j.Write(Entry{Kind: KindRefund, Credit: 30, Returned: 30, Reason: "cancelled"})
	if err := j.Close(); err != nil {
		t.Fatal(err)
	}

	raw, _ := os.ReadFile(path)
	if lines := strings.Count(string(raw), "\n"); lines != 2 {
		t.Fatalf("want 2 lines, got %d", lines)
	}
	es, err := ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if len(es) != 2 || es[0].Returned != 25 || es[1].Reason != "cancelled" || es[0].Time.IsZero() {
		t.Fatalf("got %+v", es)
	}

	// Reopening appends rather than truncating.
	j, _ = Open(path)
	j.Write(Entry{Kind: KindRefund, Returned: 5})
	j.Close()
	if es, _ := ReadFile(path); len(es) != 3 {
		t.Fatalf("want 3 entries, got %d", len(es))
	}
}
