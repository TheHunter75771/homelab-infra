# Zone settings the owner set (or that the edge depends on). Settings left at Cloudflare's
# defaults are not managed here.

resource "cloudflare_zone_setting" "always_use_https" {
  zone_id    = var.zone_id
  setting_id = "always_use_https"
  value      = "on"

  lifecycle {
    prevent_destroy = true
  }
}

resource "cloudflare_zone_setting" "min_tls_version" {
  zone_id    = var.zone_id
  setting_id = "min_tls_version"
  value      = "1.2"

  lifecycle {
    prevent_destroy = true
  }
}

resource "cloudflare_zone_setting" "tls_1_3" {
  zone_id    = var.zone_id
  setting_id = "tls_1_3"
  value      = "on"

  lifecycle {
    prevent_destroy = true
  }
}

# Edge-to-origin encryption mode. Every proxied origin here is Cloudflare's own (Pages, the
# tunnel), so "full" vs "strict" changes nothing today; it matters if a proxied record
# ever points at a real server.
resource "cloudflare_zone_setting" "ssl" {
  zone_id    = var.zone_id
  setting_id = "ssl"
  value      = "full"

  lifecycle {
    prevent_destroy = true
  }
}

# "custom" keeps the SSL mode above fixed: under "auto", Cloudflare's SSL/TLS recommender
# may change it on its own, which this code would then report as drift.
resource "cloudflare_zone_setting" "ssl_automatic_mode" {
  zone_id    = var.zone_id
  setting_id = "ssl_automatic_mode"
  value      = "custom"

  lifecycle {
    prevent_destroy = true
  }
}

# HSTS: 180 days, subdomains included, nosniff on. Preload stays off: it is close to
# irreversible and would bind every future subdomain to HTTPS for good.
resource "cloudflare_zone_setting" "security_header" {
  zone_id    = var.zone_id
  setting_id = "security_header"
  value = {
    strict_transport_security = {
      enabled            = true
      max_age            = 15552000
      include_subdomains = true
      preload            = false
      nosniff            = true
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}
