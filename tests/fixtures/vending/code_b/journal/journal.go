// Package journal appends sale and refund records to a JSON-lines file.
package journal

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"time"
)

// Record kinds.
const (
	KindSale   = "sale"
	KindRefund = "refund"
)

// Entry is one line in the journal.
type Entry struct {
	Time     time.Time `json:"time"`
	Kind     string    `json:"kind"`
	Slot     string    `json:"slot,omitempty"`
	Credit   int       `json:"credit_cents"`
	Price    int       `json:"price_cents,omitempty"`
	Returned int       `json:"returned_cents"`
	Reason   string    `json:"reason,omitempty"`
}

// Journal is an append-only log file.
type Journal struct {
	mu  sync.Mutex
	f   *os.File
	enc *json.Encoder
}

// Open opens (or creates) the journal at path for appending.
func Open(path string) (*Journal, error) {
	if dir := filepath.Dir(path); dir != "." {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			return nil, fmt.Errorf("journal dir: %w", err)
		}
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		return nil, fmt.Errorf("open journal: %w", err)
	}
	return &Journal{f: f, enc: json.NewEncoder(f)}, nil
}

// Write appends e, stamping it with the current time if unset.
func (j *Journal) Write(e Entry) error {
	if e.Time.IsZero() {
		e.Time = time.Now().UTC()
	}
	j.mu.Lock()
	defer j.mu.Unlock()
	if err := j.enc.Encode(e); err != nil {
		return fmt.Errorf("write journal: %w", err)
	}
	return nil
}

// Close closes the underlying file.
func (j *Journal) Close() error {
	j.mu.Lock()
	defer j.mu.Unlock()
	return j.f.Close()
}

// ReadFile loads every entry from a journal file.
func ReadFile(path string) ([]Entry, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	var out []Entry
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		var e Entry
		if err := json.Unmarshal(sc.Bytes(), &e); err != nil {
			return out, fmt.Errorf("journal line %d: %w", len(out)+1, err)
		}
		out = append(out, e)
	}
	return out, sc.Err()
}
