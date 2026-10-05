"""#962: a daily GitHub-plane job pages only when its conclusion's identity changes.

Measured 2026-09-26..10-05: 28 of ~40 pager messages were three conditions paged again
every day with the same conclusion. These tests pin the rule that replaces that:

- the identity is the job plus the stable key of each finding, never a reading;
- first appearance, an added finding, a removed finding with others left, and
  all-resolved page; the same identity on a later day is a report, not a page;
- state is one GitHub issue per job, written only after the page was delivered;
- nothing silences: unreadable state, an undeliverable report or a bug in the decision
  code all page exactly as the jobs did before;
- a dry run writes and delivers nothing; a drill / branch dispatch never closes state.

Falsifiability (outputs in the PR): making `decide` ignore identity (always page), and
making a state-read failure suppress the page, each turn these tests red.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from libs import page_dedup as pd
from libs.observability.issue_trail import FULL, OFF, OPEN_ONLY, ListingFailed
from libs.page_dedup import (
    ABSENT,
    CORRUPT,
    FOUND,
    NONE,
    PAGE,
    REPORT,
    RESOLVED,
    UNREADABLE,
    Finding,
    Lookup,
    State,
    dedup_page,
    resolve_page_state,
)

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ops-checks.yml"
JOB = "facet-reconcile"
LABEL = "每日 facet 对账"
BOT = "github-actions[bot]"
DAY1 = datetime(2026, 10, 5, 7, 17, tzinfo=UTC).timestamp()

SCHEDULED = {
    "GITHUB_EVENT_NAME": "schedule",
    "GITHUB_REPOSITORY": "wangzitian0/infra2",
    "GITHUB_SERVER_URL": "https://github.com",
    "GITHUB_RUN_ID": "123",
    "GITHUB_TOKEN": "token-for-tests",
}
#: A drill or a dispatch from another branch: opens and comments, never closes.
BRANCH_DISPATCH = {
    **SCHEDULED,
    "GITHUB_EVENT_NAME": "workflow_dispatch",
    "GITHUB_REF": "refs/heads/some-branch",
}


def day(n: int) -> float:
    """07:17 UTC of the n-th day, the cron's own time of day."""
    return DAY1 + (n - 1) * 86400


# --- fakes ------------------------------------------------------------------------------


class FakeIssues:
    """An in-memory GitHub issues API with a call log."""

    def __init__(self, *, author: str = BOT) -> None:
        self.author = author
        self.issues: dict[int, dict] = {}
        self.calls: list[tuple] = []
        self.listing_error: Exception | None = None
        self.write_error: Exception | None = None
        #: operations ("comment", "close", ...) that fail while the others work
        self.fail_ops: set[str] = set()
        self._next = 100

    @property
    def writes(self) -> list[tuple]:
        return [call for call in self.calls if call[0] != "list"]

    def open_titles(self) -> list[str]:
        return [i["title"] for i in self.issues.values() if i["state"] == "open"]

    def open_issues(self, *, with_body: bool = False) -> list[dict]:
        self.calls.append(("list",))
        if self.listing_error is not None:
            raise self.listing_error
        return [
            {
                "number": number,
                "title": issue["title"],
                "body": issue["body"],
                "author": issue["author"],
            }
            for number, issue in sorted(self.issues.items())
            if issue["state"] == "open"
        ]

    def _write(self, op: str, *args) -> None:
        self.calls.append((op, *args))
        if self.write_error is not None:
            raise self.write_error
        if op in self.fail_ops:
            raise RuntimeError(f"HTTP 500 on {op}")

    def create(self, title: str, body: str, labels) -> int:
        self._write("create", title)
        self._next += 1
        self.issues[self._next] = {
            "title": title,
            "body": body,
            "labels": list(labels),
            "state": "open",
            "author": self.author,
            "comments": [],
        }
        return self._next

    def comment(self, number: int, body: str) -> None:
        self._write("comment", number)
        self.issues[number]["comments"].append(body)

    def update_body(self, number: int, body: str) -> None:
        self._write("update_body", number)
        self.issues[number]["body"] = body

    def close(self, number: int) -> None:
        self._write("close", number)
        self.issues[number]["state"] = "closed"


def age_state(api, job: str, days: int) -> None:
    """Make the recorded page `days` older (tests do not wait a day)."""
    for issue in api.issues.values():
        state = pd.parse_marker(issue["body"], job)
        assert state is not None
        shifted = State(
            state.fingerprint,
            state.keys,
            state.since - days * 86400,
            state.incident_since - days * 86400,
        )
        issue["body"] = pd.encode_marker(job, shifted, len(state.keys))


class Channels:
    """The pager and the reports chat, as recorded deliveries."""

    def __init__(self) -> None:
        self.pages: list[str] = []
        self.reports: list[str] = []
        self.page_error: Exception | None = None
        self.report_result: bool | Exception = True

    def page(self, env, text: str) -> None:
        if self.page_error is not None:
            raise self.page_error
        self.pages.append(text)

    def report(self, text: str, env) -> bool:
        if isinstance(self.report_result, Exception):
            raise self.report_result
        if self.report_result:
            self.reports.append(text)
        return self.report_result


def run_day(
    api,
    channels,
    n,
    *,
    keys=(),
    env=SCHEDULED,
    clean=None,
    message="PAGE: details of today",
    **kwargs,
):
    """One scheduled run of the job on day `n` finding `keys`."""
    findings = [Finding(key, f"line for {key} on day {n}") for key in keys]
    return dedup_page(
        env,
        job=JOB,
        label=LABEL,
        findings=findings,
        page_message=message,
        deliver_page=channels.page,
        deliver_report=channels.report,
        observed_clean=(not keys) if clean is None else clean,
        now=day(n),
        api=api,
        **kwargs,
    )


# --- identity: pure -----------------------------------------------------------------------


def test_identity_ignores_order_duplicates_whitespace_and_display_text() -> None:
    a = pd.make_identity(JOB, [Finding("b", "reading 5"), Finding("a  x", "")])
    b = pd.make_identity(
        JOB, [Finding(" a x ", "reading 9"), Finding("b"), Finding("b", "again")]
    )

    assert a.keys == ("a x", "b")
    assert a.fingerprint == b.fingerprint


def test_identity_changes_with_a_key_or_with_the_job() -> None:
    base = pd.make_identity(JOB, [Finding("a"), Finding("b")])

    assert base.fingerprint != pd.make_identity(JOB, [Finding("a")]).fingerprint
    assert (
        base.fingerprint
        != pd.make_identity(JOB, [Finding("a"), Finding("c")]).fingerprint
    )
    assert (
        base.fingerprint
        != pd.make_identity(
            "vault-self-refresh-audit", [Finding("a"), Finding("b")]
        ).fingerprint
    )


def test_findings_that_exist_can_never_make_an_empty_identity() -> None:
    """An empty identity reads as "all resolved": a blank key must not produce one."""
    identity = pd.make_identity(JOB, [Finding(""), Finding("  ", "")])

    assert identity.keys == ("unkeyed finding",)
    assert pd.make_identity(JOB, [Finding("", "the line is the key")]).keys == (
        "the line is the key",
    )


def test_two_long_keys_with_the_same_prefix_are_different_findings() -> None:
    """The fingerprint covers the whole key; only what is shown is cut short."""
    prefix = "unit-" + "x" * 400
    a = pd.make_identity(JOB, [Finding(prefix + "-a")])
    b = pd.make_identity(JOB, [Finding(prefix + "-b")])

    assert a.fingerprint != b.fingerprint
    assert len(a.shown[0]) <= pd.MAX_SHOWN_KEY_CHARS


def test_public_issue_text_is_scrubbed_but_the_fingerprint_covers_the_raw_key() -> None:
    env = {"INFRA2_WATCHDOG_SSH_HOST": "vps.internal.example", "OTHER": "x"}
    identity = pd.make_identity(
        JOB, [Finding("host vps.internal.example 10.1.2.3 `x`")], env
    )

    assert "vps.internal.example" not in identity.shown[0]
    assert "10.1.2.3" not in identity.shown[0] and "`" not in identity.shown[0]
    assert identity.fingerprint == pd.fingerprint(
        JOB, ["host vps.internal.example 10.1.2.3 `x`"]
    )


def test_day_number_counts_calendar_days_in_the_owners_zone() -> None:
    first = day(1)

    assert pd.day_number(first, first) == 1
    assert pd.day_number(first, first + 86400) == 2
    # a cron that starts 20 minutes early the next day is still day 2, not day 1
    assert pd.day_number(first, first + 86400 - 20 * 60) == 2
    assert pd.day_number(first, first + 3 * 86400) == 4


def test_marker_round_trips_and_a_forged_or_torn_one_is_rejected() -> None:
    state = State(pd.fingerprint(JOB, ["k"]), ("k --> <b>",), 100, 50, 1)
    marker = pd.encode_marker(JOB, state, 1)

    assert "-->" not in marker[:-4] and "<b>" not in marker  # cannot end the comment
    assert pd.parse_marker(f"text\n{marker}\n", JOB) == state
    assert pd.parse_marker(marker, "vault-self-refresh-audit") is None
    assert pd.parse_marker(marker.replace(state.fingerprint, "abc"), JOB) is None
    assert pd.parse_marker(marker[: len(marker) // 2], JOB) is None
    assert pd.parse_marker("no marker at all", JOB) is None
    assert pd.parse_marker("", JOB) is None


# --- identity of each job's findings: no readings -----------------------------------------


def test_facet_keys_ignore_ids_hashes_and_notes(monkeypatch) -> None:
    """compose-id / config-hash / dns findings carry live ids and hashes: a key built
    from them would be a new identity every time the reading moved."""
    import libs.dokploy
    import tools.app_compose_id_drift as compose
    import tools.dokploy_config_drift as config
    import tools.dns_drift_report as dns
    from tools import facet_reconcile as fr

    def keys(live_id: str, deployed: str, note: str) -> list[str]:
        target = SimpleNamespace(service="finance_report/app", env="production")
        monkeypatch.setattr(libs.dokploy, "get_dokploy", lambda: object())
        monkeypatch.setattr(
            compose,
            "scan",
            lambda _client: [compose.Row(target, "DRIFT", live_id, note=note)],
        )
        monkeypatch.setattr(config, "_production_target_tag", lambda: "v1.2.3")
        monkeypatch.setattr(
            config,
            "scan",
            lambda _tag: [
                config.Row("platform/redis", "DRIFT", "exp-" + deployed, deployed)
            ],
        )
        monkeypatch.setenv("CF_API_TOKEN", "x")
        monkeypatch.setenv("CF_ZONE_ID", "x")
        monkeypatch.setenv("INTERNAL_DOMAIN", "example.test")
        monkeypatch.setattr(dns, "_dns_tasks", lambda: "t")
        monkeypatch.setattr(dns, "_expected_records", lambda _t: ["a.example.test"])
        monkeypatch.setattr(dns, "_actual_records", lambda _t: [])
        return [key for key, _line in fr.confirmed_finding_pairs(fr.run_all())]

    day_one = keys("live-aaa", "hash-111", "composeId='aaa'")
    day_two = keys("live-zzz", "hash-999", "composeId='zzz'")

    assert day_one == day_two
    assert sorted(day_one) == [
        "compose-id:finance_report/app:production:DRIFT",
        "config-hash:platform/redis:DRIFT",
        "dns:a.example.test",
    ]


def test_facet_pairs_without_section_keys_fall_back_to_the_line() -> None:
    """A section that gives no keys can only page more (the line moves), never less."""
    from tools import facet_reconcile as fr

    section = fr.Section("dns", confirmed=["rec a missing", "rec b missing"])

    assert fr.confirmed_finding_pairs([section]) == [
        ("dns:rec a missing", "[dns] rec a missing"),
        ("dns:rec b missing", "[dns] rec b missing"),
    ]
    assert fr.confirmed_findings([section]) == [
        "[dns] rec a missing",
        "[dns] rec b missing",
    ]


def test_vault_keys_are_service_and_check_without_the_summary() -> None:
    from tools.vault_self_refresh_audit_check import confirmed_finding_pairs

    def report(summary: str) -> dict:
        return {
            "status": "fail",
            "results": [
                {
                    "service_id": "platform/redis",
                    "check_id": "vault-agent-container",
                    "status": "fail",
                    "severity": "critical",
                    "summary": summary,
                },
                {
                    "service_id": "platform/redis",
                    "check_id": "optional-field-inertness",
                    "status": "info",
                    "severity": "info",
                    "summary": "note",
                },
                {
                    "service_id": "platform/minio",
                    "check_id": "token",
                    "status": "pass",
                    "severity": "critical",
                    "summary": "ok",
                },
            ],
        }

    first = confirmed_finding_pairs(report("restarted 3 times, last 40 min ago"))
    second = confirmed_finding_pairs(report("restarted 9 times, last 2 h ago"))

    assert [key for key, _ in first] == ["platform/redis::vault-agent-container"]
    assert [key for key, _ in first] == [key for key, _ in second]
    assert first[0][1] != second[0][1]  # the display line still carries the reading


def test_secrets_keys_are_names_not_quota_readings() -> None:
    from tools import secrets_reconcile_check as check

    def report(used: int, missing: list[str]) -> dict:
        return {
            "ok": False,
            "stores": [
                {
                    "service": "platform/alerting",
                    "env": "staging",
                    "missing": missing,
                    "ok": False,
                },
                {
                    "service": "platform/prefect",
                    "env": "staging",
                    "unclassified": ["leftover"],
                    "ok": False,
                },
            ],
            "capacity": {
                "ok": False,
                "items": [
                    {
                        "name": "cloudflare.kv.write",
                        "used": used,
                        "limit": 1000,
                        "window": "day",
                        "level": "exceeded",
                    }
                ],
            },
        }

    today = check.page_worthy_keys(report(1198, ["B", "A"]))
    tomorrow = check.page_worthy_keys(report(1500, ["A", "B"]))

    assert today == tomorrow
    assert sorted(today) == [
        "quota:cloudflare.kv.write:day",
        "store:platform/alerting:staging:missing:A",
        "store:platform/alerting:staging:missing:B",
    ]
    # one more missing name is a different finding
    assert check.page_worthy_keys(report(1198, ["A", "B", "C"])) != today
    # the summary and the keys agree on what pages
    assert (
        check.page_worthy_keys({"stores": [], "capacity": {"items": []}}) == []
        and check.page_worthy_summary({"stores": [], "capacity": {"items": []}}) == ""
    )


def test_a_secrets_report_proves_only_what_it_observed() -> None:
    from tools import secrets_reconcile_check as check

    full = {"ok": True, "stores": [], "capacity": {"ok": True, "items": []}}

    assert check.unevaluated_prefixes(full) == []
    # an unread quota says nothing about quota findings, only about them
    assert check.unevaluated_prefixes({**full, "capacity": None}) == ["quota:"]
    # a store reconciled while 1Password was unavailable cannot prove its own findings
    noted = {
        **full,
        "stores": [
            {"service": "platform/alerting", "env": "staging", "ok": True},
            {
                "service": "truealpha/app",
                "env": "production",
                "ok": True,
                "note": "1Password unavailable: ssh timeout",
            },
        ],
    }
    assert check.unevaluated_prefixes(noted) == ["store:truealpha/app:production:"]
    # no report at all proves nothing
    assert check.unevaluated_prefixes({"ok": False, "transport_error": "ssh"}) == [""]
    assert check.unevaluated_prefixes({**full, "transport_error": "x"}) == [""]
    assert check.unevaluated_prefixes({"ok": True}) == [""]


def test_facet_and_vault_name_what_they_did_not_evaluate(tmp_path) -> None:
    from libs.vault_self_refresh_audit import classify_deployed_template
    from tools import facet_reconcile as fr
    from tools import vault_self_refresh_audit_check as vault

    sections = [
        fr.Section("compose-id", blockers=["live lookup failed"]),
        fr.Section("config-hash"),
        fr.Section("dns", skipped="Cloudflare credentials not configured"),
    ]
    assert fr.unevaluated_prefixes(sections) == ["compose-id:", "dns:"]
    observation = {fr.OBSERVATION_ENV: str(tmp_path / "observation.json")}
    assert fr.read_unevaluated(observation) == [""]  # nothing recorded: nothing proven
    fr.write_observation(sections, observation)
    assert fr.read_unevaluated(observation) == ["compose-id:", "dns:"]
    (tmp_path / "observation.json").write_text("{not json", encoding="utf-8")
    assert fr.read_unevaluated(observation) == [""]

    # the vault check that can also fail reports `info` when it could not look: the
    # id in `CAN_FAIL_BUT_REPORT_INFO` is the real classifier's, not a copy that drifts
    service = SimpleNamespace(
        id="platform/redis",
        secret_template_path="/t",
        compose_path="c",
    )
    unreadable = classify_deployed_template(service, "")
    assert unreadable.status == "info"
    assert unreadable.check_id in vault.CAN_FAIL_BUT_REPORT_INFO
    report = {
        "status": "pass",
        "results": [
            unreadable.to_dict(),
            {
                "service_id": "platform/redis",
                "check_id": "preview-stack",
                "status": "info",
            },
            {"service_id": "platform/redis", "check_id": "token", "status": "pass"},
        ],
    }
    assert vault.unevaluated_keys(report) == ["platform/redis::deployed-template"]
    # a missing or failing report proves nothing
    env = {vault.REPORT_ENV: str(tmp_path / "vault.json")}
    assert vault.read_unevaluated(env) == [""]
    (tmp_path / "vault.json").write_text(json.dumps(report), encoding="utf-8")
    assert vault.read_unevaluated(env) == ["platform/redis::deployed-template"]
    (tmp_path / "vault.json").write_text(
        json.dumps({**report, "status": "fail"}), encoding="utf-8"
    )
    assert vault.read_unevaluated(env) == [""]


# --- the decision table: pure ------------------------------------------------------------


def _state(keys=("a",), since=day(1), incident=day(1)) -> State:
    return State(pd.fingerprint(JOB, keys), tuple(keys), int(since), int(incident))


def _identity(*keys: str) -> pd.Identity:
    return pd.make_identity(JOB, [Finding(key) for key in keys])


def _found(*keys: str, since=day(1)) -> Lookup:
    return Lookup(FOUND, (7,), _state(keys or ("a",), since))


@pytest.mark.parametrize(
    ("label", "keys", "lookup", "mode", "clean", "expected"),
    [
        # a finding with no recorded state: first appearance
        ("first", ("a",), Lookup(ABSENT), FULL, False, PAGE),
        # the same identity later: a report, not a page
        ("same", ("a",), _found("a"), FULL, False, REPORT),
        ("same, reordered", ("b", "a"), _found("a", "b"), FULL, False, REPORT),
        # a finding added / removed while others remain: page the new set
        ("added", ("a", "b"), _found("a"), FULL, False, PAGE),
        ("removed, others remain", ("a",), _found("a", "b"), FULL, False, PAGE),
        ("replaced", ("c",), _found("a"), FULL, False, PAGE),
        # everything resolved
        ("resolved", (), _found("a"), FULL, True, RESOLVED),
        (
            "resolved, marker unreadable",
            (),
            Lookup(CORRUPT, (7,)),
            FULL,
            True,
            RESOLVED,
        ),
        ("nothing red, nothing recorded", (), Lookup(ABSENT), FULL, True, NONE),
        # a transient no-finding failure is not a recovery
        ("transient", (), _found("a"), FULL, False, NONE),
        # state unusable: fail loud
        (
            "state unreadable",
            ("a",),
            Lookup(UNREADABLE, detail="HTTP 500"),
            FULL,
            False,
            PAGE,
        ),
        ("marker unreadable", ("a",), Lookup(CORRUPT, (7,)), FULL, False, PAGE),
        ("state unreadable, nothing red", (), Lookup(UNREADABLE), FULL, True, NONE),
        # a drill or branch dispatch never closes: no RESOLVED either
        ("open-only never resolves", (), _found("a"), OPEN_ONLY, True, NONE),
        ("open-only still pages new", ("a",), Lookup(ABSENT), OPEN_ONLY, False, PAGE),
        ("open-only still reports same", ("a",), _found("a"), OPEN_ONLY, False, REPORT),
        # dedup off: exactly the behaviour before #962
        ("off pages", ("a",), _found("a"), OFF, False, PAGE),
        ("off, nothing red", (), _found("a"), OFF, True, NONE),
    ],
)
def test_decision_table(label, keys, lookup, mode, clean, expected) -> None:
    decision = pd.decide(
        _identity(*keys), lookup, mode=mode, now=day(2), observed_clean=clean
    )

    assert decision.action == expected, label


@pytest.mark.parametrize(
    ("label", "recorded", "unevaluated", "expected"),
    [
        ("a check that ran resolves", ("dns:a",), ["compose-id:"], RESOLVED),
        ("every check ran", ("dns:a",), [], RESOLVED),
        ("its own check did not run", ("dns:a",), ["dns:"], NONE),
        ("one of several did not run", ("dns:a", "compose-id:x"), ["dns:"], NONE),
        ("nothing was observed", ("dns:a",), list(pd.ALL_UNEVALUATED), NONE),
    ],
)
def test_a_finding_whose_check_did_not_run_is_not_resolved(
    label, recorded, unevaluated, expected
) -> None:
    decision = pd.decide(
        _identity(),
        _found(*recorded),
        mode=FULL,
        now=day(2),
        observed_clean=True,
        unevaluated=unevaluated,
    )

    assert decision.action == expected, label


def test_cut_short_or_unreadable_state_is_never_resolved_past_an_unevaluated_check() -> (
    None
):
    cut = State(pd.fingerprint(JOB, ["a"]), ("a",), int(day(1)), int(day(1)), total=250)
    for lookup in (Lookup(FOUND, (7,), cut), Lookup(CORRUPT, (7,))):
        decision = pd.decide(
            _identity(),
            lookup,
            mode=FULL,
            now=day(2),
            observed_clean=True,
            unevaluated=["dns:"],
        )
        assert decision.action == NONE


def test_decision_details_name_what_changed_and_the_day() -> None:
    changed = pd.decide(_identity("a", "c"), _found("a", "b"), mode=FULL, now=day(2))
    same = pd.decide(_identity("a"), _found("a", since=day(1)), mode=FULL, now=day(3))

    assert (changed.added, changed.removed, changed.write) == (("c",), ("b",), True)
    assert (same.day, same.write) == (3, False)
    first = pd.decide(_identity("a"), Lookup(ABSENT), mode=FULL, now=day(1))
    assert first.write is True
    unreadable = pd.decide(
        _identity("a"), Lookup(UNREADABLE, detail="x"), mode=FULL, now=day(1)
    )
    assert unreadable.write is False and unreadable.note
    # a drill pages new findings but records nothing: what it writes would be what the
    # next scheduled run trusts
    drill = pd.decide(_identity("a"), Lookup(ABSENT), mode=OPEN_ONLY, now=day(1))
    assert (drill.action, drill.write) == (PAGE, False)
    drill_change = pd.decide(_identity("b"), _found("a"), mode=OPEN_ONLY, now=day(1))
    assert (drill_change.action, drill_change.write) == (PAGE, False)


# --- the daily sequence, end to end against a fake GitHub ----------------------------------


def test_a_condition_pages_once_then_reports_then_pages_on_change_then_resolves() -> (
    None
):
    """The acceptance sequence of #962, one run per day."""
    api, ch = FakeIssues(), Channels()

    # day 1: a new identity pages and is recorded
    out = run_day(api, ch, 1, keys=["A"])
    assert (out.action, out.exit_code) == (PAGE, 0)
    assert len(ch.pages) == 1 and ch.reports == []
    assert api.open_titles() == ["ops-checks paged: facet-reconcile"]
    (issue,) = api.issues.values()
    assert issue["labels"] == ["incident"] and issue["author"] == BOT

    # day 2: the same identity (the reading moved) is a compact report, not a page
    writes_before = list(api.writes)
    out = run_day(api, ch, 2, keys=["A"])
    assert out.action == REPORT and out.day == 2
    assert len(ch.pages) == 1
    assert ch.reports[-1].startswith("[报告] 仍未恢复 · 第 2 天 · facet-reconcile")
    assert "• A" in ch.reports[-1]
    assert api.writes == writes_before  # nothing written for an unchanged day

    # day 3: the same identity again is day 3 of the same condition
    run_day(api, ch, 3, keys=["A"])
    assert ch.reports[-1].startswith("[报告] 仍未恢复 · 第 3 天 · facet-reconcile")
    assert len(ch.pages) == 1

    # day 4: a finding is added -> page with the whole new set, state replaced
    out = run_day(api, ch, 4, keys=["A", "B"], message="PAGE: A and B")
    assert out.action == PAGE and len(ch.pages) == 2
    assert ch.pages[-1] == "PAGE: A and B"
    assert [c[0] for c in api.writes[len(writes_before) :]] == [
        "update_body",
        "comment",
    ]
    assert "`B`" in api.issues[101]["body"]
    assert "added: `B`" in api.issues[101]["comments"][-1]

    # day 5: the new identity is on its own day 2
    run_day(api, ch, 5, keys=["A", "B"])
    assert ch.reports[-1].startswith("[报告] 仍未恢复 · 第 2 天 · facet-reconcile")
    assert len(ch.pages) == 2

    # day 6: a finding removed while another remains -> page again with the new set
    out = run_day(api, ch, 6, keys=["B"], message="PAGE: B only")
    assert out.action == PAGE and ch.pages[-1] == "PAGE: B only"
    assert "removed: `A`" in api.issues[101]["comments"][-1]

    # day 7: everything resolved -> one RESOLVED page, state closed
    out = run_day(api, ch, 7, keys=[])
    assert out.action == RESOLVED and len(ch.pages) == 4
    resolved = ch.pages[-1]
    assert resolved.startswith("✅ [已恢复] 每日 facet 对账")
    assert "• B" in resolved and "共 6 天" in resolved  # since the incident began
    assert api.open_titles() == [] and api.issues[101]["state"] == "closed"

    # day 8: still clean -> silence
    pages, reports = len(ch.pages), len(ch.reports)
    out = run_day(api, ch, 8, keys=[])
    assert out.action == NONE
    assert (len(ch.pages), len(ch.reports)) == (pages, reports)

    # day 9: it breaks again -> a NEW condition pages and opens a new issue
    out = run_day(api, ch, 9, keys=["A"])
    assert out.action == PAGE and len(ch.pages) == 5
    assert api.open_titles() == ["ops-checks paged: facet-reconcile"]


def test_a_dispatch_from_main_resolves_like_a_schedule() -> None:
    api, ch = FakeIssues(), Channels()
    env = {
        **SCHEDULED,
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main",
    }
    run_day(api, ch, 1, keys=["A"], env=env)

    out = run_day(api, ch, 2, keys=[], env=env)

    assert out.action == RESOLVED and api.open_titles() == []


def test_resolve_page_state_only_resolves_what_was_paged() -> None:
    api, ch = FakeIssues(), Channels()

    nothing = resolve_page_state(
        SCHEDULED, job=JOB, label=LABEL, deliver_page=ch.page, now=day(1), api=api
    )
    assert nothing.action == NONE and ch.pages == [] and api.writes == []

    run_day(api, ch, 1, keys=["A"])
    done = resolve_page_state(
        SCHEDULED, job=JOB, label=LABEL, deliver_page=ch.page, now=day(2), api=api
    )
    assert done.action == RESOLVED and api.open_titles() == []
    assert ch.pages[-1].startswith("✅ [已恢复]")


def test_a_failed_run_without_a_confirmed_finding_neither_pages_nor_resolves() -> None:
    """The alert step runs on failure: a transient lookup error leaves no finding, but
    it is no proof the earlier one is gone. State stays, nothing is sent."""
    api, ch = FakeIssues(), Channels()
    run_day(api, ch, 1, keys=["A"])
    sent = (len(ch.pages), len(ch.reports), list(api.writes))

    out = run_day(api, ch, 2, keys=[], clean=False)

    assert out.action == NONE
    assert (len(ch.pages), len(ch.reports), api.writes) == sent
    assert api.open_titles() == ["ops-checks paged: facet-reconcile"]
    # ... and when A is still there the next day, it is still the same condition
    assert run_day(api, ch, 3, keys=["A"]).action == REPORT


# --- nothing silences ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        ListingFailed("GET /issues: HTTP 502"),
        RuntimeError("something nobody planned for"),
        KeyError("body"),
    ],
)
def test_unreadable_state_pages_and_writes_nothing(error) -> None:
    """The issue's acceptance: a state read failure pages (more, never silent)."""
    api, ch = FakeIssues(), Channels()
    run_day(api, ch, 1, keys=["A"])
    api.listing_error = error
    writes = list(api.writes)

    out = run_day(api, ch, 2, keys=["A"])

    assert out.action == PAGE and out.exit_code == 0
    assert len(ch.pages) == 2 and ch.reports == []
    assert "跨运行去重状态读取失败" in ch.pages[-1]
    assert ch.pages[-1].startswith("PAGE: details of today")
    assert api.writes == writes  # a failed listing never falls through to create


def test_no_token_pages_as_before() -> None:
    ch = Channels()
    env = {k: v for k, v in SCHEDULED.items() if k != "GITHUB_TOKEN"}

    out = run_day(None, ch, 1, keys=["A"], env=env)

    assert out.action == PAGE and len(ch.pages) == 1


def test_a_bug_in_the_decision_code_still_pages(monkeypatch) -> None:
    api, ch = FakeIssues(), Channels()

    def broken(*_args, **_kwargs):
        raise ZeroDivisionError("a bug in decide")

    monkeypatch.setattr(pd, "decide", broken)

    out = run_day(api, ch, 1, keys=["A"])

    assert out.action == PAGE and len(ch.pages) == 1
    assert api.writes == []
    # ... and with nothing red there is nothing to say
    assert run_day(api, ch, 2, keys=[]).action == NONE


def test_a_page_that_cannot_be_delivered_records_nothing_so_the_next_run_pages() -> (
    None
):
    """State is written only after the page went out: otherwise a lost page would be
    suppressed tomorrow as "already paged" (why the watchdog's own trail issues,
    opened by its last step regardless of delivery, are not reused as state)."""
    api, ch = FakeIssues(), Channels()
    ch.page_error = RuntimeError("Feishu is down")

    with pytest.raises(RuntimeError, match="Feishu is down"):
        run_day(api, ch, 1, keys=["A"])
    assert api.issues == {}

    ch.page_error = None
    assert run_day(api, ch, 2, keys=["A"]).action == PAGE
    assert len(ch.pages) == 1


def test_a_resolved_page_that_cannot_be_delivered_keeps_the_state_open() -> None:
    api, ch = FakeIssues(), Channels()
    run_day(api, ch, 1, keys=["A"])
    ch.page_error = RuntimeError("Feishu is down")

    with pytest.raises(RuntimeError):
        run_day(api, ch, 2, keys=[])
    assert api.open_titles() == ["ops-checks paged: facet-reconcile"]

    ch.page_error = None
    assert run_day(api, ch, 3, keys=[]).action == RESOLVED


@pytest.mark.parametrize("failure", [False, RuntimeError("Feishu report failed")])
def test_a_report_that_cannot_be_delivered_pages_instead(failure) -> None:
    api, ch = FakeIssues(), Channels()
    run_day(api, ch, 1, keys=["A"])
    ch.report_result = failure
    writes = list(api.writes)

    out = run_day(api, ch, 2, keys=["A"])

    assert out.action == PAGE and ch.reports == []
    assert len(ch.pages) == 2
    assert ch.pages[-1].startswith("PAGE: details of today")
    assert "仍未恢复(第 2 天)" in ch.pages[-1] and "回落到告警群" in ch.pages[-1]
    assert api.writes == writes


def test_a_state_write_failure_after_a_good_page_is_loud_and_repeats_tomorrow() -> None:
    api, ch = FakeIssues(), Channels()
    api.write_error = RuntimeError("HTTP 403: Resource not accessible by integration")

    out = run_day(api, ch, 1, keys=["A"])

    assert len(ch.pages) == 1  # the page itself went out
    assert out.action == PAGE and out.exit_code == 1
    api.write_error = None
    assert run_day(api, ch, 2, keys=["A"]).action == PAGE  # nothing recorded -> pages


@pytest.mark.parametrize("failing", [{"comment", "close"}, {"close"}, {"comment"}])
def test_a_close_failure_after_a_resolved_page_is_loud_and_a_relapse_still_pages(
    failing,
) -> None:
    """RESOLVED went out but the issue stayed open with the old fingerprint: a relapse
    of the same identity must be a new page, not "still unresolved, day 5"."""
    api, ch = FakeIssues(), Channels()
    run_day(api, ch, 1, keys=["A"])
    api.fail_ops = set(failing)

    out = run_day(api, ch, 2, keys=[])

    assert out.action == RESOLVED and out.exit_code == 1
    assert len(ch.pages) == 2  # the page, and RESOLVED
    # the old state may still be open on GitHub, but it no longer counts as state
    api.fail_ops = set()
    relapse = run_day(api, ch, 3, keys=["A"])
    assert relapse.action == PAGE and ch.reports == []
    assert len(ch.pages) == 3


def test_a_cleared_marker_that_cannot_be_written_resolves_again_next_run() -> None:
    api, ch = FakeIssues(), Channels()
    run_day(api, ch, 1, keys=["A"])
    api.fail_ops = {"update_body"}

    out = run_day(api, ch, 2, keys=[])

    assert out.action == RESOLVED and out.exit_code == 1
    api.fail_ops = set()
    # nothing was cleared, so a run that is still clean resolves it (and says so)
    assert run_day(api, ch, 3, keys=[]).action == RESOLVED
    assert api.open_titles() == []


def test_a_stranger_s_issue_with_the_same_title_cannot_silence_the_pager() -> None:
    """The repository is public: anyone can open an issue titled like the state and
    forge its marker. Only the Actions bot's (or the owner's) counts."""
    real, ch = FakeIssues(), Channels()
    run_day(real, ch, 1, keys=["A"])
    forged = real.issues[101]["body"]
    stranger = FakeIssues(author="someone-else")
    stranger.issues[500] = {
        **real.issues[101],
        "author": "someone-else",
        "body": forged,
    }
    ch2 = Channels()

    out = run_day(stranger, ch2, 2, keys=["A"])

    assert out.action == PAGE and len(ch2.pages) == 1
    # the owner's own issue is trusted
    owner = FakeIssues(author="wangzitian0")
    ch3 = Channels()
    run_day(owner, ch3, 1, keys=["A"])
    assert run_day(owner, ch3, 2, keys=["A"]).action == REPORT


def test_an_unreadable_marker_pages_and_is_rewritten() -> None:
    api, ch = FakeIssues(), Channels()
    run_day(api, ch, 1, keys=["A"])
    api.issues[101]["body"] = "someone edited this and removed the marker"

    out = run_day(api, ch, 2, keys=["A"])

    assert out.action == PAGE and "去重状态读取失败" in ch.pages[-1]
    assert pd.parse_marker(api.issues[101]["body"], JOB) is not None
    assert run_day(api, ch, 3, keys=["A"]).action == REPORT


def test_duplicate_state_issues_are_all_closed_on_resolution() -> None:
    api, ch = FakeIssues(), Channels()
    run_day(api, ch, 1, keys=["A"])
    api.issues[150] = {**api.issues[101], "comments": []}  # a stray duplicate

    out = run_day(api, ch, 2, keys=[])

    assert out.action == RESOLVED and api.open_titles() == []


def test_state_is_per_job() -> None:
    api, ch = FakeIssues(), Channels()
    run_day(api, ch, 1, keys=["A"])

    other = dedup_page(
        SCHEDULED,
        job="vault-self-refresh-audit",
        findings=[Finding("A")],
        page_message="PAGE: vault",
        deliver_page=ch.page,
        deliver_report=ch.report,
        now=day(2),
        api=api,
    )

    assert other.action == PAGE  # the same key under another job is a new condition
    assert sorted(api.open_titles()) == [
        "ops-checks paged: facet-reconcile",
        "ops-checks paged: vault-self-refresh-audit",
    ]


# --- dry run and drill -----------------------------------------------------------------------


class ExplodingIssues:
    """Any call at all is a failure: a dry run must not even read."""

    def __getattr__(self, name):
        raise AssertionError(f"dry run touched the issues API: {name}")


def test_a_dry_run_delivers_nothing_and_touches_no_issue(capsys) -> None:
    ch = Channels()
    env = {**SCHEDULED, "WATCHDOG_DRY_RUN": "1"}

    out = run_day(ExplodingIssues(), ch, 1, keys=["A"], env=env)
    resolved = run_day(ExplodingIssues(), ch, 2, keys=[], env=env)

    assert out.action == NONE and resolved.action == NONE
    assert (ch.pages, ch.reports) == ([], [])
    assert "PAGE: details of today" in capsys.readouterr().out  # shown, not sent


def test_manual_ssh_diagnostics_do_not_use_or_write_state() -> None:
    """issue_trail_mode is OFF for them: page as before, no state, no RESOLVED."""
    api, ch = FakeIssues(), Channels()
    run_day(api, ch, 1, keys=["A"])
    env = {**SCHEDULED, "INFRA2_WATCHDOG_SSH_TARGETS_OVERRIDDEN": "1"}
    writes = list(api.writes)

    paged = run_day(api, ch, 2, keys=["A"], env=env)
    silent = run_day(api, ch, 3, keys=[], env=env)

    assert paged.action == PAGE and silent.action == NONE
    assert api.writes == writes and api.open_titles() != []


def test_a_drill_pages_but_records_nothing_so_the_next_scheduled_run_pages() -> None:
    """A drill's identity must not become what a scheduled run trusts: the real
    first page of that identity would otherwise read as "still unresolved"."""
    api, ch = FakeIssues(), Channels()
    drill = {
        **SCHEDULED,
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "INFRA2_PEER_LIVENESS_BOUND_CAP_HOURS": "0",
    }

    first = run_day(api, ch, 1, keys=["A"], env=drill)

    assert first.action == PAGE and len(ch.pages) == 1
    assert api.issues == {} and api.writes == []  # nothing recorded, nothing opened
    # the next scheduled run with the same identity is its first appearance
    scheduled = run_day(api, ch, 2, keys=["A"])
    assert scheduled.action == PAGE and len(ch.pages) == 2 and ch.reports == []
    assert api.open_titles() == ["ops-checks paged: facet-reconcile"]


def test_a_drill_never_closes_resolves_or_rewrites_recorded_state() -> None:
    api, ch = FakeIssues(), Channels()
    drill = {
        **SCHEDULED,
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "INFRA2_PEER_LIVENESS_BOUND_CAP_HOURS": "0",
    }
    run_day(api, ch, 1, keys=["A"])  # recorded by a scheduled run
    writes = list(api.writes)

    clear = run_day(api, ch, 2, keys=[], env=drill)
    changed = run_day(api, ch, 2, keys=["A", "drill"], env=drill)
    same = run_day(api, ch, 2, keys=["A"], env=drill)

    assert clear.action == NONE  # no RESOLVED from a run whose green proves nothing
    assert changed.action == PAGE and same.action == REPORT
    assert api.writes == writes and api.open_titles() != []
    # the scheduled state is untouched: tomorrow's real run is still day 2 of {A}
    assert run_day(api, ch, 3, keys=["A"]).action == REPORT
    assert run_day(api, ch, 4, keys=[]).action == RESOLVED


def test_a_branch_dispatch_never_closes_or_records_either() -> None:
    api, ch = FakeIssues(), Channels()
    run_day(api, ch, 1, keys=["A"])
    writes = list(api.writes)

    out = run_day(api, ch, 2, keys=[], env=BRANCH_DISPATCH)
    new = run_day(api, ch, 2, keys=["B"], env=BRANCH_DISPATCH)

    assert out.action == NONE and new.action == PAGE
    assert api.writes == writes and api.open_titles() != []
    assert len(ch.pages) == 2


# --- a report shows what the identity leaves out ----------------------------------------------


def test_a_report_carries_the_current_detail_lines_so_a_worsening_is_visible() -> None:
    """Identity excludes readings, so a finding getting worse does not page; the
    daily report must show it."""
    api, ch = FakeIssues(), Channels()
    run_day(api, ch, 1, keys=["A"])

    findings = [Finding("A", "A: backup is 190h old (bound 180h), 3 of 8 artifacts")]
    outcome = dedup_page(
        SCHEDULED,
        job=JOB,
        findings=findings,
        page_message="PAGE",
        deliver_page=ch.page,
        deliver_report=ch.report,
        now=day(2),
        api=api,
    )

    assert outcome.action == REPORT and len(ch.pages) == 1
    assert "A: backup is 190h old (bound 180h), 3 of 8 artifacts" in ch.reports[-1]
    # an explicit detail text wins over the findings' own lines, and is bounded
    explicit = "\n".join(f"- line {n}" for n in range(40)) + "\n- line 0"
    dedup_page(
        SCHEDULED,
        job=JOB,
        findings=findings,
        page_message="PAGE",
        deliver_page=ch.page,
        deliver_report=ch.report,
        detail=explicit,
        now=day(2),
        api=api,
    )
    text = ch.reports[-1]
    assert "- line 14" in text and "- line 15" not in text
    assert "…及另外 25 行" in text  # 40 unique lines, 15 shown
    assert text.count("- line 0") == 1  # unique


# --- the real workflow steps, executed ---------------------------------------------------------


def _steps(job: str) -> dict[str, dict]:
    jobs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    return {step["name"]: step for step in jobs[job]["steps"] if "name" in step}


def _script(job: str, step: str) -> str:
    """The Python heredoc of a workflow step, as the runner would execute it."""
    run = _steps(job)[step]["run"]
    return run.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]


def _execute(job: str, step: str) -> int:
    with pytest.raises(SystemExit) as stop:
        exec(compile(_script(job, step), f"{job}/{step}", "exec"), {"__name__": "x"})
    return stop.value.code


#: job -> (alert step, resolve step)
DEDUPED_JOBS = {
    "facet-reconcile": ("Alert on confirmed findings", "Resolve the paged findings"),
    "vault-self-refresh-audit": (
        "Alert on confirmed failure",
        "Resolve the paged failures",
    ),
    "secrets-reconcile": ("Alert on confirmed findings", "Resolve the paged findings"),
}


@pytest.mark.parametrize("job", DEDUPED_JOBS)
def test_each_deduplicated_job_is_wired_through_page_dedup(job) -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    body = workflow["jobs"][job]
    alert, resolve = DEDUPED_JOBS[job]
    steps = _steps(job)
    names = [step.get("name") for step in body["steps"]]
    dry_run = "!(github.event_name == 'workflow_dispatch' && inputs.dry_run)"

    # issues: write for this job only, never workflow-wide (deploy-v2-canary runs PR code)
    assert workflow["permissions"] == {"contents": "read", "actions": "read"}
    assert body["permissions"] == {
        "contents": "read",
        "actions": "read",
        "issues": "write",
    }
    # the token reaches only the two steps that use it, not the job's other steps
    assert "GITHUB_TOKEN" not in body["env"]
    for name in (alert, resolve):
        assert steps[name]["env"]["GITHUB_TOKEN"] == "${{ github.token }}"
    # a failed run pages through the dedup, a green run resolves; neither on a dry run
    assert steps[alert]["if"] == f"failure() && {dry_run}"
    assert steps[resolve]["if"] == f"success() && {dry_run}"
    assert names.index(alert) < names.index(resolve)
    assert (
        f'job="{job}"' in steps[alert]["run"] and "dedup_page(" in steps[alert]["run"]
    )
    assert "deliver_page=deliver_out_of_band_alert" in steps[alert]["run"]
    assert f'job="{job}"' in steps[resolve]["run"]
    assert "resolve_page_state(" in steps[resolve]["run"]
    # the pager is never called directly any more: that was the daily re-page
    for name in (alert, resolve):
        assert "deliver_out_of_band_alert(" not in steps[name]["run"]
    # the "still unresolved" report needs the reports chat in the alert step
    reports = {
        "INFRA2_REPORTS_FEISHU_APP_ID",
        "INFRA2_REPORTS_FEISHU_APP_SECRET",
        "INFRA2_REPORTS_FEISHU_CHAT_ID",
    }
    assert reports <= set(steps[alert].get("env", {})) | set(body["env"])


@pytest.fixture
def step_env(monkeypatch):
    """The environment of a scheduled run, with the page channels and issues faked."""
    api, channels = FakeIssues(), Channels()
    import tools.out_of_band_watchdog as watchdog

    monkeypatch.setattr(pd, "_api_from_env", lambda _env: api)
    monkeypatch.setattr(pd, "deliver_infra2_report", channels.report)
    monkeypatch.setattr(watchdog, "deliver_out_of_band_alert", channels.page)
    for name, value in SCHEDULED.items():
        monkeypatch.setenv(name, value)
    return api, channels, monkeypatch


def test_facet_steps_page_report_and_resolve(step_env, tmp_path) -> None:
    import tools.facet_reconcile as fr

    api, ch, monkeypatch = step_env
    alert, resolve = DEDUPED_JOBS["facet-reconcile"]
    observation = tmp_path / "observation.json"
    monkeypatch.setenv(fr.OBSERVATION_ENV, str(observation))

    def sections(note="", *, dns="ran", compose_blocked=False):
        return [
            fr.Section(
                "compose-id",
                blockers=["live lookup failed"] if compose_blocked else [],
            ),
            fr.Section("config-hash"),
            fr.Section(
                "dns",
                confirmed=[f"a.example.test {note}"] if note else [],
                confirmed_keys=["dns:a.example.test"] if note else [],
                skipped="Cloudflare API observation blocked"
                if dns == "skipped"
                else "",
            ),
        ]

    def job_ran(secs):
        """The check step's observation, then the green job's resolve step."""
        monkeypatch.setattr(fr, "run_all", lambda: secs)
        fr.write_observation(secs)
        return _execute("facet-reconcile", resolve)

    monkeypatch.setattr(fr, "run_all", lambda: sections("missing since 09-20"))
    assert _execute("facet-reconcile", alert) == 0
    assert (
        len(ch.pages) == 1 and "[dns] a.example.test missing since 09-20" in ch.pages[0]
    )

    age_state(api, "facet-reconcile", 1)
    monkeypatch.setattr(fr, "run_all", lambda: sections("missing since 09-20 (day 2)"))
    assert _execute("facet-reconcile", alert) == 0
    assert len(ch.pages) == 1
    assert ch.reports[0].startswith("[报告] 仍未恢复 · 第 2 天 · facet-reconcile")
    assert "missing since 09-20 (day 2)" in ch.reports[0]  # the current reading

    # the job is green only because the dns section was skipped (Cloudflare 401): that
    # is not proof the drift is gone, so no RESOLVED and the state is kept
    assert job_ran(sections(dns="skipped")) == 0
    assert len(ch.pages) == 1 and api.open_titles() != []
    # ... the same on a failed run whose alert step finds no finding
    monkeypatch.setattr(fr, "run_all", lambda: sections(dns="skipped"))
    assert _execute("facet-reconcile", alert) == 0
    assert len(ch.pages) == 1 and api.open_titles() != []
    # a blocker in ANOTHER section does not stop the dns finding from being resolved
    # once dns really ran and found nothing
    monkeypatch.setattr(fr, "run_all", lambda: sections(compose_blocked=True))
    assert _execute("facet-reconcile", alert) == 0
    assert ch.pages[-1].startswith("✅ [已恢复] 每日 facet 对账")
    assert api.open_titles() == []

    # a green run resolves what it observed; without an observation nothing resolves
    monkeypatch.setattr(fr, "run_all", lambda: sections("missing again"))
    assert _execute("facet-reconcile", alert) == 0
    pages = len(ch.pages)
    observation.unlink(missing_ok=True)
    assert _execute("facet-reconcile", resolve) == 0
    assert len(ch.pages) == pages and api.open_titles() != []
    assert job_ran(sections()) == 0
    assert ch.pages[-1].startswith("✅ [已恢复] 每日 facet 对账")
    assert api.open_titles() == []


def test_vault_steps_page_report_and_resolve(step_env, tmp_path) -> None:
    import tools.vault_self_refresh_audit_check as vault

    api, ch, monkeypatch = step_env
    alert, resolve = DEDUPED_JOBS["vault-self-refresh-audit"]
    saved = tmp_path / "vault-report.json"
    monkeypatch.setenv(vault.REPORT_ENV, str(saved))

    def report(summary: str, status="fail", result_status="fail", check_id="agent"):
        return {
            "status": status,
            "results": [
                {
                    "service_id": "platform/redis",
                    "check_id": check_id,
                    "status": result_status,
                    "severity": "critical",
                    "summary": summary,
                }
            ],
        }

    monkeypatch.setattr(vault, "run", lambda: report("restarted 3 times"))
    assert _execute("vault-self-refresh-audit", alert) == 0
    assert len(ch.pages) == 1 and "restarted 3 times" in ch.pages[0]

    age_state(api, "vault-self-refresh-audit", 2)
    monkeypatch.setattr(vault, "run", lambda: report("restarted 8 times"))
    assert _execute("vault-self-refresh-audit", alert) == 0
    assert len(ch.pages) == 1
    assert ch.reports[0].startswith(
        "[报告] 仍未恢复 · 第 3 天 · vault-self-refresh-audit"
    )
    assert "restarted 8 times" in ch.reports[0]  # the reading that moved

    # the rerun is non-pass without any `fail` result (a hiccup): nothing resolves
    monkeypatch.setattr(vault, "run", lambda: report("?", "fail", "error"))
    assert _execute("vault-self-refresh-audit", alert) == 0
    assert len(ch.pages) == 1 and api.open_titles() != []

    # a green job that has no readable report proves nothing
    assert _execute("vault-self-refresh-audit", resolve) == 0
    assert len(ch.pages) == 1 and api.open_titles() != []

    # an all-pass rerun resolves
    monkeypatch.setattr(vault, "run", lambda: report("ok", "pass", "pass"))
    assert _execute("vault-self-refresh-audit", alert) == 0
    assert ch.pages[-1].startswith("✅ [已恢复] Vault self-refresh 审计")
    assert api.open_titles() == []


def test_vault_info_for_a_check_that_could_not_compare_does_not_resolve_its_failure(
    step_env, tmp_path
) -> None:
    """`deployed-template` answers `info` ("could not compare") when it could not look
    and `fail` when the template is stale: the audit passes on the info, but the
    earlier failure is unknown, not gone."""
    import tools.vault_self_refresh_audit_check as vault

    api, ch, monkeypatch = step_env
    alert, resolve = DEDUPED_JOBS["vault-self-refresh-audit"]
    saved = tmp_path / "vault-report.json"
    monkeypatch.setenv(vault.REPORT_ENV, str(saved))

    def report(result_status: str, status: str) -> dict:
        return {
            "status": status,
            "results": [
                {
                    "service_id": "platform/redis",
                    "check_id": "deployed-template",
                    "status": result_status,
                    "severity": "P1" if result_status == "fail" else "P3",
                    "summary": "template",
                }
            ],
        }

    monkeypatch.setattr(vault, "run", lambda: report("fail", "fail"))
    assert _execute("vault-self-refresh-audit", alert) == 0
    assert len(ch.pages) == 1

    # failed job, rerun passes on an `info`
    monkeypatch.setattr(vault, "run", lambda: report("info", "pass"))
    assert _execute("vault-self-refresh-audit", alert) == 0
    # green job whose saved report has the same `info`
    saved.write_text(json.dumps(report("info", "pass")), encoding="utf-8")
    assert _execute("vault-self-refresh-audit", resolve) == 0
    assert len(ch.pages) == 1 and api.open_titles() != []

    # the template was compared and matches: now it is resolved
    saved.write_text(json.dumps(report("pass", "pass")), encoding="utf-8")
    assert _execute("vault-self-refresh-audit", resolve) == 0
    assert ch.pages[-1].startswith("✅ [已恢复] Vault self-refresh 审计")
    assert api.open_titles() == []


def test_secrets_steps_page_report_and_resolve(step_env, tmp_path) -> None:
    api, ch, monkeypatch = step_env
    alert, resolve = DEDUPED_JOBS["secrets-reconcile"]
    path = tmp_path / "report.json"
    monkeypatch.setenv("SECRETS_RECONCILE_REPORT", str(path))

    def write(used: int, *, capacity=True, transport_error=""):
        path.write_text(
            json.dumps(
                {
                    "ok": False,
                    "stores": [],
                    "capacity": {
                        "ok": False,
                        "items": [
                            {
                                "name": "cloudflare.kv.write",
                                "used": used,
                                "limit": 1000,
                                "window": "day",
                                "level": "exceeded" if used > 1000 else "ok",
                            }
                        ],
                    }
                    if capacity
                    else None,
                    **({"transport_error": transport_error} if transport_error else {}),
                }
            ),
            encoding="utf-8",
        )

    write(1198)
    assert _execute("secrets-reconcile", alert) == 0
    assert len(ch.pages) == 1
    assert "quota cloudflare.kv.write 1198/1000 per day exceeded" in ch.pages[0]

    age_state(api, "secrets-reconcile", 1)
    write(1500)  # the reading moved; the finding did not
    assert _execute("secrets-reconcile", alert) == 0
    assert len(ch.pages) == 1
    assert ch.reports[0].startswith("[报告] 仍未恢复 · 第 2 天 · secrets-reconcile")

    # an unreadable report is no proof of recovery, on a failed or a green run
    write(10, capacity=False)
    assert _execute("secrets-reconcile", alert) == 0
    assert _execute("secrets-reconcile", resolve) == 0
    path.write_text(json.dumps({"ok": False, "transport_error": "ssh"}), "utf-8")
    assert _execute("secrets-reconcile", alert) == 0
    assert len(ch.pages) == 1 and api.open_titles() != []

    # a complete, clean report resolves
    write(10)
    assert _execute("secrets-reconcile", resolve) == 0
    assert ch.pages[-1].startswith("✅ [已恢复] 密钥对账与容量")
    assert api.open_titles() == []


def test_secrets_store_reconciled_without_1password_does_not_resolve_its_findings(
    step_env, tmp_path
) -> None:
    api, ch, monkeypatch = step_env
    alert, resolve = DEDUPED_JOBS["secrets-reconcile"]
    path = tmp_path / "report.json"
    monkeypatch.setenv("SECRETS_RECONCILE_REPORT", str(path))

    def write(*, missing, note=""):
        row = {
            "service": "platform/alerting",
            "env": "staging",
            "missing": missing,
            "ok": not missing,
        }
        if note:
            row["note"] = note
        path.write_text(
            json.dumps({"ok": False, "stores": [row], "capacity": {"items": []}}),
            encoding="utf-8",
        )

    write(missing=["FEISHU_APP_ID"])
    assert _execute("secrets-reconcile", alert) == 0
    assert len(ch.pages) == 1

    # 1Password was unavailable, so the comparison that found it could not be repeated
    write(missing=[], note="1Password unavailable: ssh timeout")
    assert _execute("secrets-reconcile", resolve) == 0
    assert len(ch.pages) == 1 and api.open_titles() != []

    write(missing=[])
    assert _execute("secrets-reconcile", resolve) == 0
    assert ch.pages[-1].startswith("✅ [已恢复] 密钥对账与容量")
