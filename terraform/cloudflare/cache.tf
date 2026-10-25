# The zone's custom cache rules (the http_request_cache_settings entry point). Managed
# rulesets (DDoS, the free WAF ruleset, normalization) belong to Cloudflare and are not
# touched here.
#
# The whole watch. hostname is Jellyfin: per-user API responses, images and video streams.
# Cloudflare caches .mkv/.mp4/.webm by default, which matches Jellyfin's direct-play URLs,
# so the hostname bypasses the cache (docs/runbooks/cloudflare.md, section 3).
resource "cloudflare_ruleset" "cache" {
  zone_id = var.zone_id
  name    = "default"
  kind    = "zone"
  phase   = "http_request_cache_settings"

  rules = [
    {
      description = "Hostname equals ${local.watch_host}"
      expression  = "(http.host eq \"${local.watch_host}\")"
      action      = "set_cache_settings"
      action_parameters = {
        cache = false
      }
      enabled = true
    },
  ]

  lifecycle {
    prevent_destroy = true
  }
}
