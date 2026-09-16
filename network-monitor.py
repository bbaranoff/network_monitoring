#!/usr/bin/env python3
# network-monitor - moniteur radio cellulaire pour postmarketOS (Phosh), dans
# l esprit de Network Signal Guru : cellule serveuse, mesures de signal avec
# historique, cellules voisines, journal des evenements et enregistrement CSV.
#
# LA SOURCE RADIO : ModemManager, par D-Bus (libmm-glib via GObject
# Introspection). On ne touche pas au port AT - ModemManager le tient, et
# c est lui qui sait parler a tous les modems (AT generique, QMI, MBIM), donc
# la meme appli tourne sur la VM du banc (modem « osmo-banc » emule par
# osmo-phonesim-banc.py, +CREG/+CEREG/+CSQ/+CESQ) et sur un vrai telephone.
#
# LES TRAMES (page « Trames ») : le tableau de bord du banc (osmo-egprs-web)
# fait tourner tshark sur l hote - GSMTAP (LAPDm, DTAP, RR, CC, SMS) et SCTP
# (S1AP, M3UA) - et pousse chaque trame decodee par WebSocket. L appli s y
# branche comme le navigateur : depuis la VM, l hote est 10.0.2.2 (reseau
# utilisateur de QEMU) ; sur l hote lui-meme, 127.0.0.1. Ni tshark ni GSMTAP
# n existent dans le telephone : la radio est emulee sur l hote, les trames
# ne peuvent venir que de la. Sans dashboard joignable (vrai telephone), la
# page le dit et le reste de l appli n en depend pas.
#
# Ce que ModemManager donne, et d ou :
#   - Modem           : etat, technologie d acces, qualite (%), bandes ;
#   - Modem.Modem3gpp : PLMN (MCC/MNC), nom d operateur, enregistrements
#                       CS / PS / EPS, service paquet, GetCellInfo (ARFCN,
#                       PCI, voisines - QMI/MBIM seulement, l AT generique
#                       repond « non supporte », on l affiche tel quel) ;
#   - Modem.Signal    : RSSI / RSRP / RSRQ / SNR (LTE), RSCP / EcIo (UMTS),
#                       RSSI (GSM), a la cadence demandee (+CESQ derriere) ;
#   - Modem.Location  : LAC ou TAC et Cell ID (+CREG=2 derriere).
#
# --demo : pas de modem, des valeurs simulees (marche aleatoire, une LTE qui
# redescend en GSM de temps en temps, comme le CSFB du banc) pour verifier
# l interface sans le banc.

import argparse
import collections
import csv
import json
import math
import os
import random
import sys
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Graphene, Gsk, Gtk, Pango  # noqa: E402

try:
    gi.require_version("ModemManager", "1.0")
    from gi.repository import ModemManager as MM  # noqa: E402
except (ValueError, ImportError):
    MM = None
try:
    gi.require_version("Soup", "3.0")
    from gi.repository import Soup  # noqa: E402   (client WebSocket, paquet libsoup3)
except (ValueError, ImportError):
    Soup = None

# Les noms d enumeration qui commencent par un chiffre (3GPP, 5GNR) ne sont
# pas des attributs Python valides : on garde la valeur de libmm-glib.
LOC_3GPP_LAC_CI = 1 << 0       # MM_MODEM_LOCATION_SOURCE_3GPP_LAC_CI (1 << 1 serait GPS_RAW)
ACT_5GNR = 1 << 15             # MM_MODEM_ACCESS_TECHNOLOGY_5GNR

APP_ID = "org.osmocom.NetworkMonitor"
VERSION = "0.2"
HISTORY_S = 600          # 10 min d historique par mesure
POLL_S = 2               # cadence de lecture
FRAMES_MAX = 2000        # trames gardees a l ecran
# Ou trouver le dashboard : la VM du banc voit l hote en 10.0.2.2 ; l appli
# lancee sur l hote le voit en 127.0.0.1. NETMON_FRAMES_URL ou --frames-url
# pour un autre banc (liste separee par des virgules, essayees a tour de role).
FRAMES_URLS = [u.strip() for u in os.environ.get(
    "NETMON_FRAMES_URL", "ws://10.0.2.2:8080/,ws://127.0.0.1:8080/").split(",") if u.strip()]
# Les types RR que le dashboard laisse en hexa (GSM 04.08 §10.4) : on les nomme ici.
RR_NAMES = {0x02: "SI2bis", 0x03: "SI2ter", 0x05: "SI5bis", 0x06: "SI5ter", 0x0e: "CHANNEL MODE MODIFY",
            0x10: "CHANNEL MODE MODIFY ACK", 0x13: "CLASSMARK ENQUIRY", 0x16: "CLASSMARK CHANGE",
            0x19: "SI1", 0x1a: "SI2", 0x1b: "SI3", 0x1c: "SI4", 0x1d: "SI5", 0x1e: "SI6", 0x1f: "SI7", 0x18: "SI8",
            0x22: "PAGING REQ 2", 0x24: "PAGING REQ 3", 0x27: "PAGING RESPONSE", 0x28: "HANDOVER FAILURE",
            0x2b: "HANDOVER CMD", 0x2c: "HANDOVER COMPLETE", 0x32: "CIPHERING MODE CMD",
            0x35: "CIPHERING MODE COMPLETE", 0x30: "PARTIAL RELEASE", 0x0f: "PARTIAL RELEASE COMPLETE"}

# ── Les seuils, comme les couleurs de NSG ───────────────────────────────────
# (mesure : [(seuil, niveau)]) niveau 4 = excellent ... 1 = mauvais, 0 = rien.
THRESHOLDS = {
    "rsrp": [(-80, 4), (-90, 3), (-100, 2), (-140, 1)],
    "rsrq": [(-10, 4), (-15, 3), (-20, 2), (-40, 1)],
    "snr":  [(20, 4), (13, 3), (0, 2), (-30, 1)],
    "rssi": [(-65, 4), (-75, 3), (-85, 2), (-120, 1)],
    "rscp": [(-70, 4), (-85, 3), (-100, 2), (-125, 1)],
    "ecio": [(-6, 4), (-10, 3), (-15, 2), (-25, 1)],
}
RANGES = {   # etendue tracee dans le graphe
    "rsrp": (-140, -40), "rsrq": (-25, -3), "snr": (-10, 35),
    "rssi": (-120, -40), "rscp": (-125, -40), "ecio": (-25, 0),
    "quality": (0, 100),
}
UNITS = {"rsrp": "dBm", "rsrq": "dB", "snr": "dB", "rssi": "dBm",
         "rscp": "dBm", "ecio": "dB", "quality": "%", "ber": "%"}
LABELS = {"rsrp": "RSRP", "rsrq": "RSRQ", "snr": "SNR", "rssi": "RSSI",
          "rscp": "RSCP", "ecio": "Ec/Io", "quality": "Qualité", "ber": "BER"}
# CSS des niveaux : classes GTK « level-N »
LEVEL_CSS = {4: "success", 3: "accent", 2: "warning", 1: "error", 0: "dim-label"}


def level_of(metric, value):
    if value is None:
        return 0
    for seuil, niveau in THRESHOLDS.get(metric, []):
        if value >= seuil:
            return niveau
    return 1


def fmt(value, unit="", digits=0):
    if value is None:
        return "—"
    if isinstance(value, float):
        return ("%%.%df %%s" % digits % (value, unit)).strip()
    return ("%s %s" % (value, unit)).strip()


# ── Le releve : un dictionnaire plat, une valeur par cle, None = inconnu ────
def empty_sample():
    return {
        "ts": time.time(), "present": False,
        "state": "absent", "rat": "", "rat_list": "",
        "operator": "", "opcode": "", "mcc": "", "mnc": "",
        "reg_cs": "", "reg_ps": "", "reg_eps": "", "packet": "",
        "lac": None, "tac": None, "cid": None,
        "arfcn": None, "earfcn": None, "pci": None, "bands": "",
        "quality": None, "rssi": None, "rsrp": None, "rsrq": None, "snr": None,
        "rscp": None, "ecio": None, "ber": None,
        "imei": "", "imsi": "", "number": "", "sim_op": "",
        "manufacturer": "", "model": "", "firmware": "", "driver": "",
        "neighbors": [],       # [{rat, mcc, mnc, lac/tac, cid, pci, arfcn, rsrp, rsrq, rssi, serving}]
        "cellinfo_supported": None,
    }


# ── Source ModemManager ─────────────────────────────────────────────────────
REG_NAMES = {
    0: "inactif", 1: "domestique", 2: "recherche", 3: "refusé", 4: "inconnu",
    5: "itinérance", 6: "domestique (SMS seul)", 7: "itinérance (SMS seul)",
    8: "domestique (CSFB non préféré)", 9: "itinérance (CSFB non préféré)",
    10: "attaché (RLOS)",
}
REG_OK = {1, 5, 6, 7, 8, 9}


class ModemSource:
    """Lit ModemManager. Renvoie un releve par appel a sample()."""

    def __init__(self, on_event):
        self.on_event = on_event
        self.manager = None
        self.obj = None
        self.modem = None
        self._setup_done = False
        self._cellinfo_supported = None
        self._last_reg = None
        self._last_cell = None
        self.error = None
        if MM is None:
            self.error = "libmm-glib (typelib ModemManager-1.0) introuvable"
            return
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            self.manager = MM.Manager.new_sync(
                bus, Gio.DBusObjectManagerClientFlags.DO_NOT_AUTO_START, None)
            self.manager.connect("object-added", lambda *_: self._pick())
            self.manager.connect("object-removed", lambda *_: self._pick())
            self._pick()
        except GLib.Error as e:
            self.error = "ModemManager injoignable : %s" % e.message

    def _pick(self):
        objs = self.manager.get_objects() if self.manager else []
        obj = objs[0] if objs else None
        if obj is not self.obj:
            self.obj = obj
            self.modem = obj.get_modem() if obj else None
            self._setup_done = False
            self._cellinfo_supported = None
            self.on_event("modem %s" % ("détecté : %s %s" % (self.modem.get_manufacturer(),
                          self.modem.get_model()) if self.modem else "retiré"))

    def _setup(self):
        """Une fois par modem : cadence du signal, localisation 3GPP."""
        if self._setup_done or not self.obj:
            return
        sig = self.obj.get_modem_signal()
        if sig:
            try:
                sig.setup_sync(POLL_S, None)
            except GLib.Error as e:
                self.on_event("signal : cadence refusée (%s)" % e.message)
        loc = self.obj.get_modem_location()
        if loc:
            try:
                want = int(loc.get_enabled()) | LOC_3GPP_LAC_CI
                if want != loc.get_enabled():
                    loc.setup_sync(want, False, None)
            except GLib.Error as e:
                self.on_event("localisation 3GPP refusée (%s)" % e.message)
        self._setup_done = True

    def sample(self):
        s = empty_sample()
        if self.error:
            s["state"] = self.error
            return s
        if not self.modem:
            s["state"] = "aucun modem"
            return s
        m = self.modem
        try:
            state = m.get_state()
        except GLib.Error:
            self._pick()
            return s
        s["present"] = True
        s["state"] = MM.ModemState.get_string(state) if hasattr(MM.ModemState, "get_string") \
            else MM.modem_state_get_string(state)
        s["manufacturer"] = m.get_manufacturer() or ""
        s["model"] = m.get_model() or ""
        s["firmware"] = m.get_revision() or ""
        s["driver"] = ", ".join(m.get_drivers() or [])
        s["imei"] = m.get_equipment_identifier() or ""
        nums = m.dup_own_numbers()
        s["number"] = ", ".join(nums) if nums else ""
        q, _recent = m.get_signal_quality()
        s["quality"] = int(q)
        act = m.get_access_technologies()
        s["rat_list"] = MM.modem_access_technology_build_string_from_mask(act) or ""
        s["rat"] = rat_family(act)
        try:
            bands = m.get_current_bands()
            s["bands"] = ", ".join(MM.ModemBand.get_string(b) if hasattr(MM.ModemBand, "get_string")
                                   else MM.modem_band_get_string(b) for b in bands) if bands else ""
        except Exception:
            s["bands"] = ""
        if state < MM.ModemState.ENABLED:
            return s
        self._setup()

        g = self.obj.get_modem_3gpp()
        if g:
            s["opcode"] = g.get_operator_code() or ""
            s["operator"] = g.get_operator_name() or ""
            reg = g.get_registration_state()
            s["reg_cs"] = REG_NAMES.get(int(reg), str(int(reg)))
            try:
                ps = g.get_packet_service_state()
                s["packet"] = {0: "inconnu", 1: "détaché", 2: "attaché"}.get(int(ps), str(int(ps)))
            except Exception:
                s["packet"] = ""
            s["reg_ps"] = s["packet"]
            s["reg_eps"] = s["reg_cs"] if "lte" in s["rat_list"] or "5gnr" in s["rat_list"] else ""
            if s["opcode"]:
                s["mcc"], s["mnc"] = s["opcode"][:3], s["opcode"][3:]
            if int(reg) != self._last_reg:
                self._last_reg = int(reg)
                self.on_event("enregistrement : %s (%s %s)" % (s["reg_cs"], s["opcode"], s["operator"]))
            self._cell_info(g, s)

        sim = None
        try:
            sim = m.get_sim_sync(None)
        except GLib.Error:
            pass
        if sim:
            s["imsi"] = sim.get_imsi() or ""
            s["sim_op"] = "%s %s" % (sim.get_operator_identifier() or "", sim.get_operator_name() or "")

        loc = self.obj.get_modem_location()
        if loc:
            try:
                l3 = loc.get_3gpp_sync(None)
            except GLib.Error:
                l3 = None
            if l3:
                s["mcc"] = s["mcc"] or ("%03d" % l3.get_mobile_country_code())
                mnc = l3.get_mobile_network_code()
                s["mnc"] = s["mnc"] or (mnc if isinstance(mnc, str) else "%02d" % mnc)
                lac, tac, cid = l3.get_location_area_code(), l3.get_tracking_area_code(), l3.get_cell_id()
                if lac not in (0, 0xFFFF, 0xFFFFFFFF):
                    s["lac"] = lac
                if tac not in (0, 0xFFFF, 0xFFFFFFFF):
                    s["tac"] = tac
                if cid not in (0, 0xFFFFFFFF):
                    s["cid"] = cid
                cell = (s["lac"], s["tac"], s["cid"])
                if s["cid"] is not None and cell != self._last_cell:
                    self._last_cell = cell
                    self.on_event("cellule : %s" % cell_label(s))

        sig = self.obj.get_modem_signal()
        if sig:
            lte = sig.peek_lte()
            if lte:
                s["rssi"] = sig_val(lte.get_rssi())
                s["rsrp"] = sig_val(lte.get_rsrp())
                s["rsrq"] = sig_val(lte.get_rsrq())
                s["snr"] = sig_val(lte.get_snr())
            umts = sig.peek_umts()
            if umts:
                s["rssi"] = s["rssi"] if s["rssi"] is not None else sig_val(umts.get_rssi())
                s["rscp"] = sig_val(umts.get_rscp())
                s["ecio"] = sig_val(umts.get_ecio())
            gsm = sig.peek_gsm()
            if gsm and s["rssi"] is None:
                s["rssi"] = sig_val(gsm.get_rssi())
                s["ber"] = sig_val(gsm.get_error_rate())
            nr = sig.peek_nr5g()
            if nr:
                s["rsrp"] = s["rsrp"] if s["rsrp"] is not None else sig_val(nr.get_rsrp())
                s["rsrq"] = s["rsrq"] if s["rsrq"] is not None else sig_val(nr.get_rsrq())
                s["snr"] = s["snr"] if s["snr"] is not None else sig_val(nr.get_snr())
        return s

    def _cell_info(self, g, s):
        """GetCellInfo (MM >= 1.20) : ARFCN, PCI, voisines. L AT generique
        repond « non supporte » : on retient la reponse et on n insiste pas."""
        if self._cellinfo_supported is False:
            s["cellinfo_supported"] = False
            return
        try:
            proxy = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SYSTEM, Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES, None,
                "org.freedesktop.ModemManager1", g.get_object_path(),
                "org.freedesktop.ModemManager1.Modem.Modem3gpp", None)
            res = proxy.call_sync("GetCellInfo", None, Gio.DBusCallFlags.NONE, 5000, None)
        except GLib.Error as e:
            if self._cellinfo_supported is None:
                self.on_event("cellules voisines : GetCellInfo indisponible (%s)" % e.message.split(":")[-1].strip())
            self._cellinfo_supported = False
            s["cellinfo_supported"] = False
            return
        self._cellinfo_supported = True
        s["cellinfo_supported"] = True
        cells = []
        for d in res.unpack()[0]:
            ct = {2: "gsm", 3: "umts", 4: "lte", 5: "nr"}.get(d.get("cell-type", 0), "?")
            c = {"rat": ct, "serving": bool(d.get("serving", False)),
                 "opcode": d.get("operator-id", ""),
                 "lac": hexint(d.get("lac")), "tac": hexint(d.get("tac")),
                 "cid": hexint(d.get("ci")), "pci": hexint(d.get("physical-ci")),
                 "arfcn": d.get("arfcn") or d.get("earfcn") or d.get("uarfcn") or d.get("nrarfcn"),
                 "rsrp": d.get("rsrp"), "rsrq": d.get("rsrq"), "rssi": d.get("rxlev") or d.get("rssi"),
                 "band": d.get("band")}
            cells.append(c)
            if c["serving"]:
                s["arfcn"] = c["arfcn"] if ct in ("gsm", "umts") else s["arfcn"]
                s["earfcn"] = c["arfcn"] if ct in ("lte", "nr") else s["earfcn"]
                s["pci"] = c["pci"]
        s["neighbors"] = cells

    def scan(self, callback):
        """Balayage des reseaux (AT+COPS=?) : long, asynchrone."""
        g = self.obj.get_modem_3gpp() if self.obj else None
        if not g:
            callback(None, "aucun modem")
            return

        def done(src, res):
            try:
                nets = g.scan_finish(res)
            except GLib.Error as e:
                callback(None, e.message)
                return
            out = []
            for n in nets:
                out.append({"code": n.get_operator_code() or "", "name": n.get_operator_long() or n.get_operator_short() or "",
                            "act": MM.modem_access_technology_build_string_from_mask(n.get_access_technology()),
                            "avail": MM.Modem3gppNetworkAvailability.get_string(n.get_availability())
                            if hasattr(MM.Modem3gppNetworkAvailability, "get_string")
                            else str(int(n.get_availability()))})
            callback(out, None)
        g.scan(None, done)


def sig_val(v):
    """Valeur libmm-glib : MM_SIGNAL_UNKNOWN (= -G_MAXDOUBLE) veut dire « pas de mesure »."""
    if v is None or v < -1e300 or math.isnan(v):
        return None
    return float(v)


def hexint(v):
    if v in (None, ""):
        return None
    try:
        return int(str(v), 16)
    except ValueError:
        return None


def rat_family(mask):
    if MM is None:
        return ""
    if mask & ACT_5GNR:
        return "nr"
    if mask & MM.ModemAccessTechnology.LTE:
        return "lte"
    if mask & (MM.ModemAccessTechnology.UMTS | MM.ModemAccessTechnology.HSDPA | MM.ModemAccessTechnology.HSUPA
               | MM.ModemAccessTechnology.HSPA | MM.ModemAccessTechnology.HSPA_PLUS):
        return "umts"
    if mask & (MM.ModemAccessTechnology.GSM | MM.ModemAccessTechnology.GSM_COMPACT
               | MM.ModemAccessTechnology.GPRS | MM.ModemAccessTechnology.EDGE):
        return "gsm"
    return ""


RAT_TITLE = {"nr": "5G NR", "lte": "4G LTE", "umts": "3G UMTS", "gsm": "2G GSM", "": "—"}


def cell_label(s):
    if s["cid"] is None:
        return "—"
    if s["tac"] is not None:
        return "TAC %04X  ECI %08X" % (s["tac"], s["cid"])
    if s["lac"] is not None:
        return "LAC %04X  CI %04X" % (s["lac"], s["cid"])
    return "CI %X" % s["cid"]


def primary_metric(s):
    """La mesure « principale » selon la technologie, comme NSG en tete."""
    if s["rsrp"] is not None:
        return "rsrp", s["rsrp"]
    if s["rscp"] is not None:
        return "rscp", s["rscp"]
    if s["rssi"] is not None:
        return "rssi", s["rssi"]
    if s["quality"]:
        return "quality", s["quality"]
    return "rsrp", None


# ── Source de demonstration ─────────────────────────────────────────────────
class DemoSource:
    def __init__(self, on_event):
        self.on_event = on_event
        self.t0 = time.time()
        self.rsrp, self.rsrq, self.snr, self.rssi = -85.0, -11.0, 15.0, -70.0
        self.gsm_until = 0
        on_event("mode démonstration : valeurs simulées")

    def _walk(self, v, lo, hi, step):
        return max(lo, min(hi, v + random.uniform(-step, step)))

    def sample(self):
        s = empty_sample()
        s.update(present=True, state="registered", manufacturer="Demo", model="virtuel",
                 firmware="1.0", driver="demo", imei="350000000000000", imsi="001010000000001",
                 number="100101", operator="Osmocom", opcode="00101", mcc="001", mnc="01",
                 sim_op="00101 Osmocom", packet="attaché", reg_ps="attaché")
        now = time.time()
        if now > self.gsm_until and random.random() < 0.01:
            self.gsm_until = now + 20
            self.on_event("CSFB : passage en 2G pour un service circuit")
        self.rsrp = self._walk(self.rsrp, -120, -60, 2)
        self.rsrq = self._walk(self.rsrq, -20, -5, 0.7)
        self.snr = self._walk(self.snr, -5, 30, 1.5)
        self.rssi = self._walk(self.rssi, -100, -55, 1.5)
        if now < self.gsm_until:
            s.update(rat="gsm", rat_list="gsm", reg_cs="domestique", reg_eps="recherche",
                     lac=0x0001, cid=0x0001, arfcn=514, bands="egsm",
                     rssi=self.rssi, ber=0.2,
                     quality=int((self.rssi + 110) * 100 / 60))
            s["neighbors"] = [{"rat": "gsm", "serving": True, "opcode": "00101", "lac": 1, "tac": None,
                               "cid": 1, "pci": None, "arfcn": 514, "rsrp": None, "rsrq": None,
                               "rssi": self.rssi, "band": "egsm"}]
        else:
            s.update(rat="lte", rat_list="lte", reg_cs="domestique", reg_eps="domestique",
                     tac=0x0007, cid=0x019B01, earfcn=3350, pci=1, bands="eutran-7",
                     rssi=self.rssi, rsrp=self.rsrp, rsrq=self.rsrq, snr=self.snr,
                     quality=int(max(0, min(100, (self.rsrp + 120) * 100 / 60))))
            s["neighbors"] = [
                {"rat": "lte", "serving": True, "opcode": "00101", "lac": None, "tac": 7, "cid": 0x019B01,
                 "pci": 1, "arfcn": 3350, "rsrp": self.rsrp, "rsrq": self.rsrq, "rssi": self.rssi, "band": "eutran-7"},
                {"rat": "lte", "serving": False, "opcode": "00101", "lac": None, "tac": 7, "cid": None,
                 "pci": 2, "arfcn": 3350, "rsrp": self.rsrp - 8, "rsrq": self.rsrq - 3, "rssi": None, "band": None},
                {"rat": "gsm", "serving": False, "opcode": "00101", "lac": 1, "tac": None, "cid": 1,
                 "pci": None, "arfcn": 514, "rsrp": None, "rsrq": None, "rssi": self.rssi - 10, "band": None},
            ]
        s["cellinfo_supported"] = True
        return s

    def scan(self, callback):
        GLib.timeout_add(1500, lambda: callback([{"code": "00101", "name": "Osmocom", "act": "lte", "avail": "current"},
                                                 {"code": "00101", "name": "Osmocom", "act": "gsm", "avail": "available"}],
                                                None) or False)


# ── Les trames : le WebSocket du dashboard ──────────────────────────────────
class FrameSource:
    """Se branche sur osmo-egprs-web comme le fait le navigateur : le serveur
    envoie « init », on lui demande « tshark_start », et chaque trame decodee
    arrive en { type: "packet", data: {...} } (protocol, info, arfcn, uplink,
    channel, timeslot, fn, opLabel, l2, l3, cc, rr, length, sctpPorts). La
    capture tshark est PAR CLIENT cote serveur : fermer le lien l arrete."""

    def __init__(self, urls, on_frame, on_status):
        self.urls, self.on_frame, self.on_status = list(urls), on_frame, on_status
        self.conn = None
        self.url = None
        self.tries = 0
        self.stopped = False
        self.session = None
        self.waiting = {}                   # frameNum -> rappel, pour la dissection et l hexa
        if Soup is None:
            self.on_status("libsoup 3 absent (apk add libsoup3) : pas de trames", False)
            return
        if not self.urls:
            self.on_status("aucune adresse de dashboard (NETMON_FRAMES_URL)", False)
            return
        self.session = Soup.Session()
        self.session.set_timeout(8)
        self._connect()

    def _connect(self):
        if self.stopped:
            return False
        url = self.urls[self.tries % len(self.urls)]
        self.tries += 1
        msg = Soup.Message.new("GET", url)
        if msg is None:
            self.on_status("adresse invalide : %s" % url, False)
            self._retry()
            return False
        self.on_status("connexion à %s…" % url, False)
        self.session.websocket_connect_async(msg, None, None, GLib.PRIORITY_DEFAULT, None, self._connected, url)
        return False

    def _connected(self, session, res, url):
        try:
            conn = session.websocket_connect_finish(res)
        except GLib.Error as e:
            self.on_status("dashboard injoignable (%s) : %s" % (url, e.message), False)
            self._retry()
            return
        self.conn, self.url = conn, url
        conn.set_max_incoming_payload_size(4 << 20)
        conn.connect("message", self._message)
        conn.connect("closed", self._closed)
        conn.connect("error", lambda _c, err: self.on_status("erreur WebSocket : %s" % err.message, False))
        conn.send_text(json.dumps({"type": "tshark_start"}))
        self.tries = 0
        self.on_status("dashboard %s : capture demandée" % url, True)

    def _retry(self):
        # Une adresse apres l autre, vite ; toutes epuisees, on respire.
        delay = 2 if self.tries % len(self.urls) else 10
        GLib.timeout_add_seconds(delay, self._connect)

    def _message(self, _conn, mtype, data):
        if mtype != Soup.WebsocketDataType.TEXT:
            return
        try:
            m = json.loads(bytes(data.get_data()).decode("utf-8", "replace"))
        except ValueError:
            return
        t = m.get("type")
        if t == "packet":
            self.on_frame(m.get("data") or {})
        elif t in ("packet_tree", "packet_hex"):
            d = m.get("data") or {}
            cb = self.waiting.get(d.get("frameNum"))
            if cb:
                cb("tree" if t == "packet_tree" else "hex", d.get("text") if t == "packet_tree" else d.get("hex"))
        elif t == "tshark_stopped":
            self.on_status("capture arrêtée côté dashboard (code %s), on la redemande" % (m.get("data") or {}).get("code"), True)
            GLib.timeout_add_seconds(3, self._restart)
        elif t == "tshark_status":
            d = m.get("data") or {}
            if d.get("active") is False and self.conn:
                GLib.timeout_add_seconds(3, self._restart)

    def _restart(self):
        if self.conn and not self.stopped:
            self.conn.send_text(json.dumps({"type": "tshark_start"}))
        return False

    def request(self, frame_num, cb):
        """Le contenu complet : la dissection (tshark -V) et l hexa, lus par
        le dashboard dans le pcap de NOTRE capture. cb(kind, text)."""
        if not self.conn:
            cb("tree", None)
            return
        self.waiting[frame_num] = cb
        self.conn.send_text(json.dumps({"type": "packet_tree_request", "frameNum": frame_num}))
        self.conn.send_text(json.dumps({"type": "packet_hex_request", "frameNum": frame_num}))
        GLib.timeout_add_seconds(30, lambda: self.waiting.pop(frame_num, None) and False)

    def _closed(self, _conn):
        self.conn = None
        if not self.stopped:
            self.on_status("lien avec le dashboard perdu, nouvel essai…", False)
            self._retry()

    def stop(self):
        self.stopped = True
        if self.conn:
            try:
                self.conn.close(Soup.WebsocketCloseCode.NORMAL, None)
            except GLib.Error:
                pass


class DemoFrameSource:
    """Des trames plausibles, pour voir la page sans banc."""
    SAMPLES = [
        ("LAPDm", True, "SDCCH8", "1", "SAPI0 SABM", "", "", "MM", "CM SERVICE REQUEST"),
        ("LAPDm", False, "SDCCH8", "1", "SAPI0 UA", "", "", "MM", "CM SERVICE REQUEST"),
        ("DTAP", False, "SDCCH8", "1", "SAPI0 I N(S)=0 N(R)=1", "", "", "MM", "AUTHENTICATION REQUEST"),
        ("DTAP", True, "SDCCH8", "1", "SAPI0 I N(S)=1 N(R)=1", "", "", "MM", "AUTHENTICATION RESPONSE"),
        ("DTAP", False, "SDCCH8", "1", "SAPI0 I N(S)=1 N(R)=2", "", "", "RR", "CIPHERING MODE COMMAND"),
        ("DTAP", True, "SDCCH8", "1", "SAPI0 I N(S)=2 N(R)=2", "SETUP", "", "CC", "SETUP (to 100102)"),
        ("DTAP", False, "SDCCH8", "1", "SAPI0 I N(S)=2 N(R)=3", "CALL PROCEEDING", "", "CC", "CALL PROCEEDING"),
        ("DTAP", False, "SDCCH8", "1", "SAPI0 I N(S)=3 N(R)=3", "", "ASSIGNMENT CMD TS2 TA0", "RR", "ASSIGNMENT COMMAND"),
        ("LAPDm", True, "TCH/F", "2", "SAPI0 SABM", "", "", "", ""),
        ("DTAP", True, "TCH/F", "2", "SAPI0 I N(S)=0 N(R)=0", "", "ASSIGNMENT COMPLETE", "RR", "ASSIGNMENT COMPLETE"),
        ("DTAP", False, "TCH/F", "2", "SAPI0 I N(S)=0 N(R)=1", "ALERTING", "", "CC", "ALERTING"),
        ("DTAP", False, "TCH/F", "2", "SAPI0 I N(S)=1 N(R)=1", "CONNECT", "", "CC", "CONNECT"),
        ("GSM_SMS", False, "SDCCH8", "3", "SAPI3 I N(S)=0 N(R)=0", "", "", "SMS", "RP-DATA (Network to MS) SMS-DELIVER"),
        ("DTAP", True, "SDCCH8", "3", "SAPI0 I N(S)=0 N(R)=1", "", "MEAS REPORT", "RR", "MEASUREMENT REPORT"),
        ("GSMTAP", False, "BCCH", "0", "", "", "", "", "SYSTEM INFORMATION TYPE 3"),
        ("GSMTAP", False, "CCCH", "0", "", "", "PAGING REQ 1", "RR", "PAGING REQUEST TYPE 1"),
        ("DTAP", False, "TCH/F", "2", "SAPI0 I N(S)=4 N(R)=5", "DISCONNECT", "", "CC", "DISCONNECT"),
        ("DTAP", False, "SDCCH8", "1", "SAPI0 I N(S)=5 N(R)=6", "", "CHANNEL RELEASE", "RR", "CHANNEL RELEASE"),
        ("MAC-LTE", True, "", "", "", "", "", "", "RACH Preamble chosen"),
        ("LTE RRC", True, "", "", "", "", "", "", "RRCConnectionRequest"),
        ("LTE RRC", False, "", "", "", "", "", "", "RRCConnectionSetup"),
        ("NAS-EPS", True, "", "", "", "", "", "", "RRCConnectionSetupComplete, Attach request, PDN connectivity request"),
        ("S1AP", None, "", "", "", "", "", "", "InitialUEMessage, Attach request, PDN connectivity request"),
        ("NAS-EPS", False, "", "", "", "", "", "", "DLInformationTransfer, Authentication request"),
        ("NAS-EPS", True, "", "", "", "", "", "", "ULInformationTransfer, Authentication response"),
        ("LTE RRC", False, "", "", "", "", "", "", "SecurityModeCommand"),
        ("NAS-EPS", False, "", "", "", "", "", "", "RRCConnectionReconfiguration, Attach accept, Activate default EPS bearer context request"),
        ("MAC-LTE", False, "", "", "", "", "", "", "(DL-SCH) (SDU) LCID 3 (12 bytes)"),
        ("S1AP", None, "", "", "", "", "", "", "InitialContextSetupRequest, Attach accept, Activate default EPS bearer context request"),
        ("M3UA", None, "", "", "", "", "", "", "SCCP CR (BSSMAP Complete Layer 3 Information, Paging Response)"),
    ]

    def __init__(self, urls, on_frame, on_status):
        self.on_frame, self.on_status = on_frame, on_status
        self.i = 0
        self.fn = 1_200_000
        on_status("mode démonstration : trames simulées", True)
        GLib.timeout_add(450, self._emit)

    def _emit(self):
        proto, ul, chan, ts, l2, cc, rr, l3, info = self.SAMPLES[self.i % len(self.SAMPLES)]
        self.i += 1
        self.fn += random.randint(4, 60)
        d = {"frameNum": self.i, "ts": time.time(), "protocol": proto, "info": info, "length": random.randint(23, 80),
             "l2": l2, "cc": cc, "rr": rr, "l3": l3}
        if ul is None:
            d.update(arfcn="", uplink=False, channel="", timeslot="", fn="",
                     direction="OP1→MSC", opLabel="OP1", src="127.0.1.10", dst="127.0.0.1", sctpPorts="2905 → 2905")
        elif proto in ("MAC-LTE", "LTE RRC", "NAS-EPS"):
            d.update(arfcn="", uplink=ul, channel="", timeslot="", fn="", direction="UL" if ul else "DL",
                     opLabel="4G", rnti="0x46", sfn="%d.%d" % (self.fn % 1024, self.fn % 10))
        else:
            d.update(arfcn="514", uplink=ul, channel=chan, timeslot=ts, fn=str(self.fn),
                     direction="UL" if ul else "DL", opLabel="OP1")
        self.on_frame(d)
        return True

    def request(self, frame_num, cb):
        GLib.timeout_add(300, lambda: cb("tree", "Frame %d: 83 bytes on wire\n    (simulation : la dissection "
                                         "complete vient de tshark sur l hote, via le dashboard)\nGSM TAP Header\n"
                                         "    Version: 2\n    Channel Type: SDCCH/8\nLAPDm\n    Address Field\n"
                                         "        SAPI: 0\n    Control field: I, N(R)=1, N(S)=0\nGSM A-I/F DTAP\n"
                                         "    Protocol Discriminator: Mobility Management\n    Message Type: 0x24"
                                         % frame_num) or False)
        GLib.timeout_add(400, lambda: cb("hex", "0204040b0b0a0c0101000000000000000101" * 4) or False)

    def stop(self):
        pass


class FrameItem(GObject.Object):
    """Une trame telle que le dashboard la decode, prete a afficher."""

    def __init__(self, d):
        super().__init__()
        self.d = d
        ts = d.get("ts") or time.time()
        self.when = time.strftime("%H:%M:%S", time.localtime(ts)) + ".%03d" % int((ts % 1) * 1000)
        self.proto = d.get("protocol") or "?"
        self.direction = d.get("direction") or ("UL" if d.get("uplink") else "DL" if d.get("arfcn") else "")
        where = []
        if d.get("channel"):
            where.append(d["channel"])
        if d.get("timeslot") not in (None, ""):
            where.append("TS%s" % d["timeslot"])
        if d.get("arfcn"):
            where.append("ARFCN %s" % d["arfcn"])
        if d.get("sctpPorts"):
            where.append(d["sctpPorts"])
        if d.get("rnti"):
            where.append("RNTI %s" % d["rnti"])
        if d.get("sfn"):
            where.append("SFN %s" % d["sfn"])
        if d.get("opLabel") and d["opLabel"] != "—" and "NaN" not in d["opLabel"]:
            where.append(d["opLabel"])          # « ANaN » : le dashboard n avait pas d ARFCN
        self.where = "  ".join(where)
        self.info = d.get("info") or ""
        rr = d.get("rr") or ""
        if rr.startswith("RR 0x"):
            try:
                rr = RR_NAMES.get(int(rr[5:].split()[0], 16), rr)
            except ValueError:
                pass
        l3 = d.get("l3") or ""
        if l3:
            l3 += ": " + (d.get("cc") or rr) if (d.get("cc") or rr) else ""
        detail = [x for x in (d.get("l2"), l3,
                              ("FN %s" % d["fn"]) if d.get("fn") else "", ("%s o" % d["length"]) if d.get("length") else "") if x]
        self.detail = " · ".join(detail)
        self.haystack = " ".join((self.proto, self.direction, self.where, self.info, self.detail,
                                  d.get("src") or "", d.get("dst") or "")).lower()
        self.kind = frame_kind(d, self.proto, l3)


def frame_kind(d, proto, l3):
    """La famille de la trame, pour sa couleur : ce que NSG et Wireshark font
    avec leurs regles de coloration. Une seule par trame, la plus parlante."""
    info = (d.get("info") or "").lower()
    if proto == "NAS-EPS":
        return "nas"
    if proto == "LTE RRC":
        return "rrc"
    if proto in ("MAC-LTE", "RLC-LTE", "PDCP-LTE"):
        return "lte"
    if proto == "GSM_SMS" or l3.startswith("SMS"):
        return "sms"
    if d.get("cc"):
        return "cc"
    if l3.startswith("MM"):
        return "mm"
    chan = d.get("channel") or ""
    if chan in ("BCCH", "CCCH") or "system information" in info or "paging request" in info:
        return "bcch"
    if l3.startswith("RR"):
        return "rr"
    if proto == "LAPDm":
        return "lapdm"
    if proto in ("S1AP", "M3UA", "SCTP", "DIAMETER", "GTPv2", "GTP", "SCCP", "BSSAP", "BSSMAP") or d.get("sctpPorts"):
        return "core"
    if info.startswith("mon:"):
        return "mon"
    return "other"


# ── Widgets ─────────────────────────────────────────────────────────────────
class Bars(Gtk.DrawingArea):
    """Les cinq barres du niveau de signal."""

    def __init__(self):
        super().__init__()
        self.level = 0
        self.set_size_request(44, 30)
        self.set_draw_func(self._draw)

    def set_level(self, level):
        if level != self.level:
            self.level = level
            self.queue_draw()

    def _draw(self, _area, cr, w, h):
        color = self.get_color()
        n, gap = 5, 3
        bw = (w - gap * (n - 1)) / n
        for i in range(n):
            bh = h * (0.35 + 0.65 * i / (n - 1))
            x, y = i * (bw + gap), h - bh
            lit = i < self.level
            cr.set_source_rgba(color.red, color.green, color.blue, 1.0 if lit else 0.25)
            cr.rectangle(x, y, bw, bh)
            cr.fill()


class Graph(Gtk.DrawingArea):
    """Historique d une mesure, trace au cairo."""

    def __init__(self, history):
        super().__init__()
        self.history = history
        self.metric = "rsrp"
        self.set_size_request(-1, 180)
        self.set_draw_func(self._draw)

    def set_metric(self, metric):
        self.metric = metric
        self.queue_draw()

    def _draw(self, _area, cr, w, h):
        fg = self.get_color()
        lo, hi = RANGES.get(self.metric, (-120, 0))
        pad_l, pad_r, pad_t, pad_b = 44, 10, 10, 20
        gw, gh = w - pad_l - pad_r, h - pad_t - pad_b
        cr.set_line_width(1)
        cr.select_font_face("Sans")
        cr.set_font_size(11)
        # grille et graduations
        for k in range(5):
            y = pad_t + gh * k / 4
            v = hi - (hi - lo) * k / 4
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.15)
            cr.move_to(pad_l, y); cr.line_to(w - pad_r, y); cr.stroke()
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.7)
            txt = "%d" % v
            ext = cr.text_extents(txt)
            cr.move_to(pad_l - 6 - ext.width, y + 4); cr.show_text(txt)
        now = time.time()
        for k, label in ((0, "-10 min"), (0.5, "-5"), (1, "0")):
            x = pad_l + gw * k
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.7)
            ext = cr.text_extents(label)
            cr.move_to(min(max(x - ext.width / 2, pad_l), w - pad_r - ext.width), h - 5); cr.show_text(label)
        pts = [(t, v) for (t, v) in self.history.get(self.metric, ()) if v is not None and now - t <= HISTORY_S]
        if len(pts) < 2:
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.5)
            txt = "pas encore de mesure"
            ext = cr.text_extents(txt)
            cr.move_to(pad_l + (gw - ext.width) / 2, pad_t + gh / 2); cr.show_text(txt)
            return
        cr.set_source_rgba(0.208, 0.518, 0.894, 1)   # bleu accent GNOME
        cr.set_line_width(2)
        first = True
        for t, v in pts:
            x = pad_l + gw * (1 - (now - t) / HISTORY_S)
            y = pad_t + gh * (1 - (max(lo, min(hi, v)) - lo) / (hi - lo))
            (cr.move_to if first else cr.line_to)(x, y)
            first = False
        cr.stroke()
        # aire sous la courbe
        cr.set_source_rgba(0.208, 0.518, 0.894, 0.15)
        first = True
        for t, v in pts:
            x = pad_l + gw * (1 - (now - t) / HISTORY_S)
            y = pad_t + gh * (1 - (max(lo, min(hi, v)) - lo) / (hi - lo))
            (cr.move_to if first else cr.line_to)(x, y)
            first = False
        cr.line_to(pad_l + gw, pad_t + gh); cr.line_to(pad_l + gw * (1 - (now - pts[0][0]) / HISTORY_S), pad_t + gh)
        cr.close_path(); cr.fill()


class MetricRow(Adw.ActionRow):
    """Une mesure : nom, valeur a droite coloree par niveau, barre en dessous."""

    def __init__(self, metric):
        super().__init__(title=LABELS[metric])
        self.metric = metric
        self.value = Gtk.Label(css_classes=["numeric", "title-3"])
        self.add_suffix(self.value)
        self.bar = Gtk.LevelBar(min_value=0, max_value=4, mode=Gtk.LevelBarMode.DISCRETE,
                                valign=Gtk.Align.CENTER, hexpand=True, margin_top=4)
        self.bar.set_size_request(80, 8)
        for name in ("low", "high", "full"):        # les paliers par defaut, remplaces par les notres
            self.bar.remove_offset_value(name)
        self.bar.add_offset_value("lvl-1", 1); self.bar.add_offset_value("lvl-2", 2)
        self.bar.add_offset_value("lvl-3", 3); self.bar.add_offset_value("lvl-4", 4)
        self.bar.add_css_class("metric-bar")
        self.add_suffix(self.bar)

    def update(self, value):
        unit = UNITS.get(self.metric, "")
        self.value.set_text(fmt(value, unit, 1 if isinstance(value, float) and self.metric != "quality" else 0))
        lvl = level_of(self.metric, value) if self.metric != "quality" else \
            (0 if value is None else max(1, min(4, math.ceil(value / 25))))
        self.bar.set_value(lvl)
        for cls in ("success", "accent", "warning", "error", "dim-label"):
            self.value.remove_css_class(cls)
        self.value.add_css_class(LEVEL_CSS[lvl])


class InfoRow(Adw.ActionRow):
    def __init__(self, title, mono=False):
        super().__init__(title=title)
        # Pas de retour a la ligne : dans une AdwActionRow le suffixe ne
        # recoit que sa largeur minimale, et un label qui replie se retrouve
        # coupe lettre par lettre (« Os-mo-com »). Points de suspension.
        self.value = Gtk.Label(css_classes=["dim-label"] + (["monospace"] if mono else []),
                               xalign=1, ellipsize=Pango.EllipsizeMode.END, max_width_chars=26)
        self.add_suffix(self.value)

    def set(self, text):
        self.value.set_text(text if text not in (None, "") else "—")


CSS = b"""
.hero { padding: 18px; border-radius: 12px; }
.hero-rat { font-size: 30px; font-weight: 800; }
.hero-metric { font-size: 44px; font-weight: 300; }
.metric-bar block.filled { background-color: @accent_bg_color; }
.metric-bar block.filled.lvl-1 { background-color: @error_bg_color; }
.metric-bar block.filled.lvl-2 { background-color: @warning_bg_color; }
.metric-bar block.filled.lvl-3 { background-color: @accent_bg_color; }
.metric-bar block.filled.lvl-4 { background-color: @success_bg_color; }
.metric-bar block { min-height: 8px; }
.neighbor-serving { font-weight: bold; }
/* Les trames : une couleur par famille (fond de la ligne, teinte legere qui
   passe en clair comme en sombre) et le sens sur le bord gauche - orange le
   mobile parle (UL), bleu le reseau parle (DL). Le badge reprend la teinte. */
.frame-row { padding: 6px 10px 6px 8px; border-bottom: 1px solid alpha(currentColor, 0.1);
             border-left: 4px solid transparent; }
.frame-row.dir-ul { border-left-color: #e5a50a; }
.frame-row.dir-dl { border-left-color: #3584e4; }
.frame-row.kind-bcch  { background-color: alpha(#77767b, 0.22); }
.frame-row.kind-lapdm { background-color: alpha(#3584e4, 0.38); }
.frame-row.kind-mm    { background-color: alpha(#9141ac, 0.40); }
.frame-row.kind-rr    { background-color: alpha(#26a269, 0.40); }
.frame-row.kind-cc    { background-color: alpha(#e66100, 0.42); }
.frame-row.kind-sms   { background-color: alpha(#f5c211, 0.45); }
.frame-row.kind-core  { background-color: alpha(#2190a4, 0.40); }
.frame-row.kind-lte   { background-color: alpha(#cdab8f, 0.42); }
.frame-row.kind-rrc   { background-color: alpha(#c061cb, 0.42); }
.frame-row.kind-nas   { background-color: alpha(#ed333b, 0.40); }
.frame-row.kind-mon   { opacity: 0.7; }
.proto { font-size: 11px; font-weight: 700; padding: 1px 6px; border-radius: 6px;
         background-color: alpha(currentColor, 0.15); }
.proto.kind-bcch  { background-color: alpha(#77767b, 0.55); }
.proto.kind-lapdm { background-color: alpha(#3584e4, 0.75); }
.proto.kind-mm    { background-color: alpha(#9141ac, 0.75); }
.proto.kind-rr    { background-color: alpha(#26a269, 0.75); }
.proto.kind-cc    { background-color: alpha(#e66100, 0.80); }
.proto.kind-sms   { background-color: alpha(#f5c211, 0.80); }
.proto.kind-core  { background-color: alpha(#2190a4, 0.75); }
.proto.kind-lte   { background-color: alpha(#cdab8f, 0.80); }
.proto.kind-rrc   { background-color: alpha(#c061cb, 0.80); }
.proto.kind-nas   { background-color: alpha(#ed333b, 0.75); }
.frame-text { font-family: monospace; font-size: 11px; }
label.dir-ul { color: #e5a50a; font-weight: 700; }   /* l etiquette seule : la ligne garde sa couleur de texte */
label.dir-dl { color: #3584e4; font-weight: 700; }
"""
KINDS = ("bcch", "lapdm", "mm", "rr", "cc", "sms", "core", "lte", "rrc", "nas", "mon", "other")


# ── La fenetre ──────────────────────────────────────────────────────────────
class Window(Adw.ApplicationWindow):
    def __init__(self, app, source, frame_source_cls=None, frames_urls=()):
        super().__init__(application=app, title="Signal", default_width=380, default_height=740)
        self.source = source
        self.frames = Gio.ListStore.new(FrameItem)
        self.frames_total = 0
        self.frames_pending = []            # en pause : on garde, on affiche a la reprise
        self.frames_status = ("pas de source de trames", False)
        self.frames_csv = None
        self.history = {k: collections.deque(maxlen=HISTORY_S // POLL_S + 5)
                        for k in ("rsrp", "rsrq", "snr", "rssi", "rscp", "ecio", "quality")}
        self.events = Gio.ListStore.new(EventItem)
        self.csv = None
        self.csv_path = None
        self.last = empty_sample()

        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)
        view = Adw.ToolbarView()
        self.toasts.set_child(view)

        header = Adw.HeaderBar()
        self.stack = Adw.ViewStack()
        # En-tete : le commutateur de pages quand la fenetre est large, un
        # simple titre sur un telephone (le commutateur passe alors en bas).
        self.title_stack = Gtk.Stack(hhomogeneous=False, vhomogeneous=False)
        self.title_stack.add_named(Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE), "switcher")
        self.title_stack.add_named(Adw.WindowTitle(title="Signal"), "title")
        header.set_title_widget(self.title_stack)
        menu = Gio.Menu()
        menu.append("Balayer les réseaux", "win.scan")
        menu.append("Enregistrer en CSV", "win.record")
        menu.append("Effacer le journal", "win.clear")
        menu.append("À propos", "win.about")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu))
        self.rec_icon = Gtk.Image(icon_name="media-record-symbolic", visible=False, css_classes=["error"])
        header.pack_end(self.rec_icon)
        view.add_top_bar(header)
        view.set_content(self.stack)
        self.switcher_bar = Adw.ViewSwitcherBar(stack=self.stack, reveal=True)
        view.add_bottom_bar(self.switcher_bar)
        # etroit (telephone) : commutateur en bas ; large : dans l en-tete
        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 550sp"))
        bp.add_setter(self.switcher_bar, "reveal", True)
        bp.add_setter(self.title_stack, "visible-child-name", "title")
        self.add_breakpoint(bp)
        self.switcher_bar.set_reveal(False)

        self.stack.add_titled_with_icon(self._page_cell(), "cell", "Cellule", "network-cellular-symbolic")
        self.stack.add_titled_with_icon(self._page_signal(), "signal", "Signal", "network-cellular-signal-good-symbolic")
        self.stack.add_titled_with_icon(self._page_neighbors(), "neighbors", "Voisines", "view-list-symbolic")
        self.stack.add_titled_with_icon(self._page_frames(), "frames", "Trames", "network-transmit-receive-symbolic")
        self.stack.add_titled_with_icon(self._page_log(), "log", "Journal", "document-open-recent-symbolic")

        for name, cb in (("scan", self.on_scan), ("clear", lambda *_: self.events.remove_all()),
                         ("about", self.on_about)):
            a = Gio.SimpleAction.new(name, None); a.connect("activate", cb); self.add_action(a)
        rec = Gio.SimpleAction.new_stateful("record", None, GLib.Variant.new_boolean(False))
        rec.connect("change-state", self.on_record); self.add_action(rec)

        self.frame_source = frame_source_cls(frames_urls, self.add_frame, self.on_frames_status) if frame_source_cls else None
        self.connect("close-request", lambda *_: (self.frame_source and self.frame_source.stop()) and False)

        self.tick()
        GLib.timeout_add_seconds(POLL_S, self.tick)

    # ── pages ──
    def _scroll(self, child):
        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        clamp = Adw.Clamp(maximum_size=640, child=child, margin_start=12, margin_end=12, margin_top=12, margin_bottom=12)
        sc.set_child(clamp)
        return sc

    def _hero(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, css_classes=["card", "hero"])
        top = Gtk.Box(spacing=12)
        self.h_rat = Gtk.Label(label="—", css_classes=["hero-rat"], xalign=0, hexpand=True)
        self.h_bars = Bars()
        top.append(self.h_rat); top.append(self.h_bars)
        box.append(top)
        self.h_op = Gtk.Label(label="", xalign=0, css_classes=["title-4"], ellipsize=Pango.EllipsizeMode.END)
        self.h_reg = Gtk.Label(label="", xalign=0, css_classes=["dim-label"], ellipsize=Pango.EllipsizeMode.END)
        self.h_metric = Gtk.Label(label="", xalign=0, css_classes=["hero-metric", "numeric"])
        self.h_metric_name = Gtk.Label(label="", xalign=0, css_classes=["dim-label", "caption"])
        box.append(self.h_op); box.append(self.h_reg); box.append(self.h_metric); box.append(self.h_metric_name)
        return box

    def _page_cell(self):
        v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        v.append(self._hero())
        g = Adw.PreferencesGroup(title="Cellule serveuse")
        self.r_cell = {}
        for key, title, mono in (("plmn", "PLMN (MCC / MNC)", True), ("operator", "Opérateur", False),
                                 ("lac", "LAC", True), ("tac", "TAC", True), ("cid", "Cell ID", True),
                                 ("enb", "eNB / secteur", True), ("arfcn", "ARFCN", True), ("earfcn", "EARFCN", True),
                                 ("pci", "PCI", True), ("bands", "Bandes", False)):
            r = InfoRow(title, mono); self.r_cell[key] = r; g.add(r)
        v.append(g)
        g2 = Adw.PreferencesGroup(title="Enregistrement")
        for key, title in (("rat_list", "Technologie d'accès"), ("reg_cs", "Circuit (CS)"),
                           ("reg_eps", "EPS (LTE)"), ("packet", "Service paquet"), ("state", "État du modem")):
            r = InfoRow(title); self.r_cell[key] = r; g2.add(r)
        v.append(g2)
        g3 = Adw.PreferencesGroup(title="Terminal et SIM")
        for key, title, mono in (("model", "Modem", False), ("firmware", "Micrologiciel", False), ("driver", "Pilote", False),
                                 ("imei", "IMEI", True), ("imsi", "IMSI", True), ("sim_op", "Opérateur SIM", False),
                                 ("number", "Numéro", True)):
            r = InfoRow(title, mono); self.r_cell[key] = r; g3.add(r)
        v.append(g3)
        return self._scroll(v)

    def _page_signal(self):
        v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        gg = Adw.PreferencesGroup(title="Historique (10 min)")
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["card"], spacing=6)
        self.graph = Graph(self.history)
        self.graph.set_margin_top(6); self.graph.set_margin_start(6); self.graph.set_margin_end(6)
        self.metric_sel = Gtk.DropDown.new_from_strings([LABELS[k] for k in self.history])
        self.metric_sel.set_margin_start(8); self.metric_sel.set_margin_end(8); self.metric_sel.set_margin_bottom(8)
        self.metric_sel.connect("notify::selected", self._on_metric_chosen)
        self.metric_chosen = False      # l utilisateur a-t-il choisi lui-meme la mesure tracee ?
        card.append(self.graph); card.append(self.metric_sel)
        gg.add(card)
        v.append(gg)
        g = Adw.PreferencesGroup(title="Mesures")
        self.r_metric = {}
        for k in ("rsrp", "rsrq", "snr", "rssi", "rscp", "ecio", "quality"):
            r = MetricRow(k); self.r_metric[k] = r; g.add(r)
        self.r_ber = InfoRow("BER"); g.add(self.r_ber)
        v.append(g)
        return self._scroll(v)

    def _page_neighbors(self):
        v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.n_group = Adw.PreferencesGroup(title="Cellules vues par le modem",
                                            description="Serveuse en gras. Demande GetCellInfo (QMI/MBIM) ; "
                                                        "un modem AT générique ne la fournit pas.")
        self.n_rows = []
        self.n_empty = Adw.StatusPage(icon_name="network-cellular-offline-symbolic", title="Pas de liste",
                                      description="Le modem ne fournit pas de cellules voisines.", vexpand=True)
        v.append(self.n_group); v.append(self.n_empty)
        return self._scroll(v)

    def _page_frames(self):
        # Pas de _scroll ici : la ListView doit etre l enfant direct d une
        # ScrolledWindow pour ne materialiser que les lignes visibles.
        v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_start=12, margin_end=12,
                    margin_top=12, margin_bottom=12)
        self.f_status = Gtk.Label(xalign=0, css_classes=["dim-label", "caption"], wrap=True)
        v.append(self.f_status)
        bar = Gtk.Box(spacing=6)
        self.f_filter = Gtk.SearchEntry(placeholder_text="filtre : LAPDm, DTAP, SMS, UL, SABM, OP1…", hexpand=True)
        self.f_custom = Gtk.CustomFilter.new(self._frame_match)
        self.f_filter.connect("search-changed", lambda *_: self.f_custom.changed(Gtk.FilterChange.DIFFERENT))
        # Le curseur bloque en fin de course : actif, chaque trame qui arrive
        # ramene la vue en bas (on les voit defiler). Remonter a la main le
        # debloque, revenir tout en bas le rebloque, le bouton aussi.
        self.f_follow = Gtk.ToggleButton(icon_name="go-bottom-symbolic", active=True,
                                         tooltip_text="Suivre : curseur bloqué en fin de liste")
        self.f_follow.connect("toggled", self._on_frames_follow)
        self.f_pause = Gtk.ToggleButton(icon_name="media-playback-pause-symbolic", tooltip_text="Suspendre l'affichage")
        self.f_pause.connect("toggled", self._on_frames_pause)
        clear = Gtk.Button(icon_name="edit-clear-all-symbolic", tooltip_text="Effacer les trames")
        clear.connect("clicked", lambda *_: (self.frames.remove_all(), self.frames_pending.clear(), self._render_frames_status()))
        bar.append(self.f_filter); bar.append(self.f_follow); bar.append(self.f_pause); bar.append(clear)
        v.append(bar)
        self.f_model = Gtk.FilterListModel(model=self.frames, filter=self.f_custom)
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._frame_setup)
        factory.connect("bind", self._frame_bind)
        self.f_view = Gtk.ListView(model=Gtk.NoSelection(model=self.f_model), factory=factory,
                                   css_classes=["card"], single_click_activate=True)
        self.f_view.connect("activate", self._on_frame_activate)
        self.f_empty = Adw.StatusPage(icon_name="network-transmit-receive-symbolic", title="Pas de trame",
                                      description="Les trames viennent du tableau de bord du banc (tshark sur l'hôte).",
                                      vexpand=True)
        self.f_scrolled = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True, child=self.f_view)
        sc = self.f_scrolled
        self.f_stack = Gtk.Stack(vexpand=True)
        self.f_stack.add_named(self.f_empty, "empty"); self.f_stack.add_named(sc, "list")
        v.append(self.f_stack)
        adj = sc.get_vadjustment()
        self._f_auto = False            # c est nous qui deplacons le curseur
        self._f_last_value = 0.0
        self._f_model_ts = 0.0          # derniere modification du modele (ajout, plafond)
        self._f_pin_pending = False
        adj.connect("changed", self._on_frames_adj_changed)
        adj.connect("value-changed", self._on_frames_adj_value)
        self.f_model.connect("items-changed", lambda *_: self._frames_pin())
        self.stack.connect("notify::visible-child", lambda *_: self._frames_pin())
        return v

    def _on_frames_adj_changed(self, adj):
        """Les bornes ont change (ligne mesuree, fenetre redimensionnee) :
        curseur bloque et plus en bas, on l y remet."""
        if self.f_follow.get_active() and adj.get_value() + adj.get_page_size() < adj.get_upper() - 1:
            self._frames_pin()

    def _frames_pin(self):
        """Curseur bloque en fin de course : la derniere trame en vue. Passe par
        scroll_to (la ListView mesure ses lignes a la demande : poser une
        valeur d ajustement au jugé laisse une page blanche), une fois par
        cycle d affichage."""
        if not self.f_follow.get_active() or self._f_pin_pending:
            return
        self._f_pin_pending = True
        GLib.idle_add(self._frames_pin_now)

    def _frames_pin_now(self):
        self._f_pin_pending = False
        n = self.f_model.get_n_items()
        if n and self.f_follow.get_active():
            self._f_auto = True
            if hasattr(self.f_view, "scroll_to"):          # GTK >= 4.12
                self.f_view.scroll_to(n - 1, Gtk.ListScrollFlags.NONE, None)
            adj = self.f_scrolled.get_vadjustment()
            adj.set_value(max(0.0, adj.get_upper() - adj.get_page_size()))
            self._f_auto = False
            self._f_last_value = adj.get_value()
        return False

    def _on_frames_adj_value(self, adj):
        v = adj.get_value()
        at_end = v + adj.get_page_size() >= adj.get_upper() - 1
        if not self._f_auto and time.monotonic() - self._f_model_ts > 0.15:
            if v < self._f_last_value - 1 and not at_end and self.f_follow.get_active():
                self.f_follow.set_active(False)     # l utilisateur remonte lire : on le laisse
            elif at_end and not self.f_follow.get_active():
                self.f_follow.set_active(True)      # revenu en bas : le curseur se rebloque
        self._f_last_value = v

    def _on_frames_follow(self, btn):
        if btn.get_active():
            self._frames_pin()
        self._render_frames_status()

    def _frame_match(self, item):
        words = self.f_filter.get_text().lower().split()
        return all(w in item.haystack for w in words)

    def _frame_setup(self, _factory, li):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, css_classes=["frame-row"])
        head = Gtk.Box(spacing=6)
        box.when = Gtk.Label(xalign=0, css_classes=["monospace", "caption", "dim-label"])
        box.proto = Gtk.Label(css_classes=["proto"])
        box.dir = Gtk.Label(css_classes=["caption"])
        box.where = Gtk.Label(xalign=1, hexpand=True, css_classes=["caption", "dim-label"], ellipsize=Pango.EllipsizeMode.END)
        for w in (box.when, box.proto, box.dir, box.where):
            head.append(w)
        box.info = Gtk.Label(xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, lines=2,
                             ellipsize=Pango.EllipsizeMode.END, css_classes=["body"])
        box.detail = Gtk.Label(xalign=0, css_classes=["caption", "dim-label"], ellipsize=Pango.EllipsizeMode.END)
        box.append(head); box.append(box.info); box.append(box.detail)
        li.set_child(box)

    def _frame_bind(self, _factory, li):
        box, it = li.get_child(), li.get_item()
        box.when.set_text(it.when)
        box.proto.set_text(it.proto)
        for k in KINDS:
            box.proto.remove_css_class("kind-" + k); box.remove_css_class("kind-" + k)
        box.proto.add_css_class("kind-" + it.kind); box.add_css_class("kind-" + it.kind)
        box.dir.set_text({"UL": "↑ UL", "DL": "↓ DL"}.get(it.direction, it.direction))
        for w in (box.dir, box):
            w.remove_css_class("dir-ul"); w.remove_css_class("dir-dl")
        if it.direction == "UL":
            box.dir.add_css_class("dir-ul"); box.add_css_class("dir-ul")
        elif it.direction == "DL":
            box.dir.add_css_class("dir-dl"); box.add_css_class("dir-dl")
        box.where.set_text(it.where)
        box.info.set_text(it.info or "—")
        box.detail.set_text(it.detail); box.detail.set_visible(bool(it.detail))

    def _on_frame_activate(self, _view, pos):
        it = self.f_model.get_item(pos)
        if not it:
            return
        num = it.d.get("frameNum") or 0
        dlg = Adw.Dialog(title="Trame %s · %s" % (num, it.proto), content_width=380, content_height=720)
        view = Adw.ToolbarView(); view.add_top_bar(Adw.HeaderBar())
        v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_start=12, margin_end=12, margin_bottom=12)
        lb = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
        for key in ("frameNum", "ts", "protocol", "direction", "opLabel", "arfcn", "channel", "timeslot", "fn",
                    "rnti", "sfn", "src", "dst", "sctpPorts", "length", "l2", "l3", "cc", "rr", "info"):
            val = it.d.get(key)
            if val in (None, "", False):
                continue
            lb.append(Adw.ActionRow(title=key, subtitle=str(val), subtitle_selectable=True))
        v.append(lb)
        # Le contenu complet : la dissection tshark et l hexa, demandes au
        # dashboard (il relit le pcap de notre capture).
        v.append(Gtk.Label(label="Dissection complète (tshark)", xalign=0, css_classes=["heading"]))
        tree = Gtk.TextView(editable=False, cursor_visible=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                            left_margin=8, right_margin=8, top_margin=6, bottom_margin=6, css_classes=["card", "frame-text"])
        tree.get_buffer().set_text("chargement…" if self.frame_source else "pas de source de trames")
        v.append(tree)
        v.append(Gtk.Label(label="Hexadécimal", xalign=0, css_classes=["heading"]))
        hexv = Gtk.TextView(editable=False, cursor_visible=False, monospace=True, wrap_mode=Gtk.WrapMode.CHAR,
                            left_margin=8, right_margin=8, top_margin=6, bottom_margin=6, css_classes=["card", "frame-text"])
        hexv.get_buffer().set_text("chargement…" if self.frame_source else "")
        v.append(hexv)
        view.set_content(Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, child=v, vexpand=True))
        dlg.set_child(view); dlg.present(self)

        def got(kind, text):
            if kind == "tree":
                tree.get_buffer().set_text(text or "dissection indisponible (dashboard sans pcap, ou trame déjà "
                                                    "sortie de la capture)")
            else:
                if text:
                    octets = [text[i:i + 2] for i in range(0, len(text), 2)]
                    lines = ["%04x  %s" % (o, " ".join(octets[o:o + 16])) for o in range(0, len(octets), 16)]
                    hexv.get_buffer().set_text("\n".join(lines))
                else:
                    hexv.get_buffer().set_text("hexa indisponible")
        if self.frame_source:
            self.frame_source.request(num, got)

    def _on_frames_pause(self, btn):
        if not btn.get_active():
            for d in self.frames_pending:
                self._push_frame(d)
            self.frames_pending.clear()
        self._render_frames_status()

    def add_frame(self, d):
        self.frames_total += 1
        if self.frames_csv:
            self.frames_csv.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), d.get("frameNum"), d.get("protocol"),
                                      d.get("direction"), d.get("opLabel"), d.get("arfcn"), d.get("channel"),
                                      d.get("timeslot"), d.get("fn"), d.get("src"), d.get("dst"), d.get("length"),
                                      d.get("l2"), d.get("l3"), d.get("cc"), d.get("rr"), d.get("info")])
        if self.f_pause.get_active():
            self.frames_pending.append(d)
            del self.frames_pending[:-FRAMES_MAX]
        else:
            self._push_frame(d)
        if self.frames_status[0].endswith("capture demandée"):
            self.frames_status = (self.frames_status[0][:-len("demandée")] + "active", True)
        self._render_frames_status()

    def _push_frame(self, d):
        # Chronologique, comme Wireshark : la trame arrive en bas. Le curseur
        # bloque (bouton « suivre ») la ramene en vue via le signal « changed »
        # de l ajustement ; « Pause » fige tout.
        self._f_model_ts = time.monotonic()
        self.frames.append(FrameItem(d))
        while self.frames.get_n_items() > FRAMES_MAX:
            self.frames.remove(0)

    def on_frames_status(self, text, ok):
        self.frames_status = (text, ok)
        self.add_event("trames : %s" % text)
        self._render_frames_status()

    def _render_frames_status(self):
        text, ok = self.frames_status
        n, shown = self.frames_total, self.f_model.get_n_items()
        extra = "%d trame(s)" % n
        if self.f_filter.get_text().strip():
            extra += ", %d affichée(s)" % shown
        if self.f_pause.get_active():
            extra += ", en pause (%d en attente)" % len(self.frames_pending)
        elif not self.f_follow.get_active():
            extra += ", curseur libre"
        self.f_status.set_text("%s · %s" % (text, extra) if n else text)
        self.f_stack.set_visible_child_name("list" if self.frames.get_n_items() else "empty")

    def _page_log(self):
        v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.log_status = Gtk.Label(xalign=0, css_classes=["dim-label", "caption"], wrap=True)
        v.append(self.log_status)
        lb = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
        lb.bind_model(self.events, lambda it: Adw.ActionRow(title=it.text, subtitle=it.when, title_lines=3))
        v.append(lb)
        return self._scroll(v)

    def _on_metric_chosen(self, *_):
        self.graph.set_metric(list(self.history)[self.metric_sel.get_selected()])
        self.metric_chosen = True

    # ── mise a jour ──
    def add_event(self, text):
        self.events.insert(0, EventItem(time.strftime("%H:%M:%S"), text))
        while self.events.get_n_items() > 300:
            self.events.remove(self.events.get_n_items() - 1)

    def tick(self):
        try:
            s = self.source.sample()
        except Exception as e:          # un modem qui repond de travers ne doit pas tuer l appli
            self.add_event("erreur de lecture : %s" % e)
            s = self.last
            s["ts"] = time.time()
        self.last = s
        for k in self.history:
            self.history[k].append((s["ts"], s.get(k)))
        self.render(s)
        if self.csv:
            self.csv.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), s["rat"], s["opcode"], s["operator"],
                               s["lac"], s["tac"], s["cid"], s["arfcn"], s["earfcn"], s["pci"],
                               s["rssi"], s["rsrp"], s["rsrq"], s["snr"], s["rscp"], s["ecio"], s["quality"], s["reg_cs"]])
            self.csv_file.flush()
        return True

    def render(self, s):
        m, val = primary_metric(s)
        self.h_rat.set_text(RAT_TITLE.get(s["rat"], s["rat"].upper()) if s["present"] else "—")
        self.h_op.set_text(("%s  %s" % (s["operator"], s["opcode"])).strip() if s["operator"] or s["opcode"] else
                           ("modem : %s" % s["state"]))
        self.h_reg.set_text("%s · %s" % (s["reg_cs"] or s["state"], cell_label(s)) if s["present"] else s["state"])
        self.h_metric.set_text(fmt(val, UNITS[m], 1 if isinstance(val, float) else 0))
        self.h_metric_name.set_text(LABELS[m] if val is not None else "pas de mesure")
        lvl = level_of(m, val) if m != "quality" else (0 if not val else max(1, min(4, math.ceil(val / 25))))
        self.h_bars.set_level(lvl + 1 if lvl else 0)   # 5 barres : niveau 4 -> toutes

        r = self.r_cell
        r["plmn"].set(("%s / %s" % (s["mcc"], s["mnc"])) if s["mcc"] else "")
        r["operator"].set(s["operator"])
        r["lac"].set("%04X (%d)" % (s["lac"], s["lac"]) if s["lac"] is not None else "")
        r["tac"].set("%04X (%d)" % (s["tac"], s["tac"]) if s["tac"] is not None else "")
        r["cid"].set(("%X (%d)" % (s["cid"], s["cid"])) if s["cid"] is not None else "")
        if s["tac"] is not None and s["cid"] is not None:
            r["enb"].set("%d / %d" % (s["cid"] >> 8, s["cid"] & 0xFF))
        else:
            r["enb"].set("")
        r["arfcn"].set(str(s["arfcn"]) if s["arfcn"] is not None else "")
        r["earfcn"].set(str(s["earfcn"]) if s["earfcn"] is not None else "")
        r["pci"].set(str(s["pci"]) if s["pci"] is not None else "")
        for k in ("bands", "rat_list", "reg_cs", "reg_eps", "packet", "state", "firmware", "driver",
                  "imei", "imsi", "sim_op", "number"):
            r[k].set(s[k])
        r["model"].set(("%s %s" % (s["manufacturer"], s["model"])).strip())

        for k, row in self.r_metric.items():
            row.update(s[k])
            row.set_visible(s[k] is not None or k in ("rssi", "quality") or
                            (k in ("rsrp", "rsrq", "snr") and s["rat"] in ("lte", "nr", "")) or
                            (k in ("rscp", "ecio") and s["rat"] == "umts"))
        self.r_ber.set(fmt(s["ber"], "%", 2)); self.r_ber.set_visible(s["ber"] is not None)
        # Tant que l utilisateur n a rien choisi, le graphe suit la mesure
        # principale de la technologie du moment (RSRP en LTE, RSSI en GSM).
        if not self.metric_chosen and val is not None and m != self.graph.metric and m in self.history:
            self.metric_sel.set_selected(list(self.history).index(m))
            self.metric_chosen = False
        self.graph.queue_draw()

        self._render_neighbors(s)
        self.log_status.set_text("Enregistrement CSV : %s" % (self.csv_path if self.csv_path else "arrêté (menu)"))

    def _render_neighbors(self, s):
        cells = s["neighbors"]
        self.n_empty.set_visible(not cells)
        self.n_group.set_visible(bool(cells))
        while len(self.n_rows) > len(cells):
            self.n_group.remove(self.n_rows.pop())
        while len(self.n_rows) < len(cells):
            row = Adw.ActionRow(); lab = Gtk.Label(css_classes=["numeric"], xalign=1)
            row.add_suffix(lab); row.lab = lab; self.n_group.add(row); self.n_rows.append(row)
        for row, c in zip(self.n_rows, cells):
            ident = []
            if c["tac"] is not None: ident.append("TAC %04X" % c["tac"])
            if c["lac"] is not None: ident.append("LAC %04X" % c["lac"])
            if c["cid"] is not None: ident.append("CI %X" % c["cid"])
            if c["pci"] is not None: ident.append("PCI %d" % c["pci"])
            if c["arfcn"] is not None: ident.append("ARFCN %s" % c["arfcn"])
            row.set_title("%s%s" % (RAT_TITLE.get(c["rat"], c["rat"]), "  · serveuse" if c["serving"] else ""))
            row.set_subtitle("  ".join(ident) or "—")
            v = c["rsrp"] if c["rsrp"] is not None else c["rssi"]
            row.lab.set_text(fmt(float(v), "dBm", 1) if v is not None else "—")
            (row.add_css_class if c["serving"] else row.remove_css_class)("neighbor-serving")

    # ── actions ──
    def on_scan(self, *_):
        self.toasts.add_toast(Adw.Toast(title="Balayage des réseaux en cours…", timeout=3))

        def done(nets, err):
            if err:
                self.toasts.add_toast(Adw.Toast(title="Balayage impossible : %s" % err))
                self.add_event("balayage : %s" % err)
                return
            dlg = Adw.Dialog(title="Réseaux", content_width=340)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            box.append(Adw.HeaderBar())
            lb = Gtk.ListBox(css_classes=["boxed-list"], margin_start=12, margin_end=12, margin_bottom=12)
            if not nets:
                lb.append(Adw.ActionRow(title="Aucun réseau"))
            for n in nets:
                lb.append(Adw.ActionRow(title="%s  %s" % (n["name"], n["code"]), subtitle="%s · %s" % (n["act"], n["avail"])))
            box.append(lb); dlg.set_child(box); dlg.present(self)
            self.add_event("balayage : %d réseau(x)" % len(nets))
        self.source.scan(done)

    def on_record(self, action, value):
        action.set_state(value)
        if value.get_boolean():
            d = os.path.join(GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOCUMENTS) or GLib.get_home_dir(),
                             "network-monitor")
            os.makedirs(d, exist_ok=True)
            self.csv_path = os.path.join(d, time.strftime("signal-%Y%m%d-%H%M%S.csv"))
            self.csv_file = open(self.csv_path, "w", newline="")
            self.csv = csv.writer(self.csv_file)
            self.csv.writerow(["time", "rat", "plmn", "operator", "lac", "tac", "cid", "arfcn", "earfcn", "pci",
                               "rssi", "rsrp", "rsrq", "snr", "rscp", "ecio", "quality", "registration"])
            # Les trames vont dans un second fichier, a cote : une ligne par trame.
            self.frames_csv_file = open(self.csv_path.replace("signal-", "trames-"), "w", newline="")
            self.frames_csv = csv.writer(self.frames_csv_file)
            self.frames_csv.writerow(["time", "frame", "protocol", "direction", "op", "arfcn", "channel", "timeslot",
                                      "fn", "src", "dst", "length", "l2", "l3", "cc", "rr", "info"])
            self.add_event("enregistrement : %s (et trames-…)" % self.csv_path)
        else:
            if self.csv:
                self.csv_file.close()
                self.frames_csv_file.close()
                self.add_event("enregistrement terminé : %s" % self.csv_path)
            self.csv = None; self.csv_path = None; self.frames_csv = None
        self.rec_icon.set_visible(bool(self.csv))
        self.log_status.set_text("Enregistrement CSV : %s" % (self.csv_path if self.csv_path else "arrêté (menu)"))

    def start_screenshots(self, directory):
        """Rend chaque page dans un PNG (GTK dessine lui-meme, l ecran de
        verrouillage ou l absence d ecran n y changent rien), puis quitte."""
        os.makedirs(directory, exist_ok=True)
        pages = self.stack.get_pages()
        names = [pages.get_item(i).get_name() for i in range(pages.get_n_items())]
        renderer = Gsk.CairoRenderer.new()
        renderer.realize(None)

        def shoot(i):
            if i >= len(names):
                renderer.unrealize()
                self.get_application().quit()
                return False
            self.stack.set_visible_child_name(names[i])
            GLib.timeout_add(900, snap, i)
            return False

        def snap(i):
            scale = self.get_scale_factor()
            w, h = self.get_width(), self.get_height()
            snapshot = Gtk.Snapshot()
            snapshot.scale(scale, scale)
            Gtk.WidgetPaintable.new(self).snapshot(snapshot, w, h)
            node = snapshot.to_node()
            if node is None:            # pas encore dessine (surface cachee) : on reessaie
                GLib.timeout_add(1000, snap, i)
                return False
            tex = renderer.render_texture(node, Graphene.Rect().init(0, 0, w * scale, h * scale))
            path = os.path.join(directory, "%d-%s.png" % (i + 1, names[i]))
            tex.save_to_png(path)
            print("capture :", path, tex.get_width(), "x", tex.get_height())
            shoot(i + 1)
            return False
        GLib.timeout_add(4000, shoot, 0)

    def on_about(self, *_):
        Adw.AboutDialog(application_name="Network Monitor", application_icon=APP_ID, version=VERSION,
                        developer_name="banc Osmocom", comments="Cellule, signal, voisines et journal, "
                        "lus dans ModemManager ; trames LAPDm / DTAP / S1AP relayées par le tableau de bord du banc. "
                        "Dans l'esprit de Network Signal Guru, pour postmarketOS.",
                        license_type=Gtk.License.GPL_3_0).present(self)


class EventItem(GObject.Object):
    def __init__(self, when, text):
        super().__init__()
        self.when, self.text = when, text


class App(Adw.Application):
    def __init__(self, demo, screenshot=None, frames_urls=None):
        # --screenshot : une instance a part, sinon GApplication reveille
        # l appli deja ouverte a l ecran (meme bus de session) et ne capture rien.
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE if screenshot
                         else Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.demo = demo
        self.screenshot = screenshot
        self.frames_urls = frames_urls if frames_urls is not None else FRAMES_URLS

    def do_activate(self):
        win = self.get_active_window()
        if not win:
            prov = Gtk.CssProvider(); prov.load_from_data(CSS)
            Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), prov,
                                                      Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            pending = []
            src = (DemoSource if self.demo else ModemSource)(lambda t: pending.append(t))
            win = Window(self, src, DemoFrameSource if self.demo else (FrameSource if self.frames_urls else None),
                         self.frames_urls)
            src.on_event = win.add_event
            for t in pending:
                win.add_event(t)
            if self.screenshot:
                win.start_screenshots(self.screenshot)
        win.present()


def main():
    ap = argparse.ArgumentParser(description="Moniteur radio cellulaire (ModemManager) pour postmarketOS")
    ap.add_argument("--demo", action="store_true", help="valeurs simulées, sans modem")
    ap.add_argument("--screenshot", metavar="DOSSIER", help="capture chaque page en PNG dans ce dossier, puis quitte")
    ap.add_argument("--frames-url", metavar="URL[,URL]", help="WebSocket du tableau de bord pour les trames "
                    "(défaut : %s ; vide = pas de trames)" % ",".join(FRAMES_URLS))
    args, rest = ap.parse_known_args()
    urls = FRAMES_URLS if args.frames_url is None else [u.strip() for u in args.frames_url.split(",") if u.strip()]
    sys.exit(App(args.demo, args.screenshot, urls).run([sys.argv[0]] + rest))


if __name__ == "__main__":
    main()
