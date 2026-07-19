vault {
  address = "${VAULT_ADDR}"
}
auto_auth {
  # AppRole: vault-agent logs in with role_id/secret_id, then renews the token and
  # re-authenticates natively. Identical to the staging/prod app vault-agent.hcl.
  method "approle" {
    config = {
      role_id_file_path                   = "/vault/role_id"
      secret_id_file_path                 = "/vault/secret_id"
      remove_secret_id_file_after_reading = false
    }
  }

  sink "file" {
    config = {
      path = "/vault/.token"
    }
  }

  exit_on_err = true
}

template_config {
  static_secret_render_interval = "5m"
  exit_on_retry_failure = true
}

template {
  source      = "/etc/vault/secrets.ctmpl"
  destination = "/vault/secrets/.env"
}
