"""Tests for SSH hardening, fail2ban nftables jail, and auth.log SigNoz alert (#724-R2)."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HOST_GUARD = ROOT / "bootstrap/01.dokploy_install/host_guard"
SSHD_CONF = (HOST_GUARD / "sshd_hardening.conf").read_text(encoding="utf-8")
F2B_LOCAL = (HOST_GUARD / "fail2ban_sshd.local").read_text(encoding="utf-8")
CONFIGURE_SH = HOST_GUARD / "configure_ssh_fail2ban.sh"
AUTH_ALERT_SH = HOST_GUARD / "auth_log_alert.sh"


def test_sshd_hardening_disables_password_and_kbd_interactive_auth() -> None:
    lines = {
        line.strip()
        for line in SSHD_CONF.splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    assert "PasswordAuthentication no" in lines
    assert "KbdInteractiveAuthentication no" in lines
    assert "ChallengeResponseAuthentication no" in lines
    assert "PermitRootLogin prohibit-password" in lines
    assert "PubkeyAuthentication yes" in lines


def test_fail2ban_uses_nftables_and_bans_after_five_failures() -> None:
    assert "banaction = nftables-multiport" in F2B_LOCAL
    assert "enabled = true" in F2B_LOCAL
    assert "maxretry = 5" in F2B_LOCAL


def test_configure_ssh_fail2ban_check_succeeds_and_apply_requires_root() -> None:
    check_res = subprocess.run(
        ["bash", str(CONFIGURE_SH), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert check_res.returncode == 0, check_res.stderr
    assert "valid" in check_res.stdout

    bad_res = subprocess.run(
        ["bash", str(CONFIGURE_SH), "--invalid"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert bad_res.returncode == 2


def test_auth_log_alert_emits_otlp_payload_only_when_threshold_reached(
    tmp_path: Path,
) -> None:
    auth_log = tmp_path / "auth.log"
    state_dir = tmp_path / "state"
    captured = tmp_path / "captured.json"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_curl = bin_dir / "curl"
    fake_curl.write_text(
        f'#!/usr/bin/env bash\nwhile [[ $# -gt 0 ]]; do\n  if [[ "$1" == "-d" ]]; then\n    printf "%s" "$2" > "{captured}"\n    shift 2\n  else\n    shift\n  fi\ndone\n',
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)

    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "INFRA2_AUTH_LOG_PATH": str(auth_log),
        "INFRA2_AUTH_ALERT_STATE_DIR": str(state_dir),
        "INFRA2_SSH_FAIL_THRESHOLD": "5",
    }

    # Below threshold (3 failures): no alert emitted
    auth_log.write_text(
        "\n".join(
            f"Oct 20 12:00:0{i} vps sshd[10{i}]: Failed password for root from 203.0.113.5 port 22 ssh2"
            for i in range(3)
        )
        + "\n",
        encoding="utf-8",
    )
    res1 = subprocess.run(
        ["bash", str(AUTH_ALERT_SH)], env=env, capture_output=True, text=True
    )
    assert res1.returncode == 0
    assert not captured.exists()

    # Append 5 more failures (>= threshold): emits OTLP log to SigNoz collector
    with auth_log.open("a", encoding="utf-8") as fh:
        for i in range(5):
            fh.write(
                f"Oct 20 12:01:0{i} vps sshd[20{i}]: Failed password for invalid user admin from 203.0.113.9 port 22 ssh2\n"
            )
    res2 = subprocess.run(
        ["bash", str(AUTH_ALERT_SH)], env=env, capture_output=True, text=True
    )
    assert res2.returncode == 0
    assert captured.exists()
    body = captured.read_text(encoding="utf-8")
    assert "infra-host-ssh" in body
    assert "HostSshAuthFailureSpike: 5 SSH authentication failures" in body


def test_ensure_ssh_auth_alert_rule_builds_signoz_bridge_payload(monkeypatch) -> None:
    spec = importlib.util.spec_from_file_location(
        "alerting_shared_tasks",
        ROOT / "platform/12.alerting/shared_tasks.py",
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    monkeypatch.setattr(mod, "_ensure_signoz_channel", lambda _c: "feishu-production")
    payload = mod.ensure_ssh_auth_alert_rule.body(object(), dry_run=True)
    assert payload["alert"] == "HostSshAuthFailureSpike"
    assert payload["labels"]["severity"] == "error"
    assert payload["condition"]["thresholds"]["spec"][0]["channels"] == [
        "feishu-production"
    ]
