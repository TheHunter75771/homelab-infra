#!/usr/bin/env bash
# Local tests for role edge_ssh: render the templates as Ansible does, check the drop-in with the
# local sshd (alone, and inside an Ubuntu-style config where it must win on every keyword),
# evaluate the role's own assert expressions against fixtures, and parse-check the fail2ban
# files (fail2ban itself isn't needed). Nothing touches a remote host.
#   bash roles/edge_ssh/tests/test_edge_ssh.sh        (from the repo root; uses .venv/bin/python)
# sh -c bodies are single-quoted on purpose (they read "$1").
# shellcheck disable=SC2016
set -euo pipefail
export LC_ALL=C

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../../.." && pwd)
PY=${PYTHON:-$ROOT/.venv/bin/python}
CHECK="$HERE/check_edge_ssh.py"
SSHD=${SSHD:-/usr/sbin/sshd}
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT

PASS=0
FAILS=0
ok() { PASS=$((PASS + 1)); printf 'ok   %s\n' "$1"; }
bad() { FAILS=$((FAILS + 1)); printf 'FAIL %s\n' "$1"; }
check() {
  local what=$1
  shift
  if "$@"; then ok "$what"; else bad "$what"; fi
}
refute() { # the check must say no (exit 1); a crash (any other code) is a test failure
  local what=$1 rc=0
  shift
  "$@" >"$T/refute.out" 2>&1 || rc=$?
  if [ "$rc" -eq 1 ]; then ok "$what"; else bad "$what (exit $rc: $(head -c 200 "$T/refute.out"))"; fi
}

# --- Render -------------------------------------------------------------------------------------
check "templates render" "$PY" "$CHECK" render "$T/out"
D="$T/out/10-homelab-edge.conf"
J="$T/out/homelab-sshd.local"
F="$T/out/homelab-fail2ban.local"
grep -v '^#' "$D" >"$T/dropin.settings"
check "drop-in: exactly the 11 settings, in order, no blank lines" \
  [ "$(tr '\n' ';' <"$T/dropin.settings")" = "PermitRootLogin prohibit-password;PasswordAuthentication no;KbdInteractiveAuthentication no;PubkeyAuthentication yes;X11Forwarding no;AllowAgentForwarding no;MaxAuthTries 3;LoginGraceTime 30;ClientAliveInterval 300;ClientAliveCountMax 2;LogLevel VERBOSE;" ]
check "drop-in: AllowTcpForwarding is not set" sh -c '! grep -qi "^AllowTcpForwarding" "$1"' _ "$D"
check "drop-in: no Port/ListenAddress (ssh.socket owns those)" sh -c '! grep -qiE "^(Port|ListenAddress) " "$1"' _ "$D"

# --- sshd: the drop-in alone, then inside an Ubuntu 24.04-style layout ---------------------------
if [ -x "$SSHD" ]; then
  echo "sshd under test: $("$SSHD" -V 2>&1 | head -n 1)"
  ssh-keygen -q -t ed25519 -N '' -f "$T/hostkey"
  check "sshd -t -f accepts the rendered drop-in alone" "$SSHD" -t -f "$D" -h "$T/hostkey"
  # Same shape as the VPS: Include near the top, then the stock (weaker) values; cloud-init's 50-.
  mkdir -p "$T/etc/sshd_config.d"
  cp "$D" "$T/etc/sshd_config.d/10-homelab-edge.conf"
  printf 'PasswordAuthentication yes\nPermitRootLogin yes\nMaxAuthTries 6\n' >"$T/etc/sshd_config.d/50-cloud-init.conf"
  cat >"$T/etc/sshd_config" <<EOF
# stock Ubuntu layout
Include $T/etc/sshd_config.d/*.conf
#Port 22
PermitRootLogin yes
#MaxAuthTries 6
KbdInteractiveAuthentication yes
UsePAM yes
X11Forwarding yes
AllowAgentForwarding yes
LoginGraceTime 120
ClientAliveInterval 0
LogLevel INFO
Subsystem sftp /usr/lib/openssh/sftp-server
EOF
  check "sshd -t accepts the whole layout" "$SSHD" -t -f "$T/etc/sshd_config" -h "$T/hostkey"
  "$SSHD" -T -f "$T/etc/sshd_config" -h "$T/hostkey" >"$T/sshd-T"
  check "every hardened value wins over cloud-init's 50- and the main file (real sshd -T)" "$PY" "$CHECK" effective "$T/sshd-T"
  check "AllowTcpForwarding keeps the system value" grep -qx 'allowtcpforwarding yes' "$T/sshd-T"
  # OpenSSH 9.6 (the VPS) prints prohibit-password under its old name.
  sed 's/^permitrootlogin prohibit-password$/permitrootlogin without-password/' "$T/sshd-T" >"$T/sshd-T-96"
  check "the assert accepts 9.6's 'without-password'" "$PY" "$CHECK" effective "$T/sshd-T-96"
  sed 's/^x11forwarding no$/x11forwarding yes/' "$T/sshd-T" >"$T/sshd-T-bad"
  refute "the assert catches a setting that didn't take (x11forwarding yes)" "$PY" "$CHECK" effective "$T/sshd-T-bad"
  grep -v '^maxauthtries ' "$T/sshd-T" >"$T/sshd-T-missing"
  refute "the assert catches a missing setting" "$PY" "$CHECK" effective "$T/sshd-T-missing"
  # The same layout with our drop-in sorted after cloud-init's must lose, proving order matters.
  mv "$T/etc/sshd_config.d/10-homelab-edge.conf" "$T/etc/sshd_config.d/90-late.conf"
  "$SSHD" -T -f "$T/etc/sshd_config" -h "$T/hostkey" >"$T/sshd-T-late"
  refute "control: sorted after 50-cloud-init.conf, the drop-in would NOT win" "$PY" "$CHECK" effective "$T/sshd-T-late"
else
  bad "no sshd at $SSHD (set SSHD=...)"
fi

# --- The role's preflight (layout) assert ---------------------------------------------------------
printf '# x\nInclude /etc/ssh/sshd_config.d/*.conf\nPermitRootLogin yes\nX11Forwarding yes\n' >"$T/layout-ok"
check "preflight passes on the Ubuntu layout with only 50-cloud-init.conf" \
  "$PY" "$CHECK" layout "$T/layout-ok" 50-cloud-init.conf 10-homelab-edge.conf
printf 'X11Forwarding yes\nInclude /etc/ssh/sshd_config.d/*.conf\n' >"$T/layout-late"
refute "preflight refuses a keyword set before the Include" "$PY" "$CHECK" layout "$T/layout-late" 50-cloud-init.conf
printf 'PermitRootLogin yes\n' >"$T/layout-none"
refute "preflight refuses a config without the Include" "$PY" "$CHECK" layout "$T/layout-none"
refute "preflight refuses a drop-in that sorts before ours" "$PY" "$CHECK" layout "$T/layout-ok" 05-early.conf 50-cloud-init.conf
printf '#PermitRootLogin yes\n  # X11Forwarding yes\nInclude /etc/ssh/sshd_config.d/*.conf\n' >"$T/layout-comments"
check "preflight ignores commented-out keywords above the Include" "$PY" "$CHECK" layout "$T/layout-comments"

# --- fail2ban files -------------------------------------------------------------------------------
check "jail and fail2ban.d files parse and hold the agreed values" "$PY" "$CHECK" ini "$J" "$F"
if command -v fail2ban-client >/dev/null; then
  mkdir -p "$T/f2b" && cp -r /etc/fail2ban/. "$T/f2b/" && cp "$J" "$T/f2b/jail.d/" && cp "$F" "$T/f2b/fail2ban.d/"
  check "fail2ban-client -t accepts the config" fail2ban-client -c "$T/f2b" -t
else
  echo "note: fail2ban-client not installed here; the role runs 'fail2ban-client --test' on the VPS"
fi

# --- The role's fail2ban assert, on output in fail2ban 1.0's format -------------------------------
printf 'Status for the jail: sshd\n|- Filter\n|  |- Currently failed:\t0\n`- Actions\n   |- Currently banned:\t0\n' >"$T/status"
printf 'The jail sshd has the following actions:\nnftables\n' >"$T/actions"
printf 'These IP addresses/networks are ignored:\n|- 127.0.0.0/8\n|- ::1\n|- 100.64.0.0/10\n`- fd7a:115c:a1e0::/48\n' >"$T/ignoreip"
check "jail assert passes on the expected status/actions/ignoreip" "$PY" "$CHECK" jail "$T/status" "$T/actions" "$T/ignoreip"
printf 'These IP addresses/networks are ignored:\n|- 127.0.0.0/8\n`- ::1\n' >"$T/ignoreip-bad"
refute "jail assert catches a tailnet range missing from ignoreip" "$PY" "$CHECK" jail "$T/status" "$T/actions" "$T/ignoreip-bad"
printf 'The jail sshd has the following actions:\niptables-multiport\n' >"$T/actions-bad"
refute "jail assert catches a non-nftables ban action" "$PY" "$CHECK" jail "$T/status" "$T/actions-bad" "$T/ignoreip"

echo
echo "edge_ssh tests: $PASS passed, $FAILS failed"
[ "$FAILS" -eq 0 ]
