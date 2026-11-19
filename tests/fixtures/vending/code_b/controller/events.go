package controller

import "fmt"

// Phase is where the controller is in its cycle.
type Phase int

// Phases.
const (
	Idle Phase = iota
	Collecting
	Vending
	OutOfService
)

func (p Phase) String() string {
	switch p {
	case Idle:
		return "idle"
	case Collecting:
		return "collecting"
	case Vending:
		return "vending"
	case OutOfService:
		return "out of service"
	}
	return fmt.Sprintf("Phase(%d)", int(p))
}

// Event is anything the controller reacts to.
type Event interface{ event() }

// Coin is a coin dropped into the slot.
type Coin struct{ Cents int }

// Select is the customer pressing a slot button.
type Select struct{ Slot string }

// Cancel is the customer pressing the return button.
type Cancel struct{}

// Temperature is a cabinet sensor reading.
type Temperature struct{ Celsius float64 }

// Refill is the operator adding Count items to a slot.
type Refill struct {
	Slot  string
	Count int
}

func (Coin) event()        {}
func (Select) event()      {}
func (Cancel) event()      {}
func (Temperature) event() {}
func (Refill) event()      {}

// Outcome classifies a Result.
type Outcome int

// Outcomes.
const (
	CoinAccepted Outcome = iota
	CoinRejected
	Sold
	NotEnoughCredit
	SoldOut
	UnknownSlot
	DispenseFailed
	Cancelled
	WentOutOfService
	BackInService
	Refilled
	Refused
	NoChange
)

var outcomeNames = [...]string{
	"coin accepted", "coin rejected", "sold", "not enough credit", "sold out",
	"unknown slot", "dispense failed", "cancelled", "out of service",
	"back in service", "refilled", "refused", "no change",
}

func (o Outcome) String() string {
	if o >= 0 && int(o) < len(outcomeNames) {
		return outcomeNames[o]
	}
	return fmt.Sprintf("Outcome(%d)", int(o))
}

// Result reports what Handle did.
type Result struct {
	Outcome  Outcome
	Slot     string // slot involved, if any
	Returned int    // cents handed back: change, refund or a bounced coin
	Err      error  // reason for a refusal or failure
}
