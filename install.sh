#!/bin/bash
# install.sh - pose Network Monitor sur un postmarketOS joignable en SSH, par
# son paquet apk (dist/network-monitor-<ver>-r<rel>.apk, construit par
# ./build-apk.sh s il manque) : apk tire les dependances (python3, py3-gobject3,
# gtk4.0, libadwaita, libsoup3, libmm-glib, modemmanager) et pose
# /usr/bin/network-monitor, le .desktop et l icone sous /usr/share.
#   ./install.sh                      -> la VM du banc (127.0.0.1:2222, user / 147147)
#   ./install.sh user@telephone [port] -> un vrai telephone (mot de passe demande, ou OSMO_PMOS_PASS)
# Sans SSH : copier l apk et faire   sudo apk add --allow-untrusted network-monitor-*.apk
set -e
cd "$(dirname "$0")"
HOST="${1:-user@127.0.0.1}"; PORT="${2:-${OSMO_PMOS_SSH_PORT:-2222}}"; PASS="${OSMO_PMOS_PASS:-147147}"
APK="$(ls -t dist/network-monitor-*.apk 2>/dev/null | head -1)"
[ -n "$APK" ] || { ./build-apk.sh; APK="$(ls -t dist/network-monitor-*.apk | head -1)"; }
KEY="$(ls dist/*.rsa.pub 2>/dev/null | head -1)"
if command -v sshpass >/dev/null && [ -n "$PASS" ]; then
    SSH="sshpass -p $PASS ssh"; SCP="sshpass -p $PASS scp"
else
    SSH=ssh; SCP=scp
fi
OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR"
echo "install: $HOST:$PORT  <-  $APK"
# shellcheck disable=SC2086
$SCP $OPTS -P "$PORT" "$APK" $KEY "$HOST:/tmp/"
# shellcheck disable=SC2086
$SSH $OPTS -p "$PORT" "$HOST" "echo '$PASS' | sudo -S -p '' sh -c '
set -e
[ -n \"$KEY\" ] && install -m644 /tmp/$(basename "${KEY:-x}") /etc/apk/keys/ 2>/dev/null || true
apk add -q /tmp/$(basename "$APK") 2>/dev/null || apk add -q --allow-untrusted /tmp/$(basename "$APK")
# l ancienne pose a la main (install.sh d avant le paquet) : /usr/local passerait devant /usr/bin
rm -f /usr/local/bin/network-monitor /usr/local/share/applications/org.osmocom.NetworkMonitor.desktop \
      /usr/local/share/icons/hicolor/scalable/apps/org.osmocom.NetworkMonitor.svg
rm -f /tmp/$(basename "$APK") /tmp/*.rsa.pub
echo installe : \$(apk info -q network-monitor 2>/dev/null | head -1 | cut -d\" \" -f1) - /usr/bin/network-monitor'"
