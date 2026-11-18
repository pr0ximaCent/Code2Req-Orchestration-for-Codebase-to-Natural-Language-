package orders

import "github.com/stripe/stripe-go/v76/client"

// Gateway charges cards through Stripe.
type Gateway struct {
	api *client.API
}

func NewGateway(key string) *Gateway {
	return &Gateway{api: client.New(key, nil)}
}

func (g *Gateway) Charge(id string, amount int) error {
	_, err := g.api.PaymentIntents.New(nil)
	return err
}
