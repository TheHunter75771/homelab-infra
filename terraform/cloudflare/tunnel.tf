# Ingress of the media server's remotely managed tunnel. cloudflared on the box (stack
# `edge`, role `stacks`) runs with the token from the vault and fetches this config.
#
# The tunnel itself (cloudflare_zero_trust_tunnel_cloudflared) is deliberately NOT managed:
# this config only needs its id, and owning the tunnel would put its secret in the state and
# make a wrong edit (name, config_src) plan a destroy-and-recreate, which would cut the box
# off until a new token is vaulted and deployed.
resource "cloudflare_zero_trust_tunnel_cloudflared_config" "mediaserver" {
  account_id = var.account_id
  tunnel_id  = var.tunnel_id
  source     = "cloudflare"

  config = {
    ingress = [
      # The whole watch. hostname goes to Jellyfin (ADR 0016). cloudflared shares only the
      # hl-public network with Jellyfin, so no route can reach anything else. Origin options
      # stay at their defaults (plain HTTP inside Docker); the dashboard stores that as an
      # empty object, so it is written out to match.
      {
        hostname       = local.watch_host
        service        = "http://jellyfin:8096"
        origin_request = {}
      },
      # Required catch-all: anything else gets a 404 from the edge.
      {
        service = "http_status:404"
      },
    ]
  }

  lifecycle {
    prevent_destroy = true
  }
}
