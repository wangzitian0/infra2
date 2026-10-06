# Credential Reach Record

> **SSOT Key**: `ops.credential_reach`
> **Core definition**: This record lists each credential, where it is stored, who reads it, what it can reach, and what a leak allows.

> **No values appear in this record.** It holds names, store locations, and scopes only. This repository is public, so this record is public.

---

## 1. Purpose and scope

The owner decided on #658 (2026-09-08) that rotation is not urgent and that some credentials are assumed leaked. This record bounds the reach of each credential. A statement "credential X leaked" then has edges.

- **In scope:** each credential that this repository wires, and each item in the 1Password vault `Infra2`.
- **Out of scope:** credentials on operator machines. State held only in vendor consoles. Secrets that application repositories own, such as their GitHub Actions secrets and image push tokens.
- **No rotation is implied.** This record is the input for a later rotation or scoping decision.
- **Issue:** #675.

### Evidence limits

- Rows marked `repo` prove the declared design. No live probe ran against the VPS, Vault, or any vendor.
- Names were checked on 2026-10-06 against `main` at `ac95936`. Commands: `gh secret list` and `gh variable list` (names only), `op item list` and `op item get ... | jq` (titles and field labels only), and `git grep`.
- Vendor-side scopes are unverified unless a repo file proves them. The cell says `unverified: needs console check` and names the console item.
- Engineering facts about Docker, Vault, and GitHub Actions are marked "engineering fact, not probed".

## 2. How to read the tables

| Column | Meaning |
|---|---|
| ID | Stable row id. Other sections refer to it. |
| Credential | The name that code or an operator uses. |
| Stored in | Every store that holds a copy. Stores are 1Password items, Vault paths, GitHub secrets, Dokploy environment, and host files. |
| Read by | The service, job, or tool that reads the value, with file references. |
| Reach | The systems and actions that the credential authenticates to. |
| A leak allows | The worst case that the Reach column supports. |
| Rotation | `P1` to `P7` point to section 6. `no procedure yet` means that no repo document states a procedure. |
| Verified | `repo:` names the file that proves the wiring. `unverified: needs console check` names the fact that only a vendor console or the host can prove. |

Reach classes:

- **R3:** root on the VPS, or read access to every deployed secret, or plaintext access to all data.
- **R2:** read or change the data or the control path of one system.
- **R1:** one narrow action, for example send a message or read status.
- **R0:** an identifier. It grants nothing alone.

## 3. Reach chains

Three credentials equal the host by their own API or UI: `C01`, `C02`, and `C03`. Other credentials reach one of them with one read.

| Credential | Hops to host | Path |
|---|---|---|
| `C01` Dokploy API key | 0 | Deploy any compose file. Docker lets a compose file mount host paths (engineering fact, not probed). |
| `C02` Dokploy admin password | 0 | The UI creates a new `C01`. The item is absent from the vault (F6), so the credential may live elsewhere. |
| `C03` SSH access | 0 | Root shell on the host. Port 22 is open to any source. |
| `C06` iac-runner AppRole | 1 | Reads `C01` from `secret/bootstrap/production/iac_runner`. |
| `C04` Vault root token | 1 | Reads the same path. Also writes any secret. |
| `C07` 1Password service account | 1 | Reads `C01` from the item `bootstrap/iac_runner`. Reads `C04` (ops.recovery.md SOP-007). |
| `C08` Connect token | 2 | Reads `C05` through the Connect API. `C05` yields `C04`. |
| `C05` Unseal keys | 2 | Threshold shares generate a root token (`C04`). Vault behavior, not probed. |
| `C09` Webhook secret | needs repo write | The runner deploys any commit that it can fetch. A new commit needs write access to the origin repository. |
| Any branch push to this repository | 0 | A same-repo pull request job runs branch code with `C01` (finding F2). |
| `C36` Backup remote | all data | The crypt keys and the Drive token give plaintext of every backup. |

Two containers hold several R3 credentials at once:

- **iac-runner** holds `C01`, `C06`, `C07`, and a root SSH key to the host (`bootstrap/06.iac_runner/compose.yaml`).
- **The alerting probe runner** reads `C01` when the key is set. It also mounts the Docker socket (finding F8).

## 4. Credential tables

### 4.1 Roots of the host and of the secret stores

| ID | Credential | Stored in | Read by | Reach | A leak allows | Rotation | Verified |
|---|---|---|---|---|---|---|---|
| C01 | `DOKPLOY_API_KEY` | 1Password: `init/env_vars`, `bootstrap/iac_runner`, `platform/production/alerting`, `platform/staging/alerting`.<br>Vault: `bootstrap/production/iac_runner`, `platform/production/alerting`, `platform/staging/alerting`.<br>GitHub secret.<br>Dokploy environment of the iac-runner compose. | iac-runner (`bootstrap/06.iac_runner/compose.yaml`, `libs/deploy/dokploy_client.py`).<br>Alerting probe runner (`platform/12.alerting/env.manifest.json`).<br>Workflows `deploy.yml`, `app-deploy-request.yml`, `deploy-report-main.yml`, `preview-teardown.yml`, `ops-checks.yml`.<br>Operator CLI: `DokployClient` falls back to 1Password. | Dokploy API behind `cloud.$INTERNAL_DOMAIN`. Calls in `libs/deploy/dokploy_client.py`: project, environment, compose create, update, deploy, redeploy, delete, domain create and delete, container list. Compose environment values are readable, including every AppRole id. | R3. Deploy any compose file on the host. Read every AppRole id and then every Vault secret. Delete services. A same-repo pull request job holds this key (F2). | no procedure yet. Up to nine stores hold a copy (F4). | repo: `libs/deploy/dokploy_client.py`. Names: op and gh, 2026-10-06. Key owner role and expiry: unverified: needs console check (Dokploy profile). |
| C02 | Dokploy admin password | The SSOT names the 1Password item `bootstrap/dokploy/admin`. The item is absent from `Infra2` on 2026-10-06 (F6). | An operator, in a browser. | Dokploy Web UI at `cloud.$INTERNAL_DOMAIN`. The UI creates API keys (`bootstrap/03.dokploy_setup/README.md`). | R3. Full control of Dokploy, and a new `C01`. | no procedure yet. | SSOT claim only. Where the password lives: unverified: needs console check. |
| C03 | SSH access to the VPS: `INFRA2_WATCHDOG_SSH_PRIVATE_KEY`, `INFRA2_WATCHDOG_SSH_HOST`, `INFRA2_WATCHDOG_SSH_USER` | GitHub secrets for all three. Variable `INFRA2_WATCHDOG_SSH_PORT`.<br>Host `/root/.ssh/id_ed25519`, mounted read-only into the iac-runner. The entrypoint copies it inside the container. | Workflows `deploy.yml` (jobs `deploy`, `bootstrap`), `app-deploy-request.yml`, `ops-checks.yml` (jobs `watchdog`, `digest`, `vault-self-refresh-audit`, `secrets-reconcile`).<br>iac-runner deployers: `ssh root@host` in `libs/deploy/deployer.py`. | SSH to the VPS. Port 22 is open to any source (`bootstrap/01.dokploy_install/hostfw/hostfw.nft`). Commands in the repo: `docker exec`, `docker run`, `docker inspect`, and `bash -s` with `scripts/deploy_iac_runner_bootstrap.sh`. The repo default user is `root`. | R3. A root shell, or Docker daemon access that equals root. | no procedure yet. | repo: `libs/deploy/deployer.py`, `tools/secrets_reconcile_check.py`. Names: gh, 2026-10-06. SSH user, authorized keys, sshd settings, and whether the two keys are one key: unverified: needs console check (host). |
| C04 | Vault root token (`bootstrap/vault/Root Token`) | 1Password `bootstrap/vault/Root Token`, fields `Root Token` and `Token`. No container holds it by design. | Operators: `bootstrap/05.vault/tasks.py` (`setup-approle`), `platform/10.authentik/shared_tasks.py`, ops.recovery.md SOP-003 and SOP-007. In SOP-007 the iac-runner reads it with `op read`. | Vault API at `vault.$INTERNAL_DOMAIN`. All paths, policies, auth methods, and seal state. | R3. Read and write every Vault secret, including `C01`. Mint tokens. An initial root token has no expiry unless revoked (Vault behavior, not probed). | no procedure yet. ops.recovery.md SOP-003 covers use only. | Item and labels: op, 2026-10-06. Whether the token is live and is the initial root token: unverified: needs console check (`vault token lookup` by the owner). |
| C05 | Vault unseal keys (`bootstrap/vault/Unseal Keys`) | 1Password `bootstrap/vault/Unseal Keys`, fields `Unseal Key 1` to `Unseal Key 5`. | `vault-unsealer` through 1Password Connect (`bootstrap/05.vault/unsealer.py`). Operators (ops.recovery.md SOP-001). | Unseal Vault at `vault.$INTERNAL_DOMAIN`. Threshold shares also start root-token generation (Vault behavior, not probed). | R3 with threshold shares. Unseal a sealed Vault and generate a new root token. With a copy of Vault storage (`C36`), read it offline. | no procedure yet. No repo task runs `vault operator rekey`. | repo: `bootstrap/05.vault/README.md` (5 keys, threshold 3). Names: op, 2026-10-06. Live threshold: unverified: needs console check (`vault status`). |
| C06 | AppRole `bootstrap/iac_runner` (`VAULT_ROLE_ID`, `VAULT_SECRET_ID`) | Dokploy environment of the iac-runner compose. The vault-agent container writes both values to `/vault/role_id` and `/vault/secret_id`. No label for them exists in the checked 1Password items. | iac-runner vault-agent and sync subprocesses (`bootstrap/06.iac_runner/compose.yaml`, `bootstrap/06.iac_runner/sync_runner.py`). | Policy `bootstrap/06.iac_runner/vault-policy.hcl`. Read `secret/bootstrap/+/iac_runner`. Create, read, update, patch, list on `secret/{platform,finance_report,truealpha}/+/*`, all environments. No delete. | R3. Read `C01`, `C07`, and `C09` from the bootstrap path. Read and overwrite every deployed secret. A token lives 24 hours (maximum 168). The secret id never expires. No CIDR bind exists in `bootstrap/05.vault/tasks.py`. | P1. The old secret id stays valid after P1 (F1). | repo: `bootstrap/06.iac_runner/vault-policy.hcl`, `bootstrap/05.vault/tasks.py`. Live policy and role settings: unverified: needs console check (`vault read auth/approle/role/...`). |
| C07 | `OP_SERVICE_ACCOUNT_TOKEN` | 1Password: `init/env_vars`, `bootstrap/iac_runner`, and two API-credential items titled `Service Account Auth Token: infra2-cli`.<br>Vault `bootstrap/production/iac_runner`.<br>Dokploy environment and container environment of the iac-runner. | iac-runner `op` calls: secret supply copies `human` values and mirrors values back (`libs/security/supply.py`). `tools/secrets_reconcile.py` reads `bootstrap/cloudflare-worker`. SOP-007 reads the root token. Local tools. | Vault `Infra2` in 1Password (48 items on 2026-10-06). Read is proven by SOP-007 and by supply. Write is proven by `mirror_to_1password`. | R3. Read `C04`, `C05`, `C01`, `C11`, `C12`, `C36`, and every `human` secret. A write changes the source values that the next deploy copies into Vault. | no procedure yet. | repo: ops.recovery.md SOP-007, `bootstrap/06.iac_runner/env.manifest.json`. Names: op, 2026-10-06. Which `infra2-cli` item is live, vault grants, and permission level: unverified: needs console check (1Password service accounts). |
| C08 | `OP_CONNECT_TOKEN` and `1password-credentials.json` | 1Password: `infra2.0 Access Token: infra2.0`, `infra2.0 Credentials File`, `bootstrap/1password/VPS-01 Credentials File`.<br>Host file `/data/bootstrap/1password/1password-credentials.json`.<br>Dokploy environment of the Vault compose (`OP_CONNECT_TOKEN`, `OP_VAULT_ID`, `OP_ITEM_ID`). | `vault-unsealer` (`bootstrap/05.vault/unsealer.py`). `op-connect-api` and `op-connect-sync` read the credentials file (`bootstrap/04.1password/`). | Connect API at `op.$INTERNAL_DOMAIN`. Proven: read of the item `bootstrap/vault/Unseal Keys`. Other items: the vault grants of the token. | R3. Read `C05` from the internet. Read other items in the granted vaults. | no procedure yet. | repo: `bootstrap/05.vault/unsealer.py`, `bootstrap/04.1password/README.md`. Names: op, 2026-10-06. Vault grants of the token and the server: unverified: needs console check (1Password Connect settings). |

### 4.2 Deploy path

| ID | Credential | Stored in | Read by | Reach | A leak allows | Rotation | Verified |
|---|---|---|---|---|---|---|---|
| C09 | `IAC_WEBHOOK_SECRET` (the same value is `WEBHOOK_SECRET` on the runner) | GitHub secret `IAC_WEBHOOK_SECRET`.<br>Vault `bootstrap/production/iac_runner`.<br>1Password `bootstrap/iac_runner` (mirror).<br>The GitHub repository webhook secret (`bootstrap/06.iac_runner/README.md`). | Workflows `deploy.yml`, `app-deploy-request.yml`, `reconcile-iac-inputs.yml`. iac-runner `webhook_server.py`. | Routes `/webhook`, `/deploy`, `/deploy/status` on `iac.$INTERNAL_DOMAIN`. `/deploy` accepts `env` `staging` or `production`, a 40-character commit SHA, a service list, and the action `sync` or `secrets-supply`. A signature stays valid for 300 seconds and each nonce works once. | R2. Request a production deploy of any commit that the runner can fetch. The runner checks the signature only (F3). A new commit needs write access to the origin repository. | P2. | repo: `bootstrap/06.iac_runner/webhook_server.py`. Names: op and gh, 2026-10-06. |
| C10 | AppRole credentials of the service stacks (`VAULT_ROLE_ID`, `VAULT_SECRET_ID`) | Dokploy environment of each service compose. Each vault-agent container writes the values to files. | Each service vault-agent (`bootstrap/templates/vault-agent.compose.yaml`). | The paths in section 5. Read only. | R2 per service. Read the secrets in section 5. The secret id never expires. Some policies include the Postgres superuser password. | P1. The old secret id stays valid (F1). | repo: the `vault-policy.hcl` files in section 5. Live policies: unverified: needs console check (`vault policy list`). |

### 4.3 Cloudflare and the watchdog

| ID | Credential | Stored in | Read by | Reach | A leak allows | Rotation | Verified |
|---|---|---|---|---|---|---|---|
| C11 | `CF_API_TOKEN` | 1Password `bootstrap/cloudflare` (field `CF_API_TOKEN`).<br>GitHub secret `CF_API_TOKEN`. | `bootstrap/02.dns_and_cert/tasks.py` (operator, through 1Password). `ops-checks.yml` job `facet-reconcile` (`tools/dns_drift_report.py`). | Cloudflare zone and DNS permissions on the two zones for `INTERNAL_DOMAIN` and `truealpha.club`. No Zone Settings and no Workers permission (`docs/ssot/bootstrap.dns_and_cert.md`, `docs/ssot/bootstrap.vars_and_secrets.md`). | R2. Rewrite DNS records in both zones. Point hostnames at another server. Hostname control also lets a holder pass domain validation for new certificates (engineering fact, not probed). | no procedure yet. | repo: `docs/ssot/bootstrap.dns_and_cert.md` (a claim dated 2026-07-20). Names: op and gh, 2026-10-06. Current permission list and resources: unverified: needs console check (Cloudflare API tokens). |
| C12 | `CF_WORKER_API_TOKEN` (field `CLOUDFLARE_WORKER_API_TOKEN`) | 1Password `bootstrap/cloudflare-worker`.<br>GitHub secret `CF_WORKER_API_TOKEN`. | `deploy-cloudflare-watchdog.yml` (`wrangler deploy`). iac-runner `tools/secrets_reconcile.py` (analytics read, through 1Password). `platform/12.alerting/README.md` (`wrangler secret put`). | The Cloudflare account that hosts the watchdog Worker. Deploy the Worker, set its secrets, read account analytics. | R2. Replace the watchdog code. A replaced script reads the Worker secrets (`C13`, `C14`, `C15`, `C16`, `C19`, `C21`). The holder can also stop the out-of-band alerts. | no procedure yet. | repo: `.github/workflows/deploy-cloudflare-watchdog.yml`, `tools/secrets_reconcile.py`. Names: op and gh, 2026-10-06. Token permissions: unverified: needs console check (Cloudflare API tokens). |
| C13 | `HEARTBEAT_TOKEN` (the same value is `INFRA_PROBE_HEARTBEAT_TOKEN`) | Worker secret `HEARTBEAT_TOKEN`.<br>Vault and 1Password `platform/{production,staging}/alerting`. | Worker route `POST /heartbeat` (`cloudflare/infra-watchdog/worker.js`). Probe runner (`tools/infra_probe_runner.py`). | Worker route `POST /heartbeat`. | R2. Send forged heartbeats. A fresh `ok` heartbeat hides a VPS outage. A forged `failing_public_routes` list hides the entrypoint pages (suppression rules in `cloudflare/infra-watchdog/README.md`). | no procedure yet. Both sides must hold the same value. | repo: `cloudflare/infra-watchdog/README.md`. Names: op, 2026-10-06. Whether the Worker secret is set: unverified: needs console check (Worker settings). |
| C14 | `WATCHDOG_STATUS_TOKEN` | 1Password `bootstrap/cloudflare-worker`.<br>Worker secret.<br>GitHub secret `INFRA2_WATCHDOG_WORKER_STATUS_TOKEN`. | Worker routes `/status` and `/outages`. Workflows `ops-checks.yml` (jobs `watchdog`, `digest`) and `deploy-cloudflare-watchdog.yml`. | Read-only Worker routes. Output: active alerts, heartbeat details, outages. | R1. Read the watchdog state. | P3. | repo: `platform/12.alerting/README.md`. Names: op and gh, 2026-10-06. |
| C15 | `WATCHDOG_DEADMAN_PING_URL` | Worker secret only. | Worker cron run (`cloudflare/infra-watchdog/worker.js`). | One Healthchecks.io check. The URL is the credential. | R1. Ping the check. A stopped watchdog cron then looks alive. | no procedure yet. | repo: `cloudflare/infra-watchdog/README.md`. Whether the secret is set: unverified: needs console check (Worker settings, Healthchecks.io). |

### 4.4 Alerting, chat, and mail

| ID | Credential | Stored in | Read by | Reach | A leak allows | Rotation | Verified |
|---|---|---|---|---|---|---|---|
| C16 | Feishu app of the alert bridge: `FEISHU_APP_ID`, `FEISHU_APP_SECRET` | 1Password `platform/{production,staging}/alerting`.<br>Vault `platform/{production,staging}/alerting`.<br>Worker secret `FEISHU_APP_SECRET`. | Alert bridge (`libs/alerting/delivery.py`). Watchdog Worker (`cloudflare/infra-watchdog/worker.js`). | Feishu Open Platform. Code calls: tenant access token, then send a message to a chat id. The granted scopes are not in the repo. | R1 at minimum. Send messages as the bot into the alert chats. Fake or hide alerts. More if the app holds wider scopes. | no procedure yet. | repo: `libs/alerting/delivery.py`. Names: op, 2026-10-06. App scopes and chat membership: unverified: needs console check (Feishu app console). |
| C17 | Feishu app of the out-of-band path: `INFRA2_OUT_OF_BAND_FEISHU_APP_ID`, `INFRA2_OUT_OF_BAND_FEISHU_APP_SECRET` | GitHub secrets. | Workflows `ops-checks.yml`, `preview-teardown.yml`, `reconcile-iac-inputs.yml`. The `deploy-v2-canary` job gives them to non-PR events only. | Same API as `C16`. | R1 at minimum. Same as `C16`. | no procedure yet. | repo: `.github/workflows/ops-checks.yml`. Names: gh, 2026-10-06. Whether this app is the app of `C16`: unverified: needs console check (Feishu app console). |
| C18 | Feishu app of the reports path: `INFRA2_REPORTS_FEISHU_APP_ID`, `INFRA2_REPORTS_FEISHU_APP_SECRET`, `INFRA2_REPORTS_FEISHU_CHAT_ID` | GitHub secrets. GitHub variable `INFRA2_REPORTS_FEISHU_CHAT_ID`. | Workflow `ops-checks.yml` (jobs `watchdog`, `facet-reconcile`, `vault-self-refresh-audit`, `secrets-reconcile`). | Same API as `C16`. | R1 at minimum. Same as `C16`. | no procedure yet. | repo: `.github/workflows/ops-checks.yml`. Names: gh, 2026-10-06. Whether this app is the app of `C16`: unverified: needs console check (Feishu app console). |
| C19 | `FEISHU_WEBHOOK_URL` and `INFRA2_OUT_OF_BAND_FEISHU_WEBHOOK_URL` | Vault and 1Password `platform/{production,staging}/alerting` (key is optional).<br>Worker secret.<br>The GitHub secret is referenced but not set on 2026-10-06 (F9). | Alert bridge. Worker. Workflows that call the out-of-band step. | One Feishu chat. The URL is the credential. | R1. Post into one chat. No read. | no procedure yet. | repo: `platform/12.alerting/env.manifest.json`. GitHub secret: absent in `gh secret list`, 2026-10-06. |
| C20 | `BRIDGE_BASIC_AUTH_USERNAME`, `BRIDGE_BASIC_AUTH_PASSWORD` | Vault and 1Password `platform/{production,staging}/alerting`. | Alert bridge, SigNoz webhook channel (`platform/12.alerting/README.md`). | The alert bridge webhook. The bridge is internal only. | R1. Post fake alerts from inside the Docker network. | no procedure yet. | repo: `platform/12.alerting/env.manifest.json`. Names: op, 2026-10-06. |
| C21 | `RESEND_API_KEY` | 1Password `Resend` and `platform/shared/openpanel`.<br>Vault `platform/{production,staging}/openpanel` (`resend_api_key`).<br>Worker secret (optional email fallback). | OpenPanel containers (`platform/24.openpanel/env.manifest.json`). Watchdog Worker (`cloudflare/infra-watchdog/worker.js`). | The Resend account. Send email from the verified sender domain. | R2. Send mail as the domain. Phishing risk. | no procedure yet. | repo: `platform/24.openpanel/env.manifest.json`. Names: op, 2026-10-06. Key permission, sender domains, and whether the stores hold one key: unverified: needs console check (Resend dashboard). |

### 4.5 GitHub tokens

| ID | Credential | Stored in | Read by | Reach | A leak allows | Rotation | Verified |
|---|---|---|---|---|---|---|---|
| C22 | `GITHUB_TOKEN` | None. GitHub issues it for each job. | Every workflow. | Default `contents: read`. `ops-checks.yml` jobs `watchdog`, `facet-reconcile`, `vault-self-refresh-audit`, `secrets-reconcile` add `issues: write`. `reconcile-iac-inputs.yml` has `contents: write`. `docs.yml` has `pages: write` and `id-token: write`. | R1. Use is limited to the job. | not applicable: the token expires at job end. | repo: `.github/workflows/*.yml` permission blocks. |
| C23 | `INFRA2_HOOKS_READ_TOKEN` | GitHub secret. | `ops-checks.yml` job `audit` (`tools/webhook_delivery_audit.py`). | Repository webhook deliveries. The workflow comment states `admin:repo_hook` read. | R1. Read the webhook delivery history. | no procedure yet. | repo: `tools/README.md`. Names: gh, 2026-10-06. Token type, scopes, and repositories: unverified: needs console check (GitHub token settings). |
| C24 | `PREVIEW_LEAK_GH_TOKEN` | Not set on 2026-10-06 (F9). | `ops-checks.yml` job `preview-leak-check`. It falls back to the job `github.token`. | Open pull requests of the application repository. | None while it is unset. | not applicable while unset. | repo: `.github/workflows/ops-checks.yml`. Absent in `gh secret list`, 2026-10-06. |

### 4.6 Observability

| ID | Credential | Stored in | Read by | Reach | A leak allows | Rotation | Verified |
|---|---|---|---|---|---|---|---|
| C25 | `SIGNOZ_API_KEY` (1Password field `SIGNOZ_API_TOKEN`); SigNoz admin login | GitHub secret `SIGNOZ_API_KEY`.<br>1Password `platform/signoz/admin`. | `apply-observability.yml` (`platform/12.alerting/shared_tasks.py`). | SigNoz admin API behind `signoz.$INTERNAL_DOMAIN`. The workflow comment states an admin key. | R2. Change or delete alert rules, channels, and dashboards. Read logs, traces, and metrics. | P7 creates a new key. No procedure revokes the old key. | repo: `.github/workflows/apply-observability.yml`. Names: op and gh, 2026-10-06. Key role: unverified: needs console check (SigNoz settings). |

### 4.7 Runtime secrets in Vault

The deploy supply generates most of these values once (source class `runtime`). Each row lists every 1Password copy.

| ID | Credential | Stored in | Read by | Reach | A leak allows | Rotation | Verified |
|---|---|---|---|---|---|---|---|
| C26 | Postgres superuser passwords: `root_password` (platform), `POSTGRES_PASSWORD` (finance_report, truealpha) | Vault `platform/{env}/postgres`, `finance_report/{env}/postgres`, `truealpha/{env}/postgres`. | The postgres stacks. AppRoles of authentik, prefect, openpanel (platform password), the finance_report app, the truealpha app, the data engine. | The superuser `postgres` of each instance. The platform and finance_report compose files publish no port. The truealpha compose publishes on host loopback. | R2. Read and change every database in that instance. Platform: authentik, openpanel, prefect. finance_report: personal financial records. A foothold on the host or the Docker network is needed. | no procedure yet. | repo: `platform/01.postgres/compose.yaml`, `truealpha/truealpha/01.postgres/compose.yaml`, the policy files in section 5. Live bindings: unverified: needs console check (host `docker port`). |
| C27 | Redis passwords: `password` (platform), `PASSWORD` (finance_report) | Vault `platform/{env}/redis`, `finance_report/{env}/redis`. | The redis stacks. AppRoles of authentik, prefect, openpanel, todo, the finance_report app. | Redis AUTH on the Docker network. | R1. Read and change cached and queued data. | no procedure yet. | repo: the policy files in section 5. |
| C28 | S3 root: `root_user`, `root_password` (`MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD`) | Vault `platform/{env}/minio`.<br>1Password: `platform/{production,staging}/minio`, `platform/minio/admin`, `platform/minio/admin-staging`. | The S3 stack. `platform/03.s3/shared_tasks.py` (`mc admin`). App deployers that create buckets and users. | S3 API and console routes behind Traefik. All buckets and the admin API. | R2. Read, overwrite, and delete every object of finance_report and truealpha. Create users and policies. | no procedure yet. | repo: `platform/03.s3/compose.yaml`, `platform/03.s3/shared_tasks.py`. Names: op, 2026-10-06. |
| C29 | Bucket-scoped S3 keys: `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_PUBLIC_ACCESS_KEY`, `S3_PUBLIC_SECRET_KEY` | Vault `finance_report/{env}/app`, `truealpha/{env}/app`, `truealpha/{env}/data_engine`. | The app and data-engine containers. | One bucket. The policy allows list, get, put, delete on that bucket (`platform/03.s3/shared_tasks.py`). | R2 for one bucket. | no procedure yet. | repo: `platform/03.s3/shared_tasks.py`. |
| C30 | Probe and canary credentials: `PROBE_POSTGRES_PASSWORD`, `PROBE_S3_ACCESS_KEY`, `PROBE_S3_SECRET_KEY`, `CANARY_POSTGRES_PASSWORD`, `CANARY_S3_ACCESS_KEY`, `CANARY_S3_SECRET_KEY` | Vault and 1Password: `platform/{production,staging}/alerting` and `platform/{production,staging}/todo`. | Probe runner. Canary todo service. | Canary role `canary_ro`: login only, no grants, read-only sessions. Canary S3 key: one bucket (`docs/ssot/platform.canary_todo.md`). Probe grants are not in the repo. | R1. Run `SELECT 1` and use one canary bucket. | no procedure yet. | repo: `platform/30.todo/deploy.py`. Probe role and probe S3 key grants: unverified: needs console check (database and S3 admin). |
| C31 | Authentik secrets: `secret_key`, `bootstrap_password`, `bootstrap_email`, `root_token` | Vault `platform/{env}/authentik`.<br>1Password `platform/{production,staging}/authentik` (`bootstrap_password`). | Authentik containers. `platform/10.authentik/shared_tasks.py` (`root_token`, admin API). | Authentik admin UI and API. The SSO of every host behind Forward Auth (`docs/ssot/platform.sso.md`). | R2. Create users. Grant access to SSO-protected applications. Read OIDC client settings. | P5 covers OIDC client secrets only. Other keys: no procedure yet. | repo: `libs/security/registry.py`, `platform/10.authentik/shared_tasks.py`. Names: op, 2026-10-06. |
| C32 | Application keys: `SECRET_KEY`, `COOKIE_SECRET`, `LLM_ENCRYPTION_KEYS`, `APP_SERVICE_DB_PASSWORD` | Vault `finance_report/{env}/app`, `truealpha/{env}/app`, `platform/{env}/openpanel`. | The app containers. | Session signing (HS256 in truealpha). `LLM_ENCRYPTION_KEYS` are Fernet keys that encrypt provider keys in the database. `APP_SERVICE_DB_PASSWORD` is the `app_service_login` role. | R2. Forge session cookies. With a database copy, decrypt stored provider keys. | no procedure yet. The manifest describes a key prepend for `LLM_ENCRYPTION_KEYS`. | repo: the app manifests that `libs/security/registry.py` lists. |

### 4.8 Vendor API keys

| ID | Credential | Stored in | Read by | Reach | A leak allows | Rotation | Verified |
|---|---|---|---|---|---|---|---|
| C33 | Model provider keys: `ZAI_API_KEY`, `AI_API_KEY`, `OPENROUTER_API_KEY`, `LLM_API_KEY`, `ANTHROPIC_API_KEY`, `GLM_API_TOKEN` | 1Password: `finance_report/{production,staging}/app`, `truealpha/shared/app`, `truealpha/shared/data_engine`, `GLM-token`, `playground/investbrain`.<br>Vault app and data-engine paths. No 1Password field `ANTHROPIC_API_KEY` exists. | finance_report app, truealpha app and `llm-service`, data engine. | The provider accounts. Model calls on the owner account. | R1. Spend provider quota. Exhaust the rate limit, so AI features stop. | no procedure yet. P6 covers OpenRouter only and names a Vault path that no registered service uses. | repo: the app manifests. Names: op, 2026-10-06. Provider spend limits and key scopes: unverified: needs console check (provider consoles). |
| C34 | `ZAI_CODING_CN_API_KEY` | GitHub secret. | `ops-checks.yml` job `pi-chain-smoke`. | The coding-plan model endpoint. | R1. Spend quota. | no procedure yet. | repo: `.github/workflows/ops-checks.yml`. Names: gh, 2026-10-06. Provider limits: unverified: needs console check (provider console). |
| C35 | Data vendor keys: `TWELVE_DATA_API_KEY`, `OPENFIGI_API_KEY` | 1Password: `truealpha/shared/data_engine`, `OPEN_FIGI_KEY`, `playground/investbrain` (`TWELVEDATA_API_SECRET`).<br>Vault `truealpha/{env}/data_engine`. | The truealpha data engine. | The vendor APIs. | R1. Spend vendor quota. A data capture outage follows. | no procedure yet. | repo: `truealpha/truealpha/20.data_engine/vault-policy.hcl`. Names: op, 2026-10-06. Plan limits: unverified: needs console check (vendor consoles). |

### 4.9 Backup and registry

| ID | Credential | Stored in | Read by | Reach | A leak allows | Rotation | Verified |
|---|---|---|---|---|---|---|---|
| C36 | rclone remote `gdrive-backup`: `Client ID`, `Client Secret`, `refresh_token`, `crypt_password`, `crypt_salt`, `rclone_conf` | 1Password `bootstrap/gdrive`.<br>Host `~/.config/rclone/rclone.conf` (ops.recovery.md, restore step). | `tools/host_backup.sh` and `tools/backup_runner.py`. Operators during restore. | Google Drive, with the OAuth scope in `rclone_conf`. The folder `gdrive-backup:infra2`: weekly (60 days) and quarterly (2 years) archives of Postgres, Redis, S3, Vault, and 1Password Connect state. | R3 for data. The crypt keys plus Drive access give plaintext of every archive. The Drive token alone gives ciphertext and lets a holder delete or replace backups. | no procedure yet. | repo: `docs/ssot/ops.recovery.md` SOP-005, SOP-006. Names: op, 2026-10-06. OAuth scope, client ownership, and the Google account scope: unverified: needs console check (Google Cloud and Drive). |
| C37 | Container registry credentials: item `dokploy-docker` (`Registry Name`, `Image Prefix`, `Registry URL`) | 1Password `dokploy-docker`. | `DokployClient` probes this item for `DOKPLOY_API_KEY`. No other reader exists. | The registry named in the item. This repository pulls application images without credentials (`tools/deploy_v2.py`). No GHCR push credential is wired here. | Unknown. Depends on the registry and the account role. | no procedure yet. | repo: `libs/deploy/dokploy_client.py`, `tools/deploy_v2.py`. Names: op, 2026-10-06. Registry type and role: unverified: needs console check (registry account). |

### 4.10 Held with no reader in this repository

`git grep` finds no reader for these items on `main` at `ac95936`. A holder of `C07` reads all of them. The owner decides for each item: keep, document a reader, or revoke at the vendor.

| ID | Credential | Stored in | Read by | Reach | A leak allows | Rotation | Verified |
|---|---|---|---|---|---|---|---|
| C38 | `CF_ROOT_API_TOKEN`, and `CLOUDFLARE_API_TOKEN` in the item `Infra-Cloudflare` | 1Password `bootstrap/cloudflare`, `Infra-Cloudflare`. | No reader found. | Unknown. The name `ROOT` suggests wider scope than `C11`. | Unknown. Worst case: full control of the Cloudflare account. | no procedure yet. | Labels: op, 2026-10-06. Scope and whether the tokens are live: unverified: needs console check (Cloudflare API tokens). |
| C39 | GitHub token items: `GitHub Personal Access Token` (`token`); `local` fields `[deprecated]pat`, `[deprecated]GH_TOKEN` | 1Password. | No reader found. | Unknown. A personal token can hold write access to every repository of the owner. | Unknown. | no procedure yet. | Labels: op, 2026-10-06. Scopes and whether the tokens are revoked: unverified: needs console check (GitHub token settings). |
| C40 | `zitian.github.pem` (document) | 1Password. | No reader found. | Unknown. The name suggests a GitHub App private key. | Unknown. | no procedure yet. | Title: op, 2026-10-06. Purpose: unverified: needs console check (GitHub App settings). |
| C41 | Item `local`: fields `[deprecated]api_token`, `access_key_id`, `secret_access_key`, `bucket`, `account_id`, `host`, and two `api_key` | 1Password. | No reader found. | Unknown. | Unknown. | no procedure yet. | Labels: op, 2026-10-06. Provider, scope, and revocation: unverified: needs console check (provider consoles). |
| C42 | Item `Infra-Flash`: `INFRA_FLASH_WEBHOOK_SECRET`, `INFRA_FLASH_WEB_PASSWORD`, `INFRA_FLASH_APP_ID` | 1Password. | No reader found. | Unknown. | Unknown. | no procedure yet. | Labels: op, 2026-10-06. Purpose: unverified: needs console check (owner). |
| C43 | Label `GITLAB_TOKEN` in `init/env_vars` | 1Password `init/env_vars`. | No reader found. This repository does not use GitLab. | Unknown. | Unknown. | no procedure yet. | Label: op, 2026-10-06. Whether the token is live: unverified: needs console check (GitLab). |
| C44 | Four deprecated 1Password tokens: `[deprecated]Service Account Auth Token: Infra2`, `[deprecated]VPS-01 Access Token: new_token`, `[deprecated]bootstrap/1password/VPS-01 Access Token: own_service`, `[deprecated]init/1password_service_token` | 1Password (four titles with this prefix on 2026-10-06). | No reader found. | Unknown. Each is a service-account or Connect token. | R3 if a token is still live and holds `Infra2` access. | no procedure yet. | Titles: op, 2026-10-06. Revocation: unverified: needs console check (1Password service accounts and Connect servers). |
| C45 | Login and key items: `pipeline`, `truealpha admin login`, `truealpha.club owner login`, `truealpha e2e walk principals`, `bootstrap/minio`, `3rd/Google Drive API Access` | 1Password. | No reader found. | Unknown. | Unknown. | no procedure yet. | Titles: op, 2026-10-06. Purpose and scope: unverified: needs console check (owner). |

### 4.11 Identifiers (R0)

These values grant nothing alone. They give an attacker targeting data. Some sit in secret stores.

| Name | Stored in | What it names |
|---|---|---|
| `CF_ZONE_ID` | GitHub secret; 1Password `bootstrap/cloudflare` | The default Cloudflare zone. |
| `CF_ACCOUNT_ID` | 1Password `bootstrap/cloudflare` | The Cloudflare account. |
| `INFRA2_WATCHDOG_SSH_HOST`, `INFRA2_WATCHDOG_SSH_USER` | GitHub secrets | The SSH host and user (see `C03`). |
| `VPS_HOST`, `GIT_REPO_URL` | 1Password `init/env_vars`, `bootstrap/iac_runner`; Vault `bootstrap/production/iac_runner` | The VPS address and the origin repository. |
| `INFRA2_OUT_OF_BAND_FEISHU_CHAT_ID`, `INFRA2_OUT_OF_BAND_FEISHU_API_BASE`, `INFRA2_OUT_OF_BAND_ALERT_DELIVERY_MODE` | GitHub secrets | The out-of-band alert chat, API base, and delivery mode. |
| `FEISHU_CHAT_ID`, `FEISHU_REPORT_CHAT_ID`, `FEISHU_API_BASE`, `ALERT_DELIVERY_MODE` | 1Password and Vault `platform/{env}/alerting` | The alert chats, API base, and delivery mode. |
| `INFRA_PROBE_HEARTBEAT_URL`, `PROBE_S3_BUCKET` | 1Password and Vault `platform/{env}/alerting` | The Worker heartbeat route and the probe bucket. |
| `OP_VAULT_ID`, `OP_ITEM_ID` | Dokploy environment of the Vault compose | The 1Password vault and the item that holds the unseal keys. |
| `SEC_USER_AGENT` | 1Password `truealpha/shared/app`, `truealpha/shared/data_engine`; Vault `truealpha/{env}/*` | The contact string that the SEC API requires. |

## 5. Vault AppRoles

Each service has one role per environment: `production` and `staging`. `bootstrap/iac_runner` has `production` only. Each preview row has a role that reads the `staging` application path. Each role reads only the listed paths. The policy files are in each service directory (`vault-policy.hcl`).

All roles share these facts. The secret id never expires (`secret_id_ttl=0`). A token lives 24 hours with a 168-hour maximum. `bootstrap/05.vault/tasks.py` sets no CIDR bind. The Vault API is routed at `vault.$INTERNAL_DOMAIN`.

| Service | Paths read | Secret names behind the paths | Reach |
|---|---|---|---|
| `bootstrap/iac_runner` | `secret/bootstrap/+/iac_runner` (read, list).<br>`secret/{platform,finance_report,truealpha}/+/*` (create, read, update, patch, list). | `DOKPLOY_API_KEY`, `OP_SERVICE_ACCOUNT_TOKEN`, `WEBHOOK_SECRET`, `VPS_HOST`, `GIT_REPO_URL`, and every secret below. | R3. Row `C06`. |
| `platform/postgres` | `platform/{env}/postgres` | `root_password` | R2. Postgres superuser. |
| `platform/redis` | `platform/{env}/redis` | `password` | R1. |
| `platform/minio` | `platform/{env}/minio` | `root_user`, `root_password` | R2. S3 root. |
| `platform/authentik` | `platform/{env}/authentik`, `platform/{env}/postgres`, `platform/{env}/redis` | `bootstrap_email`, `bootstrap_password`, `root_token`, `secret_key`, `root_password`, `password` | R2. Identity provider admin and the platform Postgres superuser. |
| `platform/alerting` | `platform/{env}/alerting` | 15 keys. Includes `DOKPLOY_API_KEY`, `FEISHU_APP_SECRET`, `FEISHU_WEBHOOK_URL`, `INFRA_PROBE_HEARTBEAT_TOKEN`, `PROBE_POSTGRES_PASSWORD`, `PROBE_S3_SECRET_KEY`. | R3 when `DOKPLOY_API_KEY` is set. |
| `platform/prefect` | `platform/{env}/prefect`, `platform/{env}/postgres`, `platform/{env}/redis` | No own keys. `root_password`, `password`. | R2. Platform Postgres superuser. |
| `platform/openpanel` | `platform/{env}/openpanel`, `platform/{env}/postgres`, `platform/{env}/redis` | `cookie_secret`, `resend_api_key`, `root_password`, `password` | R2. Platform Postgres superuser and mail key. |
| `platform/todo` | `platform/{env}/todo`, `platform/{env}/redis` | `CANARY_POSTGRES_PASSWORD`, `CANARY_S3_ACCESS_KEY`, `CANARY_S3_SECRET_KEY`, `password` | R1. |
| `finance_report/postgres` | `finance_report/{env}/postgres` | `POSTGRES_PASSWORD` | R2. Postgres superuser. |
| `finance_report/redis` | `finance_report/{env}/redis` | `PASSWORD` | R1. |
| `truealpha/postgres` | `truealpha/{env}/postgres` | `POSTGRES_PASSWORD` | R2. Postgres superuser. |
| `finance_report/app` | `finance_report/{env}/app`, `finance_report/{env}/postgres`, `finance_report/{env}/redis` | `LLM_ENCRYPTION_KEYS`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_PUBLIC_ACCESS_KEY`, `S3_PUBLIC_SECRET_KEY`, `SECRET_KEY`, `ZAI_API_KEY`, `S3_BUCKET`, `POSTGRES_PASSWORD`, `PASSWORD` | R2. Postgres superuser and provider key. |
| `finance_report/app` (preview) | `finance_report/staging/app` | Same app keys as the staging role. | R2. A preview runs pull-request code with the staging app secrets. |
| `truealpha/app` | `truealpha/{env}/app`, `truealpha/{env}/postgres` | `ANTHROPIC_API_KEY`, `APP_SERVICE_DB_PASSWORD`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_BUCKET`, `SECRET_KEY`, `SEC_USER_AGENT`, `POSTGRES_PASSWORD` | R2. Postgres superuser and provider key. |
| `truealpha/app` (preview) | `truealpha/staging/app` | Same app keys as the staging role. | R2. A preview runs pull-request code with the staging app secrets. |
| `truealpha/data_engine` | `truealpha/{env}/data_engine`, `truealpha/{env}/postgres` | `LLM_API_KEY`, `OPENFIGI_API_KEY`, `TWELVE_DATA_API_KEY`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_BUCKET`, `POSTGRES_PASSWORD`, and the release identity keys | R2. Postgres superuser and vendor keys. |

## 6. Rotation procedures that exist

| ID | Procedure | Pointer | Known gap |
|---|---|---|---|
| P1 | Re-issue the AppRole credentials of one service. | `bootstrap/05.vault/README.md` section 4. `docs/ssot/ops.recovery.md` SOP-007. | No task destroys the old secret id (F1). |
| P2 | Rotate `WEBHOOK_SECRET` and `IAC_WEBHOOK_SECRET`. | `docs/ssot/bootstrap.iac_runner.md` section 8.3. | The steps pre-date the source classes. A Vault write now needs `--break-glass`. The GitHub secret must hold the same value. |
| P3 | Rotate `WATCHDOG_STATUS_TOKEN` in 1Password, the Worker, and GitHub. | `platform/12.alerting/README.md` section "Out-of-band Watchdog". | None known. |
| P4 | Generic steps: update Vault, update Dokploy environment, restart. | `docs/onboarding/04.secrets.md` section "密钥轮换". | Step 1 conflicts with the rule that only the deploy supply writes Vault (`docs/ssot/bootstrap.vars_and_secrets.md` section 1.4). |
| P5 | Rotate an Authentik OIDC client secret. | `docs/ssot/platform.sso.md` SOP-004. | Covers client secrets only. |
| P6 | Rotate the OpenRouter key. | `docs/ssot/platform.ai.md` SOP-001. | The Vault path `secret/platform/<env>/ai` has no registered service. |
| P7 | Create a new SigNoz API key. | `platform/12.alerting/README.md` section "SigNoz Channel" (`invoke signoz.shared.create-api-key`). | Creates only. The old key stays valid. |

## 7. Findings

Each finding is a fact with its evidence. This record proposes no change. The owner decides whether a finding needs an issue.

- **F1. AppRole re-issue keeps the old secret id valid.** `vault.setup-approle` writes a new secret id. `bootstrap/05.vault/tasks.py` has no `secret-id/destroy` call, and `secret_id_ttl=0`. An assumed-leaked secret id stays valid after P1. Vault can destroy a secret id by accessor, or delete and recreate the role (Vault behavior, not probed).
- **F2. A same-repo pull request job runs branch code with `DOKPLOY_API_KEY`.** `ops-checks.yml` job `deploy-v2-canary` runs on `pull_request` from the same repository and exports the key. The job comment states that fork pull requests get no secrets. Any holder of branch-push access reads the key.
- **F3. The iac-runner checks the signature only.** `webhook_server.py` accepts `env=production` with a valid signature. The on-main check and the production gate sit in the caller (`tools/reconcile_iac_inputs.py`). A holder of `IAC_WEBHOOK_SECRET` skips both.
- **F4. One credential has many stores.** `C01` has up to nine stores: four 1Password items, three Vault paths, one GitHub secret, and one Dokploy environment. One rotation must change all of them. `C07` and `C09` also have several stores.
- **F5. A console task prints part of a token.** `platform/10.authentik/shared_tasks.py` prints the first 20 characters of the Authentik admin API token (line `Token prefix:`). A console log is a leak path.
- **F6. SSOT item names differ from the vault.** `docs/ssot/bootstrap.vars_and_secrets.md` section 2.1 names `Service Account Auth Token: Infra2`, `bootstrap/dokploy/admin`, and `platform/authentik/admin`. On 2026-10-06 the first item has the prefix `[deprecated]`, and the other two are absent. `docs/ssot/ops.recovery.md` names a 1Password SSH key. The vault holds no SSH-key item. Two items share the title `Service Account Auth Token: infra2-cli`.
- **F7. Held items have no reader.** Section 4.10 lists them. Four deprecated token items remain in `Infra2`. Their revocation is unverified.
- **F8. The probe runner mounts the Docker socket.** `platform/12.alerting/compose.yaml` mounts `/var/run/docker.sock:ro`. The `:ro` flag limits file writes on the socket path. It does not limit Docker API calls (engineering fact, not probed). The same container reads `/secrets`, which holds `C01` when the key is set.
- **F9. Two referenced GitHub secrets are not set.** `INFRA2_OUT_OF_BAND_FEISHU_WEBHOOK_URL` and `PREVIEW_LEAK_GH_TOKEN` appear in workflows. `gh secret list` shows neither on 2026-10-06. Workflows receive an empty value.
- **F10. The iac-runner cannot rotate itself.** `docs/ssot/bootstrap.iac_runner.md` section 6.4 states that the runner never deploys or rotates itself. The operator rotates `C06`.

## 8. Monthly credential rotation audit

`docs/ssot/ops.observability.md` lists a monthly credential rotation audit. This record is its input. No job runs the audit on 2026-10-06 (`git grep`). A person or an agent runs it by hand.

1. **Compare names.** List GitHub secret names with `gh secret list`. List 1Password titles and labels with `op item list` and `op item get ... | jq`. Each name must have a row. Each row must have a name. Update the table for each difference.
2. **Compare dates.** For a credential with several stores, read the update date of each store (`gh secret list`, and `updated_at` in `op item get --format json`). A store older than the newest store is a stale copy. Read dates only. Never read values.
3. **Clear `unverified` cells.** For each cell marked `unverified: needs console check`, the owner reads the console item and records the result and the date in the cell.
4. **List deprecated items.** The owner confirms that each `[deprecated]` token is revoked at the vendor.
5. **Report the age.** Report the newest store date of each credential. No age threshold applies until the owner sets one.

## 9. Maintenance rules

- Add a row in the same change that adds a workflow secret, a service in `libs/security/registry.py`, or a 1Password credential.
- `libs/tests/test_credential_reach_doc.py` guards this record. It fails in these cases:
  - A `secrets.<NAME>` reference in `.github/workflows/` has no row.
  - A registered service has no AppRole row.
  - A credential row has no `Verified` evidence or no `Rotation` value.
  - The file holds a string that looks like a credential value.
- A row never holds a value, a token prefix, or an IP address. Write `$INTERNAL_DOMAIN` for the shared domain.

## Proof

| Behavior | Verification |
|---|---|
| Each workflow secret and each registered service has a row. | `libs/tests/test_credential_reach_doc.py` |
| The doc holds no credential-like string. | `libs/tests/test_credential_reach_doc.py` |
| The doc is in the SSOT index and the docs navigation. | `libs/tests/test_ssot_governance.py`, `libs/tests/test_mkdocs_nav_manifest.py` |

## Related

- [bootstrap.vars_and_secrets.md](./bootstrap.vars_and_secrets.md): token boundaries and the source classes.
- [bootstrap.iac_runner.md](./bootstrap.iac_runner.md): the runner and its AppRole design.
- [ops.recovery.md](./ops.recovery.md): break-glass use and backups.
- [ops.observability.md](./ops.observability.md): the monthly audit line.
