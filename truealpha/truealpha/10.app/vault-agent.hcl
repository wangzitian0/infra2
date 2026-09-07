vault {
  address = "${VAULT_ADDR}"
}
auto_auth {
  # AppRole: vault-agent logs in with role_id/secret_id, then renews the token
  # and re-authenticates natively when it can no longer be renewed.
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
  # The template is generated from the service's manifest; a required key missing in
  # Vault must fail the render (visible, no container start) instead of rendering
  # `%!q(<nil>)`. Optional keys are omitted by the template itself (empty_ok).
  error_on_missing_key = true
}
