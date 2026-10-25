# Adopts the hand-made configuration. Each id is built from git-ignored variables, so no
# identifier is committed. `tofu plan` must show only imports: N to import, 0 to add,
# 0 to change, 0 to destroy. Once a reviewed apply has imported everything, these blocks are
# no-ops; they (and the *_id variables) can then be removed in a follow-up change.

import {
  to = cloudflare_dns_record.apex
  id = "${var.zone_id}/${var.dns_record_ids.apex}"
}

import {
  to = cloudflare_dns_record.watch
  id = "${var.zone_id}/${var.dns_record_ids.watch}"
}

import {
  to = cloudflare_dns_record.game
  id = "${var.zone_id}/${var.dns_record_ids.game}"
}

import {
  to = cloudflare_dns_record.mx_route1
  id = "${var.zone_id}/${var.dns_record_ids.mx_route1}"
}

import {
  to = cloudflare_dns_record.mx_route2
  id = "${var.zone_id}/${var.dns_record_ids.mx_route2}"
}

import {
  to = cloudflare_dns_record.mx_route3
  id = "${var.zone_id}/${var.dns_record_ids.mx_route3}"
}

import {
  to = cloudflare_dns_record.dkim
  id = "${var.zone_id}/${var.dns_record_ids.dkim}"
}

import {
  to = cloudflare_dns_record.spf
  id = "${var.zone_id}/${var.dns_record_ids.spf}"
}

import {
  to = cloudflare_zone_setting.always_use_https
  id = "${var.zone_id}/always_use_https"
}

import {
  to = cloudflare_zone_setting.min_tls_version
  id = "${var.zone_id}/min_tls_version"
}

import {
  to = cloudflare_zone_setting.tls_1_3
  id = "${var.zone_id}/tls_1_3"
}

import {
  to = cloudflare_zone_setting.ssl
  id = "${var.zone_id}/ssl"
}

import {
  to = cloudflare_zone_setting.ssl_automatic_mode
  id = "${var.zone_id}/ssl_automatic_mode"
}

import {
  to = cloudflare_zone_setting.security_header
  id = "${var.zone_id}/security_header"
}

import {
  to = cloudflare_ruleset.cache
  id = "zones/${var.zone_id}/${var.cache_ruleset_id}"
}

import {
  to = cloudflare_zero_trust_tunnel_cloudflared_config.mediaserver
  id = "${var.account_id}/${var.tunnel_id}"
}

import {
  to = cloudflare_pages_domain.apex
  id = "${var.account_id}/${var.pages_project_name}/${var.domain}"
}

import {
  to = cloudflare_email_routing_settings.this
  id = var.zone_id
}

import {
  to = cloudflare_email_routing_address.owner
  id = "${var.account_id}/${var.email_routing_address_id}"
}

import {
  to = cloudflare_email_routing_rule.hello
  id = "${var.zone_id}/${var.email_routing_rule_id}"
}

import {
  to = cloudflare_email_routing_catch_all.this
  id = var.zone_id
}
