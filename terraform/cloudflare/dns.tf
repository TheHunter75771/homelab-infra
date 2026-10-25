# The zone's eight DNS records, as they are live. ttl = 1 means "automatic".

locals {
  watch_host = "watch.${var.domain}"
}

# Apex: the static homepage on Cloudflare Pages (ADR 0016). Never the tunnel.
resource "cloudflare_dns_record" "apex" {
  zone_id = var.zone_id
  name    = var.domain
  type    = "CNAME"
  content = "${var.pages_project_name}.pages.dev"
  proxied = true
  ttl     = 1

  lifecycle {
    prevent_destroy = true
  }
}

# watch.: public Jellyfin through the media server's tunnel (ADR 0016).
resource "cloudflare_dns_record" "watch" {
  zone_id = var.zone_id
  name    = local.watch_host
  type    = "CNAME"
  content = "${var.tunnel_id}.cfargotunnel.com"
  proxied = true
  ttl     = 1

  lifecycle {
    prevent_destroy = true
  }
}

# game.: the game-server relay on the edge VPS. DNS-only, because game traffic isn't HTTP.
resource "cloudflare_dns_record" "game" {
  zone_id = var.zone_id
  name    = "game.${var.domain}"
  type    = "A"
  content = var.game_ipv4
  proxied = false
  ttl     = 1

  lifecycle {
    prevent_destroy = true
  }
}

# Email Routing. Cloudflare created these when Email Routing was enabled. The MX and DKIM
# records are read-only in the API (meta.read_only): change them through Email Routing,
# not here, or the apply fails.

resource "cloudflare_dns_record" "mx_route1" {
  zone_id  = var.zone_id
  name     = var.domain
  type     = "MX"
  content  = "route1.mx.cloudflare.net"
  priority = var.email_mx_priorities.route1
  proxied  = false
  ttl      = 1

  lifecycle {
    prevent_destroy = true
  }
}

resource "cloudflare_dns_record" "mx_route2" {
  zone_id  = var.zone_id
  name     = var.domain
  type     = "MX"
  content  = "route2.mx.cloudflare.net"
  priority = var.email_mx_priorities.route2
  proxied  = false
  ttl      = 1

  lifecycle {
    prevent_destroy = true
  }
}

resource "cloudflare_dns_record" "mx_route3" {
  zone_id  = var.zone_id
  name     = var.domain
  type     = "MX"
  content  = "route3.mx.cloudflare.net"
  priority = var.email_mx_priorities.route3
  proxied  = false
  ttl      = 1

  lifecycle {
    prevent_destroy = true
  }
}

resource "cloudflare_dns_record" "dkim" {
  zone_id = var.zone_id
  name    = "cf2024-1._domainkey.${var.domain}"
  type    = "TXT"
  content = var.email_dkim_txt
  proxied = false
  ttl     = 1

  lifecycle {
    prevent_destroy = true
  }
}

resource "cloudflare_dns_record" "spf" {
  zone_id = var.zone_id
  name    = var.domain
  type    = "TXT"
  content = "\"v=spf1 include:_spf.mx.cloudflare.net ~all\""
  proxied = false
  ttl     = 1

  lifecycle {
    prevent_destroy = true
  }
}
