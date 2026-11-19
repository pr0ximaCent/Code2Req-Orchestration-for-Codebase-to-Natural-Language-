package auth

import (
	"net/http"
	"net/netip"
	"strings"
)

func clientIP(r *http.Request) string {
	if xff := strings.TrimSpace(r.Header.Get("X-Forwarded-For")); xff != "" {
		parts := strings.Split(xff, ",")
		return strings.TrimSpace(parts[0])
	}
	if xrip := strings.TrimSpace(r.Header.Get("X-Real-IP")); xrip != "" {
		return xrip
	}
	host := strings.TrimSpace(r.RemoteAddr)
	if addr, err := netip.ParseAddrPort(host); err == nil {
		return addr.Addr().String()
	}
	return host
}

// BearerToken returns the raw bearer token from the Authorization header, or "" if missing.
func BearerToken(r *http.Request) string {
	header := strings.TrimSpace(r.Header.Get("Authorization"))
	if !strings.HasPrefix(strings.ToLower(header), "bearer ") {
		return ""
	}
	return strings.TrimSpace(header[7:])
}
