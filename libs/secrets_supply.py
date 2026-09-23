"""The one writer of the deployment secret store (plan PR-E).

Every value a service reads is declared in its manifest with a source class. On every
deployment this module applies that declaration through the infra2-sdk resolver:

- human values are copied from 1Password into Vault (only when different);
- required runtime values that Vault does not hold yet are generated once;
- runtime values flagged ``mirror_to_1password`` are copied back for humans;
- what the store still lacks is reported by name and fails the deploy.

When 1Password cannot be reached the deploy proceeds on what Vault already holds
(#625) and the daily reconcile reports the drift instead. Nobody types into Vault.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from infra2_sdk.secrets import (
    HttpTransport,
    OnePasswordBackend,
    SecretsError,
    SecretsResolver,
    VaultKvBackend,
    urllib_transport,
)

from libs.secrets_registry import Service, merged_manifest

ONEPASSWORD_VAULT = "Infra2"

# #810: one SSL EOF ("SSL: UNEXPECTED_EOF_WHILE_READING") mid a Vault call failed a
# whole staging deploy that a 28-minutes-earlier release had passed fine -- the TCP/TLS
# session died before there was a status code to classify, so none of #759's retryable
# codes ever saw it. Same shape of blip as #759's Cloudflare/Dokploy gateway retries,
# one layer lower; reuse its constants (2 retries, 2**attempt seconds) rather than
# inventing a second backoff policy.
_TRANSIENT_TRANSPORT_MARKERS = ("UNEXPECTED_EOF", "ECONNRESET", "Connection aborted")
_TRANSIENT_RETRIES = 2


class TransientTransportError(RuntimeError):
    """A TLS/connection blip survived the #810 retry budget.

    Deliberately NOT a ``SecretsError``: ``apply()`` below tolerates a ``SecretsError``
    around ``sync_human()`` as "1Password unreachable, deploy on what Vault holds"
    (#625) or "store write refused" -- both are policy/reachability calls already made.
    This is neither: it means the retry budget ran out on a Vault call whose outcome is
    still unknown, so it must propagate as a hard failure exactly like the un-retried
    exception did before #810, not be swallowed by that fallback.
    """


def _is_transient_transport_error(exc: BaseException) -> bool:
    """#810: TLS EOF, a reset connection, or an aborted one during a Vault HTTP call.

    Checks both the exception itself and (for ``urllib.error.URLError``, what
    ``urllib_transport`` lets an ``OSError``/``ssl.SSLError`` surface as) its wrapped
    ``reason`` -- ``URLError`` isn't an ``OSError`` subclass, so an errno-backed reason
    (``ConnectionResetError`` = ECONNRESET, ``ConnectionAbortedError`` = ECONNABORTED)
    only matches by type one level down. A message-text match covers the OpenSSL EOF
    case, whose reason is an ``ssl.SSLError`` we deliberately don't treat as transient
    in general (a cert failure is not a blip).
    """
    candidates: list[BaseException] = [exc]
    reason = getattr(exc, "reason", None)
    if isinstance(reason, BaseException):
        candidates.append(reason)
    for candidate in candidates:
        if isinstance(candidate, (ConnectionResetError, ConnectionAbortedError)):
            return True
        if any(marker in str(candidate) for marker in _TRANSIENT_TRANSPORT_MARKERS):
            return True
    return False


def retrying_transport(
    transport: HttpTransport | None = None,
    *,
    _retries: int = _TRANSIENT_RETRIES,
    _sleep: Callable[[float], None] = time.sleep,
) -> HttpTransport:
    """Wrap an ``infra2_sdk`` HTTP transport so a transient TLS/connection blip (#810)
    is retried with #759's backoff instead of failing the whole secret supply on one
    dropped session. Safe to retry blindly: every call this module makes through it is
    either a GET or ``vault_backend``'s idempotent read-merge-POST (a set-desired-state
    write, like #759's ``compose.update``) -- never a non-idempotent create/delete.
    """
    send = transport or urllib_transport()

    def wrapped(method: str, url: str, headers: Mapping[str, str], body: bytes | None):
        attempt = 0
        while True:
            try:
                return send(method, url, headers, body)
            except Exception as exc:  # noqa: BLE001 - reclassified below, or re-raised as-is
                if not _is_transient_transport_error(exc):
                    raise
                if attempt < _retries:
                    attempt += 1
                    _sleep(2**attempt)
                    continue
                raise TransientTransportError(
                    f"transient transport error talking to Vault, retried {attempt} "
                    f"time(s) without success: {exc}"
                ) from exc

    return wrapped


@dataclass(frozen=True)
class SupplyReport:
    service: str
    env: str
    changed: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.missing

    def summary(self) -> str:
        parts = [f"{self.service} ({self.env}):"]
        parts.append(f"changed={list(self.changed)}" if self.changed else "no changes")
        if self.missing:
            parts.append(f"MISSING={list(self.missing)}")
        parts.extend(self.notes)
        return " ".join(parts)


def vault_backend(environ: Mapping[str, str] | None = None) -> VaultKvBackend:
    """Vault over VAULT_ADDR + VAULT_TOKEN, or the runner's AppRole (VAULT_ROLE_ID/SECRET_ID)."""
    env = dict(environ or os.environ)
    if not env.get("VAULT_ADDR"):
        domain = env.get("INTERNAL_DOMAIN", "localhost")
        env["VAULT_ADDR"] = f"https://vault.{domain}"
    if not env.get("VAULT_TOKEN") and env.get("VAULT_ROOT_TOKEN"):
        # Where this name comes from: an operator exports it by hand for a break-glass
        # run (the READMEs under bootstrap/, platform/ and truealpha/ all do), and the
        # iac-runner forwards it next to VAULT_TOKEN into an invoke child whenever it
        # resolves a token at all (bootstrap/06.iac_runner/sync_runner.py), so a deploy
        # of an OLDER iac_ref still finds one. No container holds it in its own
        # environment; the deploy identity itself is an AppRole login.
        #
        # Whether it is permanent or transitional is not settled here: libs/README.md,
        # docs/ssot/bootstrap.vars_and_secrets.md and sync_runner all call it a
        # transition alias for one release. This comment describes the behaviour rather
        # than asserting the policy, and carries no date or issue number — a previous
        # version said "removed with #640", and that is how it went stale.
        env["VAULT_TOKEN"] = env["VAULT_ROOT_TOKEN"]
    # update mode: read → merge → POST — the only write the deploy identities' policies allow
    # (create/read/update/list, no patch) — infra2-sdk 1.5.0.
    return VaultKvBackend.from_environ(
        env, write_mode="update", transport=retrying_transport()
    )


def resolver_for(
    service: Service,
    env: str,
    *,
    store: VaultKvBackend | None = None,
    human: OnePasswordBackend | None = None,
) -> SecretsResolver:
    return SecretsResolver(
        merged_manifest(service),
        project=service.project,
        service=service.service,
        env=env,
        store=store or vault_backend(),
        human=human if human is not None else OnePasswordBackend(ONEPASSWORD_VAULT),
    )


def apply(
    service: Service,
    env: str,
    *,
    resolver: SecretsResolver | None = None,
    restart: Callable[[tuple[str, ...]], None] | None = None,
) -> SupplyReport:
    """sync human → ensure runtime → mirror → reconcile; restart consumers when a value changed."""
    resolver = resolver or resolver_for(service, env)
    changed: set[str] = set()
    notes: list[str] = []
    human_reachable = True
    try:
        human = resolver.sync_human()
        changed.update(human.changed)
        if human.missing:
            notes.append(f"1Password lacks {list(human.missing)}")
    except SecretsError as error:
        if "Vault write" in str(error):
            # the human side answered; the STORE refused the copy (policy, sealed)
            notes.append(
                f"store write refused ({error}); deploying on what Vault holds"
            )
        else:
            human_reachable = False
            notes.append(f"1Password unavailable ({error}); deploying on Vault (#625)")
    runtime = resolver.ensure_runtime()
    changed.update(runtime.changed)
    if human_reachable:
        try:
            mirror = resolver.mirror()
            changed_mirror = mirror.changed
            if changed_mirror:
                notes.append(f"mirrored to 1Password: {list(changed_mirror)}")
        except SecretsError as error:
            notes.append(f"mirror skipped ({error})")
    if not human_reachable:
        resolver.human = None  # reconcile without an expected set
    report = resolver.reconcile()
    if restart is not None and changed:
        restart(tuple(sorted(changed)))
    return SupplyReport(
        service=f"{service.project}/{service.service}",
        env=env,
        changed=tuple(sorted(changed)),
        missing=report.missing,
        notes=tuple(notes),
    )


# Re-exports for Phase 3 Domain Convergence (SSOT)
apply_secret_supply = apply
create_secrets_resolver = resolver_for
