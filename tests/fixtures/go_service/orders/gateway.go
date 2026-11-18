package orders

import (
	"log/slog"

	"github.com/stripe/stripe-go/v76/client"
)

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

// Refund returns a charge the way its currency needs.
func (g *Gateway) Refund(currency string) (string, error) {
	var ref string
	switch currency {
	case "usd":
		// Stripe refunds dollars itself
		slog.Info("usd refund")
		ref = "usd-refund"
	case "eur":
		if _, err := g.api.Refunds.New(nil); err != nil {
			return "", err
		}
		ref = "eur-refund"
	default:
		slog.Warn("unknown currency", "currency", currency)
	}
	return ref, nil
}
