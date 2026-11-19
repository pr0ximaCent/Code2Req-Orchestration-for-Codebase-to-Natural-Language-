package auth

import "net/http"

func RegisterPublic(mux *http.ServeMux, h Handler) {
	// login related
	mux.HandleFunc("POST /auth/login/credentials", h.LoginCredentials)
	mux.HandleFunc("POST /auth/login/google", h.LoginGoogle)
	mux.HandleFunc("POST /auth/signup", h.Signup)

	mux.HandleFunc("POST /auth/token/refresh", h.RefreshToken)
	mux.HandleFunc("POST /auth/verification-otps/request", h.RequestVerificationOTP)

	// password related
	mux.HandleFunc("PUT /auth/password/reset", h.ResetPassword)
}
