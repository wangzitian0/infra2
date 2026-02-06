vault {
  # VAULT_ADDR is set via container environment variable
  # vault-agent CLI automatically uses VAULT_ADDR env var when address is not specified
}

cache {
  use_auto_auth_token = true
}

auto_auth {
  method {
    type = "token_file"
    config = {
      token_file_path = "/vault/token"
    }
  }

  sink {
    type = "file"
    config = {
      path = "/home/vault/.vault-token"
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
  perms       = 0644
}
