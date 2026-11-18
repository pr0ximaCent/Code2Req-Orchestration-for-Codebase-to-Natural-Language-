package store

import "database/sql"

// Repository keeps the rows of one table.
type Repository struct {
	db    *sql.DB
	table string
}

func NewRepository(db *sql.DB, table string) *Repository {
	return &Repository{db: db, table: table}
}

func (r *Repository) Insert(id string, status string) error {
	_, err := r.db.Exec("INSERT INTO "+r.table+" (id, status) VALUES ($1, $2)", id, status)
	return err
}

func (r *Repository) SetStatus(id string, status string) error {
	_, err := r.db.Exec("UPDATE "+r.table+" SET status = $1 WHERE id = $2", status, id)
	return err
}
