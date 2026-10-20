#!/usr/bin/env bash
# infra2 host firewall: lockout-safe apply, persist and drift check.
# SSOT: docs/ssot/bootstrap.nodep.md §4 "Host firewall" (Infra-022 T1.3, #724).
#
# Run on the VPS as root, from a copy of this directory:
#   hostfw.sh check              syntax-check hostfw.nft and confirm the public interface
#   hostfw.sh apply              arm a 5-minute automatic revert, then load the table
#   hostfw.sh confirm            cancel the revert (only after a NEW ssh login worked)
#   hostfw.sh install            persist: /etc/infra2/hostfw.nft + infra2-hostfw.service + cf-refresh timer
#   hostfw.sh refresh-cf         check (--check) or atomically apply (--apply) live Cloudflare IP sets
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
REVERT_SECONDS="300"

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
  if systemctl is-enabled --quiet nftables 2>/dev/null || systemctl is-active --quiet nftables 2>/dev/null; then
    # Ubuntu's stock /etc/nftables.conf starts with `flush ruleset`, which would wipe Docker's and fail2ban's tables
    # the next time nftables.service (re)starts. Indentation is legal nft syntax, so match it too.
    if grep -Eq '^[[:space:]]*flush[[:space:]]+ruleset' /etc/nftables.conf 2>/dev/null; then
      die "nftables.service is enabled or active and /etc/nftables.conf flushes the ruleset; disable it first"
    fi
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
  install -m 0755 "$HERE/hostfw.sh" /etc/infra2/hostfw.sh
  install -m 0644 "$UNIT" "$INSTALLED_UNIT"
  if [ -f "$HERE/infra2-hostfw-cf-refresh.service" ] && [ -f "$HERE/infra2-hostfw-cf-refresh.timer" ]; then
    install -m 0644 "$HERE/infra2-hostfw-cf-refresh.service" /etc/systemd/system/infra2-hostfw-cf-refresh.service
    install -m 0644 "$HERE/infra2-hostfw-cf-refresh.timer" /etc/systemd/system/infra2-hostfw-cf-refresh.timer
  fi
  systemctl daemon-reload
  systemctl enable --quiet infra2-hostfw.service
  # Loads the same rules once more; idempotent, since the file deletes and recreates only its own table.
  systemctl start infra2-hostfw.service
  if [ -f /etc/systemd/system/infra2-hostfw-cf-refresh.timer ]; then
    systemctl enable --now --quiet infra2-hostfw-cf-refresh.timer
  fi
  echo "hostfw: installed $INSTALLED_RULES and enabled infra2-hostfw.service"
}

refresh_cf() {
  local mode="${1:---check}"
  [ "$mode" = "--check" ] || [ "$mode" = "--apply" ] || die "usage: $0 refresh-cf [--check|--apply]"
  local v4_url="${CF_IPS_V4_URL:-https://www.cloudflare.com/ips-v4}"
  local v6_url="${CF_IPS_V6_URL:-https://www.cloudflare.com/ips-v6}"
  local v4_raw v6_raw v4_elems="" v6_elems="" cidr
  v4_raw="$(curl -fsSL --max-time 15 "$v4_url")" || die "failed to fetch $v4_url"
  v6_raw="$(curl -fsSL --max-time 15 "$v6_url")" || die "failed to fetch $v6_url"

  local v4_count=0 v6_count=0
  while IFS= read -r cidr; do
    cidr="${cidr%%[[:space:]]*}"
    [ -z "$cidr" ] && continue
    [[ "$cidr" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/[0-9]+$ ]] || die "invalid IPv4 CIDR from Cloudflare: $cidr"
    v4_elems="${v4_elems:+$v4_elems, }$cidr"
    v4_count=$((v4_count + 1))
  done <<< "$v4_raw"

  while IFS= read -r cidr; do
    cidr="${cidr%%[[:space:]]*}"
    [ -z "$cidr" ] && continue
    [[ "$cidr" =~ ^[0-9a-fA-F:]+/[0-9]+$ ]] || die "invalid IPv6 CIDR from Cloudflare: $cidr"
    v6_elems="${v6_elems:+$v6_elems, }$cidr"
    v6_count=$((v6_count + 1))
  done <<< "$v6_raw"

  [ "$v4_count" -ge 5 ] || die "refusing Cloudflare IPv4 list with too few entries ($v4_count)"
  [ "$v6_count" -ge 3 ] || die "refusing Cloudflare IPv6 list with too few entries ($v6_count)"

  if [ "$mode" = "--check" ]; then
    echo "hostfw: Cloudflare IP sets verified (v4=$v4_count, v6=$v6_count)"
    return 0
  fi

  [ "$(id -u)" = "0" ] || die "--apply requires root"
  nft -f - <<EOF
flush set inet $TABLE cf_ipv4
add element inet $TABLE cf_ipv4 { $v4_elems }
flush set inet $TABLE cf_ipv6
add element inet $TABLE cf_ipv6 { $v6_elems }
EOF
  echo "hostfw: updated inet $TABLE @cf_ipv4 ($v4_count) and @cf_ipv6 ($v6_count)"
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
  refresh-cf|--check|--apply)
    if [ "${1:-}" = "refresh-cf" ]; then
      refresh_cf "${2:---check}"
    else
      refresh_cf "$1"
    fi
    ;;
  status) status ;;
  *) die "usage: $0 check|apply|confirm|install|refresh-cf [--check|--apply]|status" ;;
esac
