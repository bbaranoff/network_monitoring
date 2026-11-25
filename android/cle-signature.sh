#!/bin/bash
# cle-signature.sh - la cle de signature FIXE des APK Android, une fois pour toutes.
#
# Sans elle, chaque compilation GitHub signe avec une cle de debug neuve :
# Android refuse de mettre a jour l appli (« package non valide ») et il faut
# la desinstaller - donc perdre son compte du noeud - a chaque version.
#
# Ce script :
#   1. cree la cle (RSA 2048, 30 ans, PKCS#12, alias « banc ») dans
#      ~/.config/network-monitor/banc-signature.p12 - A GARDER : la perdre,
#      c est ne plus pouvoir mettre a jour les applis deja installees ;
#   2. la donne au depot GitHub en deux secrets (BANC_KEYSTORE_B64,
#      BANC_KEYSTORE_PASS), que .github/workflows/apk.yml utilise.
# Il faut openssl et gh (connecte, avec le droit d ecrire les secrets du depot).
#   ./cle-signature.sh [depot]        defaut : bbaranoff/network_monitoring
set -eu
DEPOT="${1:-bbaranoff/network_monitoring}"
D="${XDG_CONFIG_HOME:-$HOME/.config}/network-monitor"
K="$D/banc-signature.p12"; P="$D/banc-signature.pass"
mkdir -p "$D"; umask 077
if [ -s "$K" ] && [ -s "$P" ]; then
    echo "cle deja presente : $K (on la reutilise)"
else
    openssl rand -hex 16 > "$P"
    T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
    openssl req -x509 -newkey rsa:2048 -nodes -keyout "$T/k.pem" -out "$T/c.pem" -days 10950 \
        -subj "/CN=Network Monitor (banc osmo-operator)" 2>/dev/null
    openssl pkcs12 -export -inkey "$T/k.pem" -in "$T/c.pem" -name banc -out "$K" -passout "file:$P"
    echo "cle creee : $K"
fi
chmod 600 "$K" "$P"
openssl pkcs12 -in "$K" -passin "file:$P" -nokeys 2>/dev/null | openssl x509 -noout -fingerprint -sha256 | sed 's/^/  /'
base64 -w0 "$K" | gh secret set BANC_KEYSTORE_B64 -R "$DEPOT"
gh secret set BANC_KEYSTORE_PASS -R "$DEPOT" < "$P"
echo "secrets poses sur $DEPOT : la prochaine compilation signera avec cette cle."
echo "L empreinte ci-dessus est celle a mettre dans la regle ARA-M d une SIM (Forcer la 2G)."
