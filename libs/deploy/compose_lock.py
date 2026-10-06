"""In-process advisory lock serializing Dokploy compose read-modify-write-deploy
sequences by compose_id (infra2#525).

## Why this exists

``DokployClient.update_compose_env()`` (libs/dokploy.py) does a plain GET-merge-POST
against Dokploy's ``compose.one`` / ``compose.update`` endpoints, with no server-side
version/etag/compare-and-swap. This was confirmed by reading a LIVE ``compose.one``
response (2026-07-18, via the iac-runner's ``DOKPLOY_API_KEY``): its top-level keys are
``appName, autoDeploy, backups, ..., composeId, ..., createdAt, ..., env, ...`` — there
is a ``createdAt`` on the compose row but no ``updatedAt`` / ``version`` / etag-shaped
field. Dokploy's deployment records (``deployment.allByCompose`` / ``compose.one``'s
``deployments``) carry no caller-supplied correlation id either: their ``description``
is auto-derived from the bound git ref's latest commit, not from the specific
``compose.deploy`` call that triggered them, and no field on the record is settable by
the caller. So true Dokploy-side optimistic locking / rollout tagging is not available
with the API as it exists today — this module is the alternative the infra2#525
write-up asked to confirm before building.

Two concurrent read-modify-write-deploy sequences against the SAME compose_id can
therefore:

1. Lose an update — whichever POST lands last silently wins, discarding the other
   caller's env write (see ``DokployClient.update_compose_env``).
2. Cross-contaminate rollout polling — ``libs.deploy.promote.wait_for_rollout`` /
   ``libs.deploy.deployer.Deployer._wait_for_new_deployment_record`` can pick up a
   deployment record triggered by the OTHER sequence and report its outcome as this
   caller's own.

## What this closes, and what it deliberately does not

This is an IN-PROCESS lock: it serializes callers running in the same Python
process/interpreter (two threads, or a caller invoking ``deploy()`` twice in a loop). It
does NOT serialize across separate OS processes or separate CI runners. Today that
boundary is covered by ``.github/workflows/app-deploy-request.yml``'s
``concurrency: group: app-deploy-${service}-${deploy_type}, cancel-in-progress: false``
(see infra2#525's root-cause write-up) — that GitHub Actions concurrency group is what
actually serializes the two known callers (finance_report and truealpha) today. This
module exists so the PRIMITIVE ITSELF is not silently unsafe for any future or direct
caller that bypasses that workflow (an operator running the CLI twice, a script that
fans out `deploy()` calls), rather than relying entirely on caller discipline.

Reentrant (``RLock``) so ``libs.deploy.promote.deploy()`` can hold the lock across its
whole read-modify-write-deploy-wait-verify sequence AND re-enter it inside
``DokployClient.update_compose_env()`` (called from within that sequence) without
deadlocking, while a call to ``update_compose_env()`` made directly by some OTHER
caller (that does not already hold the compose's lock) is still independently
serialized against it.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

# Guards creation of per-compose_id locks in `_locks` (not the locks themselves).
_registry_guard = threading.Lock()
_locks: dict[str, threading.RLock] = {}


def _lock_for(compose_id: str) -> threading.RLock:
    with _registry_guard:
        lock = _locks.get(compose_id)
        if lock is None:
            lock = threading.RLock()
            _locks[compose_id] = lock
        return lock


@contextmanager
def compose_write_lock(compose_id: str) -> Iterator[None]:
    """Serialize the wrapped block against other in-process callers of the same compose_id.

    Blocks until acquired (no timeout) — mirrors Dokploy's own single-concurrency
    deploy queue (see libs/deploy_queue.py's FIFO framing) rather than failing fast: a
    second in-process caller for the same compose simply waits its turn instead of
    racing the first.
    """
    if not compose_id:
        # Nothing sound to key the lock on; refuse rather than silently serializing
        # unrelated callers against each other (or not serializing at all).
        raise ValueError("compose_write_lock requires a non-empty compose_id")
    with _lock_for(compose_id):
        yield
