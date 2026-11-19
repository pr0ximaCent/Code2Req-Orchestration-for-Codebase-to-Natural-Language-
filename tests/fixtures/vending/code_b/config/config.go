// Package config loads the machine settings from JSON.
package config

import (
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"time"
)

//go:embed machine.json
var defaultJSON []byte

// Slot describes one shelf position and what it starts with.
type Slot struct {
	ID    string `json:"id"`
	Name  string `json:"name"`
	Price int    `json:"price_cents"`
	Stock int    `json:"stock"`
}

// Machine holds every setting the controller needs.
type Machine struct {
	AcceptedCoins []int
	MinTemp       float64
	MaxTemp       float64
	MotorTries    int
	MotorPause    time.Duration
	Slots         []Slot
	JournalPath   string
}

type fileFormat struct {
	AcceptedCoins []int `json:"accepted_coins"`
	Temperature   struct {
		Min float64 `json:"min_c"`
		Max float64 `json:"max_c"`
	} `json:"safe_temperature"`
	Motor struct {
		Tries   int `json:"tries"`
		PauseMS int `json:"pause_ms"`
	} `json:"motor"`
	Slots       []Slot `json:"slots"`
	JournalPath string `json:"journal_path"`
}

// Load reads and validates the settings file at path.
func Load(path string) (Machine, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return Machine{}, fmt.Errorf("read config: %w", err)
	}
	return Parse(data)
}

// Default returns the settings shipped with the repository.
func Default() (Machine, error) {
	return Parse(defaultJSON)
}

// Parse decodes and validates settings from raw JSON.
func Parse(data []byte) (Machine, error) {
	var f fileFormat
	if err := json.Unmarshal(data, &f); err != nil {
		return Machine{}, fmt.Errorf("decode config: %w", err)
	}
	m := Machine{
		AcceptedCoins: f.AcceptedCoins,
		MinTemp:       f.Temperature.Min,
		MaxTemp:       f.Temperature.Max,
		MotorTries:    f.Motor.Tries,
		MotorPause:    time.Duration(f.Motor.PauseMS) * time.Millisecond,
		Slots:         f.Slots,
		JournalPath:   f.JournalPath,
	}
	return m, m.validate()
}

func (m Machine) validate() error {
	var errs []error
	if len(m.AcceptedCoins) == 0 {
		errs = append(errs, errors.New("no accepted coins"))
	}
	if m.MinTemp >= m.MaxTemp {
		errs = append(errs, fmt.Errorf("temperature range %.1f..%.1f is empty", m.MinTemp, m.MaxTemp))
	}
	if m.MotorTries < 1 {
		errs = append(errs, errors.New("motor tries must be at least 1"))
	}
	if m.JournalPath == "" {
		errs = append(errs, errors.New("journal path is empty"))
	}
	if len(m.Slots) == 0 {
		errs = append(errs, errors.New("no slots configured"))
	}
	seen := make(map[string]bool)
	for _, s := range m.Slots {
		if s.ID == "" || seen[s.ID] {
			errs = append(errs, fmt.Errorf("slot id %q is empty or repeated", s.ID))
		}
		seen[s.ID] = true
		if s.Price <= 0 || s.Stock < 0 {
			errs = append(errs, fmt.Errorf("slot %s: bad price or stock", s.ID))
		}
	}
	if err := errors.Join(errs...); err != nil {
		return fmt.Errorf("invalid config: %w", err)
	}
	return nil
}
