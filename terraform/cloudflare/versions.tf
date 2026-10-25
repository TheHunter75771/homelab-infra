terraform {
  # OpenTofu only: the encryption block below is an OpenTofu feature.
  required_version = "~> 1.11"

  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "= 5.28.1"
    }
  }

  # Local backend (the default). The state file is git-ignored and encrypted client-side.
  encryption {
    key_provider "pbkdf2" "state" {
      passphrase = var.state_passphrase
    }

    method "aes_gcm" "state" {
      keys = key_provider.pbkdf2.state
    }

    state {
      method   = method.aes_gcm.state
      enforced = true
    }

    plan {
      method   = method.aes_gcm.state
      enforced = true
    }
  }
}

# Credentials come from the environment (CLOUDFLARE_API_TOKEN), never from this repo.
provider "cloudflare" {}
