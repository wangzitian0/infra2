"""Tests for container-breakdown detection + alerting (the 'why is it down' gap)."""

from __future__ import annotations

from dataclasses import replace

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
    assert reason == "Vault AppRole 凭据缺失(VAULT_ROLE_ID / VAULT_SECRET_ID)"
    assert "VAULT_ROLE_ID" in detail


def test_classify_reason_falls_back_to_last_nonempty_line():
    reason, detail = classify_reason("boom: something unexpected\n\n")
    assert reason == "崩溃循环 / 不健康(见日志尾)"
    assert detail == "boom: something unexpected"


def test_classify_reason_handles_no_logs():
    reason, detail = classify_reason("")
    assert reason == "崩溃循环 / 不健康(未取到日志)"
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
    assert "Vault AppRole 凭据缺失" in by_name["vault-agent"].reason
    assert by_name["sick"].reason == "依赖不可达(`connection refused`)"


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
    import libs.observability.watchers.breakdown_watch as w

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
    import libs.observability.watchers.breakdown_watch as w

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
    import libs.observability.watchers.breakdown_watch as w

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
    import libs.observability.watchers.breakdown_watch as w

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
    import libs.observability.watchers.breakdown_watch as w

    # a production container: a preview one (…-branch-main) is digest-only (#903)
    bd = Breakdown(
        container="finance_report-frontend",
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
    assert "finance_report-frontend" in blob  # named, not anonymous


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

    import libs.observability.watchers.breakdown_watch as w

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

    import libs.observability.watchers.breakdown_watch as w

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

    import libs.observability.watchers.breakdown_watch as w

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


def test_oom_killed_breakdown_classifies_as_host_memory():
    """Verify that containers terminated by host CGroup OOM classify as host-memory instead of runtime."""
    reason, detail = classify_reason(
        "kernel: Out of memory: Killed process 1234 (python)"
    )
    assert reason == "内存耗尽(宿主机 CGroup 触发 OOM kill)"

    bd = Breakdown(
        container="finance_report-backend",
        state="exited",
        reason=reason,
        detail=detail,
        service_id="finance_report/app",
        component="backend",
        environment="production",
    )
    payload = build_breakdown_alert_payload([bd])
    assert payload["status"] == "firing"
    alert = payload["alerts"][0]
    assert alert["labels"]["failure_domain"] == "host-memory"
    assert alert["annotations"]["description"] == reason
    # the cause alone says host memory, whatever the log line kept
    (bare,) = build_breakdown_alert_payload([replace(bd, detail="")])["alerts"]
    assert bare["labels"]["failure_domain"] == "host-memory"


def _docker_entry(name: str, environment_label: str | None = None) -> dict:
    """A Docker Engine /containers/json element for an unhealthy container."""
    labels = {"com.docker.compose.project": "dokploy-app"}
    if environment_label is not None:
        labels["party.zitian.infra.environment"] = environment_label
    return {
        "Id": f"id-{name}",
        "Names": [f"/{name}"],
        "State": "running",
        "Status": "Up 5 minutes (unhealthy)",
        "Labels": labels,
    }


def test_report_only_follows_the_environment_label_then_the_name():
    """#903 review: report-only is the container's ENVIRONMENT — the canonical label
    when present, else what its compose project and name say (-staging, or a preview
    slot's -pr-/-branch-/-commit-/-tag-). Not a second name-only classifier."""
    from libs.observability.watchers.breakdown_watch import is_report_only

    def report_only(entry: dict) -> bool:
        [breakdown] = find_breakdown_containers([entry], lambda _cid: "")
        return is_report_only(breakdown)

    unlabelled_reports = [
        "platform-prefect-worker-staging",
        "finance_report-backend-pr-12",
        "finance_report-preview-db-branch-main",
        "finance_report-frontend-commit-1ab32d5",
        "finance_report-backend-tag-v1-2-3",
    ]
    unlabelled_pages = ["platform-prefect-worker", "vault", "platform-alerting-probes"]
    assert [n for n in unlabelled_reports if not report_only(_docker_entry(n))] == []
    assert [n for n in unlabelled_pages if report_only(_docker_entry(n))] == []

    # the label wins over the name, both ways
    assert not report_only(_docker_entry("x-staging", environment_label="production"))
    assert not report_only(_docker_entry("x-staging", environment_label="prod"))
    assert report_only(_docker_entry("worker", environment_label="staging"))
    assert report_only(_docker_entry("worker", environment_label="pr-7"))


def _sweeping_watch(monkeypatch, entries: dict):
    """run_once over Docker-shaped entries (name -> entry, or absent when healthy)."""
    import time

    import libs.observability.watchers.breakdown_watch as w

    clock = {"now": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(w, "_list_containers", lambda _client: list(entries.values()))
    monkeypatch.setattr(w, "_container_logs", lambda _client, _cid, _tail: "")
    posted: list = []
    monkeypatch.setattr(w, "_post_alert", lambda payload: posted.append(payload))
    kept = {"state": {}, "resolved_at": {}, "chronic": {}, "chronic_context": {}}

    def sweep_n(n: int, *, jump: float = 0.0) -> int:
        clock["now"] += jump
        fired = 0
        for _ in range(n):
            clock["now"] += 60
            fired += w.run_once(
                client=None,
                log_tail=25,
                container_state=kept["state"],
                renotify=0,
                failure_threshold=3,
                recovery_threshold=5,
                resolved_at=kept["resolved_at"],
                chronic=kept["chronic"],
                chronic_context=kept["chronic_context"],
                stay_resolved_seconds=6 * 3600,
                chronic_digest_seconds=3600,
            )
        return fired

    return sweep_n, posted


def _digests(posted: list) -> list:
    return [
        p
        for p in posted
        if p["commonLabels"]["alertname"] == "ContainerBreakdownChronic"
    ]


def _lines(payload: dict) -> dict:
    return {
        a["annotations"]["summary"].split()[0]: a["annotations"]["description"]
        for a in payload["alerts"]
    }


def test_staging_and_preview_breakdowns_go_to_the_digest_never_the_pager(monkeypatch):
    """#903: the production runner sees every container on the shared engine, and
    `platform-prefect-worker(-staging)` alone fired 684 times in 30 days. A staging or
    preview container never pages — firing or resolved — and is reported in the
    daily ContainerBreakdownChronic digest as a REPORT."""
    names = [
        "platform-prefect-worker-staging",
        "finance_report-backend-pr-5",
        "platform-prefect-worker",
    ]
    entries = {name: _docker_entry(name) for name in names}
    sweep_n, posted = _sweeping_watch(monkeypatch, entries)

    assert sweep_n(10) == 1
    entries.clear()
    sweep_n(5)

    pages = [
        p for p in posted if p["commonLabels"]["alertname"] == "ContainerBreakdown"
    ]
    assert [p["status"] for p in pages] == ["firing", "resolved"]
    assert [list(_lines(p)) for p in pages] == [["platform-prefect-worker"]] * 2
    assert all("delivery" not in p["commonLabels"] for p in pages)

    sweep_n(1, jump=3600)
    reports = [p for p in _digests(posted) if p["commonLabels"].get("delivery")]
    assert len(reports) == 1
    assert reports[0]["commonLabels"]["delivery"] == "report"
    assert all(a["labels"]["delivery"] == "report" for a in reports[0]["alerts"])
    lines = _lines(reports[0])
    assert set(lines) == {
        "platform-prefect-worker-staging",
        "finance_report-backend-pr-5",
    }
    assert all(
        reason.startswith("staging/预览容器,从不呼人:") for reason in lines.values()
    )


def test_a_production_re_break_inside_the_floor_reaches_the_pager(monkeypatch):
    """#903 review (HIGH): a production container that resolves and breaks again inside
    the stay-resolved floor is only ever told in the digest. With the whole digest a
    report, that incident reached nobody on call. Its line must go to the pager; the
    staging line of the same digest stays a report."""
    entries = {
        name: _docker_entry(name)
        for name in ("platform-prefect-worker", "platform-prefect-worker-staging")
    }
    sweep_n, posted = _sweeping_watch(monkeypatch, entries)

    assert sweep_n(3) == 1  # production pages; staging does not
    broken = dict(entries)
    entries.clear()
    sweep_n(5)  # both recover; production resolves
    entries.update(broken)
    assert sweep_n(3) == 0  # re-broke inside the 6h floor: no page now
    entries.clear()
    sweep_n(5)  # recovered again: both pruned from the tracked state

    sweep_n(1, jump=3600)
    digests = _digests(posted)
    pager = [p for p in digests if "delivery" not in p["commonLabels"]]
    reports = [p for p in digests if p["commonLabels"].get("delivery") == "report"]
    assert len(pager) == 1 and len(reports) == 1
    assert _lines(pager[0]) == {
        "platform-prefect-worker": "恢复后 6h 保持期内又坏了 1 次"
    }
    assert list(_lines(reports[0])) == ["platform-prefect-worker-staging"]


def test_a_breakdown_pages_when_it_started_and_resolves_how_long(monkeypatch):
    """#905: the page says when the container first broke, the RESOLVED how long it
    was down -- from the first broken sweep, not the page."""
    import time

    import libs.observability.watchers.breakdown_watch as w

    clock = {"now": 1_790_236_800.0}  # 2026-09-24 08:00 UTC
    monkeypatch.setattr(time, "time", lambda: clock["now"])
    breakdown = Breakdown(
        container="vault-agent",
        state="restarting",
        reason="r",
        detail="d",
        service_id="finance_report/app",
    )
    posted: list = []
    monkeypatch.setattr(w, "_post_alert", posted.append)
    state: dict = {}

    def sweeps(found: list, count: int) -> None:
        monkeypatch.setattr(w, "sweep", lambda client, tail: found)
        for _ in range(count):
            w.run_once(None, 25, state, 0, 3, 2)
            clock["now"] += 60

    sweeps([breakdown], 3)  # 08:00, 08:01, 08:02 pages
    sweeps([], 2)  # 08:03, 08:04 resolves

    firing, resolved = posted
    assert firing["alerts"][0]["startsAt"] == "2026-09-24T08:00:00Z"
    assert "endsAt" not in firing["alerts"][0]
    assert (resolved["alerts"][0]["startsAt"], resolved["alerts"][0]["endsAt"]) == (
        "2026-09-24T08:00:00Z",
        "2026-09-24T08:04:00Z",
    )


def test_docker_log_frames_are_removed_from_the_log_tail():
    """#905: without a TTY every Engine log frame starts with an 8-byte header; a
    length of 65 is the byte "A", which would open the line the card shows."""
    from libs.observability.watchers.breakdown_watch import demux_docker_logs

    def frame(stream: int, text: str) -> bytes:
        data = text.encode()
        return bytes([stream, 0, 0, 0]) + len(data).to_bytes(4, "big") + data

    line = "x" * 64 + "\n"
    raw = frame(1, "booting\n") + frame(2, "permission denied\n") + frame(1, line)

    assert demux_docker_logs(raw) == "booting\npermission denied\n" + line
    assert demux_docker_logs(b"tty output, no frames\n") == "tty output, no frames\n"
