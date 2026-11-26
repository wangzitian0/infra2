"""#658: the two scheduled jobs that stayed silent this week now page.

The weekly digest died on the watchdog ledger two Mondays running with no failure path;
the runner's /health carried a dead 1Password service account for three days with no
scheduled reader. Both alert paths deliver through deliver_out_of_band_alert.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ops-checks.yml"


def _steps(job: dict) -> dict[str, dict]:
    return {step.get("name"): step for step in job["steps"] if step.get("name")}


def test_the_weekly_digest_pages_when_it_fails() -> None:
    jobs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    digest = jobs["digest"]
    steps = _steps(digest)
    alert = steps["Alert on failure"]
    names = [step.get("name") for step in digest["steps"]]
    assert names.index("Build and send digest") < names.index("Alert on failure")
    assert (
        alert["if"].startswith("failure()")
        and "INFRA2_STABILITY_REPORT_DRY_RUN" in alert["if"]
    )
    assert "promotion-reminder" not in alert["if"]
    assert "0 13 * * *" not in alert["if"]
    assert "deliver_out_of_band_alert" in alert["run"]
    assert (
        digest["env"]["INFRA2_OUT_OF_BAND_FEISHU_CHAT_ID"]
        == "${{ secrets.INFRA2_OUT_OF_BAND_FEISHU_CHAT_ID }}"
    )


def test_the_nightly_watchdog_reads_the_runner_health_and_fails_on_a_dead_prerequisite() -> (
    None
):
    jobs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    watchdog = jobs["watchdog"]
    steps = _steps(watchdog)
    health = steps["Runner health — 1Password service account and deploy prerequisites"]
    assert health["if"] == "always()"
    assert "/health" in health["run"] and "op_service_account_token" in health["run"]
    # #1648: the 60 s probe iac-runner-http pages this condition; the daily step goes red
    assert "deliver_out_of_band_alert" not in health["run"]
    assert "sys.exit(1)" in health["run"]
    assert health["env"]["IAC_RUNNER_URL"].startswith("${{ vars.IAC_RUNNER_URL")


def test_watchdog_daily_positive_stability_report_has_no_ssh_key_if_guard() -> None:
    """Verify daily stability report step has no if condition gating on SSH key."""
    jobs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    watchdog = jobs["watchdog"]
    steps = _steps(watchdog)
    report_step = steps["Build and send daily positive stability report"]
    assert "if" not in report_step or "INFRA2_WATCHDOG_SSH_PRIVATE_KEY" not in str(
        report_step.get("if", "")
    )


def test_the_daily_report_runs_only_after_a_successful_watchdog() -> None:
    """Daily positive stability report runs only when watchdog and prior steps succeed."""
    steps = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["watchdog"][
        "steps"
    ]
    names = [step.get("name") for step in steps]
    report_name = "Build and send daily positive stability report"
    assert names.index("Run watchdog") < names.index(report_name)
    watchdog = steps[names.index("Run watchdog")]
    report = steps[names.index(report_name)]
    assert watchdog["id"] == "watchdog"
    assert report["if"] == "${{ success() }}"
    assert (
        report["env"]["INFRA2_WATCHDOG_OUTCOME"]
        == "${{ steps." + watchdog["id"] + ".outcome }}"
    )


def test_ops_checks_hook_duration_report_wires_page_dedup_alert_and_resolve() -> None:
    """hook-duration-report opens/closes a dedup GitHub Issue on failure/recovery without Feishu secrets."""
    jobs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    job = jobs["hook-duration-report"]
    assert job["permissions"] == {
        "contents": "read",
        "actions": "read",
        "issues": "write",
    }
    assert "secrets." not in yaml.safe_dump(job)
    steps = _steps(job)
    names = [step.get("name") for step in job["steps"]]
    alert = steps["Alert on failure"]
    resolve = steps["Resolve alert on recovery"]
    assert (
        names.index("Report hook durations")
        < names.index("Alert on failure")
        < names.index("Resolve alert on recovery")
    )
    assert (
        alert["if"]
        == "failure() && !(github.event_name == 'workflow_dispatch' && inputs.dry_run)"
    )
    assert (
        resolve["if"]
        == "success() && !(github.event_name == 'workflow_dispatch' && inputs.dry_run)"
    )
    assert 'job="ops-checks.hook-duration-report"' in alert["run"]
    assert "dedup_page(" in alert["run"]
    assert 'job="ops-checks.hook-duration-report"' in resolve["run"]
    assert "resolve_page_state(" in resolve["run"]


def test_ops_checks_scheduled_app_audit_runs_on_ubuntu_24_04_for_playwright() -> None:
    """#1543: scheduled-app-audit runs the scheduled_audit hook slot, whose bootstrap installs
    Playwright chromium. ubuntu-latest is 26.04, where Playwright 1.57.0 has no build."""
    jobs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    assert jobs["audit"]["runs-on"] == "ubuntu-latest"
    assert jobs["scheduled-app-audit"]["runs-on"] == "ubuntu-24.04"


def test_ops_checks_scheduled_app_audit_installs_uv_before_the_hook_audit() -> None:
    """#1561: the scheduled_audit bootstrap of finance_report runs `uv pip install`.
    The scheduled-app-audit job must install uv, with the reconcile job's version, after Python
    and before that step."""
    # the release train installs uv in its composite action (#1646)
    train_setup = ROOT / ".github" / "actions" / "train-setup" / "action.yml"
    reconcile_steps = yaml.safe_load(train_setup.read_text(encoding="utf-8"))["runs"][
        "steps"
    ]
    reconcile_uv_uses = [
        step["uses"]
        for step in reconcile_steps
        if str(step.get("uses", "")).startswith("astral-sh/setup-uv@")
    ]
    assert reconcile_uv_uses, (
        "the train-setup action of reconcile-iac-inputs.yml has no astral-sh/setup-uv step"
    )
    expected_uses = reconcile_uv_uses[0]

    audit_steps = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"][
        "scheduled-app-audit"
    ]["steps"]
    audit_names = [step.get("name") for step in audit_steps]
    hook_name = "Run scheduled App runtime audits (#1493)"
    assert hook_name in audit_names, (
        f"job scheduled-app-audit of ops-checks.yml has no step named {hook_name!r}"
    )
    python_indexes = [
        index
        for index, step in enumerate(audit_steps)
        if str(step.get("uses", "")).startswith("actions/setup-python@")
    ]
    assert python_indexes, (
        "job scheduled-app-audit of ops-checks.yml has no actions/setup-python step"
    )
    uv_indexes = [
        index
        for index, step in enumerate(audit_steps)
        if step.get("uses") == expected_uses
    ]
    assert len(uv_indexes) == 1, (
        f"job scheduled-app-audit of ops-checks.yml must hold exactly one step that uses "
        f"{expected_uses}; found {len(uv_indexes)}"
    )
    uv_index = uv_indexes[0]
    uv_step = audit_steps[uv_index]
    for key in ("if", "continue-on-error", "env"):
        assert key not in uv_step, (
            f"the uv step of job scheduled-app-audit in ops-checks.yml must not set `{key}`"
        )
    assert uv_step.get("with") == {
        "enable-cache": True,
        "cache-dependency-glob": "uv.lock",
    }, (
        "the uv step of job scheduled-app-audit in ops-checks.yml must enable uv.lock caching; "
        f"found {uv_step.get('with')!r}"
    )
    assert max(python_indexes) < uv_index, (
        "the uv step of job scheduled-app-audit in ops-checks.yml must come after the "
        "actions/setup-python step"
    )
    assert uv_index < audit_names.index(hook_name), (
        f"the uv step of job scheduled-app-audit in ops-checks.yml must come before {hook_name!r}"
    )


def test_ops_checks_audit_and_scheduled_app_audit_route_findings_through_page_dedup() -> (
    None
):
    """#1583/#1590: static deploy-guard steps run in `audit`, scheduled App hooks and
    canary verify run in `scheduled-app-audit`, and each routes paging through `dedup_page`."""
    jobs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]

    audit_steps = jobs["audit"]["steps"]
    audit_names = [step.get("name") for step in audit_steps]
    for guard_name in (
        "Audit deploy fan-out coverage (sufficiency)",
        "Audit App runtime hooks manifests (#1416)",
        "Audit single deploy trigger (necessity)",
    ):
        assert guard_name in audit_names
    assert "Run scheduled App runtime audits (#1493)" not in audit_names
    audit_alert = audit_steps[audit_names.index("Alert on confirmed failure")]
    assert "dedup_page(" in audit_alert["run"]
    assert "STATIC_GUARD_KEYS" in audit_alert["run"]
    assert audit_names.index("Alert on confirmed failure") < audit_names.index(
        "Resolve the paged failures"
    )

    app_steps = jobs["scheduled-app-audit"]["steps"]
    names = [step.get("name") for step in app_steps]
    hook_name = "Run scheduled App runtime audits (#1493)"
    hook_idx = names.index(hook_name)
    hook_step = app_steps[hook_idx]
    assert "--run-scheduled-audit" in hook_step["run"]
    assert "--output-json scheduled-audit-result.json" in hook_step["run"]

    alert_idx = names.index("Alert on confirmed failure")
    resolve_idx = names.index("Resolve the paged failures")
    assert hook_idx < alert_idx < resolve_idx
    alert_step = app_steps[alert_idx]
    assert "dedup_page(" in alert_step["run"]
    assert "RUNTIME_GUARD_KEYS" in alert_step["run"]


def test_ops_checks_audit_job_timeouts_and_artifact_uploads() -> None:
    """audit runs in 10m; scheduled-app-audit has 90m timeout, artifacts, and stage record."""
    jobs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    assert jobs["audit"]["timeout-minutes"] == 10
    app_audit_job = jobs["scheduled-app-audit"]
    assert app_audit_job["timeout-minutes"] == 90
    steps = _steps(app_audit_job)
    hook_step = steps["Run scheduled App runtime audits (#1493)"]
    assert "--artifacts-dir scheduled-audit-artifacts" in hook_step["run"]

    results_step = steps["Upload scheduled audit artifacts"]
    assert (
        results_step["if"]
        == "${{ !cancelled() && github.event.inputs.dry_run != 'true' }}"
    )
    assert results_step["with"]["name"] == "scheduled-audit-results"
    assert results_step["with"]["retention-days"] == 30

    record_step = steps["Upload scheduled audit stage record"]
    assert (
        record_step["if"]
        == "${{ !cancelled() && github.event.inputs.dry_run != 'true' && env.STAGE_RECORD_POST != '' }}"
    )
    assert record_step["continue-on-error"] is True
    assert record_step["with"]["name"] == "${{ env.STAGE_RECORD_POST }}"


def _ops_checks_scheduler_ping_step() -> tuple[dict, str]:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["watchdog"]["steps"]
    names = [step.get("name") for step in steps]
    step_name = "Ping Cloudflare watchdog scheduler heartbeat (#1448)"
    assert step_name in names, f"{step_name!r} missing from ops-checks.yml watchdog"
    assert names.index(step_name) < names.index("Record the verdicts as GitHub issues")
    ping_step = steps[names.index(step_name)]
    # #1877: the step runs the module, which records a verdict on each run.
    assert ping_step["run"].strip() == "python tools/scheduler_heartbeat.py"
    python_src = "tools/scheduler_heartbeat.py"
    return ping_step, python_src


def _eval_ping_step_if(expr: str, *, event_name: str, cancelled: bool = False) -> bool:
    assert expr.startswith("${{") and expr.endswith("}}")
    inner = expr[3:-2].strip()
    # #1877: the step runs on each event; the module sends on a scheduled run only.
    assert inner == "!cancelled()", inner
    return not cancelled


def _emit_ops_checks_ping(python_src: str, *, event_name: str = "schedule") -> dict:
    import json
    import os
    import subprocess
    import sys
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    captured: list[dict] = []

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8")
            captured.append(
                {
                    "path": self.path,
                    "auth": self.headers.get("Authorization"),
                    "body": json.loads(raw),
                }
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true,"persisted":true}')

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        proc = subprocess.run(
            [sys.executable, python_src],
            cwd=ROOT,
            env={
                **{
                    k: v
                    for k, v in os.environ.items()
                    if k != "INFRA2_WATCHDOG_VERDICTS_PATH"
                },
                "GITHUB_EVENT_NAME": event_name,
                "INFRA2_WATCHDOG_HEARTBEAT_URL": f"http://127.0.0.1:{port}/heartbeat",
                "INFRA2_WATCHDOG_HEARTBEAT_TOKEN": "test-hb-token",
            },
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr + proc.stdout
    finally:
        server.shutdown()
        server.server_close()

    if event_name != "schedule":
        assert captured == []
        return {}
    assert len(captured) == 1
    return captured[0]


def test_ops_checks_watchdog_step_emits_scheduler_heartbeat_only_on_schedule() -> None:
    """#1448 (Mutation evidence, no-node): ops-checks.yml watchdog pings POST /heartbeat
    only on scheduled runs (so manual dispatches never throttle or mask 02:17 UTC)."""
    ping_step, python_src = _ops_checks_scheduler_ping_step()
    assert (
        ping_step["env"]["INFRA2_WATCHDOG_HEARTBEAT_TOKEN"]
        == "${{ secrets.INFRA2_WATCHDOG_HEARTBEAT_TOKEN }}"
    )
    if_expr = ping_step["if"]
    assert _eval_ping_step_if(if_expr, event_name="schedule") is True
    assert _eval_ping_step_if(if_expr, event_name="schedule", cancelled=True) is False
    # A manual run starts the module and the module sends nothing.
    assert _emit_ops_checks_ping(python_src, event_name="workflow_dispatch") == {}

    req = _emit_ops_checks_ping(python_src)
    assert req["path"] == "/heartbeat"
    assert req["auth"] == "Bearer test-hb-token"
    assert req["body"]["env"] == "production"
    assert req["body"]["name"] == "ops-checks-scheduler"
    assert req["body"]["ok"] is True


def test_ops_checks_watchdog_emits_scheduler_heartbeat_and_worker_alerts_when_missed(
    tmp_path: Path,
) -> None:
    """#1448 (Mutation evidence): cloudflare/infra-watchdog/worker.js accepts the
    ops-checks.yml heartbeat, stays quiet while fresh, pages P0 ops-scheduler without
    polluting /outages when missed past 93600s, and resolves on recovery."""
    import json
    import shutil
    import subprocess
    import tomllib

    import pytest

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    _ping_step, python_src = _ops_checks_scheduler_ping_step()
    req = _emit_ops_checks_ping(python_src)

    worker_mjs = tmp_path / "worker.mjs"
    worker_mjs.write_text(
        (ROOT / "cloudflare/infra-watchdog/worker.js").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    wrangler_vars = tomllib.loads(
        (ROOT / "cloudflare/infra-watchdog/wrangler.toml").read_text(encoding="utf-8")
    )["vars"]
    card_text_js = (ROOT / "common/tests/fixtures/watchdog_card_text.mjs").as_uri()
    driver = f"""
    import worker from {json.dumps(worker_mjs.as_uri())};
    import {{ cardText }} from {json.dumps(card_text_js)};
    const vars = {json.dumps(wrangler_vars)};
    const payload = {json.dumps(req["body"])};
    const kv = new Map();
    const messages = [];
    let clock = 1790208000000;
    Date.now = () => clock;
    console.log = () => {{}};
    globalThis.fetch = async (input, init = {{}}) => {{
      const u = String(input);
      if (u.startsWith("https://feishu.invalid/")) {{
        messages.push(cardText(JSON.parse(init.body).card));
        return new Response("ok", {{ status: 200 }});
      }}
      return new Response("ok", {{ status: 200 }});
    }};
    const env = {{
      ...vars,
      ALERT_DELIVERY_MODE: "feishu_webhook",
      FEISHU_WEBHOOK_URL: "https://feishu.invalid/hook",
      HEARTBEAT_TOKEN: "test-hb-token",
      WATCHDOG_STATUS_TOKEN: "test-status-token",
      WATCHDOG_DEADMAN_PING_URL: "https://hc-ping.com/test",
      WATCHDOG_STATE: {{
        async get(k) {{ return kv.has(k) ? kv.get(k) : null; }},
        async put(k, v) {{ kv.set(k, v); }},
        async delete(k) {{ kv.delete(k); }},
      }},
    }};
    const seedRunners = (at) => {{
      for (const [k, e] of [
        ["heartbeat:production:platform-alerting-probes", "production"],
        ["heartbeat:staging:platform-alerting-probes-staging", "staging"],
      ]) {{
        kv.set(k, JSON.stringify({{ environment: e, ok: true, schema: 2, receivedAt: at - 60000, failingPublicRoutes: [], lastDeliveryOkAt: Math.floor(at / 1000) }}));
      }}
    }};
    const postBeat = async () => {{
      const r = await worker.fetch(new Request("https://w.invalid/heartbeat", {{
        method: "POST",
        headers: {{ Authorization: "Bearer test-hb-token", "Content-Type": "application/json" }},
        body: JSON.stringify({{ ...payload, timestamp: Math.floor(clock / 1000) }}),
      }}), env);
      return r.json();
    }};
    const runCron = async () => {{
      const before = messages.length;
      const p = [];
      await worker.scheduled({{ scheduledTime: clock }}, env, {{ waitUntil: (x) => p.push(x) }});
      await Promise.all(p);
      return messages.slice(before);
    }};
    const getOutages = async () => {{
      const r = await worker.fetch(new Request("https://w.invalid/outages", {{
        headers: {{ Authorization: "Bearer test-status-token" }},
      }}), env);
      return (await r.json()).outages;
    }};

    const firstPost = await postBeat();
    clock += 3600 * 1000;
    seedRunners(clock);
    const freshMsgs = await runCron();

    clock += 91000 * 1000;
    seedRunners(clock);
    const missedMsgs = await runCron();
    const outagesWhileMissed = await getOutages();

    clock += 60 * 1000;
    const recoverPost = await postBeat();
    clock += 1800 * 1000;
    seedRunners(clock);
    const recoveredMsgs = await runCron();

    console.log = process.stdout.write.bind(process.stdout);
    process.stdout.write(JSON.stringify({{ firstPost, freshMsgs, missedMsgs, outagesWhileMissed, recoverPost, recoveredMsgs }}));
    """
    res = subprocess.run(
        [node, "--input-type=module", "-e", driver],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    assert out["firstPost"]["persisted"] is True
    assert out["freshMsgs"] == []
    assert len(out["missedMsgs"]) == 1
    assert "infra/github · ops-checks-scheduler" in out["missedMsgs"][0]
    assert "[ops-scheduler]" in out["missedMsgs"][0]
    assert out["outagesWhileMissed"] == []
    assert out["recoverPost"]["persisted"] is True
    assert len(out["recoveredMsgs"]) == 1
    assert "✅ [已恢复]" in out["recoveredMsgs"][0]
    assert "ops-checks-scheduler" in out["recoveredMsgs"][0]
