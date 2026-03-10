output "kv_namespace_id" {
  description = "SESSION_REGISTRY KV namespace ID — paste as `id` in wrangler.toml [[kv_namespaces]]."
  value       = cloudflare_workers_kv_namespace.session_registry.id
}

output "kv_namespace_preview_id" {
  description = "SESSION_REGISTRY KV preview namespace ID — paste as `preview_id` in wrangler.toml [[kv_namespaces]]."
  value       = cloudflare_workers_kv_namespace.session_registry_preview.id
}

output "wrangler_kv_block" {
  description = "Ready-to-paste wrangler.toml [[kv_namespaces]] block."
  value       = <<-EOT
    [[kv_namespaces]]
    binding    = "SESSION_REGISTRY"
    id         = "${cloudflare_workers_kv_namespace.session_registry.id}"
    preview_id = "${cloudflare_workers_kv_namespace.session_registry_preview.id}"
  EOT
}
