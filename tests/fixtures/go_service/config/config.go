package config

import "os"

type DBConfig struct {
	URL string
}

type StripeConfig struct {
	Key string
}

type Config struct {
	Port   string
	DB     DBConfig
	Stripe StripeConfig
}

// Load reads the settings from the environment.
func Load() Config {
	return Config{
		Port: getEnv("PORT", "8080"),
		DB: DBConfig{
			URL: getEnv("DATABASE_URL", "postgres://localhost/orders"),
		},
		Stripe: StripeConfig{
			Key: getEnv("STRIPE_KEY", "sk_test"),
		},
	}
}

func getEnv(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}
