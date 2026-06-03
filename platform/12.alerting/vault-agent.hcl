exit_after_auth = false
pid_file = "/vault/.pid"

vault {
  address = "https://vault.zitian.party"
}

auto_auth {
  method "token_file" {
    config = {
      token_file_path = "/vault/token"
    }
  }
}

template {
  source      = "/etc/vault/secrets.ctmpl"
  destination = "/vault/secrets/.env"
  perms       = "0640"
}
