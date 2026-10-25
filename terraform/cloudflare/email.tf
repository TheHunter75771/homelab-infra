# Email Routing: hello@<domain> forwards to Noah's own mailbox; every other address is
# rejected (the catch-all stays disabled). The MX/SPF/DKIM records are in dns.tf.
#
# The empty names and support_subaddress = false are spelled out because the provider
# stores the API's "" / false, and leaving them unset plans a change to null.

resource "cloudflare_email_routing_settings" "this" {
  zone_id            = var.zone_id
  support_subaddress = false

  lifecycle {
    prevent_destroy = true
  }
}

# Destination addresses are account-level and must be verified by a click in the mailbox.
resource "cloudflare_email_routing_address" "owner" {
  account_id = var.account_id
  email      = var.email_forward_to

  lifecycle {
    prevent_destroy = true
  }
}

resource "cloudflare_email_routing_rule" "hello" {
  zone_id  = var.zone_id
  name     = ""
  enabled  = true
  priority = 0

  matchers = [{
    type  = "literal"
    field = "to"
    value = "hello@${var.domain}"
  }]

  actions = [{
    type  = "forward"
    value = [cloudflare_email_routing_address.owner.email]
  }]

  lifecycle {
    prevent_destroy = true
  }
}

# Disabled on purpose: mail to any other address bounces instead of landing in the inbox.
resource "cloudflare_email_routing_catch_all" "this" {
  zone_id = var.zone_id
  name    = ""
  enabled = false

  matchers = [{
    type = "all"
  }]

  actions = [{
    type = "drop"
  }]

  lifecycle {
    prevent_destroy = true
  }
}
