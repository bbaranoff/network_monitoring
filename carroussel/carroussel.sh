#!/bin/bash
# carroussel.sh [dossier] - refait le carrousel de l appli, de vraies captures.
#
#   ./carroussel.sh                 -> /root/carroussel (les PNG y sont ECRASES)
#   ./carroussel.sh /tmp/essai
#   ./carroussel.sh --page [dossier]   ne refait que index.html avec les PNG presents
#
# Ce qu il fait :
#   1. un compte SIP a part pour la capture (osmo-inscription, garde dans
#      ~/.config/network-monitor/capture.json) : la fenetre de capture ne se
#      bat pas avec l appli ouverte sur le telephone (meme numero = les SMS et
#      les appels entrants iraient a l autre) ;
#   2. dans la VM postmarketOS (SSH 2222), capture.py pilote la vraie appli en
#      mode Expert, theme clair, sur le vrai noeud ;
#   3. ici, les evenements entrants : le mobile GSM du banc (VTY 4247) envoie
#      un SMS et appelle, quand capture.py le demande ;
#   4. les PNG reviennent dans le dossier, page.py ecrit index.html.
#
# Il faut : la VM lancee, l appli installee dedans (/usr/bin/network-monitor),
# le banc 2G en service (mobile 100101 sur le VTY 4247), osmo-inscription actif.
# NE PAS TOUCHER au telephone pendant la capture (4 a 6 min) : Phosh ne dessine
# que l appli au premier plan.
#
# Reglages : OSMO_PMOS_SSH_PORT (2222), OSMO_PMOS_PASS, OSMO_PMOS_USER,
# CARROUSSEL_NOEUD (adresse du noeud vue de la VM, defaut : la source de la
# route par defaut), CARROUSSEL_MOBILE (100101), CARROUSSEL_VTY (4247),
# CARROUSSEL_MODE (expert).
set -u
ICI="$(cd "$(dirname "$0")" && pwd)"
PAGE_SEULE=0
[ "${1:-}" = "--page" ] && { PAGE_SEULE=1; shift; }
DEST="${1:-/root/carroussel}"
PORT="${OSMO_PMOS_SSH_PORT:-2222}"; PASS="${OSMO_PMOS_PASS:-147147}"; USER_VM="${OSMO_PMOS_USER:-user}"
NOEUD="${CARROUSSEL_NOEUD:-$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p')}"
MOBILE="${CARROUSSEL_MOBILE:-100101}"; VTY="${CARROUSSEL_VTY:-4247}"; MODE="${CARROUSSEL_MODE:-expert}"
INSCRIPTION="${OSMO_REPO:-/opt/GSM/osmo-operator}/tools/osmo-inscription.py"
COMPTE="${XDG_CONFIG_HOME:-$HOME/.config}/network-monitor/capture.json"
SSHO="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=6 -o PreferredAuthentications=password"
vm()  { sshpass -p "$PASS" ssh -p "$PORT" -T $SSHO "$USER_VM@127.0.0.1" "$@"; }
dit() { echo "carroussel: $*"; }
vty() { { sleep 0.5; printf 'enable\n'; sleep 0.5; printf '%s\n' "$1"; sleep 1; } | timeout 6 nc -q1 127.0.0.1 "$VTY" >/dev/null 2>&1; }

mkdir -p "$DEST"
if [ "$PAGE_SEULE" = 1 ]; then python3 "$ICI/page.py" "$DEST"; exit $?; fi

vm 'echo ok' 2>/dev/null | grep -q ok || { dit "la VM ne repond pas en SSH sur le port $PORT"; exit 1; }
{ sleep 0.4; printf 'show ms\n'; sleep 1; } | timeout 5 nc -q1 127.0.0.1 "$VTY" 2>/dev/null | grep -q 'service is normal' \
    || dit "ATTENTION : le mobile $MOBILE n est pas en service normal (VTY $VTY) - SMS et appel entrants manqueront"

# 1. le compte de la capture
if [ ! -s "$COMPTE" ]; then
    [ -f "$INSCRIPTION" ] || { dit "osmo-inscription introuvable ($INSCRIPTION)"; exit 1; }
    code="$(python3 "$INSCRIPTION" --nouveau-code)" || exit 1
    mkdir -p "$(dirname "$COMPTE")"
    curl -s -m 10 -X POST "http://$NOEUD:8088/inscription" -d "{\"code\":\"$code\",\"pseudo\":\"capture\"}" \
        | python3 -c '
import json, sys
j = json.load(sys.stdin)
if not j.get("ok"): sys.exit("inscription refusee : %s" % j.get("error"))
json.dump({k: j[k] for k in ("msisdn", "sip_user", "sip_password", "sip_port", "sip_transport")}, open(sys.argv[1], "w"))
' "$COMPTE" || { dit "pas de compte de capture (osmo-inscription actif sur $NOEUD:8088 ?)"; rm -f "$COMPTE"; exit 1; }
    chmod 600 "$COMPTE"
fi
NUM="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["msisdn"])' "$COMPTE")"
dit "compte de capture : $NUM sur le noeud $NOEUD"

# 2. la VM : reglages a part (XDG_CONFIG_HOME), scenario, sortie
python3 -c '
import json, sys
a = json.load(open(sys.argv[1])); a["sip_domain"] = sys.argv[2]
print(json.dumps({"mode": 2, "theme": "clair", "node": sys.argv[2], "account": a}))
' "$COMPTE" "$NOEUD" | vm 'rm -rf /tmp/carroussel-vm /tmp/carroussel-cfg; mkdir -p /tmp/carroussel-cfg/network-monitor /tmp/carroussel-vm; cat > /tmp/carroussel-cfg/network-monitor/reglages.json'
vm 'cat > /tmp/carroussel-capture.py' < "$ICI/capture.py"

# 3. les evenements entrants, a la demande de capture.py
(
    for _ in $(seq 1 240); do
        sleep 2
        f="$(vm 'cd /tmp/carroussel-vm 2>/dev/null && for x in want-mt want-call want-hangup; do [ -e $x ] && { echo "DRAPEAU-$x"; rm -f $x; }; done')"
        case "$f" in *DRAPEAU-want-mt*)     vty "sms 1 $NUM Recu 5 sur 5, ici $MOBILE"; dit "le mobile $MOBILE ecrit a $NUM";; esac
        case "$f" in *DRAPEAU-want-call*)   vty "hangup 1"; vty "call 1 $NUM"; dit "le mobile $MOBILE appelle $NUM";; esac
        case "$f" in *DRAPEAU-want-hangup*) vty "hangup 1"; dit "le mobile raccroche"; break;; esac
    done
) &
GUETTEUR=$!
trap 'kill $GUETTEUR 2>/dev/null; vty "hangup 1"' EXIT

dit "capture en cours dans la VM (4 a 6 min) - ne pas toucher au telephone"
vm "export XDG_CONFIG_HOME=/tmp/carroussel-cfg XDG_RUNTIME_DIR=/run/user/\$(id -u) WAYLAND_DISPLAY=wayland-0 DISPLAY=:1 \
        NETMON_MODE=$MODE CAPTURE_OUT=/tmp/carroussel-vm CAPTURE_PAIR=$MOBILE
    export DBUS_SESSION_BUS_ADDRESS=unix:path=\$XDG_RUNTIME_DIR/bus
    timeout 540 python3 /tmp/carroussel-capture.py > /tmp/carroussel-capture.log 2>&1
    grep -E '^capture|attente|relance|Traceback|Error:' /tmp/carroussel-capture.log" | sed 's/^/carroussel:   /'

# 4. les PNG, puis la page
n="$(vm 'ls /tmp/carroussel-vm/*.png 2>/dev/null | wc -l')"
[ "${n:-0}" -gt 0 ] || { dit "aucune capture produite - voir /tmp/carroussel-capture.log dans la VM"; exit 1; }
rm -f "$DEST"/*.png
sshpass -p "$PASS" scp -q -P "$PORT" $SSHO "$USER_VM@127.0.0.1:/tmp/carroussel-vm/*.png" "$DEST"/ || { dit "copie des PNG impossible"; exit 1; }
python3 "$ICI/page.py" "$DEST" | sed 's/^/carroussel: /'
dit "termine : $n captures dans $DEST - ouvrir $DEST/index.html"
