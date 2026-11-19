package auth

import (
	"net/http"
	"strings"

	"sysmodeler/app/domains/user/authaction"

	"sysmodeler/app/core/httputil"
	"sysmodeler/app/core/response"
	userdomain "sysmodeler/app/domains/user"
)

// *PUT /api/v1/auth/password/reset

// ResetPassword (PUT /api/v1/auth/password/reset).
func (h Handler) ResetPassword(w http.ResponseWriter, r *http.Request) {
	var req ResetPasswordRequest
	if !httputil.DecodeJSONBody(w, r, &req) {
		return
	}
	ipAddress := clientIP(r)
	uniqueData := map[string]string{"email": strings.ToLower(strings.TrimSpace(req.Email))}
	if _, err := h.userSvc.IsActionAllowed(r.Context(), authaction.ActionPassReset, uniqueData, ipAddress); err != nil {
		response.WriteError(w, err, "auth request failed")
		return
	}

	if err := h.userSvc.VerifyOTP(r.Context(), ipAddress, req.Email, req.OTPID, req.OTP, userdomain.VerificationOTPPurposePasswordReset); err != nil {
		response.WriteError(w, err, "auth request failed")
		return
	}

	if err := h.userSvc.SetPassword(r.Context(), req.Email, req.NewPassword); err != nil {
		response.WriteError(w, err, "auth request failed")
		return
	}

	user, err := h.userSvc.GetUserByEmail(r.Context(), req.Email)
	if err != nil {
		response.WriteError(w, err, "auth request failed")
		return
	}

	loginRes, err := h.userSvc.CreateLoginResponse(user)
	if err != nil {
		response.WriteError(w, err, "auth request failed")
		return
	}
	response.JSON(w, http.StatusOK, loginRes)
}
