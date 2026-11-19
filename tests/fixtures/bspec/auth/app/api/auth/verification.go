package auth

import (
	"net/http"
	"strings"

	"sysmodeler/app/domains/user/authaction"

	"sysmodeler/app/core/httputil"
	"sysmodeler/app/core/response"
	userdomain "sysmodeler/app/domains/user"
)

// *POST /api/v1/auth/verification-otps/request

// RequestVerificationOTP (POST /api/v1/auth/verification-otps/request).
func (h Handler) RequestVerificationOTP(w http.ResponseWriter, r *http.Request) {
	var req VerificationOTPRequest
	if !httputil.DecodeJSONBody(w, r, &req) {
		return
	}

	purpose := userdomain.VerificationOTPPurpose(strings.TrimSpace(req.Purpose))
	email := strings.TrimSpace(req.Email)
	if email == "" || !purpose.IsValid() {
		response.Error(w, http.StatusBadRequest, "email and purpose are required")
		return
	}

	ipAddress := clientIP(r)
	uniqueData := map[string]string{
		"email":   strings.ToLower(email),
		"purpose": purpose.String(),
	}
	if _, err := h.userSvc.IsActionAllowed(r.Context(), authaction.ActionSendOTP, uniqueData, ipAddress); err != nil {
		response.WriteError(w, err, "auth request failed")
		return
	}

	otpID, err := h.userSvc.SendOTP(
		r.Context(),
		ipAddress,
		strings.TrimSpace(r.UserAgent()),
		email,
		purpose,
	)
	if err != nil {
		response.WriteError(w, err, "auth request failed")
		return
	}

	response.JSON(w, http.StatusCreated, map[string]any{
		"message": "Verification code sent",
		"otp_id":  otpID,
	})
}
