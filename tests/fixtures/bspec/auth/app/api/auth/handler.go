// Package auth serves signup, login, token refresh and password endpoints.
package auth

import (
	billing "sysmodeler/app/domains/billing"
	userdomain "sysmodeler/app/domains/user"
)

type Handler struct {
	userSvc    *userdomain.Service
	billingSvc *billing.Service
}

func NewHandler(userService *userdomain.Service, billing *billing.Service) Handler {
	return Handler{
		userSvc:    userService,
		billingSvc: billing,
	}
}
