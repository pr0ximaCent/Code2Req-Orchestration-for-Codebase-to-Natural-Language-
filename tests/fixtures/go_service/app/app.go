package app

import (
	"database/sql"
	"net/http"

	"example.com/orders/api"
	"example.com/orders/config"
	"example.com/orders/orders"
	"example.com/orders/store"
)

// Build wires the parts and returns the HTTP handler.
func Build(cfg config.Config, db *sql.DB) http.Handler {
	ordersRepo := store.NewRepository(db, "orders")
	paymentsRepo := store.NewRepository(db, "payments")
	gateway := orders.NewGateway(cfg.Stripe.Key)
	svc := orders.NewService(ordersRepo, paymentsRepo, gateway)
	h := api.NewHandler(svc)
	return Router(h)
}

// Router mounts the API under /api.
func Router(h api.Handler) http.Handler {
	root := http.NewServeMux()
	apiMux := http.NewServeMux()
	api.Register(apiMux, h)
	root.Handle("/api/", http.StripPrefix("/api", apiMux))
	return root
}
