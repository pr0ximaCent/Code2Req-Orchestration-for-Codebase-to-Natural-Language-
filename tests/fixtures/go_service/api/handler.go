package api

import (
	"encoding/json"
	"net/http"

	"example.com/orders/orders"
)

type Handler struct {
	svc *orders.Service
}

func NewHandler(svc *orders.Service) Handler { return Handler{svc: svc} }

type placeRequest struct {
	ID     string `json:"id"`
	Amount int    `json:"amount"`
}

func (h Handler) Place(w http.ResponseWriter, r *http.Request) {
	var req placeRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"detail": "bad body"})
		return
	}
	o, err := h.svc.Place(req.ID, req.Amount)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]any{"detail": err.Error()})
		return
	}
	writeJSON(w, http.StatusCreated, o)
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func Register(mux *http.ServeMux, h Handler) {
	mux.HandleFunc("POST /orders", h.Place)
}
