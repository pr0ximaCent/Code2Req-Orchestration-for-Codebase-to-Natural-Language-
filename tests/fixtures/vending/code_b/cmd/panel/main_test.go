package main

import (
	"io"
	"log/slog"
	"path/filepath"
	"strings"
	"testing"

	"snackbox/config"
	"snackbox/controller"
)

func TestPanelSession(t *testing.T) {
	cfg, err := config.Default()
	if err != nil {
		t.Fatal(err)
	}
	cfg.JournalPath = filepath.Join(t.TempDir(), "j.jsonl")
	cfg.MotorPause = 0
	ctrl, err := controller.New(cfg,
		controller.WithPush(func(string) error { return nil }),
		controller.WithLogger(slog.New(slog.NewTextHandler(io.Discard, nil))))
	if err != nil {
		t.Fatal(err)
	}
	defer ctrl.Close()

	in := strings.NewReader("coin 25\ncoin 3\ncoin 25\ncoin 25\ncoin 25\ncoin 25\npick a2\nlist\nbogus\nquit\n")
	var out strings.Builder
	run(ctrl, in, &out)

	got := out.String()
	for _, want := range []string{
		"coin rejected, returned $0.03",
		"sold (A2), returned $0.25",
		"A2   Pretzels           $1.00   2",
		`unknown command "bogus"`,
	} {
		if !strings.Contains(got, want) {
			t.Errorf("output missing %q:\n%s", want, got)
		}
	}
}
