package config

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestDefaultMatchesShippedFile(t *testing.T) {
	m, err := Default()
	if err != nil {
		t.Fatal(err)
	}
	if len(m.AcceptedCoins) != 3 || m.MinTemp != 2 || m.MaxTemp != 8 || m.MotorTries != 3 {
		t.Fatalf("unexpected settings %+v", m)
	}
	if m.MotorPause != 250*time.Millisecond || len(m.Slots) == 0 {
		t.Fatalf("unexpected settings %+v", m)
	}
	fromDisk, err := Load("machine.json")
	if err != nil {
		t.Fatal(err)
	}
	if fromDisk.JournalPath != m.JournalPath {
		t.Fatal("embedded and on-disk config differ")
	}
}

func TestParseRejectsBadSettings(t *testing.T) {
	bad := []string{
		`{`,
		`{"accepted_coins":[],"safe_temperature":{"min_c":2,"max_c":8},"motor":{"tries":3},"slots":[{"id":"A","price_cents":1}],"journal_path":"j"}`,
		`{"accepted_coins":[5],"safe_temperature":{"min_c":8,"max_c":2},"motor":{"tries":3},"slots":[{"id":"A","price_cents":1}],"journal_path":"j"}`,
		`{"accepted_coins":[5],"safe_temperature":{"min_c":2,"max_c":8},"motor":{"tries":0},"slots":[{"id":"A","price_cents":1}],"journal_path":"j"}`,
		`{"accepted_coins":[5],"safe_temperature":{"min_c":2,"max_c":8},"motor":{"tries":3},"slots":[{"id":"A","price_cents":1},{"id":"A","price_cents":1}],"journal_path":"j"}`,
		`{"accepted_coins":[5],"safe_temperature":{"min_c":2,"max_c":8},"motor":{"tries":3},"slots":[{"id":"A","price_cents":1}]}`,
	}
	for i, b := range bad {
		if _, err := Parse([]byte(b)); err == nil {
			t.Errorf("case %d: expected error", i)
		}
	}
}

func TestLoadMissingFile(t *testing.T) {
	if _, err := Load(filepath.Join(t.TempDir(), "nope.json")); err == nil {
		t.Fatal("expected error")
	}
	p := filepath.Join(t.TempDir(), "m.json")
	os.WriteFile(p, []byte(`{"accepted_coins":[5],"safe_temperature":{"min_c":1,"max_c":3},"motor":{"tries":1,"pause_ms":5},"slots":[{"id":"X","name":"x","price_cents":5,"stock":1}],"journal_path":"j.jsonl"}`), 0o644)
	m, err := Load(p)
	if err != nil || m.MotorPause != 5*time.Millisecond {
		t.Fatalf("got %+v %v", m, err)
	}
}
