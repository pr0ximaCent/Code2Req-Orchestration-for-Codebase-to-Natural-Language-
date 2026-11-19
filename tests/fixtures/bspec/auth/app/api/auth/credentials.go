package auth

import (
	"net/http"
	"strings"

	"sysmodeler/app/domains/user/authaction"

	"sysmodeler/app/core/apperrors"

	"sysmodeler/app/core/httputil"
	"sysmodeler/app/core/response"
	userdomain "sysmodeler/app/domains/user"
)

// LoginCredentials (POST /api/v1/auth/login/credentials).
func (h Handler) LoginCredentials(w http.ResponseWriter, r *http.Request) {
	var req LoginRequest
	if !httputil.DecodeJSONBody(w, r, &req) {
		return
	}

	ipAddress := clientIP(r)
	uniqueData := map[string]string{"email": strings.ToLower(strings.TrimSpace(req.Email))}

	isActionAllowed, err := h.userSvc.IsActionAllowed(r.Context(), authaction.ActionLogin, uniqueData, ipAddress)
	if err != nil {
		response.WriteError(w, err, "auth request failed")
		return
	}

	if !isActionAllowed {
		response.WriteError(w, apperrors.ErrActionBlocked, "auth request failed")
		return
	}

	res, err := h.userSvc.SignIn(r.Context(), req.Email, req.Password)
	if err != nil {
		response.WriteError(w, err, "auth request failed")
		return
	}

	response.JSON(w, http.StatusOK, res)
}

// LoginGoogle (POST /api/v1/auth/login/google).
func (h Handler) LoginGoogle(w http.ResponseWriter, r *http.Request) {
	var req GoogleOAuthRequest
	if !httputil.DecodeJSONBody(w, r, &req) {
		return
	}
	loginRes, loginErr := h.userSvc.OAuthGoogleLogin(r.Context(), req.AccessToken)
	if loginErr != nil {
		response.WriteError(w, loginErr, "auth request failed")
		return
	}
	response.JSON(w, http.StatusOK, loginRes)
}

// Signup (POST /api/v1/auth/signup).
func (h Handler) Signup(w http.ResponseWriter, r *http.Request) {
	var req SignupRequest
	if !httputil.DecodeJSONBody(w, r, &req) {
		return
	}
	ipAddress := clientIP(r)
	uniqueData := map[string]string{
		"email": strings.ToLower(strings.TrimSpace(req.Email)),
	}
	if _, err := h.userSvc.IsActionAllowed(r.Context(), authaction.ActionSignup, uniqueData, ipAddress); err != nil {
		response.WriteError(w, err, "auth request failed")
		return
	}
	if err := h.userSvc.VerifyOTP(r.Context(), ipAddress, req.Email, req.OTPID, req.OTP, userdomain.VerificationOTPPurposeSignup); err != nil {
		response.WriteError(w, err, "auth request failed")
		return
	}

	res, err := h.userSvc.Signup(r.Context(), req.Email, req.Name, req.Password)
	if err != nil {
		response.WriteError(w, err, "auth request failed")
		return
	}

	response.JSON(w, http.StatusCreated, res)
}

// RefreshToken (POST /api/v1/auth/token/refresh).
func (h Handler) RefreshToken(w http.ResponseWriter, r *http.Request) {
	var req RefreshTokenRequest
	if !httputil.DecodeJSONBody(w, r, &req) {
		return
	}
	res, err := h.userSvc.Refresh(r.Context(), strings.TrimSpace(req.RefreshToken))
	if err != nil {
		response.WriteError(w, err, "auth request failed")
		return
	}

	response.JSON(w, http.StatusOK, res)
}
