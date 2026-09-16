"""One GitHub issue per red ops-checks watchdog check (truealpha#876 W4).

The nightly ops-checks watchdog job paged Feishu and nothing else, so on
2026-09-16 ``cloudflare-worker-status unhealthy`` (run 35048603264) left no
record anywhere that it was seen or that it ended. This keeps that record in
the repository, in addition to Feishu (never instead of it):

- a red check opens the issue titled exactly ``ops-checks watchdog is red:
  <check>``, or comments on it when it is already open;
- a trail issue whose check is green in a run allowed to close is commented on
  with that run and closed.

Each step of the watchdog job appends its verdicts to one JSON-lines file
(``record_verdicts``) and the job's last step reconciles the issues from it
(``tools/watchdog_issue_trail.py``). A step expected to record that did not is
itself a red check named after that step: a watchdog that crashed is not a
quiet one.

Dedup is equality on the exact title over the repository's open-issue listing
(the issues endpoint; the search index matches words and lags). A listing that
fails never falls through to create, which would turn one breakage into
duplicates: it warns, and the next run retries. A write that fails after a good
listing is exit 1, because that is a token or permission regression.

Issue bodies land in a public repository, so every name and detail is scrubbed
of secret-valued environment variables and IP addresses on top of the
watchdog's own redaction.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from libs.scheduler_peer_liveness import BOUND_CAP_ENV

TITLE_PREFIX = "ops-checks watchdog is red: "
#: An existing label ("A failure in a running environment."), so creation never
#: depends on a label being created first.
ISSUE_LABELS = ("incident",)
VERDICTS_ENV = "INFRA2_WATCHDOG_VERDICTS_PATH"
SSH_OVERRIDE_ENV = "INFRA2_WATCHDOG_SSH_TARGETS_OVERRIDDEN"
DRY_RUN_ENV = "WATCHDOG_DRY_RUN"

#: The watchdog job's steps that record verdicts. One that did not is red.
WATCHDOG_SOURCE = "out-of-band-watchdog"
RUNNER_HEALTH_SOURCE = "iac-runner-health"
EXPECTED_SOURCES = (WATCHDOG_SOURCE, RUNNER_HEALTH_SOURCE)

FULL = "full"  # open, comment and close
OPEN_ONLY = "open-only"  # a drill or a branch dispatch: never closes
OFF = "off"  # a dry run or manual SSH diagnostics

PER_PAGE = 100
#: A listing longer than this is not read to the end, so it is not trusted.
MAX_PAGES = 10
API_BASE = "https://api.github.com"
API_TIMEOUT_SECONDS = 20.0
MAX_DETAIL_CHARS = 1000
#: Values of the job's variables whose names say they are credentials or private
#: coordinates are scrubbed. Only the job's own prefixes: the runner's `USER`
#: (`runner`) would otherwise turn every "iac-runner" into "iac-***".
_SECRET_ENV_NAME = re.compile(
    r"^(?:INFRA2_|DOKPLOY_|CF_|FEISHU_|OP_|VAULT_)\w*"
    r"(?:HOST|USER|TOKEN|KEY|SECRET|PASSWORD|WEBHOOK|CHAT_ID|APP_ID)"
    r"|^(?:GITHUB_TOKEN|GH_TOKEN)$"
)
_MIN_SCRUBBED_VALUE = 4
_IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
_IPV6 = re.compile(
    r"(?<![\w:])(?:(?:[0-9A-Fa-f]{1,4}:){3,7}[0-9A-Fa-f]{1,4}"
    r"|[0-9A-Fa-f:]*::[0-9A-Fa-f:]*[0-9A-Fa-f])(?![\w:])"
)


@dataclass(frozen=True)
class CheckVerdict:
    name: str
    ok: bool
    detail: str = ""
    severity: str = ""
    failure_domain: str = ""


@dataclass
class Trail:
    """What one watchdog run decided, merged over every recording step."""

    failing: dict[str, CheckVerdict] = field(default_factory=dict)
    green: set[str] = field(default_factory=set)
    #: Families whose members are reported only when red (``dokploy-status:``):
    #: once the family was read, a member that is absent is green.
    green_prefixes: set[str] = field(default_factory=set)

    def is_green(self, name: str) -> bool:
        if name in self.failing:
            return False
        return name in self.green or any(
            name.startswith(p) for p in self.green_prefixes
        )

    def renamed(self, rename) -> Trail:
        """The same verdicts under `rename(name)` (titles carry scrubbed names)."""
        return Trail(
            failing={rename(name): verdict for name, verdict in self.failing.items()},
            green={rename(name) for name in self.green},
            green_prefixes={rename(prefix) for prefix in self.green_prefixes},
        )


def title_for(name: str) -> str:
    return f"{TITLE_PREFIX}{name}"


def check_name(title: str) -> str | None:
    """The check a trail title names, or None for any other issue."""
    if not title.startswith(TITLE_PREFIX):
        return None
    return title[len(TITLE_PREFIX) :] or None


def scrub(text: str, env: Mapping[str, str]) -> str:
    """Text safe for a public issue: no secret env values, no IP addresses."""
    secrets = sorted(
        {
            value.strip()
            for name, value in env.items()
            if _SECRET_ENV_NAME.search(name)
            and isinstance(value, str)
            and len(value.strip()) >= _MIN_SCRUBBED_VALUE
        },
        key=len,
        reverse=True,
    )
    for value in secrets:
        text = text.replace(value, "***")
    text = _IPV4.sub("<ip>", text)
    text = _IPV6.sub("<ip>", text)
    if len(text) > MAX_DETAIL_CHARS:
        text = text[:MAX_DETAIL_CHARS] + " ...(truncated)"
    return text


# --- recording ------------------------------------------------------------------


def record_verdicts(
    path: str | Path,
    *,
    source: str,
    checks: Iterable[CheckVerdict],
    green_prefixes: Iterable[str] = (),
) -> None:
    """Append one step's verdicts to the job's verdict file."""
    line = json.dumps(
        {
            "source": source,
            "checks": [asdict(check) for check in checks],
            "green_prefixes": sorted(green_prefixes),
        },
        sort_keys=True,
    )
    with Path(path).open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _parse_record(line: str) -> tuple[str, list[CheckVerdict], list[str]] | None:
    try:
        record = json.loads(line)
        source = record["source"]
        checks = [
            CheckVerdict(
                name=str(item["name"]),
                ok=item["ok"] is True,
                detail=str(item.get("detail", "")),
                severity=str(item.get("severity", "")),
                failure_domain=str(item.get("failure_domain", "")),
            )
            for item in record["checks"]
        ]
        prefixes = [str(prefix) for prefix in record.get("green_prefixes", [])]
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    if not isinstance(source, str) or not source:
        return None
    return source, checks, prefixes


def load_trail(
    path: str | Path, expected_sources: Iterable[str] = EXPECTED_SOURCES
) -> Trail:
    """Merge the verdict file. A missing file or a torn line records nothing."""
    trail = Trail()
    seen: set[str] = set()
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        parsed = _parse_record(line) if line.strip() else None
        if parsed is None:
            continue
        source, checks, prefixes = parsed
        seen.add(source)
        trail.green.add(source)
        trail.green_prefixes.update(prefixes)
        for check in checks:
            if check.ok:
                trail.green.add(check.name)
            else:
                trail.failing.setdefault(check.name, check)
    for source in expected_sources:
        if source not in seen:
            trail.failing[source] = CheckVerdict(
                name=source,
                ok=False,
                detail=(
                    "the step recorded no verdict: it crashed or was skipped before "
                    "recording one, so none of its checks were proven (see the run log)"
                ),
                severity="P1",
                failure_domain="watchdog-step",
            )
    trail.green.difference_update(trail.failing)
    return trail


# --- mode -----------------------------------------------------------------------


def issue_trail_mode(env: Mapping[str, str]) -> str:
    """Which issue writes this run may make."""
    if env.get(DRY_RUN_ENV) == "1" or env.get(SSH_OVERRIDE_ENV) == "1":
        return OFF
    event = env.get("GITHUB_EVENT_NAME", "")
    if event not in ("schedule", "workflow_dispatch"):
        return OFF
    if (env.get(BOUND_CAP_ENV) or "").strip():
        # A drill measures with a tightened bound, so its green proves nothing.
        return OPEN_ONLY
    if event == "schedule" or env.get("GITHUB_REF") == "refs/heads/main":
        return FULL
    # A branch dispatch runs that branch's checks, not the ones the issue was
    # opened under.
    return OPEN_ONLY


# --- the GitHub issues API --------------------------------------------------------


class ListingFailed(RuntimeError):
    """The open issues could not be read, so nothing may be decided from them."""


class WriteFailed(RuntimeError):
    """A create, comment or close did not land."""


class IssueApi(Protocol):
    def open_issues(self) -> list[dict]: ...

    def create(self, title: str, body: str, labels: Iterable[str]) -> int: ...

    def comment(self, number: int, body: str) -> None: ...

    def close(self, number: int) -> None: ...


class GitHubIssues:
    """The REST calls the trail needs, for one repository."""

    def __init__(
        self,
        repository: str,
        token: str,
        *,
        opener=urlopen,
        timeout: float = API_TIMEOUT_SECONDS,
    ) -> None:
        self.repository = repository
        self._token = token
        self._opener = opener
        self._timeout = timeout

    def _call(self, method: str, path: str, payload: dict | None = None) -> object:
        request = Request(
            f"{API_BASE}/repos/{self.repository}{path}",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "infra2-watchdog-issue-trail/1.0",
                "Content-Type": "application/json",
            },
            data=None if payload is None else json.dumps(payload).encode("utf-8"),
            method=method,
        )
        with self._opener(request, timeout=self._timeout) as response:  # noqa: S310
            raw = response.read().decode("utf-8")
        return json.loads(raw) if raw.strip() else None

    def open_issues(self) -> list[dict]:
        issues: list[dict] = []
        for page in range(1, MAX_PAGES + 1):
            path = f"/issues?state=open&per_page={PER_PAGE}&page={page}"
            try:
                items = self._call("GET", path)
            except (HTTPError, URLError, OSError, ValueError) as exc:
                raise ListingFailed(f"GET {path}: {exc}") from exc
            if not isinstance(items, list):
                raise ListingFailed(
                    f"GET {path} returned {type(items).__name__}, not a list"
                )
            for item in items:
                if not isinstance(item, dict):
                    raise ListingFailed(f"GET {path} returned a non-object issue")
                if "pull_request" in item:
                    continue
                number, title = item.get("number"), item.get("title")
                if not isinstance(number, int) or not isinstance(title, str):
                    raise ListingFailed(
                        f"GET {path} returned an issue without number/title"
                    )
                issues.append({"number": number, "title": title})
            if len(items) < PER_PAGE:
                return issues
        raise ListingFailed(
            f"more than {MAX_PAGES * PER_PAGE} open issues; not read to the end"
        )

    def _write(self, method: str, path: str, payload: dict) -> object:
        try:
            return self._call(method, path, payload)
        except (HTTPError, URLError, OSError, ValueError) as exc:
            raise WriteFailed(f"{method} {path}: {exc}") from exc

    def create(self, title: str, body: str, labels: Iterable[str]) -> int:
        created = self._write(
            "POST", "/issues", {"title": title, "body": body, "labels": list(labels)}
        )
        number = created.get("number") if isinstance(created, dict) else None
        if not isinstance(number, int):
            raise WriteFailed("POST /issues returned no issue number")
        return number

    def comment(self, number: int, body: str) -> None:
        self._write("POST", f"/issues/{number}/comments", {"body": body})

    def close(self, number: int) -> None:
        self._write(
            "PATCH",
            f"/issues/{number}",
            {"state": "closed", "state_reason": "completed"},
        )


# --- reconcile ---------------------------------------------------------------------


def _red_body(
    name: str,
    verdict: CheckVerdict,
    *,
    run_url: str,
    context: str,
    env: Mapping[str, str],
) -> str:
    labels = " ".join(
        part for part in (verdict.severity, verdict.failure_domain) if part
    )
    lines = [
        f"**`{name}` is red** in the ops-checks watchdog"
        + (f" ({labels})" if labels else ""),
        "",
        f"- Run: {run_url or '(no run URL)'}",
        f"- Run kind: {context}",
        f"- Detail: {scrub(verdict.detail, env) or '(none)'}",
        "",
        "Recorded by the ops-checks watchdog issue trail (`tools/watchdog_issue_trail.py`, "
        "truealpha#876) in addition to the Feishu page. One open issue per check: a later "
        "red run comments here, and the next green scheduled run closes it.",
    ]
    return "\n".join(lines)


def _green_body(name: str, *, run_url: str, context: str) -> str:
    return "\n".join(
        [
            f"**`{name}` is green again** in {run_url or 'the latest run'} ({context}).",
            "",
            "Closed by the ops-checks watchdog issue trail (truealpha#876). A later red run opens a new issue.",
        ]
    )


def reconcile(
    api: IssueApi,
    trail: Trail,
    *,
    mode: str,
    run_url: str = "",
    context: str = "",
    env: Mapping[str, str] | None = None,
) -> int:
    """Open or comment on every red check's issue; close green ones when allowed."""
    env = env or {}
    if mode not in (FULL, OPEN_ONLY, OFF):
        print(f"::error::unknown issue-trail mode {mode!r}")
        return 1
    if mode == OFF:
        print(
            "issue trail: off for this run (dry run, manual diagnostics, or not a scheduled/dispatched run)"
        )
        return 0
    try:
        listed = api.open_issues()
    except ListingFailed as exc:
        print(
            f"::warning::issue trail: open issues could not be listed ({exc}); nothing written, the next run retries"
        )
        return 0

    trail = trail.renamed(lambda name: scrub(name, env))
    by_title: dict[str, list[int]] = {}
    for issue in listed:
        by_title.setdefault(issue["title"], []).append(issue["number"])
    for numbers in by_title.values():
        numbers.sort()

    failed_writes = 0
    for name in sorted(trail.failing):
        title = title_for(name)
        body = _red_body(
            name, trail.failing[name], run_url=run_url, context=context, env=env
        )
        existing = by_title.get(title)
        try:
            if existing:
                # The oldest: a duplicate left over must not split the history further.
                api.comment(existing[0], body)
                print(f"issue trail: commented on #{existing[0]} ({title})")
            else:
                number = api.create(title, body, ISSUE_LABELS)
                by_title[title] = [number]
                print(f"issue trail: opened #{number} ({title})")
        except WriteFailed as exc:
            failed_writes += 1
            print(f"::error::issue trail: could not record {title!r}: {exc}")

    if mode == FULL:
        for title in sorted(by_title):
            name = check_name(title)
            if name is None or not trail.is_green(name):
                continue
            body = _green_body(name, run_url=run_url, context=context)
            # Every exact match: a duplicate left open is the stale alert this closes.
            for number in by_title[title]:
                try:
                    api.comment(number, body)
                    api.close(number)
                    print(f"issue trail: closed #{number} ({title})")
                except WriteFailed as exc:
                    failed_writes += 1
                    print(f"::error::issue trail: could not close #{number}: {exc}")
    else:
        print(
            "issue trail: open-only run (drill or branch dispatch); no issue is closed"
        )

    return 1 if failed_writes else 0
