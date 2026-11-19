package auth

type LoginRequest struct {
	Email    string `json:"email"`
	Password string `json:"password"`
}

type SignupRequest struct {
	Email    string `json:"email"`
	Name     string `json:"name"`
	Password string `json:"password"`
	OTPID    string `json:"otp_id"`
	OTP      string `json:"otp"`
}

type RefreshTokenRequest struct {
	RefreshToken string `json:"refresh_token"`
}

type GoogleOAuthRequest struct {
	AccessToken string `json:"access_token"`
}

type ForgotPasswordRequest struct {
	Email   string `json:"email"`
	Purpose string `json:"purpose,omitempty"`
}

type ResetPasswordRequest struct {
	Email           string `json:"email"`
	OTPID           string `json:"otp_id"`
	OTP             string `json:"otp"`
	NewPassword     string `json:"new_password"`
	ConfirmPassword string `json:"confirm_password"`
}

type ChangePasswordRequest struct {
	CurrentPassword string `json:"current_password"`
	NewPassword     string `json:"new_password"`
	Email           string `json:"email"`
	OTPID           string `json:"otp_id"`
	OTP             string `json:"otp"`
}

type VerificationOTPRequest struct {
	Email   string `json:"email"`
	Purpose string `json:"purpose"`
	UserID  string `json:"user_id"`
}
