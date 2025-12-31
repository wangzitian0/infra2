vault {
  address = "https://vault.zitian.party"
}

auto_auth {
  method {
    type = "token"
    config = {}
  }

  sink {
    type = "file"
    config = {
      path = "/home/vault/.vault-token"
    }
  }
}

template_config {
  static_secret_render_interval = "5m"
}

template {
  source      = "/etc/vault/secrets.ctmpl"
  destination = "/vault/secrets/.env"
  perms       = 0644
}
