# Secrets — infra2's role

infra2 is the **deployed-secret source of truth**: Vault KV v2 plus
`secrets.ctmpl` templates, rendered into containers by the vault-agent sidecar
(no disk persistence; tmpfs). It supplies whatever a deployed app declares it
requires, and is rigorous, IaC-style, and assumed always-on.

This repo owns **only** the deployed/online layer. It does **not** own:

- local developer secrets — those live in 1Password, injected by `dev_env`;
- the app's variable schema or defaults — owned by the app (e.g. finance_report
  `config.py`), which **fails loudly at boot** if a required deployed secret is
  missing.

The cross-repo secret contract (who declares, who injects, who supplies, and
when the app fails loudly) is defined once, authoritatively, in
[finance_report `docs/ssot/deployment.md` → Secret Contract](https://github.com/wangzitian0/finance_report/blob/main/docs/ssot/deployment.md#secret-contract-cross-repo-seam).
This file is just infra2's one-paragraph statement of its part.
