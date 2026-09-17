"""The host firewall's public surface is SSH plus Traefik, and nothing else (Infra-022 T1.3, #724)."""

from __future__ import annotations

import re
from pathlib import Path

HOSTFW = Path(__file__).resolve().parents[2] / "bootstrap/01.dokploy_install/hostfw"
RULES = (HOSTFW / "hostfw.nft").read_text()
UNIT = (HOSTFW / "infra2-hostfw.service").read_text()
SCRIPT = (HOSTFW / "hostfw.sh").read_text()


def _set_elements(name: str) -> set[int]:
    match = re.search(r"set %s \{[^}]*elements = \{([^}]*)\}" % name, RULES)
    assert match, f"set {name} not found"
    return {int(item) for item in match.group(1).replace(",", " ").split()}


def _chain(name: str) -> str:
    match = re.search(r"chain %s \{(.*?)\n\t\}" % name, RULES, re.S)
    assert match, f"chain {name} not found"
    return match.group(1)


def test_public_allow_list_is_ssh_and_traefik_only() -> None:
    assert _set_elements("public_tcp") == {22, 80, 443}
    # 443: HTTP/3; 68/546: DHCP client replies.
    assert _set_elements("public_udp") == {68, 443, 546}


def test_admin_and_swarm_ports_are_not_public() -> None:
    public = _set_elements("public_tcp") | _set_elements("public_udp")
    # Dokploy UI, swarm control plane, gossip, VXLAN: all were reachable from the internet on 2026-09-17.
    assert not public & {3000, 2377, 7946, 4789}


def test_input_defaults_to_drop_on_the_public_interface_only() -> None:
    body = _chain("input")
    lines = [line.strip() for line in body.strip().splitlines()]
    assert lines[1] == "iifname != $PUBLIC_IF accept", "non-public interfaces (lo, docker bridges) must pass first"
    assert "ct state established,related accept" in lines
    assert lines[-1].startswith("counter drop"), "anything not allow-listed is dropped last"
    assert 'define PUBLIC_IF = "eth0"' in RULES


def test_dokploy_port_is_dropped_before_dockers_forward_chain() -> None:
    body = _chain("forward")
    assert "priority filter - 1" in body, "must run before Docker's FORWARD (priority filter)"
    assert "iifname $PUBLIC_IF meta l4proto tcp ct original proto-dst 3000" in body
    assert "policy accept" in body, "the forward chain drops only the Dokploy port"


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
    assert apply.index("check") < apply.index("systemd-run") < apply.index('nft -f "$RULES"')
    assert "delete table inet \"$TABLE\"" in apply
    assert 'nft -c -f "$RULES"' in SCRIPT
    # Refuses to proceed when the stock nftables.service would flush every table at boot.
    assert "flush ruleset" in SCRIPT
