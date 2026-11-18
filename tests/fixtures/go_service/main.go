package main

import (
	"database/sql"
	"log"
	"net/http"

	"example.com/orders/app"
	"example.com/orders/config"
)

func main() {
	cfg := config.Load()
	db, err := sql.Open("postgres", cfg.DB.URL)
	if err != nil {
		log.Fatal(err)
	}
	h := app.Build(cfg, db)
	log.Fatal(http.ListenAndServe(":"+cfg.Port, h))
}
