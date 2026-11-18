package orders

import "example.com/orders/store"

// Status of an order.
type Status string

const (
	StatusNew    Status = "new"
	StatusPaid   Status = "paid"
	StatusFailed Status = "failed"
)

type Order struct {
	ID     string `json:"id"`
	Amount int    `json:"amount"`
	Status Status `json:"status"`
}

// Service takes orders and charges them.
type Service struct {
	orders   *store.Repository
	payments *store.Repository
	gateway  *Gateway
}

func NewService(orders, payments *store.Repository, gateway *Gateway) *Service {
	return &Service{orders: orders, payments: payments, gateway: gateway}
}

// Place stores a new order and charges it in the background.
func (s *Service) Place(id string, amount int) (*Order, error) {
	o := Order{ID: id, Amount: amount, Status: StatusNew}
	if err := s.orders.Insert(o.ID, string(StatusNew)); err != nil {
		return nil, err
	}
	go s.charge(o)
	return &o, nil
}

func (s *Service) charge(o Order) {
	if err := s.gateway.Charge(o.ID, o.Amount); err != nil {
		_ = s.orders.SetStatus(o.ID, string(StatusFailed))
		return
	}
	_ = s.payments.Insert(o.ID, "ok")
	_ = s.orders.SetStatus(o.ID, string(StatusPaid))
}
