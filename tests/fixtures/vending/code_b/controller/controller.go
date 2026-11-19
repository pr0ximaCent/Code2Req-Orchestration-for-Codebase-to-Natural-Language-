// Package controller decides what the machine does in response to customer,
// operator and sensor events.
package controller

import (
	"errors"
	"fmt"
	"log/slog"

	"snackbox/coinslot"
	"snackbox/config"
	"snackbox/cooling"
	"snackbox/inventory"
	"snackbox/journal"
	"snackbox/money"
	"snackbox/motor"
)

var (
	// ErrOutOfService is set on results refused because the cabinet is too warm or cold.
	ErrOutOfService = errors.New("machine is out of service")
	// ErrCreditHeld is set when the operator tries to refill while a customer has credit.
	ErrCreditHeld = errors.New("cannot refill while credit is held")
	// ErrBadCount is set when a refill count is not positive.
	ErrBadCount = errors.New("refill count must be positive")
)

// Option customises a Controller.
type Option func(*Controller)

// WithPush replaces the motor's hardware call.
func WithPush(p motor.PushFunc) Option { return func(c *Controller) { c.push = p } }

// WithLogger sets the logger used by the controller and the motor.
func WithLogger(l *slog.Logger) Option { return func(c *Controller) { c.log = l } }

// Controller is the machine's state machine. It is not safe for concurrent use.
type Controller struct {
	phase   Phase
	credit  int
	coins   *coinslot.Slot
	shelf   *inventory.Inventory
	motor   *motor.Driver
	cooling *cooling.Watcher
	journal *journal.Journal
	push    motor.PushFunc
	log     *slog.Logger
}

// New builds a controller from settings and opens its journal.
func New(cfg config.Machine, opts ...Option) (*Controller, error) {
	c := &Controller{log: slog.Default(), push: motor.Simulated(0.05)}
	for _, o := range opts {
		o(c)
	}
	j, err := journal.Open(cfg.JournalPath)
	if err != nil {
		return nil, err
	}
	c.journal = j
	c.coins = coinslot.New(cfg.AcceptedCoins)
	c.shelf = inventory.New()
	for _, s := range cfg.Slots {
		c.shelf.Add(s.ID, s.Name, s.Price, s.Stock)
	}
	c.motor = motor.New(c.push, cfg.MotorTries, cfg.MotorPause, c.log)
	c.cooling = cooling.New(cfg.MinTemp, cfg.MaxTemp)
	c.log.Info("machine ready", "slots", len(cfg.Slots), "journal", cfg.JournalPath)
	return c, nil
}

// Close releases the journal file.
func (c *Controller) Close() error { return c.journal.Close() }

// Phase returns the current phase.
func (c *Controller) Phase() Phase { return c.phase }

// Credit returns the customer's credit in cents.
func (c *Controller) Credit() int { return c.credit }

// Stock returns the item count of a slot, and false if the slot does not exist.
func (c *Controller) Stock(slot string) (int, bool) {
	it, err := c.shelf.Lookup(slot)
	return it.Stock, err == nil
}

// Slots returns every slot in display order.
func (c *Controller) Slots() []inventory.Item { return c.shelf.All() }

// Display returns the text the front panel shows.
func (c *Controller) Display() string {
	switch c.phase {
	case OutOfService:
		return "out of service"
	case Collecting:
		return "credit " + money.Format(c.credit)
	default:
		return "insert coins"
	}
}

// Handle processes one event and reports what happened.
func (c *Controller) Handle(ev Event) Result {
	switch e := ev.(type) {
	case Coin:
		return c.insert(e.Cents)
	case Select:
		return c.choose(e.Slot)
	case Cancel:
		return c.cancel()
	case Temperature:
		return c.temperature(e.Celsius)
	case Refill:
		return c.refill(e.Slot, e.Count)
	}
	return Result{Outcome: Refused, Err: fmt.Errorf("unsupported event %T", ev)}
}

func (c *Controller) insert(cents int) Result {
	if c.phase == OutOfService {
		c.log.Warn("coin refused", "cents", cents)
		return Result{Outcome: Refused, Returned: cents, Err: ErrOutOfService}
	}
	if !c.coins.Accepts(cents) {
		c.log.Info("coin rejected", "cents", cents)
		return Result{Outcome: CoinRejected, Returned: cents}
	}
	c.credit += cents
	c.phase = Collecting
	c.log.Info("coin accepted", "cents", cents, "credit", c.credit)
	return Result{Outcome: CoinAccepted}
}

func (c *Controller) choose(slot string) Result {
	if c.phase == OutOfService {
		c.log.Warn("selection refused", "slot", slot)
		return Result{Outcome: Refused, Slot: slot, Err: ErrOutOfService}
	}
	item, err := c.shelf.Lookup(slot)
	if err != nil {
		return Result{Outcome: UnknownSlot, Slot: slot, Err: err}
	}
	if item.Stock == 0 {
		c.log.Info("slot empty", "slot", slot)
		return Result{Outcome: SoldOut, Slot: slot}
	}
	if c.credit < item.Price {
		c.log.Info("credit too low", "slot", slot, "credit", c.credit, "price", item.Price)
		return Result{Outcome: NotEnoughCredit, Slot: slot}
	}

	c.phase = Vending
	if err := c.motor.Dispense(slot); err != nil {
		c.log.Error("dispense failed, refunding", "slot", slot, "err", err)
		back := c.refund("dispense_failed", slot)
		c.phase = Idle
		return Result{Outcome: DispenseFailed, Slot: slot, Returned: back, Err: err}
	}
	if err := c.shelf.Take(slot); err != nil {
		c.log.Error("stock out of sync", "slot", slot, "err", err)
	}
	paid := c.credit
	change := paid - item.Price
	c.credit = 0
	c.phase = Idle
	c.record(journal.Entry{Kind: journal.KindSale, Slot: slot, Credit: paid, Price: item.Price, Returned: change})
	c.log.Info("sold", "slot", slot, "price", item.Price, "change", change)
	return Result{Outcome: Sold, Slot: slot, Returned: change}
}

func (c *Controller) cancel() Result {
	back := c.refund("cancelled", "")
	if c.phase != OutOfService {
		c.phase = Idle
	}
	return Result{Outcome: Cancelled, Returned: back}
}

func (c *Controller) temperature(celsius float64) Result {
	switch c.cooling.Observe(celsius) {
	case cooling.WentUnsafe:
		c.log.Warn("temperature out of range", "celsius", celsius)
		back := c.refund("out_of_service", "")
		c.phase = OutOfService
		return Result{Outcome: WentOutOfService, Returned: back}
	case cooling.Recovered:
		c.log.Info("temperature back in range", "celsius", celsius)
		c.phase = Idle
		return Result{Outcome: BackInService}
	}
	return Result{Outcome: NoChange}
}

func (c *Controller) refill(slot string, count int) Result {
	if count <= 0 {
		return Result{Outcome: Refused, Slot: slot, Err: ErrBadCount}
	}
	switch c.phase {
	case Idle, OutOfService:
	default:
		c.log.Warn("refill refused", "slot", slot, "phase", c.phase, "credit", c.credit)
		return Result{Outcome: Refused, Slot: slot, Err: ErrCreditHeld}
	}
	if err := c.shelf.Refill(slot, count); err != nil {
		return Result{Outcome: UnknownSlot, Slot: slot, Err: err}
	}
	stock, _ := c.Stock(slot)
	c.log.Info("refilled", "slot", slot, "added", count, "stock", stock)
	return Result{Outcome: Refilled, Slot: slot}
}

// refund clears the credit, journals it when non-zero and returns the amount.
func (c *Controller) refund(reason, slot string) int {
	amount := c.credit
	c.credit = 0
	if amount > 0 {
		c.record(journal.Entry{Kind: journal.KindRefund, Slot: slot, Credit: amount, Returned: amount, Reason: reason})
		c.log.Info("refunded", "cents", amount, "reason", reason)
	}
	return amount
}

func (c *Controller) record(e journal.Entry) {
	if err := c.journal.Write(e); err != nil {
		c.log.Error("journal write failed", "kind", e.Kind, "err", err)
	}
}
