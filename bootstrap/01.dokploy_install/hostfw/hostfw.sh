#!/usr/bin/env bash
# infra2 host firewall: lockout-safe apply, persist and drift check.
# SSOT: docs/ssot/bootstrap.nodep.md §4 "Host firewall" (Infra-022 T1.3, #724).
#
# Run on the VPS as root, from a copy of this directory:
#   hostfw.sh check              syntax-check hostfw.nft and confirm the public interface
#   hostfw.sh apply              arm a 5-minute automatic revert, then load the table
#   hostfw.sh confirm            cancel the revert (only after a NEW ssh login worked)
#   hostfw.sh install            persist: /etc/infra2/hostfw.nft + infra2-hostfw.service (enabled)
#   hostfw.sh status             table, drop counters, revert timer, installed-vs-repo sha256
#
# `apply` never flushes other tables. If the revert fires, the host is back to "no infra2 table",
# which is exactly the state before the first apply.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RULES="$HERE/hostfw.nft"
UNIT="$HERE/infra2-hostfw.service"
TABLE="infra2_hostfw"
INSTALLED_RULES="/etc/infra2/hostfw.nft"
INSTALLED_UNIT="/etc/systemd/system/infra2-hostfw.service"
REVERT_UNIT="infra2-hostfw-revert"
REVERT_SECONDS="${HOSTFW_REVERT_SECONDS:-300}"

die() { echo "hostfw: $*" >&2; exit 1; }

declared_interface() {
  sed -n 's/^define PUBLIC_IF = "\(.*\)"$/\1/p' "$RULES"
}

check() {
  [ "$(id -u)" = "0" ] || die "run as root"
  command -v nft >/dev/null || die "nft not installed"
  local declared routed
  declared="$(declared_interface)"
  routed="$(ip -o route get 1.1.1.1 | sed -n 's/.* dev \([^ ]*\).*/\1/p')"
  [ -n "$declared" ] || die "hostfw.nft declares no PUBLIC_IF"
  [ "$declared" = "$routed" ] || die "hostfw.nft filters '$declared' but the default route leaves via '$routed'; fix PUBLIC_IF before applying"
  if systemctl is-enabled --quiet nftables 2>/dev/null; then
    # Ubuntu's stock /etc/nftables.conf starts with `flush ruleset`, which would wipe Docker's and fail2ban's tables.
    grep -q '^flush ruleset' /etc/nftables.conf 2>/dev/null && die "nftables.service is enabled with 'flush ruleset'; disable it first"
  fi
  nft -c -f "$RULES"
  echo "hostfw: $RULES is valid; public interface $declared"
}

apply() {
  check
  systemctl stop "$REVERT_UNIT.timer" 2>/dev/null || true
  systemctl reset-failed "$REVERT_UNIT.service" "$REVERT_UNIT.timer" 2>/dev/null || true
  systemd-run --quiet --unit "$REVERT_UNIT" --on-active="${REVERT_SECONDS}s" \
    /usr/sbin/nft delete table inet "$TABLE"
  nft -f "$RULES"
  echo "hostfw: table inet $TABLE loaded; automatic revert in ${REVERT_SECONDS}s"
  echo "hostfw: open a NEW ssh session now; if it works, run: $0 confirm"
}

confirm() {
  nft list table inet "$TABLE" >/dev/null 2>&1 || die "table inet $TABLE is not loaded (did the revert already fire?)"
  systemctl stop "$REVERT_UNIT.timer" 2>/dev/null || true
  echo "hostfw: revert cancelled; table inet $TABLE stays until reboot unless installed"
}

install_persistent() {
  check
  install -d -m 0755 /etc/infra2
  install -m 0644 "$RULES" "$INSTALLED_RULES"
  install -m 0644 "$UNIT" "$INSTALLED_UNIT"
  systemctl daemon-reload
  systemctl enable --quiet infra2-hostfw.service
  # Loads the same rules once more; idempotent, since the file deletes and recreates only its own table.
  systemctl start infra2-hostfw.service
  echo "hostfw: installed $INSTALLED_RULES and enabled infra2-hostfw.service"
}

status() {
  nft list table inet "$TABLE" 2>/dev/null | grep -E 'counter|elements' || echo "hostfw: table inet $TABLE not loaded"
  systemctl list-timers "$REVERT_UNIT.timer" --no-legend 2>/dev/null | grep -q . \
    && echo "hostfw: REVERT PENDING" || echo "hostfw: no revert pending"
  local enabled active
  enabled="$(systemctl is-enabled infra2-hostfw.service 2>/dev/null)" || true
  active="$(systemctl is-active infra2-hostfw.service 2>/dev/null)" || true
  echo "hostfw: service ${enabled:-not-installed}/${active:-inactive}"
  if [ -f "$INSTALLED_RULES" ]; then
    local repo installed
    repo="$(sha256sum "$RULES" | cut -d' ' -f1)"
    installed="$(sha256sum "$INSTALLED_RULES" | cut -d' ' -f1)"
    if [ "$repo" = "$installed" ]; then
      echo "hostfw: installed rules match this copy (${repo:0:12})"
    else
      echo "hostfw: DRIFT: installed ${installed:0:12} != this copy ${repo:0:12}"
    fi
  fi
}

case "${1:-}" in
  check) check ;;
  apply) apply ;;
  confirm) confirm ;;
  install) install_persistent ;;
  status) status ;;
  *) die "usage: $0 check|apply|confirm|install|status" ;;
esac
