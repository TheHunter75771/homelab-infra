# Every value here identifies Noah's account, so none of them has a default.
# They live in the git-ignored terraform.tfvars (see terraform.tfvars.example).

variable "state_passphrase" {
  description = "Passphrase for client-side state and plan encryption. Set TF_VAR_state_passphrase from ~/.config/homelab-infra/tofu-state.key."
  type        = string
  sensitive   = true
  nullable    = false

  validation {
    condition     = length(var.state_passphrase) >= 32
    error_message = "state_passphrase must be at least 32 characters (generate it with: openssl rand -base64 32)."
  }
}

variable "account_id" {
  description = "Cloudflare account id."
  type        = string
  nullable    = false

  validation {
    condition     = can(regex("^[0-9a-f]{32}$", var.account_id))
    error_message = "account_id must be a 32-character lowercase hex id."
  }
}

variable "zone_id" {
  description = "Zone id of the apex domain."
  type        = string
  nullable    = false

  validation {
    condition     = can(regex("^[0-9a-f]{32}$", var.zone_id))
    error_message = "zone_id must be a 32-character lowercase hex id."
  }
}

variable "domain" {
  description = "The zone's apex domain (public_watch_host without the leading \"watch.\")."
  type        = string
  nullable    = false

  validation {
    condition     = can(regex("^[a-z0-9-]+(\\.[a-z0-9-]+)+$", var.domain))
    error_message = "domain must be a bare lowercase domain name, without a scheme or trailing dot."
  }
}

variable "tunnel_id" {
  description = "Id (UUID) of the media server's remotely managed Cloudflare tunnel."
  type        = string
  nullable    = false

  validation {
    condition     = can(regex("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", var.tunnel_id))
    error_message = "tunnel_id must be a lowercase UUID."
  }
}

variable "pages_project_name" {
  description = "Name of the Cloudflare Pages project that serves the apex homepage (its pages.dev subdomain has the same name)."
  type        = string
  nullable    = false
}

variable "game_ipv4" {
  description = "Public IPv4 address of the game-server relay (the edge VPS), for the DNS-only game record."
  type        = string
  nullable    = false

  validation {
    condition     = can(regex("^[0-9]{1,3}(\\.[0-9]{1,3}){3}$", var.game_ipv4)) && can(cidrhost("${var.game_ipv4}/32", 0))
    error_message = "game_ipv4 must be an IPv4 address."
  }
}

variable "email_forward_to" {
  description = "Verified Email Routing destination address that hello@<domain> forwards to."
  type        = string
  nullable    = false

  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+$", var.email_forward_to))
    error_message = "email_forward_to must be an email address."
  }
}

variable "email_dkim_txt" {
  description = "Content of the Email Routing DKIM TXT record, exactly as the API returns it (quoted strings). Public, but unique to this zone."
  type        = string
  nullable    = false
}

variable "email_mx_priorities" {
  description = "Priorities of the three Email Routing MX records. Cloudflare picks them per zone, so together they fingerprint it."
  type = object({
    route1 = number
    route2 = number
    route3 = number
  })
  nullable = false
}

# Import ids. They are only read by imports.tf, i.e. until the first reviewed apply has
# brought every resource into state; after that the import blocks and these variables can go.

variable "dns_record_ids" {
  description = "Ids of the existing DNS records, by key: apex, watch, game, mx_route1, mx_route2, mx_route3, dkim, spf."
  type = object({
    apex      = string
    watch     = string
    game      = string
    mx_route1 = string
    mx_route2 = string
    mx_route3 = string
    dkim      = string
    spf       = string
  })
  nullable = false
}

variable "cache_ruleset_id" {
  description = "Id of the zone's http_request_cache_settings entry-point ruleset."
  type        = string
  nullable    = false
}

variable "email_routing_address_id" {
  description = "Id of the Email Routing destination address."
  type        = string
  nullable    = false
}

variable "email_routing_rule_id" {
  description = "Id of the Email Routing rule for hello@<domain>."
  type        = string
  nullable    = false
}
