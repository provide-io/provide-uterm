terraform {
  required_version = ">= 1.5"

  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 4.0"
    }
  }
}

provider "cloudflare" {
  api_token = var.cloudflare_api_token
}

# ---------------------------------------------------------------------------
# KV Namespace — SESSION_REGISTRY
# Stores fleet-wide session status; each DO writes on worker connect/disconnect.
# ---------------------------------------------------------------------------

resource "cloudflare_workers_kv_namespace" "session_registry" {
  account_id = var.cloudflare_account_id
  title      = "${var.worker_name}-session-registry"
}

resource "cloudflare_workers_kv_namespace" "session_registry_preview" {
  account_id = var.cloudflare_account_id
  title      = "${var.worker_name}-session-registry-preview"
}

# ---------------------------------------------------------------------------
# Worker script
# The Python Worker is deployed via `uv run pywrangler deploy` (not terraform).
# This resource binds the KV namespace to the deployed worker so wrangler.toml
# does not need to be edited by hand after `terraform apply`.
#
# After apply, copy the IDs from `terraform output` into wrangler.toml:
#
#   [[kv_namespaces]]
#   binding    = "SESSION_REGISTRY"
#   id         = "<kv_namespace_id output>"
#   preview_id = "<kv_namespace_preview_id output>"
#
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Custom domain: uterm.neurotic.org
# Maps the undef-terminal-cloudflare worker to a subdomain for easier access.
# ---------------------------------------------------------------------------

resource "cloudflare_workers_custom_domain" "uterm" {
  account_id  = var.cloudflare_account_id
  zone_id     = var.cloudflare_zone_id
  hostname    = "uterm.neurotic.org"
  service     = var.worker_name
  environment = var.environment
}
