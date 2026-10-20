#!/usr/bin/env bash
# Validate or apply SSH hardening, fail2ban nftables jail, and auth.log monitoring (#724-R2).
set -euo pipefail

case "${1:---check}" in
  --check|--apply) MODE="${1:---check}" ;;
  *) echo "usage: $0 [--check|--apply]" >&2; exit 2 ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SSHD_CONF="${SCRIPT_DIR}/sshd_hardening.conf"
F2B_LOCAL="${SCRIPT_DIR}/fail2ban_sshd.local"
AUTH_SCRIPT="${SCRIPT_DIR}/auth_log_alert.sh"
AUTH_SERVICE="${SCRIPT_DIR}/infra2-auth-log-alert.service"
AUTH_TIMER="${SCRIPT_DIR}/infra2-auth-log-alert.timer"

for f in "${SSHD_CONF}" "${F2B_LOCAL}" "${AUTH_SCRIPT}" "${AUTH_SERVICE}" "${AUTH_TIMER}"; do
  [[ -f "${f}" ]] || { echo "missing required file: ${f}" >&2; exit 1; }
done

bash -n "${AUTH_SCRIPT}"

grep -Eq '^PasswordAuthentication[[:space:]]+no$' "${SSHD_CONF}" || {
  echo "sshd_hardening.conf must set PasswordAuthentication no" >&2
  exit 1
}
grep -Eq '^KbdInteractiveAuthentication[[:space:]]+no$' "${SSHD_CONF}" || {
  echo "sshd_hardening.conf must set KbdInteractiveAuthentication no" >&2
  exit 1
}
grep -Eq '^PermitRootLogin[[:space:]]+prohibit-password$' "${SSHD_CONF}" || {
  echo "sshd_hardening.conf must set PermitRootLogin prohibit-password" >&2
  exit 1
}
grep -Eq '^banaction[[:space:]]*=[[:space:]]*nftables-multiport$' "${F2B_LOCAL}" || {
  echo "fail2ban_sshd.local must set banaction = nftables-multiport" >&2
  exit 1
}
grep -Eq '^maxretry[[:space:]]*=[[:space:]]*5$' "${F2B_LOCAL}" || {
  echo "fail2ban_sshd.local must set maxretry = 5" >&2
  exit 1
}

if [[ "${MODE}" == "--check" ]]; then
  echo "SSH hardening, fail2ban jail, and auth.log alert configurations are valid"
  exit 0
fi

if [[ "${EUID}" -ne 0 ]]; then
  echo "--apply requires root" >&2
  exit 1
fi

if [[ ! -s /root/.ssh/authorized_keys ]]; then
  echo "refusing to disable password auth: /root/.ssh/authorized_keys is missing or empty" >&2
  exit 1
fi

install -d -m 0755 /etc/ssh/sshd_config.d /etc/fail2ban/jail.d
install -m 0644 "${SSHD_CONF}" /etc/ssh/sshd_config.d/10-infra2-hardening.conf
if command -v sshd >/dev/null 2>&1; then
  sshd -t
fi
systemctl reload ssh 2>/dev/null || systemctl reload sshd

install -m 0644 "${F2B_LOCAL}" /etc/fail2ban/jail.d/infra2-sshd.local
systemctl enable --now fail2ban
systemctl restart fail2ban
fail2ban-client status sshd >/dev/null

install -m 0755 "${AUTH_SCRIPT}" /usr/local/sbin/infra2-auth-log-alert.sh
install -m 0644 "${AUTH_SERVICE}" /etc/systemd/system/infra2-auth-log-alert.service
install -m 0644 "${AUTH_TIMER}" /etc/systemd/system/infra2-auth-log-alert.timer
systemctl daemon-reload
systemctl enable --now infra2-auth-log-alert.timer
echo "Applied SSH hardening, fail2ban sshd jail, and auth.log alert timer"
