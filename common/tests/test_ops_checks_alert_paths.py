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


def test_the_daily_report_runs_after_a_failed_watchdog_with_its_outcome() -> None:
    """#1433: a paged day still gets its report, marked red from the watchdog outcome."""
    steps = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["watchdog"][
        "steps"
    ]
    names = [step.get("name") for step in steps]
    report_name = "Build and send daily positive stability report"
    assert names.index("Run watchdog") < names.index(report_name)
    watchdog = steps[names.index("Run watchdog")]
    report = steps[names.index(report_name)]
    assert watchdog["id"] == "watchdog"
    assert report["if"] == "${{ !cancelled() }}"
    assert (
        report["env"]["INFRA2_WATCHDOG_OUTCOME"]
        == "${{ steps." + watchdog["id"] + ".outcome }}"
    )


def test_ops_checks_audit_job_runs_on_ubuntu_24_04_for_playwright() -> None:
    """#1543: the audit job runs the scheduled_audit hook slot, whose bootstrap installs
    Playwright chromium. ubuntu-latest is 26.04, where Playwright 1.57.0 has no build."""
    jobs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    runs_on = jobs["audit"]["runs-on"]
    assert runs_on == "ubuntu-24.04"


def test_ops_checks_audit_job_installs_uv_before_the_hook_audit() -> None:
    """#1561: the scheduled_audit bootstrap of finance_report runs `uv pip install`.
    The audit job must install uv, with the reconcile job's version, after Python
    and before that step. The cache is off: the hook step is skipped in a dry run,
    and setup-uv then fails its post step with "Cache path does not exist"."""
    reconcile_workflow = ROOT / ".github" / "workflows" / "reconcile-iac-inputs.yml"
    reconcile_steps = yaml.safe_load(reconcile_workflow.read_text(encoding="utf-8"))[
        "jobs"
    ]["reconcile"]["steps"]
    reconcile_uv_uses = [
        step["uses"]
        for step in reconcile_steps
        if str(step.get("uses", "")).startswith("astral-sh/setup-uv@")
    ]
    assert reconcile_uv_uses, (
        "job reconcile of reconcile-iac-inputs.yml has no astral-sh/setup-uv step"
    )
    expected_uses = reconcile_uv_uses[0]

    audit_steps = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["audit"][
        "steps"
    ]
    audit_names = [step.get("name") for step in audit_steps]
    hook_name = "Run scheduled App runtime audits (#1493)"
    assert hook_name in audit_names, (
        f"job audit of ops-checks.yml has no step named {hook_name!r}"
    )
    python_indexes = [
        index
        for index, step in enumerate(audit_steps)
        if str(step.get("uses", "")).startswith("actions/setup-python@")
    ]
    assert python_indexes, (
        "job audit of ops-checks.yml has no actions/setup-python step"
    )
    uv_indexes = [
        index
        for index, step in enumerate(audit_steps)
        if step.get("uses") == expected_uses
    ]
    assert len(uv_indexes) == 1, (
        f"job audit of ops-checks.yml must hold exactly one step that uses "
        f"{expected_uses}; found {len(uv_indexes)}"
    )
    uv_index = uv_indexes[0]
    uv_step = audit_steps[uv_index]
    for key in ("if", "continue-on-error", "env"):
        assert key not in uv_step, (
            f"the uv step of job audit in ops-checks.yml must not set `{key}`"
        )
    assert uv_step.get("with") == {"enable-cache": False}, (
        "the uv step of job audit in ops-checks.yml must set `with` to "
        f"{{enable-cache: false}} only; found {uv_step.get('with')!r}"
    )
    assert max(python_indexes) < uv_index, (
        "the uv step of job audit in ops-checks.yml must come after the "
        "actions/setup-python step"
    )
    assert uv_index < audit_names.index(hook_name), (
        f"the uv step of job audit in ops-checks.yml must come before {hook_name!r}"
    )


def test_ops_checks_audit_job_runs_all_deploy_guards_before_scheduled_app_hooks() -> (
    None
):
    """#1583/#1590: all deploy-guard steps run unconditionally before scheduled App hooks,
    scheduled App hooks emit `scheduled-audit-result.json`, and `Alert on confirmed failure`
    routes paging through `common.observability.page_dedup.dedup_page`."""
    audit_steps = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["audit"][
        "steps"
    ]
    names = [step.get("name") for step in audit_steps]
    hook_name = "Run scheduled App runtime audits (#1493)"
    hook_idx = names.index(hook_name)
    for guard_name in (
        "Audit deploy fan-out coverage (sufficiency)",
        "Audit App runtime hooks manifests (#1416)",
        "Audit single deploy trigger (necessity)",
    ):
        assert names.index(guard_name) < hook_idx, (
            f"{guard_name!r} must run before {hook_name!r} in ops-checks.yml jobs.audit"
        )
    hook_step = audit_steps[hook_idx]
    assert "--run-scheduled-audit" in hook_step["run"]
    assert "--output-json scheduled-audit-result.json" in hook_step["run"]

    alert_idx = names.index("Alert on confirmed failure")
    resolve_idx = names.index("Resolve the paged failures")
    assert hook_idx < alert_idx < resolve_idx
    alert_step = audit_steps[alert_idx]
    assert "dedup_page(" in alert_step["run"]
    assert "confirmed_finding_pairs(" in alert_step["run"]


def test_ops_checks_watchdog_emits_scheduler_heartbeat_and_worker_alerts_when_missed(
    tmp_path: Path,
) -> None:
    """#1448 (Mutation evidence): ops-checks.yml watchdog emits the scheduler heartbeat
    POST /heartbeat, and cloudflare/infra-watchdog/worker.js accepts it, stays quiet
    while fresh, pages P0 ops-scheduler when missed past 93600s, and resolves on recovery.
    Removing the ping step or breaking its payload/token fails this test."""
    import json
    import os
    import shutil
    import subprocess
    import sys
    import textwrap
    import threading
    import tomllib
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    import pytest

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    watchdog_job = workflow["jobs"]["watchdog"]
    steps = watchdog_job["steps"]
    names = [step.get("name") for step in steps]
    step_name = "Ping Cloudflare watchdog scheduler heartbeat (#1448)"
    assert step_name in names, f"{step_name!r} missing from ops-checks.yml watchdog"
    assert names.index(step_name) < names.index("Record the verdicts as GitHub issues")
    ping_step = steps[names.index(step_name)]
    assert "dry_run" in ping_step["if"] and "!cancelled()" in ping_step["if"]
    assert (
        ping_step["env"]["INFRA2_WATCHDOG_HEARTBEAT_TOKEN"]
        == "${{ secrets.INFRA2_WATCHDOG_HEARTBEAT_TOKEN }}"
    )

    run_script = ping_step["run"]
    marker = "python - <<'PY'\n"
    assert marker in run_script and "\nPY" in run_script
    python_src = textwrap.dedent(run_script.split(marker, 1)[1].rsplit("\nPY", 1)[0])

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
            [sys.executable, "-c", python_src],
            env={
                **os.environ,
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

    assert len(captured) == 1
    req = captured[0]
    assert req["path"] == "/heartbeat"
    assert req["auth"] == "Bearer test-hb-token"
    assert req["body"]["env"] == "production"
    assert req["body"]["name"] == "ops-checks-scheduler"
    assert req["body"]["ok"] is True

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

    const firstPost = await postBeat();
    clock += 3600 * 1000;
    seedRunners(clock);
    const freshMsgs = await runCron();

    clock += 91000 * 1000;
    seedRunners(clock);
    const missedMsgs = await runCron();

    clock += 60 * 1000;
    const recoverPost = await postBeat();
    clock += 1800 * 1000;
    seedRunners(clock);
    const recoveredMsgs = await runCron();

    console.log = process.stdout.write.bind(process.stdout);
    process.stdout.write(JSON.stringify({{ firstPost, freshMsgs, missedMsgs, recoverPost, recoveredMsgs }}));
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
    assert out["recoverPost"]["persisted"] is True
    assert len(out["recoveredMsgs"]) == 1
    assert "✅ [已恢复]" in out["recoveredMsgs"][0]
    assert "ops-checks-scheduler" in out["recoveredMsgs"][0]
