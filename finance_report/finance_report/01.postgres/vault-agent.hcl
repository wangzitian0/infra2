pid_file = "/vault/.pid"

vault {
  address = "${VAULT_ADDR}"
}

auto_auth {
  method "token_file" {
    config = {
      token_file_path = "/vault/token"
    }
  }

  sink "file" {
    config = {
      path = "/vault/.token"
    }
  }
}

template {
  source      = "/etc/vault/secrets.ctmpl"
  destination = "/vault/secrets/.env"
}
