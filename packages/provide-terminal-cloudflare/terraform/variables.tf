variable "cloudflare_api_token" {
  description = "Cloudflare API token with Workers, KV, and Durable Objects permissions."
  type        = string
  sensitive   = true
}

variable "cloudflare_account_id" {
  description = "Cloudflare account ID (found in the dashboard URL or account settings)."
  type        = string
}

variable "worker_name" {
  description = "Name of the Cloudflare Worker (must match wrangler.toml `name`)."
  type        = string
  default     = "undef-terminal-cloudflare"
}

variable "environment" {
  description = "Deployment environment label (e.g. production, staging)."
  type        = string
  default     = "production"
}
