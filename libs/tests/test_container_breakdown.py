"""Tests for container-breakdown detection + alerting (the 'why is it down' gap)."""

from __future__ import annotations

from libs.container_breakdown import (
    Breakdown,
    broken_state,
    build_breakdown_alert_payload,
    classify_reason,
    container_identity,
    container_name,
    find_breakdown_containers,
)


def test_broken_state_flags_restarting_unhealthy_exited_and_dead():
    assert (
        broken_state({"State": "restarting", "Status": "Restarting (1) 3s ago"})
        == "restarting"
    )
    assert (
        broken_state({"State": "running", "Status": "Up 2 hours (unhealthy)"})
        == "unhealthy"
    )
    # crashed-and-stopped (gave up retrying) — the steadier failure that the
    # restarting-only check used to miss
    assert (
        broken_state({"State": "exited", "Status": "Exited (137) 5 minutes ago"})
        == "exited"
    )
    assert broken_state({"State": "dead", "Status": "Dead"}) == "dead"
    # healthy / clean intentional stop are NOT breakdowns
    assert broken_state({"State": "running", "Status": "Up 2 hours (healthy)"}) is None
    assert broken_state({"State": "exited", "Status": "Exited (0) 1h ago"}) is None


def test_container_name_strips_leading_slash():
    assert (
        container_name({"Names": ["/finance_report-app-vault-agent"]})
        == "finance_report-app-vault-agent"
    )
    assert container_name({"Id": "abcdef1234567890"}) == "abcdef123456"


def test_classify_reason_matches_known_breakdown_first():
    logs = "starting agent\nVAULT_ROLE_ID and VAULT_SECRET_ID are required\nexiting"
    reason, detail = classify_reason(logs)
    assert "Vault AppRole creds missing" in reason
    assert "VAULT_ROLE_ID" in detail


def test_classify_reason_falls_back_to_last_nonempty_line():
    reason, detail = classify_reason("boom: something unexpected\n\n")
    assert "see log tail" in reason
    assert detail == "boom: something unexpected"


def test_classify_reason_handles_no_logs():
    reason, detail = classify_reason("")
    assert "no logs" in reason
    assert detail == ""


def test_container_identity_prefers_canonical_labels_then_registry_fallback():
    labeled = {
        "Names": ["/anything"],
        "Labels": {
            "party.zitian.infra.service-id": "finance_report/app",
            "party.zitian.infra.component": "backend",
            "party.zitian.infra.environment": "staging",
        },
    }
    assert container_identity(labeled) == (
        "finance_report/app",
        "backend",
        "staging",
    )

    legacy = {
        "Names": ["/platform-alerting-deploy-guard-staging"],
        "Labels": {"com.docker.compose.service": "deploy-queue-guard"},
    }
    assert container_identity(legacy) == (
        "platform/alerting",
        "deploy-queue-guard",
        "staging",
    )


def test_find_breakdown_containers_filters_and_attaches_reason():
    containers = [
        {
            "Id": "1",
            "Names": ["/healthy-svc"],
            "State": "running",
            "Status": "Up (healthy)",
        },
        {
            "Id": "2",
            "Names": ["/vault-agent"],
            "State": "restarting",
            "Status": "Restarting (1)",
        },
        {"Id": "3", "Names": ["/sick"], "State": "running", "Status": "Up (unhealthy)"},
    ]
    logs = {
        "2": "VAULT_ROLE_ID and VAULT_SECRET_ID are required",
        "3": "connection refused",
    }
    found = find_breakdown_containers(containers, lambda cid: logs.get(cid, ""))

    names = {b.container for b in found}
    assert names == {"vault-agent", "sick"}  # healthy one excluded
    by_name = {b.container: b for b in found}
    assert by_name["vault-agent"].state == "restarting"
    assert "Vault AppRole creds missing" in by_name["vault-agent"].reason
    assert "dependency unreachable" in by_name["sick"].reason


def test_build_alert_payload_shape_is_alertmanager_like():
    bd = Breakdown(
        container="vault-agent",
        state="restarting",
        reason="Vault creds missing",
        detail="...required",
        service_id="finance_report/app",
        component="vault-agent",
        environment="staging",
    )
    payload = build_breakdown_alert_payload([bd])

    assert payload["status"] == "firing"
    assert payload["commonLabels"]["alertname"] == "ContainerBreakdown"
    assert payload["commonLabels"]["severity"] == "critical"
    assert len(payload["alerts"]) == 1
    alert = payload["alerts"][0]
    assert alert["labels"]["service_id"] == "finance_report/app"
    assert alert["labels"]["component"] == "vault-agent"
    assert alert["labels"]["environment"] == "staging"
    assert alert["labels"]["failure_domain"] == "runtime"
    assert "vault-agent restarting" in alert["annotations"]["summary"]


def test_build_alert_payload_resolved_when_empty():
    payload = build_breakdown_alert_payload([], firing=False)
    assert payload["status"] == "resolved"
    assert payload["alerts"] == []


def test_run_once_requires_consecutive_broken_polls_before_firing(monkeypatch):
    """#475 flap hysteresis: a single broken sweep must NOT fire; only the Nth
    (failure_threshold) CONSECUTIVE broken sweep does."""
    import libs.container_breakdown_watch as w

    breakdown = Breakdown(
        container="vault-agent", state="restarting", reason="r", detail="d"
    )
    monkeypatch.setattr(w, "sweep", lambda client, tail: [breakdown])
    posted: list = []
    monkeypatch.setattr(w, "_post_alert", lambda payload: posted.append(payload))

    state: dict = {}
    assert (
        w.run_once(
            client=None,
            log_tail=25,
            container_state=state,
            renotify=1800,
            failure_threshold=3,
            recovery_threshold=5,
        )
        == 0
    )
    assert (
        w.run_once(
            client=None,
            log_tail=25,
            container_state=state,
            renotify=1800,
            failure_threshold=3,
            recovery_threshold=5,
        )
        == 0
    )
    assert not posted  # still below threshold after 2 consecutive broken polls
    assert (
        w.run_once(
            client=None,
            log_tail=25,
            container_state=state,
            renotify=1800,
            failure_threshold=3,
            recovery_threshold=5,
        )
        == 1
    )
    assert len(posted) == 1  # 3rd consecutive broken poll fires
    assert posted[0]["status"] == "firing"


def test_run_once_respects_renotify_window_once_active(monkeypatch):
    """BREAKDOWN_RENOTIFY_SECONDS still suppresses repeat firing of an ALREADY-active
    incident (unrelated to the flap-hysteresis fire/resolve thresholds)."""
    import libs.container_breakdown_watch as w

    breakdown = Breakdown(
        container="vault-agent", state="restarting", reason="r", detail="d"
    )
    monkeypatch.setattr(w, "sweep", lambda client, tail: [breakdown])
    posted: list = []
    monkeypatch.setattr(w, "_post_alert", lambda payload: posted.append(payload))

    state: dict = {}
    for _ in range(3):  # reach failure_threshold=3 -> fires once
        w.run_once(
            client=None,
            log_tail=25,
            container_state=state,
            renotify=1800,
            failure_threshold=3,
            recovery_threshold=5,
        )
    assert len(posted) == 1
    # further broken sweeps within the renotify window are suppressed
    assert (
        w.run_once(
            client=None,
            log_tail=25,
            container_state=state,
            renotify=1800,
            failure_threshold=3,
            recovery_threshold=5,
        )
        == 0
    )
    assert len(posted) == 1


def test_run_once_requires_consecutive_healthy_polls_before_resolving(monkeypatch):
    """#475 flap hysteresis: recovery requires recovery_threshold CONSECUTIVE healthy
    sweeps, not just one -- the exact ContainerBreakdown fire/resolve storm."""
    import libs.container_breakdown_watch as w

    breakdown = Breakdown(
        container="vault-agent", state="restarting", reason="r", detail="d"
    )
    posted: list = []
    monkeypatch.setattr(w, "_post_alert", lambda payload: posted.append(payload))
    state: dict = {}

    monkeypatch.setattr(w, "sweep", lambda client, tail: [breakdown])
    for _ in range(3):  # reach failure_threshold -> fires
        w.run_once(
            client=None,
            log_tail=25,
            container_state=state,
            renotify=1800,
            failure_threshold=3,
            recovery_threshold=5,
        )
    assert [p["status"] for p in posted] == ["firing"]

    monkeypatch.setattr(w, "sweep", lambda client, tail: [])  # now healthy
    for _ in range(4):  # below recovery_threshold=5 -> must NOT resolve yet
        w.run_once(
            client=None,
            log_tail=25,
            container_state=state,
            renotify=1800,
            failure_threshold=3,
            recovery_threshold=5,
        )
    assert [p["status"] for p in posted] == ["firing"]  # still no RESOLVED

    w.run_once(  # 5th consecutive healthy sweep -> resolves
        client=None,
        log_tail=25,
        container_state=state,
        renotify=1800,
        failure_threshold=3,
        recovery_threshold=5,
    )
    assert [p["status"] for p in posted] == ["firing", "resolved"]
    # the resolved alert must carry the ORIGINAL state label so its label set matches
    # the firing instance — a stub "recovered" state would never resolve the page
    assert (
        posted[1]["alerts"][0]["labels"]["state"]
        == posted[0]["alerts"][0]["labels"]["state"]
        == "restarting"
    )
    # fully resolved and forgotten so it can start a fresh incident later
    assert "vault-agent" not in state


def test_run_once_relapse_before_recovery_threshold_is_same_incident(monkeypatch):
    """The critical #475 property: a broken poll seen WHILE recovering (before
    recovery_threshold healthy polls) must NOT start a new incident and must NOT
    reset the renotify clock -- this is what produced 333 firing+resolved pairs."""
    import libs.container_breakdown_watch as w

    breakdown = Breakdown(
        container="vault-agent", state="restarting", reason="r", detail="d"
    )
    posted: list = []
    monkeypatch.setattr(w, "_post_alert", lambda payload: posted.append(payload))
    state: dict = {}

    monkeypatch.setattr(w, "sweep", lambda client, tail: [breakdown])
    for _ in range(3):  # fires (failure_threshold=3)
        w.run_once(
            client=None,
            log_tail=25,
            container_state=state,
            renotify=1800,
            failure_threshold=3,
            recovery_threshold=5,
        )
    assert len(posted) == 1

    # 2 healthy polls (below recovery_threshold=5), then broken again: relapse.
    monkeypatch.setattr(w, "sweep", lambda client, tail: [])
    w.run_once(
        client=None,
        log_tail=25,
        container_state=state,
        renotify=1800,
        failure_threshold=3,
        recovery_threshold=5,
    )
    w.run_once(
        client=None,
        log_tail=25,
        container_state=state,
        renotify=1800,
        failure_threshold=3,
        recovery_threshold=5,
    )
    monkeypatch.setattr(w, "sweep", lambda client, tail: [breakdown])
    fired = w.run_once(
        client=None,
        log_tail=25,
        container_state=state,
        renotify=1800,
        failure_threshold=3,
        recovery_threshold=5,
    )
    # no fresh firing (same incident, still well inside the renotify window) and no
    # RESOLVED was ever posted for the blip
    assert fired == 0
    assert [p["status"] for p in posted] == ["firing"]
    assert state["vault-agent"].active is True
    assert state["vault-agent"].good_streak == 0  # relapse reset the recovery streak


def test_run_once_logs_firing_and_resolved_decisions(monkeypatch):
    """The watcher must log WHICH container it fired/resolved on (the 07:33 case: a brief
    fire->resolve was only attributable by forensic IP->container reconstruction). Firing was
    already logged; resolve + the bridge-post were not. Capture logger.warning directly (the
    module calls logging.basicConfig, so caplog is unreliable here)."""
    import libs.container_breakdown_watch as w

    bd = Breakdown(
        container="finance_report-frontend-branch-main",
        state="unhealthy",
        reason="r",
        detail="d",
    )
    monkeypatch.setattr(w, "_post_alert", lambda payload: None)
    logs: list[str] = []
    monkeypatch.setattr(
        w.logger, "warning", lambda msg, *a: logs.append(msg % a if a else msg)
    )
    state: dict = {}

    monkeypatch.setattr(w, "sweep", lambda client, tail: [bd])  # broken -> fires
    for _ in range(3):
        w.run_once(
            client=None,
            log_tail=25,
            container_state=state,
            renotify=1800,
            failure_threshold=3,
            recovery_threshold=5,
        )
    monkeypatch.setattr(w, "sweep", lambda client, tail: [])  # recovered -> resolves
    for _ in range(5):
        w.run_once(
            client=None,
            log_tail=25,
            container_state=state,
            renotify=1800,
            failure_threshold=3,
            recovery_threshold=5,
        )

    blob = "\n".join(logs)
    assert "BREAKDOWN-ALERT firing" in blob  # firing decision + bridge-post logged
    assert "BREAKDOWN-RESOLVED" in blob  # resolve decision now logged (was silent)
    assert "finance_report-frontend-branch-main" in blob  # named, not anonymous


def test_an_unchanged_ongoing_incident_pages_once_and_escalation_pages_again(
    monkeypatch,
):
    """finance_report-backend-branch-main, production 2026-09-08/09: unhealthy for 18
    hours, 31 identical `ContainerBreakdown critical` pages delivered to Feishu at
    ~30-minute intervals, every one accepted, and the container stayed broken. An alert
    that repeats at the same severity is one an operator learns to skip.

    Owner decision 2026-09-09: page once, re-page only on a change, digest the rest.
    """
    import time

    import libs.container_breakdown_watch as w

    clock = {"now": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["now"])
    current = {
        "b": Breakdown(
            container="fr-preview",
            state="unhealthy",
            reason="s3 unreachable",
            detail="d",
        )
    }
    monkeypatch.setattr(w, "sweep", lambda client, tail: [current["b"]])
    posted: list = []
    monkeypatch.setattr(w, "_post_alert", lambda payload: posted.append(payload))
    state: dict = {}
    chronic: dict = {}

    def sweep_n(n: int) -> int:
        fired = 0
        for _ in range(n):
            clock["now"] += 60
            fired += w.run_once(
                client=None,
                log_tail=25,
                container_state=state,
                renotify=0,
                failure_threshold=3,
                recovery_threshold=5,
                chronic=chronic,
                chronic_digest_seconds=24 * 3600,
            )
        return fired

    assert sweep_n(3) == 1  # the incident pages once
    # 18 hours of it, at the production sweep interval
    assert sweep_n(18 * 60) == 0
    assert [p["status"] for p in posted] == ["firing"], "one page, not 31"
    assert chronic["fr-preview"] == 18 * 60

    # the cause changes: that is news again
    current["b"] = Breakdown(
        container="fr-preview", state="restarting", reason="crash loop", detail="d"
    )
    assert sweep_n(1) == 1
    assert [p["status"] for p in posted] == ["firing", "firing"]
    assert posted[-1]["alerts"][0]["labels"]["state"] == "restarting"

    # and back to steady state: still no repeat pages
    assert sweep_n(30) == 0
    assert len([p for p in posted if p["status"] == "firing"]) == 2


def test_a_positive_renotify_restores_the_timer_and_pages_reset_the_digest_counter(
    monkeypatch,
):
    """Review on #686: the two mechanisms must not overlap.

    An operator who sets a positive BREAKDOWN_RENOTIFY_SECONDS has asked for periodic
    re-paging and must not also collect a digest about the same incident; and any real
    page — escalation included — resets "since it last paged", or the digest's own
    wording is wrong.
    """
    import time

    import libs.container_breakdown_watch as w

    clock = {"now": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["now"])
    current = {"b": Breakdown(container="c", state="unhealthy", reason="r", detail="d")}
    monkeypatch.setattr(w, "sweep", lambda client, tail: [current["b"]])
    monkeypatch.setattr(w, "_post_alert", lambda payload: None)

    def sweeps(n, *, renotify, state, chronic):
        fired = 0
        for _ in range(n):
            clock["now"] += 60
            fired += w.run_once(
                client=None,
                log_tail=25,
                container_state=state,
                renotify=renotify,
                failure_threshold=3,
                recovery_threshold=5,
                chronic=chronic,
                chronic_digest_seconds=24 * 3600,
            )
        return fired

    # timer on: re-pages on the timer, and nothing accrues to the digest
    state, chronic = {}, {}
    assert sweeps(3, renotify=1800, state=state, chronic=chronic) == 1
    assert sweeps(60, renotify=1800, state=state, chronic=chronic) >= 1
    assert [n for n in chronic if n != "__digest_at__"] == []

    # timer off: pages once, accrues, and an escalation both re-pages and resets the count
    state, chronic = {}, {}
    assert sweeps(3, renotify=0, state=state, chronic=chronic) == 1
    assert sweeps(10, renotify=0, state=state, chronic=chronic) == 0
    assert chronic["c"] == 10
    current["b"] = Breakdown(container="c", state="restarting", reason="r2", detail="d")
    assert sweeps(1, renotify=0, state=state, chronic=chronic) == 1
    assert "c" not in chronic, "a page resets 'since it last paged'"


def test_run_once_stay_resolved_floor_suppresses_a_refire_and_digests_chronic_once(
    monkeypatch,
):
    """#658 recommendation 7: platform-prefect-worker fired 05:48 → resolved 05:55 →
    fired again 06:45 for seven weeks. After a RESOLVED, the same container does not
    page again inside the floor; it is counted as chronic and reported once per
    digest window."""
    import time

    import libs.container_breakdown_watch as w

    clock = {"now": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["now"])
    broken = Breakdown(
        container="prefect-worker", state="unhealthy", reason="r", detail="d"
    )
    observed = {"broken": True}
    monkeypatch.setattr(
        w, "sweep", lambda client, tail: [broken] if observed["broken"] else []
    )
    posted: list = []
    monkeypatch.setattr(w, "_post_alert", lambda payload: posted.append(payload))
    state: dict = {}
    resolved_at: dict = {}
    chronic: dict = {}

    def sweep_n(n: int) -> int:
        fired = 0
        for _ in range(n):
            clock["now"] += 60
            fired += w.run_once(
                client=None,
                log_tail=25,
                container_state=state,
                # the shipped default: an unchanged ongoing incident never re-pages,
                # it accrues into the digest instead (#475)
                renotify=0,
                failure_threshold=3,
                recovery_threshold=5,
                resolved_at=resolved_at,
                chronic=chronic,
                stay_resolved_seconds=6 * 3600,
                # shorter than the floor on purpose, so a digest window can elapse while
                # the container is still inside it — the scenario this test is about
                chronic_digest_seconds=3600,
            )
        return fired

    def chronic_digests() -> list:
        return [
            p
            for p in posted
            if any(
                a.get("labels", {}).get("state") == "chronic"
                for a in p.get("alerts", [])
            )
        ]

    assert sweep_n(3) == 1  # first incident pages
    assert [p["status"] for p in posted] == ["firing"]
    observed["broken"] = False
    assert sweep_n(5) == 0  # resolves after 5 healthy sweeps
    assert [p["status"] for p in posted] == ["firing", "resolved"]
    assert "prefect-worker" in resolved_at

    observed["broken"] = True
    assert sweep_n(3) == 0  # re-broke 8 minutes after resolving: chronic, no page
    # The first chronic observation STARTS the digest clock rather than posting on it:
    # an ongoing unchanged incident is chronic too, so an unset clock would put a digest
    # on the pager one sweep after every incident began.
    assert not chronic_digests()
    assert chronic["prefect-worker"] == 1  # exactly the one suppressed refire
    clock["now"] += 3600
    assert sweep_n(1) == 0  # a digest window later, the chronic set goes out once
    assert len(chronic_digests()) == 1
    digest = chronic_digests()[0]
    assert digest["commonLabels"]["alertname"] == "ContainerBreakdownChronic"
    assert digest["commonLabels"]["severity"] == "warning"
    assert all(a["labels"]["severity"] == "warning" for a in digest["alerts"])
    assert "prefect-worker" not in chronic and chronic["__digest_at__"] == clock["now"]
    assert (
        len([p for p in posted if p["status"] == "firing"]) == 2
    )  # the incident + the digest

    observed["broken"] = False
    sweep_n(5)
    observed["broken"] = True
    assert (
        sweep_n(3) == 0
    )  # still inside the floor: counted again, no second digest today
    assert chronic["prefect-worker"] == 1
    assert len(chronic_digests()) == 1

    # the floor is measured from the LATEST resolve: a container that keeps flapping keeps
    # refreshing it and only ever reaches the daily digest — that is the point. Once it has
    # stayed resolved for longer than the floor, a fresh incident pages normally again.
    observed["broken"] = False
    sweep_n(5)
    clock["now"] += 7 * 3600
    observed["broken"] = True
    assert sweep_n(3) == 1
