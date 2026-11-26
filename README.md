# network-monitor — moniteur radio cellulaire pour postmarketOS

Dans l'esprit de *Network Signal Guru* (Android), pour Phosh : cellule
serveuse, niveaux de signal avec historique, cellules voisines, trames
(LAPDm, DTAP, S1AP…), journal des événements et enregistrement CSV. La radio
vient d'une seule source, **ModemManager**, par D-Bus (libmm-glib via GObject
Introspection) ; l'appli ne touche pas au port AT, elle tourne donc aussi bien
sur la VM du banc (modem « osmo-banc » émulé par `osmo-phonesim-banc.py`) que
sur un vrai téléphone. Les trames, elles, viennent du **tableau de bord du
banc** (osmo-egprs-web), qui fait tourner tshark sur l'hôte et les pousse par
WebSocket : depuis la VM l'hôte est `10.0.2.2`, l'appli s'y branche toute
seule.

## 0.3 puis 0.4 : le moniteur devient un poste du réseau

Cinq onglets : **Téléphone**, **Messages**, **Radio**, **Nœud**, **Compte**.
« Radio » (les pages d'avant) et « Nœud » se feuillettent du doigt.

Tout part d'**une adresse**, celle d'un nœud osmo-operator (IP locale,
WireGuard ou VPS), saisie une fois dans Compte :

| service du nœud | ce que l'appli en fait |
|---|---|
| `http://<nœud>:8081/psd` (fft-web) | Nœud : spectres BTS et MS, cascade en palette turbo |
| `http://<nœud>:8090/api/etat`, `/api/console` (osmo-reglages) | Nœud : services, console VTY |
| `http://<nœud>:8090/api/trunk` (osmo-reglages) | Compte : sortie vers l'extérieur, trunk SIP ou dongle |
| `ws://<nœud>:8080/` (tableau de bord) | Radio : trames |
| `http://<nœud>:8088/inscription` | Téléphone : code d'invitation → compte SIP |
| `sip:<nœud>:5060` (Asterisk) | Téléphone, Messages : appels et SMS (SIP MESSAGE), pjsua2 |

Rien de tout cela n'est authentifié côté nœud : réseau local ou WireGuard,
jamais l'Internet ouvert. `osmo-reglages.py` écoute sur 127.0.0.1 par
défaut : le lancer avec `--bind <adresse du nœud>`.

**Modes** (Compte → Affichage) : *Basique* (téléphone, messages, compte),
*Medium* (+ cellule, signal, voisines, trames, journal, spectres, services),
*Expert* (+ console VTY, choix de la sortie). `NETMON_MODE=expert`
le force ; `NETMON_NODE=192.168.1.20` donne le nœud sans passer par Compte.

**Android** : `android/` est la même appli en Kotlin (SDK Linphone pour le
SIP, `TelephonyManager` pour la radio). L'APK est construite par
`.github/workflows/apk.yml` (Actions → artefact `network-monitor-apk`).
Pas encore de page Trames sur Android.

Pas encore validé : le SIP de l'appli pmOS (pjsua2) contre un vrai Asterisk,
la compilation Android.

**Inscription** : côté nœud, `osmo-operator/tools/osmo-inscription.py`
(service `osmo-inscription`, port 8088). Un code d'invitation se crée avec
`python3 tools/osmo-inscription.py --nouveau-code` ; le téléphone reçoit un
numéro 3000-3999 et son mot de passe SIP. « J'ai déjà un compte SIP » reste
possible avec un compte existant.

**Sur le nœud, tout se fait dans osmo-gtk, onglet « Téléphones »** : invitation par QR code, comptes,
proxy, ligne Bluetooth.

**Page « Banc »** (onglet Nœud, 0.11) : une bonne partie de ce que montre osmo-gtk, lue sur le réseau
avec le compte du téléphone - la chaîne (DSP, QEMU, osmocon, mobile, pont), le service du banc et ses
deux boutons Démarrer / Arrêter, les démons du cœur, les abonnés du HLR, un journal au choix. Elle
passe par `osmo-inscription` (`/banc/etat`, `/banc/journal`, `/banc/vty`, `/banc/action`), par le
réseau (Wi-Fi, WireGuard, USB).

**Invitation par QR code** : `python3 tools/osmo-inscription.py --nouveau-qrcode PSEUDO`
affiche un QR code (terminal et PNG) qui porte l'adresse du nœud, le pseudo et
une invitation de 16 caractères. Dans l'appli : « Scanner le QR code » (caméra),
« Image » sur postmarketOS (capture d'écran), ou coller le lien dans Invitation.

## Fichiers

| fichier | rôle |
|---|---|
| `network-monitor.py` | l'appli (Python 3, GTK 4, libadwaita) |
| `org.osmocom.NetworkMonitor.desktop` | l'entrée de menu Phosh |
| `org.osmocom.NetworkMonitor.svg` | l'icône |
| `apk/APKBUILD` | la recette du paquet Alpine / postmarketOS |
| `build-apk.sh` | construit `dist/network-monitor-<ver>-r<rel>.apk` (abuild, chroot natif de pmbootstrap) |
| `dist/` | le `.apk` (noarch, signé) et la clé publique `pmos-….rsa.pub` |
| `install.sh` | pose le paquet sur un postmarketOS joignable en SSH |

## Installation

L'appli est un paquet Alpine : `dist/network-monitor-<version>-r0.apk` (noarch,
il va sur la VM comme sur un téléphone ARM). Directement sur le pmOS :

```sh
sudo apk add --allow-untrusted network-monitor-<version>-r0.apk
# ou, pour que apk vérifie la signature (la clé est publiée à côté du paquet) :
sudo cp pmos-*.rsa.pub /etc/apk/keys/ && sudo apk add network-monitor-<version>-r0.apk
```

Les paquets sont publiés en **release** du dépôt : `network-monitor-<version>-r0-postmarketos.apk`
(Alpine / postmarketOS), `network-monitor-<version>-android.apk` (Android) et la clé `pmos-*.rsa.pub`.
Sur le banc osmo-operator rien n'est à faire : l'ISO embarque le paquet de la dernière release, et la
VM postmarketOS le reçoit à son premier démarrage (`osmo-pmos-setup` prend la dernière release publiée,
sinon celui du banc).

`apk` tire les dépendances (`python3 py3-gobject3 py3-cairo gtk4.0 libadwaita
libsoup3 libmm-glib modemmanager`, déjà là sur une image Phosh) et pose
`/usr/bin/network-monitor`, le `.desktop` et l'icône sous `/usr/share` ;
l'icône apparaît dans le menu Phosh. `apk del network-monitor` retire tout.

Par SSH, depuis l'hôte :

```sh
./install.sh                        # la VM du banc : 127.0.0.1:2222, user / 147147
./install.sh user@192.168.1.42 22   # un téléphone (mot de passe : OSMO_PMOS_PASS ou demandé)
```

Reconstruire le paquet après une modification de `network-monitor.py`
(la version vient de `VERSION` dans le script ; `--rel N` pour un pkgrel) :

```sh
./build-apk.sh          # abuild dans le chroot natif de pmbootstrap, signé par
                        # la clé de ~/.local/var/pmbootstrap/config_abuild (créée au 1er passage)
```

## Pages

- **Cellule** : technologie (2G/3G/4G/5G), opérateur et PLMN, mesure
  principale (RSRP en LTE, RSSI en GSM), LAC/TAC, Cell ID (hex et décimal),
  eNB / secteur (déduits de l'ECI), ARFCN / EARFCN / PCI quand le modem les
  donne, bandes, enregistrements CS / EPS, service paquet, IMEI, IMSI.
- **Signal** : graphe des 10 dernières minutes (mesure au choix, suit la
  technologie tant qu'on n'a rien choisi) et les mesures avec un niveau
  coloré : RSRP, RSRQ, SNR, RSSI, RSCP, Ec/Io, qualité, BER.
- **Voisines** : la liste `GetCellInfo` de ModemManager (modems QMI / MBIM ;
  un modem AT générique, comme celui du banc, ne la fournit pas).
- **Trames** : ce que le tableau de bord du banc capture sur l'hôte —
  - 2G, GSMTAP : LAPDm (SABM / UA / I…), DTAP (MM, RR, CC, SMS), avec le canal,
    le slot, le FN, le sens et l'opérateur ;
  - 4G, MAC-LTE de srsENB (`enb.conf` : `mac_net_enable`, UDP 5847) : MAC,
    RLC, PDCP, **RRC et NAS** (Attach, Authentication, Security Mode,
    Activate default EPS bearer… — open5gs chiffre en EEA0, tout est
    lisible), avec le RNTI et la trame système ;
  - cœur : S1AP (eNB ↔ MME), M3UA / SCCP (pile Osmocom), Diameter.

  Les trames défilent dans l'ordre d'arrivée, **le rectangle entier coloré
  par famille** : SI / paging gris, LAPDm bleu, MM violet, RR vert, CC
  orange, SMS jaune, cœur turquoise, MAC-LTE sable, RRC magenta, NAS rouge ;
  bord gauche orange ↑ UL (le mobile parle), bleu ↓ DL (le réseau parle).
  Le bouton ⤓ **bloque le curseur en fin de course** : chaque trame qui
  arrive ramène la vue en bas ; remonter à la main le libère, revenir en bas
  le rebloque. Pause (les trames s'accumulent, affichées à la reprise),
  effacement, filtre par mots (`DTAP UL`, `SABM`, `NAS`, `RRC`, `OP1`…).
  Toucher une trame ouvre **son contenu complet** : ses champs, la
  dissection tshark entière (toutes les couches) et l'hexadécimal — le
  dashboard les relit dans le pcap de la capture. 2000 trames gardées.
- **Journal** : détection du modem, changements d'enregistrement et de
  cellule, état du lien avec le dashboard, erreurs. Menu : balayage des
  réseaux (`AT+COPS=?`), enregistrement CSV dans
  `~/Documents/network-monitor/` (`signal-….csv` pour les mesures, toutes les
  2 s, et `trames-….csv`, une ligne par trame), à propos.

## D'où viennent les valeurs

| donnée | interface ModemManager | côté modem AT |
|---|---|---|
| état, technologie, qualité %, bandes | `Modem` | `+CSQ`, `+COPS?` |
| PLMN, opérateur, enregistrement, service paquet | `Modem.Modem3gpp` | `+CREG` / `+CGREG` / `+CEREG`, `+COPS` |
| LAC / TAC, Cell ID | `Modem.Location` (source `3gpp-lac-ci`) | `+CREG=2` |
| RSSI / RSRP / RSRQ / SNR, RSCP / Ec/Io | `Modem.Signal` (cadence 2 s) | `+CESQ` |
| ARFCN, PCI, voisines | `Modem.Modem3gpp.GetCellInfo` | — (QMI/MBIM) |
| trames | WebSocket du dashboard (`tshark_start` → `packet`, `packet_tree_request` / `packet_hex_request` → contenu) | tshark sur l'hôte : GSMTAP 4729, MAC-LTE 5847, SCTP |

Sur le banc, le modem émulé répond `+CESQ` avec RSRP/RSRQ fixes en LTE et
`+CSQ` en GSM ; LAC/CI viennent du VTY d'OsmoBSC, TAC/ECI de srsENB. Banc
arrêté = modem « inactif », toutes les mesures à « — ».

## Options de mise au point

```sh
network-monitor --demo                  # valeurs et trames simulées, sans modem ni dashboard
network-monitor --screenshot DOSSIER    # rend chaque page en PNG puis quitte
network-monitor --frames-url ws://192.168.1.91:8080/   # un autre dashboard (ou NETMON_FRAMES_URL) ; vide = pas de trames
```

Sans dashboard joignable (vrai téléphone hors du banc), la page Trames dit
« dashboard injoignable » et réessaie toutes les 10 s ; le reste de l'appli
n'en dépend pas. Il n'y a ni tshark ni GSMTAP dans le téléphone : la radio est
émulée sur l'hôte, les trames ne peuvent venir que de là.

Pour capturer sans écran (par SSH, VM verrouillée ou non) :

```sh
GDK_BACKEND=x11 GDK_SCALE=2 xvfb-run -a -s "-screen 0 800x1500x24" \
    network-monitor --demo --screenshot ~/shots      # apk add xvfb-run
```
