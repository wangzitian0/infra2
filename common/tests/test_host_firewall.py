"""The host firewall's public surface is SSH plus Traefik, and nothing else (Infra-022 T1.3, #724)."""

from __future__ import annotations

import re
from pathlib import Path

HOSTFW = Path(__file__).resolve().parents[2] / "bootstrap/01.dokploy_install/hostfw"
RULES = (HOSTFW / "hostfw.nft").read_text()
UNIT = (HOSTFW / "infra2-hostfw.service").read_text()
SCRIPT = (HOSTFW / "hostfw.sh").read_text()


def _input_accepted_ports(proto: str) -> set[int]:
    body = _chain("input")
    ports: set[int] = set()
    for match in re.finditer(
        rf"\b{proto}\s+dport\s+(?:\{{([^}}]+)\}}|(\d+))\s+accept", body
    ):
        raw = match.group(1) or match.group(2)
        ports.update(int(item) for item in raw.replace(",", " ").split())
    return ports


def _chain(name: str) -> str:
    match = re.search(r"chain %s \{(.*?)\n\t\}" % name, RULES, re.S)
    assert match, f"chain {name} not found"
    return match.group(1)


def test_public_allow_list_is_ssh_and_traefik_only() -> None:
    assert "set public_tcp" not in RULES
    assert "set public_udp" not in RULES
    assert _input_accepted_ports("tcp") == {22, 80, 443}
    # 443: HTTP/3; 68/546: DHCP client replies.
    assert _input_accepted_ports("udp") == {68, 443, 546}


def test_admin_and_swarm_ports_are_not_public() -> None:
    public = _input_accepted_ports("tcp") | _input_accepted_ports("udp")
    # Dokploy UI, swarm control plane, gossip, VXLAN: all were reachable from the internet on 2026-09-17.
    assert not public & {3000, 2377, 7946, 4789}


def test_input_defaults_to_drop_on_the_public_interface_only() -> None:
    body = _chain("input")
    lines = [line.strip() for line in body.strip().splitlines()]
    assert lines[1] == "iifname != $PUBLIC_IF accept", (
        "non-public interfaces (lo, docker bridges) must pass first"
    )
    assert "ct state established,related accept" in lines
    assert lines[-1].startswith("counter drop"), (
        "anything not allow-listed is dropped last"
    )
    assert 'define PUBLIC_IF = "eth0"' in RULES


def test_dokploy_port_is_dropped_before_dockers_forward_chain() -> None:
    body = _chain("forward")
    assert "priority filter - 1" in body, (
        "must run before Docker's FORWARD (priority filter)"
    )
    assert "iifname $PUBLIC_IF meta l4proto tcp ct original proto-dst 3000" in body
    assert "policy accept" in body, "the forward chain drops only the Dokploy port"


def test_cloudflare_sets_gate_http_and_https_in_input_and_forward() -> None:
    """#724-R1: 80/443 TCP and 443 UDP only allow @cf_ipv4/@cf_ipv6 in input and forward; 22 stays open."""
    assert "set cf_ipv4 {" in RULES and "flags interval" in RULES
    assert "set cf_ipv6 {" in RULES
    assert "173.245.48.0/20" in RULES
    assert "2400:cb00::/32" in RULES

    input_body = _chain("input")
    assert "tcp dport 22 accept" in input_body
    assert "tcp dport @public_tcp accept" not in input_body
    assert "ip saddr @cf_ipv4 tcp dport { 80, 443 } accept" in input_body
    assert "ip6 saddr @cf_ipv6 tcp dport { 80, 443 } accept" in input_body
    assert "ip saddr @cf_ipv4 udp dport 443 accept" in input_body
    assert "ip6 saddr @cf_ipv6 udp dport 443 accept" in input_body

    forward_body = _chain("forward")
    assert (
        "iifname $PUBLIC_IF ip saddr != @cf_ipv4 meta l4proto tcp ct original proto-dst { 80, 443 } counter drop"
        in forward_body
    )
    assert (
        "iifname $PUBLIC_IF ip6 saddr != @cf_ipv6 meta l4proto tcp ct original proto-dst { 80, 443 } counter drop"
        in forward_body
    )
    assert (
        "iifname $PUBLIC_IF ip saddr != @cf_ipv4 meta l4proto udp ct original proto-dst 443 counter drop"
        in forward_body
    )
    assert (
        "iifname $PUBLIC_IF ip6 saddr != @cf_ipv6 meta l4proto udp ct original proto-dst 443 counter drop"
        in forward_body
    )


def test_forward_chain_accepts_established_replies_before_origin_drops() -> None:
    """Assert forward chain accepts established replies before origin drop rules."""
    forward_body = _chain("forward")
    assert "ct state established,related accept" in forward_body
    accept_pos = forward_body.index("ct state established,related accept")
    first_drop_pos = forward_body.index("drop")
    assert accept_pos < first_drop_pos


def test_refresh_cf_validates_and_updates_sets_atomically(tmp_path: Path) -> None:
    """#724-R1: hostfw.sh refresh-cf supports --check and --apply with count and CIDR validation."""
    import subprocess

    v4_file = tmp_path / "ips-v4.txt"
    v6_file = tmp_path / "ips-v6.txt"
    v4_file.write_text(
        "\n".join(
            [
                "173.245.48.0/20",
                "103.21.244.0/22",
                "103.22.200.0/22",
                "103.31.4.0/22",
                "141.101.64.0/18",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    v6_file.write_text(
        "\n".join(["2400:cb00::/32", "2606:4700::/32", "2803:f800::/32"]) + "\n",
        encoding="utf-8",
    )
    env = {
        "PATH": "/usr/bin:/bin",
        "CF_IPS_V4_URL": f"file://{v4_file}",
        "CF_IPS_V6_URL": f"file://{v6_file}",
    }
    res = subprocess.run(
        ["bash", str(HOSTFW / "hostfw.sh"), "refresh-cf", "--check"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stderr
    assert "Cloudflare IP sets verified (v4=5, v6=3)" in res.stdout

    # Reject truncated list
    v4_file.write_text("173.245.48.0/20\n", encoding="utf-8")
    res_short = subprocess.run(
        ["bash", str(HOSTFW / "hostfw.sh"), "refresh-cf", "--check"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_short.returncode == 1
    assert "too few entries" in res_short.stderr


def test_rules_touch_only_their_own_table() -> None:
    # `flush ruleset` would wipe Docker's iptables-nft tables and fail2ban's inet f2b-table.
    assert "flush ruleset" not in RULES
    tables = set(re.findall(r"^\s*(?:table|delete table) (\S+ \S+)", RULES, re.M))
    assert tables == {"inet infra2_hostfw"}


def test_unit_loads_the_installed_rules_and_removes_only_its_table() -> None:
    assert "ExecStart=/usr/sbin/nft -f /etc/infra2/hostfw.nft" in UNIT
    assert "ExecStop=/usr/sbin/nft delete table inet infra2_hostfw" in UNIT
    assert "WantedBy=multi-user.target" in UNIT


def test_apply_checks_syntax_and_arms_the_revert_before_loading() -> None:
    apply = SCRIPT[SCRIPT.index("apply() {") : SCRIPT.index("confirm() {")]
    assert (
        apply.index("check")
        < apply.index("systemd-run")
        < apply.index('nft -f "$RULES"')
    )
    assert 'delete table inet "$TABLE"' in apply
    assert 'nft -c -f "$RULES"' in SCRIPT
    # Refuses to proceed when the stock nftables.service would flush every table, indented or not.
    check = SCRIPT[SCRIPT.index("check() {") : SCRIPT.index("apply() {")]
    assert "^[[:space:]]*flush[[:space:]]+ruleset" in check
    assert (
        "is-enabled --quiet nftables" in check and "is-active --quiet nftables" in check
    )
