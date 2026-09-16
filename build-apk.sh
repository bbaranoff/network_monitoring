#!/bin/bash
# build-apk.sh - construit le paquet Alpine/postmarketOS de Network Monitor :
#   dist/network-monitor-<version>-r<rel>.apk   (noarch, signe)
#   dist/<cle>.rsa.pub                          la cle publique qui le signe
# avec abuild dans le chroot natif de pmbootstrap (le meme dossier de travail
# que le noyau PPP du banc ; la cle est celle de config_abuild/, creee au
# premier passage). La version vient de VERSION dans network-monitor.py.
#   ./build-apk.sh            construit
#   ./build-apk.sh --rel N    change pkgrel (memes sources, paquet refait)
set -e
cd "$(dirname "$0")"
REL=""
[ "$1" = "--rel" ] && REL="$2"
PMB="pmbootstrap"; [ "$(id -u)" = 0 ] && PMB="pmbootstrap --as-root"
WORK="$($PMB config work 2>/dev/null | tail -1)"
[ -d "$WORK/chroot_native/home/pmos" ] || { echo "pas de chroot natif pmbootstrap dans $WORK (osmo-pmos-build --init d abord)" >&2; exit 1; }
CH="$WORK/chroot_native"
VER="$(sed -n 's/^VERSION = "\(.*\)"/\1/p' network-monitor.py)"
[ -n "$VER" ] || { echo "VERSION introuvable dans network-monitor.py" >&2; exit 1; }
sed -i "s/^pkgver=.*/pkgver=$VER/" apk/APKBUILD
[ -n "$REL" ] && sed -i "s/^pkgrel=.*/pkgrel=$REL/" apk/APKBUILD

# Le dossier de construction, dans le chroot : ~pmos/banc/network-monitor
# (« banc » devient le nom du depot dans REPODEST).
D="$CH/home/pmos/banc/network-monitor"
rm -rf "$D"; mkdir -p "$D"
cp apk/APKBUILD network-monitor.py org.osmocom.NetworkMonitor.desktop org.osmocom.NetworkMonitor.svg "$D/"
chown -R 12345:12345 "$CH/home/pmos/banc"

$PMB chroot -- apk add -q abuild >/dev/null
if ! grep -qs '^PACKAGER_PRIVKEY=' "$WORK/config_abuild/abuild.conf"; then
    echo "cle de signature : creation dans $WORK/config_abuild"
    $PMB chroot --user -- abuild-keygen -a -n >/dev/null
fi
# La cle publique : dans /etc/apk/keys du chroot (l index du depot local la
# verifie) et dans config_apk_keys/ (les images pmbootstrap suivantes la
# connaissent, comme celle du noyau PPP).
for k in "$WORK"/config_abuild/*.rsa.pub; do
    install -m644 "$k" "$CH/etc/apk/keys/"
    cp -n "$k" "$WORK/config_apk_keys/" 2>/dev/null || true
done
$PMB chroot --user -- sh -c 'cd ~/banc/network-monitor && abuild checksum && abuild -d -P ~/packages' 
cp "$D/APKBUILD" apk/APKBUILD          # les sha512 a jour
APK="$(ls -t "$CH"/home/pmos/packages/banc/*/network-monitor-"$VER"-r*.apk | head -1)"
[ -n "$APK" ] || { echo "pas d apk produit" >&2; exit 1; }
rm -f dist/network-monitor-*.apk dist/*.rsa.pub
cp "$APK" dist/
cp "$WORK"/config_abuild/*.rsa.pub dist/
echo "apk : dist/$(basename "$APK")"
echo "cle : dist/$(basename "$WORK"/config_abuild/*.rsa.pub)"
echo "installation sur pmOS : ./install.sh   ou   sudo apk add --allow-untrusted /chemin/$(basename "$APK")"
