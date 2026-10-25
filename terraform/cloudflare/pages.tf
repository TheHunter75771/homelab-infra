# The apex's binding to the homepage's Pages project (ADR 0016). Only the custom domain is
# managed here; the project itself (Git source, build command, env vars, deployments) stays
# with the homepage's own repository and the Pages dashboard. README.md says why.
resource "cloudflare_pages_domain" "apex" {
  account_id   = var.account_id
  project_name = var.pages_project_name
  name         = var.domain

  lifecycle {
    prevent_destroy = true
  }
}
