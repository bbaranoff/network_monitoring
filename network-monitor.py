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
import re
import sys
import threading
import time
import urllib.error
import urllib.request

import cairo

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
    import pjsua2 as pj                             # pile SIP du téléphone (paquet py3-pjsua)
except ImportError:
    pj = None
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
VERSION = "0.4"
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
        self.last_rx = time.monotonic()
        GLib.timeout_add_seconds(10, self._watchdog)
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
            if not self.stopped:
                self.on_status("dashboard injoignable (%s) : %s" % (url, e.message), False)
                self._retry()
            return
        if self.stopped:
            # arrêtée pendant la poignée de main (le nœud a été choisi entre-temps) :
            # sans ça, deux sources vivent, deux tshark écrivent /tmp/capture.pcap
            # côté dashboard et se coupent l un l autre toutes les 4 s.
            conn.close(Soup.WebsocketCloseCode.NORMAL, None)
            return
        self.conn, self.url = conn, url
        self.last_rx = time.monotonic()
        conn.set_max_incoming_payload_size(4 << 20)
        conn.connect("message", self._message)
        conn.connect("closed", self._closed)
        conn.connect("error", lambda _c, err: self.on_status("erreur WebSocket : %s" % err.message, False))
        conn.send_text(json.dumps({"type": "tshark_start"}))
        self.tries = 0
        self.on_status("dashboard %s : capture demandée" % url, True)

    def _watchdog(self):
        """UN LIEN MUET EST UN LIEN MORT. Le banc émet sans cesse (Paging, SI,
        mesures) : 25 s sans rien, c est que le chemin a changé sous le lien
        (4G qui revient, route de secours) sans qu il se ferme. On le referme
        et on se rebranche ; sans ça les trames ne défilent plus jamais."""
        if self.stopped:
            return False
        if self.conn is not None and time.monotonic() - self.last_rx > 25:
            self.on_status("lien muet depuis 25 s, on se rebranche…", False)
            conn, self.conn = self.conn, None
            try:
                conn.close(Soup.WebsocketCloseCode.NORMAL, None)
            except GLib.Error:
                pass
            self.last_rx = time.monotonic()
            self.tries = 0
            self._connect()
        return True

    def _retry(self):
        # Une adresse apres l autre, vite ; toutes epuisees, on respire.
        delay = 2 if self.tries % len(self.urls) else 10
        GLib.timeout_add_seconds(delay, self._connect)

    def _message(self, _conn, mtype, data):
        self.last_rx = time.monotonic()
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
        if _conn is not self.conn:          # un ancien lien (chien de garde) : le courant reste
            return
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
        # une espace avant une parenthèse collée : « N(S)=2(DTAP) » -> « N(S)=2 (DTAP) »
        self.info = re.sub(r"(?<=[\w=])\((?=[A-Z]{2,5}\))", " (", d.get("info") or "")
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
        self.title, self.meta = frame_title(d, self.info, rr), frame_meta(self)
        self.full = re.sub(r"^(\s*\([^)]*\))+\s*", "", self.info).strip()
        low = (self.info + " " + self.title).lower()
        self.cat = ("meas" if self.kind == "mon" or "measurement" in low
                    else "bcast" if self.kind == "bcch"
                    else "sms" if self.kind == "sms"
                    else "core" if self.kind in ("core", "lte")
                    # « other » : les lignes de journal des démons (GSMTAP log) - dans Tout seulement
                    else "divers" if self.kind == "other"
                    else "sig")
        self.key = any(k in low for k in KEY_MESSAGES)


# Ce qu on veut voir passer sans le chercher : liseré autour de la ligne.
KEY_MESSAGES = ("location updating", "measurement report", "cm service", "paging response", "setup",
                "connect", "disconnect", "release", "authentication", "ciphering mode", "identity",
                "imsi detach", "assignment", "handover", "attach", "tracking area", "cp-data")
FAMILY = {"bcch": "BCCH", "lapdm": "LAPDm", "mm": "MM", "rr": "RR", "cc": "CC", "sms": "SMS", "core": "CŒUR",
          "lte": "LTE", "rrc": "RRC", "nas": "NAS", "mon": "MESURE", "other": "AUTRE"}


def frame_title(d, info, rr):
    """Le nom du message, comme on le dit : « Location Updating Request »,
    pas « (SDCCH) (MM) Location Updating Request » ni « RR 0x15 »."""
    if info.lower().startswith("mon:"):
        return "Mesure de cellule"
    t = re.sub(r"^(\s*\([^)]*\))+\s*", "", info).strip()
    # LAPDm : tshark colle l en-tête et le message (« I, N(R)=1, N(S)=2(DTAP) (CC) Connect ») ;
    # le titre est le message, l en-tête LAPDm est déjà dans le détail (l2).
    m = re.search(r"\((?:DTAP|RR|GMM|SM)\)\s*(?:\([^)]*\)\s*)*(\S.*)$", t)
    if m:
        t = m.group(1).strip()
    if (not t or t.startswith("RR 0x")) and rr and not rr.startswith("RR 0x"):
        t = rr
    if d.get("cc") and not t:
        t = d["cc"]
    t = t.split(", ")[0] if len(t) > 48 and ", " in t else t
    return t or (d.get("protocol") or "Trame")


def frame_meta(it):
    """La ligne sous le titre : où et quand, sans le sens (il a sa pastille)."""
    info = it.info
    extra = info[4:].strip() if info.lower().startswith("mon:") else ""
    return "  ·  ".join(x for x in (it.where, extra) if x)


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
    if chan in ("BCCH", "CCCH", "PCH", "AGCH", "CBCH51", "CBCH52") or "system information" in info \
            or "paging request" in info:
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
        grad = cairo.LinearGradient(0, pad_t, 0, pad_t + gh)
        grad.add_color_stop_rgba(0, 0.208, 0.518, 0.894, 0.45)
        grad.add_color_stop_rgba(1, 0.208, 0.518, 0.894, 0.0)
        cr.set_source(grad)
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


# ═══════════════════════════════════════════════════════════════════════════
# LE NŒUD ET LE TÉLÉPHONE (0.3)
#
# L appli n est plus seulement un moniteur : avec l adresse d un nœud
# osmo-operator (IP locale, adresse WireGuard ou VPS), elle devient un poste
# du réseau. Tout part d UNE adresse, saisie une fois (page Compte) :
#
#   http://<nœud>:8081/psd?src=bts|ms   le spectre I/Q du banc (fft-web)
#   http://<nœud>:8090/api/etat         les processus et les VTY (osmo-reglages)
#   http://<nœud>:8090/api/console      une commande VTY
#   ws://<nœud>:8080/                   les trames (tableau de bord)
#   http://<nœud>:8088/inscription      code d invitation -> compte SIP
#   sip:<nœud>:5060                     appels et SMS (SIP MESSAGE), pjsua2
#
# Rien de tout cela n est authentifié côté nœud (la console VTY exécute ce
# qu on lui donne) : réseau local ou WireGuard, jamais l Internet ouvert.
# osmo-reglages écoute sur 127.0.0.1 par défaut : --bind <adresse du nœud>.
# ═══════════════════════════════════════════════════════════════════════════
PORT_FFT = int(os.environ.get("NETMON_FFT_PORT", "8081"))
PORT_API = int(os.environ.get("NETMON_API_PORT", "8090"))
PORT_WS = int(os.environ.get("NETMON_WS_PORT", "8080"))
PORT_JOIN = int(os.environ.get("NETMON_JOIN_PORT", "8088"))
# Où chercher osmo-reglages quand il ne répond pas à l adresse du nœud (voir NodeClient._api).
API_REPLI = [h for h in os.environ.get("NETMON_API_FALLBACK", "10.0.2.2").split(",") if h]
SETTINGS = os.path.join(GLib.get_user_config_dir(), "network-monitor", "reglages.json")


def load_settings():
    try:
        with open(SETTINGS) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(s):
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    fd = os.open(SETTINGS, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)   # contient un mot de passe SIP
    with os.fdopen(fd, "w") as f:
        json.dump(s, f)


def node_host(text):
    """'http://192.168.1.20:8080/x' -> '192.168.1.20'."""
    t = re.sub(r"^[a-z]+://", "", (text or "").strip())
    return t.split("/")[0].split(":")[0]


def peer_of(uri):
    """'"Bob" <sip:100102@host;x=y>' -> '100102'."""
    m = re.search(r"sips?:([^@>;]+)", uri or "")
    return m.group(1) if m else (uri or "?")


def http_json(url, data=None, timeout=4):
    req = urllib.request.Request(url, method="POST" if data is not None else "GET",
                                 data=json.dumps(data).encode() if data is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except ValueError:
            return {"error": "HTTP %d" % e.code, "err": "HTTP %d" % e.code}


class NodeClient:
    def __init__(self, host):
        self.host = host
        self.api_host = None

    def psd(self, src):
        return http_json("http://%s:%d/psd?src=%s" % (self.host, PORT_FFT, src), timeout=3)

    def _api(self, chemin, data=None, timeout=4):
        """osmo-reglages écoute sur 127.0.0.1 par défaut (sa console VTY exécute
        tout) : injoignable par l adresse du nœud. Depuis la VM du banc, l hôte
        est aussi 10.0.2.2, qui mène à son 127.0.0.1 : on l essaie en repli.
        L adresse qui a répondu est gardée."""
        hotes = [self.api_host] if self.api_host else [self.host] + [h for h in API_REPLI if h != self.host]
        err = None
        for h in hotes:
            try:
                r = http_json("http://%s:%d%s" % (h, PORT_API, chemin), data, timeout=timeout)
                self.api_host = h
                return r
            except (OSError, ValueError) as e:
                err = e
        self.api_host = None
        raise err

    def etat(self):
        return self._api("/api/etat")

    def console(self, hote, port, ligne):
        return self._api("/api/console", {"hote": hote, "port": port, "lignes": [ligne]}, timeout=10)

    def trunk(self, choice=None):
        """La sortie vers le réseau public : lue (None) ou choisie."""
        return self._api("/api/trunk", None if choice is None else {"trunk": choice})

    def enrol(self, pseudo, code):
        j = http_json("http://%s:%d/inscription" % (self.host, PORT_JOIN),
                      {"code": code, "pseudo": pseudo}, timeout=10)
        if not j.get("ok"):
            raise RuntimeError(j.get("error") or "refus sans motif")
        return {"msisdn": j.get("msisdn") or j["sip_user"], "sip_user": j["sip_user"],
                "sip_password": j["sip_password"], "sip_domain": j.get("sip_domain") or self.host,
                "sip_port": int(j.get("sip_port") or 5060), "sip_transport": j.get("sip_transport") or "udp"}


class DemoNode:
    """Un nœud simulé : un porteur GSM qui respire, quatre démons."""
    host = "démo"

    def __init__(self, host=None):
        self.t0 = time.time()

    def psd(self, src):
        t = time.time() - self.t0
        off = 67.7 if src == "bts" else -67.7           # la tonalité FCCH, à ±67,7 kHz
        burst = 1.0 if src == "bts" else (0.5 + 0.5 * math.sin(t * 1.3) > 0.35)
        freqs, psd = [], []
        for i in range(512):
            f = -541.0 + 1082.0 * i / 511
            carrier = 38.0 * math.exp(-(f / 115.0) ** 4) * burst
            tone = 14.0 * math.exp(-((f - off) / 6.0) ** 2) * (0.5 + 0.5 * math.sin(t * 0.7))
            freqs.append(round(f, 1))
            psd.append(round(-96 + carrier + tone + random.uniform(-3, 3), 2))
        return {"label": "BTS (descendant)" if src == "bts" else "MS (montant)",
                "arfcn": 514, "rate": 1083333, "freqs": freqs, "psd": psd}

    def etat(self):
        age = int(time.time() - self.t0) + 4000
        return {"procs": [{"pid": str(1200 + i), "age": age - 3 * i, "rss": rss, "cpu": cpu, "cmd": cmd}
                          for i, (cmd, rss, cpu) in enumerate((
                              ("osmo-bsc -c /etc/osmocom/osmo-bsc.cfg", 14, "0.3"),
                              ("osmo-bts-trx -c /etc/osmocom/osmo-bts-trx.cfg", 22, "4.1"),
                              ("osmo-hlr -c /etc/osmocom/osmo-hlr.cfg", 9, "0.0"),
                              ("osmo-mgw -c /etc/osmocom/osmo-mgw.cfg", 11, "0.1"),
                              ("osmo-msc -c /etc/osmocom/osmo-msc.cfg", 18, "0.2"),
                              ("osmo-stp -c /etc/osmocom/osmo-stp.cfg", 8, "0.0"),
                              ("qemu-system-arm -M calypso", 190, "38.0")))],
                "vty": [{"hote": "127.0.0.1", "port": p, "nom": n, "pid": "1", "invite": inv}
                        for n, p, inv in (("osmo-msc", 4254, "OsmoMSC"), ("osmo-bsc", 4242, "OsmoBSC"),
                                          ("osmo-hlr", 4258, "OsmoHLR"), ("osmo-bts-trx", 4241, "OsmoBTS"))]}

    def console(self, hote, port, ligne):
        return {"sortie": "> %s\n  IMSI: 001010000000001  MSISDN: 100101  LAC: 1  attaché\n"
                          "  IMSI: 001010000000002  MSISDN: 100102  LAC: 1  attaché" % ligne}

    def trunk(self, choice=None):
        DemoNode._trunk = choice or getattr(DemoNode, "_trunk", "dongle")
        return {"trunk": DemoNode._trunk, "choix": list(TRUNKS)}

    def enrol(self, pseudo, code):
        return {"msisdn": "100150", "sip_user": "100150", "sip_password": "", "sip_domain": "10.8.0.1",
                "sip_port": 5060, "sip_transport": "udp"}


# ── SIP : une interface, deux arrière-plans ─────────────────────────────────
#   on_reg(ok, texte)   on_call(état, pair)   on_message(pair, texte)
#   état : "incoming" | "calling" | "ringing" | "active" | "ended"
# pjsua2 tourne DANS la boucle GTK (aucun fil pjsua : les rappels Python
# venus d un fil étranger sont ce qui casse les applis pjsua2), sondé à 20 ms.
if pj:
    class _Call(pj.Call):
        def __init__(self, sip, acc, call_id=pj.PJSUA_INVALID_ID):
            pj.Call.__init__(self, acc, call_id)
            self.sip = sip

        def onCallState(self, prm):
            ci = self.getInfo()
            peer = peer_of(ci.remoteUri)
            if ci.state == pj.PJSIP_INV_STATE_EARLY and ci.role == pj.PJSIP_ROLE_UAC:
                self.sip.on_call("ringing", peer)
            elif ci.state == pj.PJSIP_INV_STATE_CONFIRMED:
                self.sip.on_call("active", peer)
            elif ci.state == pj.PJSIP_INV_STATE_DISCONNECTED:
                self.sip.on_call("ended", ci.lastReason or peer)
                GLib.idle_add(self.sip._forget, self)      # jamais détruit depuis son propre rappel

        def onCallMediaState(self, prm):
            ci = self.getInfo()
            adm = pj.Endpoint.instance().audDevManager()
            for i, mi in enumerate(ci.media):
                if mi.type == pj.PJMEDIA_TYPE_AUDIO and mi.status == pj.PJSUA_CALL_MEDIA_ACTIVE:
                    am = self.getAudioMedia(i)
                    adm.getCaptureDevMedia().startTransmit(am)
                    am.startTransmit(adm.getPlaybackDevMedia())

    class _Account(pj.Account):
        def __init__(self, sip):
            pj.Account.__init__(self)
            self.sip = sip

        def onRegState(self, prm):
            ok = prm.code // 100 == 2 and self.getInfo().regIsActive
            self.sip.on_reg(ok, "" if ok else "%d %s" % (prm.code, prm.reason))

        def onIncomingCall(self, prm):
            call = _Call(self.sip, self, prm.callId)
            op = pj.CallOpParam()
            if self.sip.call is not None:                  # un appel à la fois
                op.statusCode = 486
                call.hangup(op)
                return
            self.sip.call = call
            op.statusCode = 180
            call.answer(op)
            self.sip.on_call("incoming", peer_of(call.getInfo().remoteUri))

        def onInstantMessage(self, prm):
            self.sip.on_message(peer_of(prm.fromUri), prm.msgBody)


class PjSip:
    def __init__(self, on_reg, on_call, on_message):
        self.on_reg, self.on_call, self.on_message = on_reg, on_call, on_message
        self.ep = self.acc = self.call = None
        self.domain = ""
        self.buddies = {}
        self.timer = 0

    def start(self, a):
        if pj is None:
            raise RuntimeError("pjsua2 absent : apk add py3-pjsua")
        self.stop()
        self.domain = a["sip_domain"]
        self.ep = pj.Endpoint()
        self.ep.libCreate()
        cfg = pj.EpConfig()
        cfg.uaConfig.threadCnt = 0
        cfg.uaConfig.mainThreadOnly = True
        cfg.uaConfig.userAgent = "network-monitor/" + VERSION
        cfg.logConfig.level = 2
        self.ep.libInit(cfg)
        tr = {"tcp": pj.PJSIP_TRANSPORT_TCP, "tls": pj.PJSIP_TRANSPORT_TLS}.get(
            a["sip_transport"], pj.PJSIP_TRANSPORT_UDP)
        self.ep.transportCreate(tr, pj.TransportConfig())
        self.ep.libStart()
        ac = pj.AccountConfig()
        ac.idUri = "sip:%s@%s" % (a["sip_user"], self.domain)
        ac.regConfig.registrarUri = "sip:%s:%d;transport=%s" % (self.domain, a["sip_port"], a["sip_transport"])
        ac.sipConfig.authCreds.append(pj.AuthCredInfo("digest", "*", a["sip_user"], 0, a["sip_password"]))
        self.acc = _Account(self)
        self.acc.create(ac)
        self.timer = GLib.timeout_add(20, self._poll)

    def _poll(self):
        self.ep.libHandleEvents(0)
        return True

    def _forget(self, call):
        if self.call is call:
            self.call = None
        return False

    def _uri(self, num):
        return "sip:%s@%s" % (num, self.domain)

    def dial(self, num):
        if self.acc is None:
            raise RuntimeError("pas de compte sur un nœud")
        if self.call is None:
            self.call = _Call(self, self.acc)
            self.call.makeCall(self._uri(num), pj.CallOpParam(True))
            self.on_call("calling", num)

    def answer(self):
        if self.call is not None:
            op = pj.CallOpParam()
            op.statusCode = 200
            self.call.answer(op)

    def hangup(self):
        if self.call is not None:
            self.call.hangup(pj.CallOpParam())

    def mute(self, on):
        """Le micro coupé : niveau du périphérique de capture à zéro (remis à la fin de l appel)."""
        try:
            pj.Endpoint.instance().audDevManager().getCaptureDevMedia().adjustRxLevel(0.0 if on else 1.0)
        except pj.Error:
            pass

    def dtmf(self, digit):
        """Une touche pendant l appel (RFC 2833) : serveur vocal, code d accès."""
        if self.call is not None:
            try:
                self.call.dialDtmf(digit)
            except pj.Error:
                pass

    def send(self, num, text):
        if self.acc is None:
            raise RuntimeError("pas de compte sur un nœud")
        b = self.buddies.get(num)
        if b is None:
            bc = pj.BuddyConfig()
            bc.uri = self._uri(num)
            bc.subscribe = False
            b = pj.Buddy()
            b.create(self.acc, bc)
            self.buddies[num] = b
        prm = pj.SendInstantMessageParam()
        prm.content = text
        b.sendInstantMessage(prm)

    def stop(self):
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = 0
        if self.ep is not None:
            self.buddies.clear()
            self.call = self.acc = None
            self.ep.libDestroy()
            self.ep = None


class DemoSip:
    def __init__(self, on_reg, on_call, on_message):
        self.on_reg, self.on_call, self.on_message = on_reg, on_call, on_message
        self.in_call = False

    def start(self, a):
        GLib.timeout_add(300, lambda: self.on_reg(True, "") and False)
        GLib.timeout_add(900, lambda: self.on_message("100102", "Le banc est en ligne, tu me reçois ?") and False)

    def dial(self, num):
        self.in_call = True
        self.on_call("calling", num)
        GLib.timeout_add(800, lambda: self.in_call and self.on_call("ringing", num) and False)
        GLib.timeout_add(2500, lambda: self.in_call and self.on_call("active", num) and False)

    def answer(self):
        self.on_call("active", "100102")

    def hangup(self):
        self.in_call = False
        self.on_call("ended", "")

    def mute(self, on):
        pass

    def dtmf(self, digit):
        pass

    def send(self, num, text):
        GLib.timeout_add(900, lambda: self.on_message(num, "reçu : " + text) and False)

    def stop(self):
        pass


# ── La palette turbo (Google, approximation polynomiale) ────────────────────
def _turbo(x):
    x = max(0.0, min(1.0, x))
    r = 0.13572138 + x * (4.61539260 + x * (-42.66032258 + x * (132.13108234 + x * (-152.94239396 + x * 59.28637943))))
    g = 0.09140261 + x * (2.19418839 + x * (4.84296658 + x * (-14.18503333 + x * (4.27729857 + x * 2.82956604))))
    b = 0.10667330 + x * (12.64194608 + x * (-60.58204836 + x * (110.36276771 + x * (-89.90310912 + x * 27.34824973))))
    return tuple(max(0.0, min(1.0, c)) for c in (r, g, b))


TURBO = [_turbo(i / 255) for i in range(256)]
TURBO_BGRX = [bytes((int(b * 255), int(g * 255), int(r * 255), 255)) for r, g, b in TURBO]
LEVEL_RGB = {4: (0.18, 0.76, 0.49), 3: (0.21, 0.52, 0.89), 2: (0.90, 0.65, 0.04), 1: (0.88, 0.11, 0.14),
             0: (0.5, 0.5, 0.5)}


class Spectrum(Gtk.DrawingArea):
    """Le spectre d une source I/Q du nœud : la DSP en haut, la cascade
    (waterfall, palette turbo) en dessous. Les lignes descendent."""
    W, ROWS = 256, 150

    def __init__(self):
        super().__init__(vexpand=True)
        self.set_size_request(-1, 380)
        self.stride = cairo.ImageSurface.format_stride_for_width(cairo.FORMAT_RGB24, self.W)
        self.buf = bytearray(TURBO_BGRX[0] * (self.stride // 4) * self.ROWS)
        self.surf = cairo.ImageSurface.create_for_data(self.buf, cairo.FORMAT_RGB24, self.W, self.ROWS, self.stride)
        self.trace = []
        self.peak_hold = []
        self.lo, self.hi = -100.0, -50.0
        self.span = (-541.0, 541.0)
        self.label = ""
        self.message = "en attente du nœud…"
        self.set_draw_func(self._draw)

    def push(self, d):
        if not d or d.get("error") or not d.get("psd"):
            self.message = (d or {}).get("error") or "pas de réponse du nœud"
            self.queue_draw()
            return
        psd, n = d["psd"], len(d["psd"])
        if max(psd) - min(psd) < 0.5:                       # plancher plat : la FIFO ne reçoit rien
            self.trace = []
            self.message = ("Le mobile n'émet pas (au repos) : le montant s'anime pendant un appel, "
                            "un SMS ou un Location Update." if (d.get("label") or "").startswith("MS")
                            else "%s : aucun signal (source muette)" % (d.get("label") or "source"))
            self.queue_draw()
            return
        # W colonnes : le maximum de chaque groupe (un pic étroit ne disparaît pas)
        cols = [max(psd[i * n // self.W:max(i * n // self.W + 1, (i + 1) * n // self.W)]) for i in range(self.W)]
        s = sorted(cols)
        lo, hi = s[len(s) // 10] - 4, s[-1] + 4             # plancher de bruit et crête, lissés
        # ATTAQUE IMMÉDIATE, RETOUR LENT. Lissée dans les deux sens, l échelle
        # traînait derrière une salve (le montant, par rafales) : trace écrêtée
        # en plateau et étiquette du pic (36 dB) hors de l axe (-1 dB). Le haut
        # suit la crête dès qu elle le dépasse, le bas dès qu il descend ; ils
        # ne reviennent que lentement.
        hi = max(hi, lo + 20)
        self.hi = hi if hi > self.hi else self.hi + (hi - self.hi) * 0.05
        self.lo = lo if lo < self.lo else self.lo + (lo - self.lo) * 0.05
        rng = max(1.0, self.hi - self.lo)
        self.trace = [max(0.0, min(1.0, (v - self.lo) / rng)) for v in cols]
        self.peak_hold = [max(a * 0.985, b) for a, b in zip(self.peak_hold or self.trace, self.trace)]
        self.surf.flush()
        self.buf[self.stride:] = self.buf[:-self.stride]
        self.buf[:self.W * 4] = b"".join(TURBO_BGRX[int(v * 255)] for v in self.trace)
        self.surf.mark_dirty()
        if d.get("freqs"):
            self.span = (d["freqs"][0], d["freqs"][-1])
        self.label = "%s · ARFCN %s" % (d.get("label", ""), d.get("arfcn", "?"))
        self.peak = (cols.index(s[-1]), s[-1])
        self.message = ""
        self.queue_draw()

    @staticmethod
    def _round(cr, x, y, w, h, r):
        cr.new_sub_path()
        cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
        cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
        cr.close_path()

    def _draw(self, _area, cr, w, h):
        th = h * 0.36                                       # hauteur de la trace
        self._round(cr, 0, 0, w, h, 14)
        cr.clip()
        cr.set_source_rgb(0.03, 0.04, 0.07)
        cr.paint()
        cr.select_font_face("Sans")
        # la cascade
        cr.save()
        cr.translate(0, th)
        cr.scale(w / self.W, (h - th) / self.ROWS)
        cr.set_source_surface(self.surf, 0, 0)
        cr.get_source().set_filter(cairo.FILTER_BILINEAR)
        cr.paint()
        cr.restore()
        if not self.trace:
            cr.set_source_rgba(1, 1, 1, 0.6)
            cr.set_font_size(13)
            lines, cur = [], ""                             # au mot près, dans la largeur
            for word in self.message.split():
                t = (cur + " " + word).strip()
                if cur and cr.text_extents(t).width > w - 32:
                    lines.append(cur); cur = word
                else:
                    cur = t
            lines.append(cur)
            y = th / 2 - (len(lines) - 1) * 9
            for line in lines:
                e = cr.text_extents(line)
                cr.move_to((w - e.width) / 2, y)
                cr.show_text(line)
                y += 18
            return
        # la grille
        cr.set_line_width(1)
        cr.set_font_size(10)
        for k in range(1, 4):
            y = th * k / 4
            cr.set_source_rgba(1, 1, 1, 0.08)
            cr.move_to(0, y); cr.line_to(w, y); cr.stroke()
            cr.set_source_rgba(1, 1, 1, 0.45)
            cr.move_to(6, y - 3)
            cr.show_text("%d dB" % (self.hi - (self.hi - self.lo) * k / 4))
        # l aire sous la trace, aux couleurs de la palette
        pad = 6
        ys = [th - pad - v * (th - 2 * pad - 14) for v in self.trace]
        grad = cairo.LinearGradient(0, pad, 0, th)
        for stop in (0.0, 0.35, 0.7, 1.0):
            r, g, b = TURBO[int((1 - stop) * 255)]
            grad.add_color_stop_rgba(stop, r, g, b, 0.85 - 0.6 * stop)
        cr.move_to(0, th)
        for i, y in enumerate(ys):
            cr.line_to(w * i / (self.W - 1), y)
        cr.line_to(w, th)
        cr.close_path()
        cr.set_source(grad)
        cr.fill()
        # la crête retenue, puis la trace
        cr.set_source_rgba(1, 1, 1, 0.28)
        for i, v in enumerate(self.peak_hold):
            (cr.line_to if i else cr.move_to)(w * i / (self.W - 1), th - pad - v * (th - 2 * pad - 14))
        cr.stroke()
        cr.set_source_rgba(1, 1, 1, 0.95)
        cr.set_line_width(1.6)
        for i, y in enumerate(ys):
            (cr.line_to if i else cr.move_to)(w * i / (self.W - 1), y)
        cr.stroke()
        # le pic et les étiquettes
        pi, pv = self.peak
        px, f0, f1 = w * pi / (self.W - 1), self.span[0], self.span[1]
        green = (0.341, 0.890, 0.537)                   # #57e389, le vert GNOME
        cr.set_source_rgb(*green)
        cr.arc(px, ys[pi], 4, 0, 2 * math.pi)
        cr.fill()
        txt = "%+.0f kHz · %.0f dB" % (f0 + (f1 - f0) * pi / (self.W - 1), pv)
        cr.set_font_size(12)
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        e = cr.text_extents(txt)
        # sous le titre de la source (en haut à droite), jamais par-dessus
        tx, ty = max(6, min(w - e.width - 10, px - e.width / 2)), max(40, ys[pi] - 12)
        self._round(cr, tx - 6, ty - e.height - 5, e.width + 12, e.height + 10, 8)
        cr.set_source_rgba(0.02, 0.10, 0.05, 0.85)
        cr.fill()
        cr.set_source_rgb(*green)
        cr.move_to(tx, ty)
        cr.show_text(txt)
        cr.select_font_face("Sans")
        cr.set_source_rgba(1, 1, 1, 0.75)
        cr.move_to(8, h - 8); cr.show_text("%+.0f kHz" % f0)
        t = "%+.0f kHz" % f1
        cr.move_to(w - cr.text_extents(t).width - 8, h - 8); cr.show_text(t)
        cr.set_font_size(12)
        cr.move_to(w - cr.text_extents(self.label).width - 8, 16); cr.show_text(self.label)
        if self.message:
            cr.set_source_rgba(1, 0.6, 0.3, 0.95)
            cr.move_to(8, th + 16); cr.show_text(self.message)


class Gauge(Gtk.DrawingArea):
    """La mesure principale en arc : la couleur est celle du niveau."""

    def __init__(self):
        super().__init__(halign=Gtk.Align.CENTER)
        self.set_size_request(230, 170)
        self.frac, self.shown, self.level = 0.0, 0.0, 0
        self.text, self.name = "—", ""
        self.set_draw_func(self._draw)

    def set_value(self, metric, value, level):
        lo, hi = RANGES.get(metric, (0, 100))
        self.frac = 0.0 if value is None else max(0.0, min(1.0, (value - lo) / (hi - lo)))
        self.level = level or 0
        self.text = fmt(value, "", 1 if isinstance(value, float) else 0)
        self.name = ("%s · %s" % (LABELS[metric], UNITS[metric])) if value is not None else "pas de mesure"
        if abs(self.frac - self.shown) > 0.002:
            GLib.timeout_add(16, self._ease)
        self.queue_draw()

    def _ease(self):
        self.shown += (self.frac - self.shown) * 0.2
        self.queue_draw()
        return abs(self.frac - self.shown) > 0.002

    def _draw(self, _area, cr, w, h):
        fg = self.get_color()
        cx, cy, r = w / 2, h * 0.60, min(w / 2, h * 0.60) - 10
        a0, a1 = math.radians(150), math.radians(390)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_width(13)
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.12)
        cr.arc(cx, cy, r, a0, a1)
        cr.stroke()
        red, green, blue = LEVEL_RGB[self.level]
        if self.shown > 0.005:
            a = a0 + (a1 - a0) * self.shown
            cr.set_source_rgba(red, green, blue, 0.22)       # le halo
            cr.set_line_width(22)
            cr.arc(cx, cy, r, a0, a)
            cr.stroke()
            cr.set_source_rgb(red, green, blue)
            cr.set_line_width(13)
            cr.arc(cx, cy, r, a0, a)
            cr.stroke()
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(40)
        e = cr.text_extents(self.text)
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 1)
        cr.move_to(cx - e.width / 2 - e.x_bearing, cy + 10)
        cr.show_text(self.text)
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(12)
        e = cr.text_extents(self.name)
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.6)
        cr.move_to(cx - e.width / 2, cy + 34)
        cr.show_text(self.name)


class Swiper(Gtk.Box):
    """Des pages que l on feuillette du doigt (Adw.Carousel), le titre de la
    page et les points au-dessus ; les chevrons pour qui n a pas d écran tactile."""

    def __init__(self, on_change=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        self.names, self.titles, self.on_change = [], [], on_change
        self.all = []                    # (nom, titre, page) : tout ce qui existe, montré ou non
        head = Gtk.CenterBox(margin_top=6, margin_start=6, margin_end=6)
        prev = Gtk.Button(icon_name="go-previous-symbolic", css_classes=["flat", "circular"])
        nxt = Gtk.Button(icon_name="go-next-symbolic", css_classes=["flat", "circular"])
        prev.connect("clicked", lambda *_: self.step(-1))
        nxt.connect("clicked", lambda *_: self.step(1))
        mid = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.title = Gtk.Label(css_classes=["heading"])
        self.dots = Adw.CarouselIndicatorDots()
        mid.append(self.title)
        mid.append(self.dots)
        head.set_start_widget(prev); head.set_center_widget(mid); head.set_end_widget(nxt)
        self.append(head)
        self.carousel = None
        self._new_carousel()

    def _new_carousel(self):
        if self.carousel is not None:
            self.remove(self.carousel)
        self.carousel = Adw.Carousel(vexpand=True, hexpand=True, allow_scroll_wheel=False, spacing=12)
        self.carousel.connect("page-changed", self._changed)
        self.dots.set_carousel(self.carousel)
        self.append(self.carousel)

    def restrict(self, names):
        """Ne montre que ces pages (les modes) ; l ordre d origine est gardé.
        Jamais de carousel.remove() : Adw.Carousel garde à la place de la page
        retirée une image figée qui rétrécit en s animant, et sur un onglet
        caché l animation ne finit pas (spectres tassés en colonnes étroites).
        On vide l ancien carrousel et on en monte un neuf."""
        want = [(n, t, w) for n, t, w in self.all if n in names]
        if [n for n, _t, _w in want] == self.names:
            return
        old = self.carousel
        for _n, _t, w in self.all:
            if w.get_parent() is not None:
                w.unparent() if w.get_parent() is not old else old.remove(w)
        self._new_carousel()
        for _n, _t, w in want:
            self.carousel.append(w)
        self.names, self.titles = [n for n, _t, _w in want], [t for _n, t, _w in want]
        if self.names:
            self.title.set_text(self.titles[0])

    def add(self, name, title, widget):
        # Une page = toute la largeur. Sans enfant qui s étire, Adw.Carousel se
        # contente de sa largeur naturelle : en mode Medium (pas de console),
        # les spectres tombaient en colonnes étroites côte à côte.
        widget.set_hexpand(True)
        self.all.append((name, title, widget))
        self.names.append(name); self.titles.append(title)
        self.carousel.append(widget)
        if len(self.names) == 1:
            self.title.set_text(title)

    def index(self):
        return max(0, min(len(self.names) - 1, int(round(self.carousel.get_position()))))

    def current(self):
        return self.names[self.index()] if self.names else ""

    def show(self, name, animate=True):
        if name not in self.names:
            return
        self.carousel.scroll_to(self.carousel.get_nth_page(self.names.index(name)), animate)
        self.title.set_text(self.titles[self.names.index(name)])

    def step(self, d):
        i = max(0, min(len(self.names) - 1, self.index() + d))
        self.show(self.names[i])

    def _changed(self, _c, i):
        if i >= len(self.names):
            return
        self.title.set_text(self.titles[i])
        if self.on_change:
            self.on_change(self.names[i])


# La sortie vers le réseau public (TRUNK_EXT du plan de numérotation Asterisk du nœud).
TRUNKS = {"zadarma": "Zadarma", "dongle": "Dongle"}
# Trois niveaux : ce que l on montre, pas ce que l on sait faire.
#   Basique : Téléphone, Messages, Compte.
#   Medium  : + Radio (cellule, signal, voisines) et Nœud (spectres, services).
#   Expert  : + trames, journal, console VTY, choix de la sortie.
MODES = ("Basique", "Medium", "Expert")
# (clé des réglages, libellé, schéma libadwaita) ; le premier est le défaut.
THEMES = (("clair", "Clair", Adw.ColorScheme.FORCE_LIGHT), ("sombre", "Sombre", Adw.ColorScheme.FORCE_DARK),
          ("systeme", "Comme le système", Adw.ColorScheme.DEFAULT))
MODE_RADIO = (None, ("cell", "signal", "neighbors", "frames", "log"), ("cell", "signal", "neighbors", "frames", "log"))
MODE_NODE = (None, ("bts", "ms", "services"), ("bts", "ms", "services", "console"))

KEYS = (("1", ""), ("2", "ABC"), ("3", "DEF"), ("4", "GHI"), ("5", "JKL"), ("6", "MNO"),
        ("7", "PQRS"), ("8", "TUV"), ("9", "WXYZ"), ("*", ""), ("0", "+"), ("#", ""))
CALL_TEXT = {"incoming": "Appel entrant", "calling": "Appel en cours…", "ringing": "Ça sonne…",
             "active": "En communication"}


class NodeUi:
    """Les pages Téléphone, Messages, Nœud et Compte de la fenêtre."""

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=4))

    def _in_thread(self, work, done):
        def run():
            try:
                res, err = work(), None
            except Exception as e:                          # noqa: BLE001  (montré à l utilisateur)
                res, err = None, str(e) or e.__class__.__name__
            GLib.idle_add(lambda: done(res, err) and False)
        threading.Thread(target=run, daemon=True).start()

    # ── Téléphone ──
    def _page_phone(self):
        self.phone_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        # pas encore de compte : rejoindre un nœud
        page = Adw.StatusPage(icon_name="call-start-symbolic", title="Rejoindre un nœud",
                              description="Un numéro du réseau, sans carte SIM.")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        grp = Adw.PreferencesGroup()
        self.j_node = Adw.EntryRow(title="Adresse du nœud (IP locale, WireGuard, VPS)")
        self.j_user = Adw.EntryRow(title="Pseudo")
        self.j_code = Adw.PasswordEntryRow(title="Code d'invitation")
        for r in (self.j_node, self.j_user, self.j_code):
            grp.add(r)
        box.append(grp)
        self.j_go = Gtk.Button(label="S'inscrire", css_classes=["pill", "suggested-action"], halign=Gtk.Align.CENTER)
        self.j_go.connect("clicked", self.on_join)
        box.append(self.j_go)
        direct = Gtk.Button(label="J'ai déjà un compte SIP", css_classes=["flat"], halign=Gtk.Align.CENTER,
                            tooltip_text="Pseudo = identifiant SIP, code = mot de passe SIP")
        direct.connect("clicked", self.on_direct)
        box.append(direct)
        page.set_child(Adw.Clamp(maximum_size=420, child=box))
        self.phone_stack.add_named(page, "join")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=12, margin_bottom=16,
                      margin_start=10, margin_end=10)          # 16 : 364 px, plus qu un téléphone
        self.call_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                                 css_classes=["call-card"], visible=False,
                                 vexpand=True, valign=Gtk.Align.CENTER)
        # Pendant un appel, la carte prend la place du clavier : centrée, en grand.
        self.call_icon = Gtk.Image(icon_name="call-start-symbolic", pixel_size=56, css_classes=["call-icon"],
                                   margin_bottom=10)
        self.call_peer = Gtk.Label(xalign=0.5, css_classes=["call-peer"], ellipsize=Pango.EllipsizeMode.END)
        self.call_info = Gtk.Label(xalign=0.5, css_classes=["numeric", "call-info"])
        self.call_card.append(self.call_icon)
        self.call_card.append(self.call_peer)
        self.call_card.append(self.call_info)
        box.append(self.call_card)
        self.number = Gtk.Entry(css_classes=["dial-entry"], xalign=0.5, vexpand=True, valign=Gtk.Align.CENTER,
                                input_purpose=Gtk.InputPurpose.PHONE)     # pas d invite : le curseur la chevauchait
        self.number.connect("activate", lambda *_: self.on_dial())
        box.append(self.number)
        grid = Gtk.Grid(row_spacing=10, column_spacing=22, halign=Gtk.Align.CENTER)
        for i, (digit, sub) in enumerate(KEYS):
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
            inner.append(Gtk.Label(label=digit))
            if sub:
                inner.append(Gtk.Label(label=sub, css_classes=["sub"]))
            key = Gtk.Button(child=inner, css_classes=["dial-key"])
            key.connect("clicked", lambda _b, d=digit: self._key(d))
            grid.attach(key, i % 3, i // 3, 1, 1)
        box.append(grid)
        self.dial_grid = grid
        # Pendant l appel : micro coupé, clavier (DTMF), haut-parleur.
        self.call_tools = Gtk.Box(spacing=18, halign=Gtk.Align.CENTER, visible=False)
        self.t_mute = Gtk.ToggleButton(icon_name="microphone-sensitivity-muted-symbolic", css_classes=["call-tool"],
                                       tooltip_text="Couper le micro")
        self.t_keys = Gtk.ToggleButton(icon_name="input-dialpad-symbolic", css_classes=["call-tool"],
                                       tooltip_text="Clavier (touches DTMF)")
        self.t_spk = Gtk.ToggleButton(icon_name="audio-speakers-symbolic", css_classes=["call-tool"],
                                      tooltip_text="Haut-parleur")
        self.t_mute.connect("toggled", lambda b: self.sip.mute(b.get_active()))
        self.t_keys.connect("toggled", lambda b: self._call_keys(b.get_active()))
        self.t_spk.connect("toggled", lambda b: self._speaker(b.get_active()))
        for b in (self.t_mute, self.t_keys, self.t_spk):
            self.call_tools.append(b)
        box.append(self.call_tools)
        row = Gtk.Box(spacing=22, halign=Gtk.Align.CENTER, margin_top=8)

        def round_btn(icon, classes, tip, cb, visible=True):
            b = Gtk.Button(icon_name=icon, css_classes=["call-btn"] + classes, tooltip_text=tip, visible=visible,
                           halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
            b.connect("clicked", cb)
            return b
        self.b_answer = round_btn("call-start-symbolic", ["go"], "Répondre", lambda *_: self.sip.answer(), False)
        self.b_call = round_btn("call-start-symbolic", ["go"], "Appeler", lambda *_: self.on_dial())
        self.b_hang = round_btn("call-stop-symbolic", ["destructive-action"], "Raccrocher",
                                lambda *_: self.sip.hangup(), False)
        self.dial_side = (round_btn("mail-send-symbolic", ["flat"], "Écrire à ce numéro", self._to_messages),
                          round_btn("edit-clear-symbolic", ["flat"], "Effacer",
                                    lambda *_: self.number.set_text(self.number.get_text()[:-1])))
        for b in (self.dial_side[0], self.b_answer, self.b_call, self.b_hang, self.dial_side[1]):
            row.append(b)
        row.set_margin_bottom(8)
        box.append(row)
        self.phone_stack.add_named(Adw.Clamp(maximum_size=420, child=box), "dial")
        return self.phone_stack

    def on_join(self, _btn):
        host, user, code = node_host(self.j_node.get_text()), self.j_user.get_text().strip(), self.j_code.get_text().strip()
        if not (host and user and code):
            return self.toast("Adresse du nœud, pseudo et code sont nécessaires")
        self.set_node(host)
        self.j_go.set_sensitive(False)
        self.j_go.set_label("Inscription…")

        def done(acc, err):
            self.j_go.set_sensitive(True)
            self.j_go.set_label("S'inscrire")
            if err:
                self.toast("Inscription refusée : " + err)
            else:
                self.use_account(acc)
        self._in_thread(lambda: self.node.enrol(user, code), done)

    def on_direct(self, _btn):
        hp, user = re.sub(r"^[a-z]+://", "", self.j_node.get_text().strip()), self.j_user.get_text().strip()
        if not (hp and user):
            return self.toast("Adresse du nœud et identifiant SIP sont nécessaires (le code sert de mot de passe)")
        host, _, port = hp.partition(":")
        self.set_node(host)
        self.use_account({"msisdn": user, "sip_user": user, "sip_password": self.j_code.get_text(),
                          "sip_domain": host, "sip_port": int(port) if port.isdigit() else 5060,
                          "sip_transport": "udp"})

    def use_account(self, acc, save=True):
        self.account = acc
        if save and not self.demo:
            self.settings["account"] = acc
            save_settings(self.settings)
        self.a_number.set_subtitle(acc["msisdn"])
        self.a_sip.set_subtitle("%s:%d (%s)" % (acc["sip_domain"], acc["sip_port"], acc["sip_transport"]))
        self.a_leave.set_sensitive(True)
        self.on_reg(False, "connexion…")
        self.phone_stack.set_visible_child_name("dial")
        try:
            self.sip.start(acc)
        except Exception as e:                              # noqa: BLE001
            self.on_reg(False, str(e))

    def on_leave(self, _btn):
        self.sip.stop()
        self.account = None
        self.settings.pop("account", None)
        if not self.demo:
            save_settings(self.settings)
        self.a_number.set_subtitle("—")
        self.a_sip.set_subtitle("—")
        self.a_leave.set_sensitive(False)
        self.on_reg(False, "")
        self.phone_stack.set_visible_child_name("join")

    def _key(self, d):
        if self.call_state is not None:             # en appel, une touche est un DTMF
            self.sip.dtmf(d)
            self.call_info.set_text("%s  ·  %s" % (CALL_TEXT.get(self.call_state, ""), d))
            return
        self.number.set_text(self.number.get_text() + d)
        self.number.set_position(-1)

    def _call_keys(self, on):
        """Le clavier en appel : la carte se resserre pour que tout tienne à l écran."""
        busy = self.call_state is not None
        self.dial_grid.set_visible(on or not busy)
        self.call_icon.set_visible(not (on and busy))
        (self.call_card.add_css_class if on and busy else self.call_card.remove_css_class)("compact")
        (self.dial_grid.add_css_class if on and busy else self.dial_grid.remove_css_class)("in-call")
        self.call_card.set_vexpand(not (on and busy))

    def _speaker(self, on):
        """Le haut-parleur : c est callaudiod qui route le son sur postmarketOS
        (comme pour Calls). Absent (bureau, banc) : sans effet, sans erreur."""
        try:
            Gio.bus_get_sync(Gio.BusType.SESSION, None).call(
                "org.mobian_project.CallAudio", "/org/mobian_project/CallAudio", "org.mobian_project.CallAudio",
                "EnableSpeaker", GLib.Variant("(b)", (on,)), None, Gio.DBusCallFlags.NONE, 2000, None, None)
        except GLib.Error:
            pass

    def on_dial(self):
        num = self.number.get_text().replace(" ", "")
        if not num:
            return self.toast("Numéro manquant")
        try:
            self.sip.dial(num)
        except Exception as e:                              # noqa: BLE001
            self.toast("Appel impossible : %s" % e)

    def _to_messages(self, _btn):
        if self.number.get_text():
            self.m_to.set_text(self.number.get_text())
        self.tabs.set_visible_child_name("messages")

    # ── Messages ──
    def _page_messages(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.m_to = Adw.EntryRow(title="À (numéro)", input_purpose=Gtk.InputPurpose.PHONE)
        top = Adw.PreferencesGroup(margin_top=12, margin_start=12, margin_end=12)
        top.add(self.m_to)
        # En toutes lettres, hors du champ : une corbeille dans « À (numéro) »
        # se lisait « effacer le numéro ».
        wipe = Gtk.Button(label="Effacer la conversation", css_classes=["flat", "caption"], halign=Gtk.Align.END)
        wipe.connect("clicked", lambda *_: self.on_wipe())
        top.set_header_suffix(wipe)
        box.append(top)
        self.m_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=12, margin_bottom=12,
                              margin_start=12, margin_end=12, valign=Gtk.Align.END)
        self.m_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True, child=self.m_list)
        self.m_stack = Gtk.Stack()
        self.m_stack.add_named(Adw.StatusPage(icon_name="mail-unread-symbolic", title="Aucun message",
                                              description="Les SMS du réseau arrivent ici.", vexpand=True), "empty")
        self.m_stack.add_named(self.m_scroll, "list")
        box.append(self.m_stack)
        bar = Gtk.Box(spacing=8, margin_start=12, margin_end=12, margin_bottom=12)
        self.m_text = Gtk.Entry(hexpand=True, placeholder_text="Message")
        self.m_text.connect("activate", self.on_send)
        send = Gtk.Button(icon_name="mail-send-symbolic", css_classes=["circular", "suggested-action"])
        send.connect("clicked", self.on_send)
        bar.append(self.m_text)
        bar.append(send)
        box.append(bar)
        return Adw.Clamp(maximum_size=640, child=box)

    def on_wipe(self):
        """Les conversations, le numéro saisi, et les appels / SMS du journal."""
        while (c := self.m_list.get_first_child()) is not None:
            self.m_list.remove(c)
        self.m_stack.set_visible_child_name("empty")
        self.m_to.set_text("")
        self.m_text.set_text("")
        if self.call_state is None:
            self.number.set_text("")
        for i in reversed(range(self.events.get_n_items())):
            t = self.events.get_item(i).text.lower()
            if any(k in t for k in ("appel", "sms", "message")):
                self.events.remove(i)
        self.msg_page.set_needs_attention(False)
        self.toast("Appels et conversations effacés")

    def on_send(self, _w):
        num, text = self.m_to.get_text().replace(" ", ""), self.m_text.get_text()
        if not (num and text):
            return self.toast("Numéro ou message manquant")
        try:
            self.sip.send(num, text)
        except Exception as e:                              # noqa: BLE001
            return self.toast("SMS non envoyé : %s" % e)
        self._bubble(num, text, out=True)
        self.m_text.set_text("")

    def _bubble(self, peer, text, out):
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                      halign=Gtk.Align.END if out else Gtk.Align.START)
        row.append(Gtk.Label(label=text, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, max_width_chars=28,
                             xalign=0, selectable=True, css_classes=["bubble", "out" if out else "in"]))
        row.append(Gtk.Label(label="%s %s · %s" % ("à" if out else "de", peer, time.strftime("%H:%M")),
                             xalign=1 if out else 0, css_classes=["bubble-meta"]))
        self.m_list.append(row)
        self.m_stack.set_visible_child_name("list")
        GLib.idle_add(lambda: self.m_scroll.get_vadjustment().set_value(
            self.m_scroll.get_vadjustment().get_upper()) and False)

    # ── Nœud : spectres, services, console ──
    def _page_node(self):
        self.node_swiper = Swiper()
        self.spec = {}
        for src, title in (("bts", "Spectre BTS (descendant)"), ("ms", "Spectre MS (montant)")):
            self.spec[src] = Spectrum()
            self.spec[src].set_margin_start(12); self.spec[src].set_margin_end(12)
            self.spec[src].set_margin_top(8); self.spec[src].set_margin_bottom(12)
            self.node_swiper.add(src, title, self.spec[src])

        v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.n_procs = Adw.PreferencesGroup(title="Processus du banc", description="en attente du nœud…")
        self.n_proc_rows = []
        v.append(self.n_procs)
        self.node_swiper.add("services", "Services", self._scroll(v))

        v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_start=12, margin_end=12,
                    margin_top=8, margin_bottom=12)
        self.n_vty = []
        self.n_vty_sel = Gtk.DropDown.new_from_strings(["(aucun démon)"])
        v.append(self.n_vty_sel)
        self.n_out = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                                  top_margin=8, left_margin=8, right_margin=8, bottom_margin=8,
                                  css_classes=["console"])
        v.append(Gtk.ScrolledWindow(vexpand=True, child=self.n_out, css_classes=["card"]))
        # Not homogeneous, no pills: three pills of equal width asked for 390 px,
        # more than a phone (360) - the whole window overflowed in Expert, and in
        # Basique too (the hidden tab still counts in the window's width).
        quick = Gtk.Box(spacing=6)
        self.n_console_widgets = []
        for label, cmd in (("abonnés", "show subscriber all"), ("stats", "show stats"), ("version", "show version")):
            b = Gtk.Button(label=label, hexpand=True)
            b.connect("clicked", lambda _b, c=cmd: self.on_vty(c))
            quick.append(b)
            self.n_console_widgets.append(b)
        v.append(quick)
        bar = Gtk.Box(spacing=8)
        self.n_cmd = Gtk.Entry(hexpand=True, placeholder_text="commande VTY (show …)", css_classes=["monospace"])
        self.n_cmd.connect("activate", lambda *_: self.on_vty(self.n_cmd.get_text()))
        go = Gtk.Button(icon_name="go-next-symbolic", css_classes=["circular", "suggested-action"])
        go.connect("clicked", lambda *_: self.on_vty(self.n_cmd.get_text()))
        bar.append(self.n_cmd); bar.append(go)
        v.append(bar)
        self.n_console_widgets += [self.n_vty_sel, self.n_cmd, go]
        self._console_ready(False)                  # jusqu à la première réponse du nœud
        self.node_swiper.add("console", "Console VTY", v)
        self.node_busy = False
        self.node_etat_at = 0
        GLib.timeout_add(250, self._node_poll)
        return self.node_swiper

    def _node_poll(self):
        if self.tabs.get_visible_child_name() != "node" or self.node_busy or self.node is None:
            if self.node is None and self.tabs.get_visible_child_name() == "node":
                for s in self.spec.values():
                    s.message = "pas de nœud : donne son adresse dans Compte"
                    s.queue_draw()
            return True
        page, node = self.node_swiper.current(), self.node
        if page in self.spec:
            self.node_busy = True

            def done(d, err):
                self.node_busy = False
                self.spec[page].push(d if d else {"error": "nœud injoignable (%s)" % err})
            self._in_thread(lambda: node.psd(page), done)
        elif time.time() - self.node_etat_at > 5:
            self.node_busy = True
            self.node_etat_at = time.time()

            def done(d, err):
                self.node_busy = False
                self._render_etat(d, err)
            self._in_thread(node.etat, done)
        return True

    def _render_etat(self, d, err):
        for r in self.n_proc_rows:
            self.n_procs.remove(r)
        self.n_proc_rows = []
        if err or not d or "procs" not in d:
            self.n_procs.set_description(GLib.markup_escape_text(
                "Le service de réglages du nœud (osmo-reglages, port %d) ne répond pas. "
                "Les services et la console VTY passent par lui." % PORT_API))
            self.n_procs.set_tooltip_text(str(err or d))          # le détail technique, au survol
            self._console_ready(False)
            return
        self.n_procs.set_tooltip_text(None)
        self.n_procs.set_description("%d processus · %s" % (len(d["procs"]), self.node.host))
        for p in d["procs"]:
            name = os.path.basename(p["cmd"].split()[0])
            age = p["age"]
            row = Adw.ActionRow(title=name, subtitle="pid %s · %s · %d Mo" % (
                p["pid"], "%d h %02d" % (age // 3600, age % 3600 // 60) if age >= 3600 else "%d min" % (age // 60),
                p["rss"]), tooltip_text=p["cmd"])
            cpu = float(p["cpu"] or 0)
            row.add_prefix(Gtk.Box(css_classes=["dot", "on"], valign=Gtk.Align.CENTER))
            row.add_suffix(Gtk.Label(label="%.1f %%" % cpu, css_classes=["numeric"] + (
                ["error"] if cpu > 80 else ["warning"] if cpu > 30 else ["dim-label"])))
            self.n_procs.add(row)
            self.n_proc_rows.append(row)
        vty = d.get("vty") or []
        if [(x["hote"], x["port"]) for x in vty] != [(x["hote"], x["port"]) for x in self.n_vty]:
            self.n_vty = vty
            self.n_vty_sel.set_model(Gtk.StringList.new(
                ["%s  (%s:%d)" % (x.get("invite") or x["nom"], x["hote"], x["port"]) for x in vty] or ["(aucun démon)"]))
        self._console_ready(bool(vty))

    def _console_ready(self, ok):
        """Sans démon VTY, rien à interroger : commandes et raccourcis grisés."""
        if not ok:
            self.n_vty = []
            self.n_vty_sel.set_model(Gtk.StringList.new(["(aucun démon)"]))
        for w in self.n_console_widgets:
            w.set_sensitive(ok)

    def on_vty(self, cmd):
        cmd = cmd.strip()
        if not cmd:
            return
        if self.node is None:
            return self.toast("Pas de nœud : donne son adresse dans Compte")
        if not self.n_vty:
            self.node_etat_at = 0
            return self.toast("Aucun démon VTY connu pour l'instant (page Services)")
        d = self.n_vty[min(self.n_vty_sel.get_selected(), len(self.n_vty) - 1)]
        buf = self.n_out.get_buffer()

        def done(res, err):
            buf.insert(buf.get_end_iter(), ((res or {}).get("sortie") or (res or {}).get("err")
                                            or "erreur : %s" % (err or res)) + "\n\n")
            GLib.idle_add(lambda: self.n_out.scroll_to_iter(buf.get_end_iter(), 0, False, 0, 1) and False)
        self._in_thread(lambda: self.node.console(d["hote"], d["port"], cmd), done)
        self.n_cmd.set_text("")

    # ── Compte ──
    def _page_account(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        grp = Adw.PreferencesGroup(title="Nœud", description="Spectres, services, console et trames viennent de "
                                   "cette adresse : IP locale, WireGuard ou VPS.")
        self.a_node = Adw.EntryRow(title="Adresse du nœud", show_apply_button=True)
        self.a_node.connect("apply", lambda r: self.set_node(node_host(r.get_text()), announce=True))
        grp.add(self.a_node)
        self.a_trunk = Adw.ComboRow(title="Sortie", subtitle="appels vers l'extérieur",          # titre court : « Zadarma » était tronqué
                                    model=Gtk.StringList.new(list(TRUNKS.values())), sensitive=False)
        self.a_trunk_quiet = True
        self.a_trunk.connect("notify::selected", self.on_trunk)
        grp.add(self.a_trunk)
        box.append(grp)
        grp = Adw.PreferencesGroup(title="Téléphone")
        self.a_number = Adw.ActionRow(title="Numéro", subtitle="—", css_classes=["property"], subtitle_selectable=True)
        self.a_sip = Adw.ActionRow(title="Compte SIP", subtitle="—", css_classes=["property"])
        self.a_state = Adw.ActionRow(title="État", subtitle="pas de compte", css_classes=["property"])
        for r in (self.a_number, self.a_sip, self.a_state):
            grp.add(r)
        box.append(grp)
        self.a_leave = Gtk.Button(label="Quitter ce nœud", css_classes=["pill", "destructive-action"],
                                  halign=Gtk.Align.CENTER, sensitive=False)
        self.a_leave.connect("clicked", self.on_leave)
        box.append(self.a_leave)
        wipe = Gtk.Button(label="Effacer les appels et conversations", css_classes=["pill"], halign=Gtk.Align.CENTER)
        wipe.connect("clicked", lambda *_: self.on_wipe())
        box.append(wipe)
        grp = Adw.PreferencesGroup(title="Affichage")
        self.a_mode = Adw.ComboRow(title="Mode", subtitle="ce que l'appli montre",
                                   model=Gtk.StringList.new(list(MODES)))
        self.a_mode.connect("notify::selected", lambda r, _p: self.set_mode(r.get_selected()))
        grp.add(self.a_mode)
        # Clair par défaut ; le choix est gardé dans les réglages.
        self.a_theme = Adw.ComboRow(title="Thème", model=Gtk.StringList.new([t for _k, t, _s in THEMES]))
        self.a_theme.set_selected(self.apply_theme(self.settings.get("theme", "clair")))
        self.a_theme.connect("notify::selected", lambda r, _p: self.set_theme(THEMES[r.get_selected()][0]))
        grp.add(self.a_theme)
        box.append(grp)
        box.append(Gtk.Label(label="Network Monitor " + VERSION, css_classes=["dim-label", "caption"]))
        return self._scroll(box)

    def _trunk_shown(self, d, err):
        ok = bool(d) and d.get("trunk") in TRUNKS
        self.a_trunk.set_sensitive(ok)
        self.a_trunk.set_subtitle("appels vers l'extérieur" if ok else "indisponible : le service de réglages ne répond pas")
        self.a_trunk.set_tooltip_text(None if ok else str(err or (d or {}).get("err") or ""))
        if ok:
            self.a_trunk_quiet = True
            self.a_trunk.set_selected(list(TRUNKS).index(d["trunk"]))
            self.a_trunk_quiet = False

    def on_trunk(self, *_):
        if self.a_trunk_quiet or self.node is None:
            return
        choice, node = list(TRUNKS)[self.a_trunk.get_selected()], self.node

        def done(d, err):
            self._trunk_shown(d, err)
            self.toast("Sortie : %s" % TRUNKS[d["trunk"]] if d and d.get("trunk") == choice
                       else "Sortie inchangée : %s" % (err or (d or {}).get("err") or "refus du nœud"))
        self._in_thread(lambda: node.trunk(choice), done)

    def apply_theme(self, key):
        """Applique le thème, rend son rang dans THEMES."""
        i = next((n for n, (k, _t, _s) in enumerate(THEMES) if k == key), 0)
        Adw.StyleManager.get_default().set_color_scheme(THEMES[i][2])
        return i

    def set_theme(self, key):
        self.apply_theme(key)
        if not self.demo and self.settings.get("theme") != key:
            self.settings["theme"] = key
            save_settings(self.settings)

    def set_mode(self, mode):
        mode = max(0, min(len(MODES) - 1, mode))
        if getattr(self, "mode", None) == mode:
            return
        self.mode = mode
        if self.a_mode.get_selected() != mode:
            self.a_mode.set_selected(mode)
        for tab, pages, swiper in ((self.tab_radio, MODE_RADIO[mode], self.stack),
                                   (self.tab_node, MODE_NODE[mode], self.node_swiper)):
            tab.set_visible(pages is not None)
            if pages is not None:
                swiper.restrict(pages)
        self.a_trunk.set_visible(mode == 2)
        if mode == 0 and self.tabs.get_visible_child_name() in ("radio", "node"):
            self.tabs.set_visible_child_name("phone")
        if not self.demo and self.settings.get("mode") != mode:
            self.settings["mode"] = mode
            save_settings(self.settings)

    def set_node(self, host, announce=False):
        """L adresse du nœud : tout ce qui vient du banc repart de là."""
        self.node = (DemoNode if self.demo else NodeClient)(host) if host else None
        for row in (self.a_node, self.j_node):
            if row.get_text() != host:
                row.set_text(host)
        if not self.demo:
            self.settings["node"] = host
            save_settings(self.settings)
            if self._frame_cls and host:
                if self.frame_source:
                    self.frame_source.stop()
                self.frame_source = self._frame_cls(["ws://%s:%d/" % (host, PORT_WS)] + list(self._frame_urls),
                                                    self.add_frame, self.on_frames_status)
        self.node_etat_at = 0
        if self.node is not None:
            self._in_thread(self.node.trunk, self._trunk_shown)
        else:
            self._trunk_shown(None, "pas de nœud")
        if announce:
            self.toast("Nœud : %s" % host if host else "Plus de nœud")

    # ── les événements SIP ──
    def on_reg(self, ok, text):
        state = ("En ligne" if ok else "Hors ligne" + (" · " + text if text else "")) if self.account else ""
        self.title.set_title(self.account["msisdn"] if self.account else "Network Monitor")
        self.title.set_subtitle(state)
        self.a_state.set_subtitle(state or "pas de compte")
        self.dot.set_visible(self.account is not None)
        (self.dot.add_css_class if ok else self.dot.remove_css_class)("on")
        return True

    def on_call(self, state, peer):
        self.call_state = None if state == "ended" else state
        busy = self.call_state is not None
        self.b_call.set_visible(not busy)
        self.b_hang.set_visible(busy)
        self.b_answer.set_visible(state == "incoming")
        self.call_card.set_visible(busy)
        # Pas de clavier ni de champ de numéro pendant un appel : la carte au centre,
        # décrocher / raccrocher dessous. (Il débordait de l écran et décentrait tout.)
        for w in (self.number, self.dial_grid) + tuple(self.dial_side):
            w.set_visible(not busy)
        # Les bascules de l appel : visibles une fois décroché ou en train d appeler,
        # remises à zéro à la fin (micro rouvert, haut-parleur coupé).
        self.call_tools.set_visible(busy and state != "incoming")
        self._call_keys(busy and self.t_keys.get_active())
        if not busy:
            for t in (self.t_mute, self.t_keys, self.t_spk):
                if t.get_active():
                    t.set_active(False)
        for c in ("live", "ring"):
            self.call_card.remove_css_class(c)
        if busy:
            self.call_card.add_css_class("live" if state == "active" else "ring")
            self.call_peer.set_text(peer)
            self.call_info.set_text(CALL_TEXT[state])
            if state == "active":
                self.call_since = time.monotonic()
            if state == "incoming":
                self.tabs.set_visible_child_name("phone")
                self.present()
        return True

    def on_message(self, peer, text):
        self._bubble(peer, text, out=False)
        if not self.m_to.get_text():
            self.m_to.set_text(peer)
        if self.tabs.get_visible_child_name() != "messages":
            self.msg_page.set_needs_attention(True)
            self.toast("SMS de %s" % peer)
        return True

    def _call_tick(self):
        if self.call_state == "active":
            s = int(time.monotonic() - self.call_since)
            self.call_info.set_text("%s · %02d:%02d" % (CALL_TEXT["active"], s // 60, s % 60))
        return True


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
.frame-row { padding: 8px 12px 8px 10px; border-bottom: 1px solid alpha(currentColor, 0.07);
             border-left: 4px solid transparent; }
.frame-title { font-size: 15px; font-weight: 700; }
.frame-row.kind-bcch .frame-title, .frame-row.kind-mon .frame-title { font-weight: 500; opacity: 0.8; }
.frame-row.key { box-shadow: inset 0 0 0 1px alpha(@accent_color, 0.35); }
.frame-when { font-family: monospace; font-size: 11px; opacity: 0.6; }
.frame-meta { font-size: 12px; opacity: 0.75; }
.proto { font-size: 10px; font-weight: 800; letter-spacing: 0.04em; padding: 1px 7px; border-radius: 99px;
         color: white; background-color: alpha(currentColor, 0.15); }
.dirchip { font-size: 11px; font-weight: 700; padding: 1px 7px; border-radius: 99px; }
.dirchip.dir-ul { color: @warning_color; background-color: alpha(#e5a50a, 0.18); }
.dirchip.dir-dl { color: @accent_color; background-color: alpha(#3584e4, 0.18); }
.fcat { font-size: 12px; padding: 2px 10px; min-height: 26px; border-radius: 99px; }
.frame-row.kind-bcch { border-left-color: #77767b; background-color: alpha(#77767b, 0.06); }
.proto.kind-bcch { background-color: #77767b; }
.frame-row.kind-lapdm { border-left-color: #3584e4; background-color: alpha(#3584e4, 0.14); }
.proto.kind-lapdm { background-color: #3584e4; }
.frame-row.kind-mm { border-left-color: #9141ac; background-color: alpha(#9141ac, 0.14); }
.proto.kind-mm { background-color: #9141ac; }
.frame-row.kind-rr { border-left-color: #26a269; background-color: alpha(#26a269, 0.14); }
.proto.kind-rr { background-color: #26a269; }
.frame-row.kind-cc { border-left-color: #e66100; background-color: alpha(#e66100, 0.14); }
.proto.kind-cc { background-color: #e66100; }
.frame-row.kind-sms { border-left-color: #f5c211; background-color: alpha(#f5c211, 0.14); }
.proto.kind-sms { background-color: #f5c211; }
.frame-row.kind-core { border-left-color: #2190a4; background-color: alpha(#2190a4, 0.14); }
.proto.kind-core { background-color: #2190a4; }
.frame-row.kind-lte { border-left-color: #cdab8f; background-color: alpha(#cdab8f, 0.14); }
.proto.kind-lte { background-color: #cdab8f; }
.frame-row.kind-rrc { border-left-color: #c061cb; background-color: alpha(#c061cb, 0.14); }
.proto.kind-rrc { background-color: #c061cb; }
.frame-row.kind-nas { border-left-color: #ed333b; background-color: alpha(#ed333b, 0.14); }
.proto.kind-nas { background-color: #ed333b; }
.frame-row.kind-mon { border-left-color: #5e5c64; background-color: alpha(#5e5c64, 0.06); }
.proto.kind-mon { background-color: #5e5c64; }
.frame-row.kind-other { border-left-color: #77767b; background-color: alpha(#77767b, 0.06); }
.proto.kind-other { background-color: #77767b; }
.proto.kind-sms { color: #241f31; }
.hero { background-image: linear-gradient(160deg, alpha(@accent_bg_color, 0.20), alpha(@accent_bg_color, 0.03)); }
.dial-entry { font-size: 34px; font-weight: 300; background: none; box-shadow: none; outline: none; }
.dial-key { font-size: 26px; min-width: 76px; min-height: 76px; border-radius: 38px; }
.dial-key .sub { font-size: 10px; font-weight: 700; opacity: 0.55; letter-spacing: 2px; }
.call-btn { min-width: 68px; min-height: 68px; border-radius: 34px; }
.call-tool { min-width: 52px; min-height: 52px; border-radius: 26px; }
.call-btn.go { background-color: @success_bg_color; color: @success_fg_color; }
.call-card { padding: 28px 18px; border-radius: 24px; }
.call-card.compact { padding: 6px 18px; }
.call-card.compact .call-peer { font-size: 22px; }
.in-call .dial-key { font-size: 21px; min-width: 60px; min-height: 52px; border-radius: 26px; }
.in-call .dial-key .sub { font-size: 0; }
.call-card .call-peer { font-size: 30px; font-weight: 800; }
.call-card .call-info { font-size: 15px; opacity: 0.8; }
.call-card.ring .call-icon { color: @success_color; }
.call-card.live { background-color: alpha(@success_bg_color, 0.22); }
.call-card.ring { background-color: alpha(@accent_bg_color, 0.25); }
.call-peer { font-size: 22px; font-weight: 700; }
.bubble { padding: 8px 12px; border-radius: 16px; }
.bubble.in { background-color: alpha(currentColor, 0.10); border-bottom-left-radius: 4px; }
.bubble.out { background-color: @accent_bg_color; color: @accent_fg_color; border-bottom-right-radius: 4px; }
.bubble-meta { font-size: 11px; opacity: 0.6; }
.dot { min-width: 10px; min-height: 10px; border-radius: 5px; background-color: @error_bg_color; }
.dot.on { background-color: @success_bg_color; }
.console { font-size: 12px; background: none; }
"""
KINDS = ("bcch", "lapdm", "mm", "rr", "cc", "sms", "core", "lte", "rrc", "nas", "mon", "other")


# ── La fenetre ──────────────────────────────────────────────────────────────
class Window(NodeUi, Adw.ApplicationWindow):
    def __init__(self, app, source, frame_source_cls=None, frames_urls=(), demo=False):
        super().__init__(application=app, title="Network Monitor", default_width=380, default_height=740)
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
        self.title = Adw.WindowTitle(title="Network Monitor")
        header.set_title_widget(self.title)
        self.dot = Gtk.Box(css_classes=["dot"], valign=Gtk.Align.CENTER, margin_start=6, visible=False)
        header.pack_start(self.dot)
        menu = Gio.Menu()
        menu.append("Balayer les réseaux", "win.scan")
        menu.append("Enregistrer en CSV", "win.record")
        menu.append("Effacer le journal", "win.clear")
        menu.append("À propos", "win.about")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu))
        self.rec_icon = Gtk.Image(icon_name="media-record-symbolic", visible=False, css_classes=["error"])
        header.pack_end(self.rec_icon)
        view.add_top_bar(header)
        # Cinq onglets en bas ; « Radio » et « Nœud » se feuillettent du doigt.
        self.tabs = Adw.ViewStack()
        view.set_content(self.tabs)
        view.add_bottom_bar(Adw.ViewSwitcherBar(stack=self.tabs, reveal=True))

        self.demo = demo
        self.settings = {} if demo else load_settings()
        self.account = self.node = self.call_state = self.frame_source = None
        self.call_since = 0
        self._frame_cls, self._frame_urls = frame_source_cls, frames_urls
        self.sip = (DemoSip if demo else PjSip)(self.on_reg, self.on_call, self.on_message)

        self.stack = Swiper(on_change=lambda *_: self._frames_pin())
        for name, title, page in (("cell", "Cellule", self._page_cell()), ("signal", "Signal", self._page_signal()),
                                  ("neighbors", "Voisines", self._page_neighbors()),
                                  ("frames", "Trames", self._page_frames()), ("log", "Journal", self._page_log())):
            self.stack.add(name, title, page)
        self.tabs.add_titled_with_icon(self._page_phone(), "phone", "Téléphone", "call-start-symbolic")
        self.msg_page = self.tabs.add_titled_with_icon(self._page_messages(), "messages", "Messages",
                                                       "mail-unread-symbolic")
        self.tab_radio = self.tabs.add_titled_with_icon(self.stack, "radio", "Radio", "network-cellular-symbolic")
        self.tab_node = self.tabs.add_titled_with_icon(self._page_node(), "node", "Nœud", "network-server-symbolic")
        self.tabs.add_titled_with_icon(self._page_account(), "account", "Compte", "avatar-default-symbolic")
        self.tabs.connect("notify::visible-child-name", lambda *_: self.tabs.get_visible_child_name() == "messages"
                          and self.msg_page.set_needs_attention(False))
        self.tabs.set_visible_child_name("radio")
        mode = os.environ.get("NETMON_MODE", "")
        self.set_mode(MODES.index(mode.capitalize()) if mode.capitalize() in MODES
                      else int(self.settings.get("mode", 2 if demo else 1)))

        for name, cb in (("scan", self.on_scan), ("clear", lambda *_: self.events.remove_all()),
                         ("about", self.on_about)):
            a = Gio.SimpleAction.new(name, None); a.connect("activate", cb); self.add_action(a)
        rec = Gio.SimpleAction.new_stateful("record", None, GLib.Variant.new_boolean(False))
        rec.connect("change-state", self.on_record); self.add_action(rec)

        self.frame_source = frame_source_cls(frames_urls, self.add_frame, self.on_frames_status) if frame_source_cls else None
        self.connect("close-request", lambda *_: (self.sip.stop(), self.frame_source and self.frame_source.stop())
                     and False)
        GLib.timeout_add_seconds(1, self._call_tick)
        host = "démo" if demo else (os.environ.get("NETMON_NODE") or self.settings.get("node") or "")
        if host:
            self.set_node(host)
        if self.settings.get("account"):
            self.use_account(self.settings["account"], save=False)
        if demo:                        # un passé simulé : le graphe n attend pas dix minutes
            now = time.time()
            for i in range(200, 0, -1):
                smp = self.source.sample()
                for k in self.history:
                    self.history[k].append((now - i * POLL_S, smp.get(k)))

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
        self.h_gauge = Gauge()
        box.append(self.h_op); box.append(self.h_reg); box.append(self.h_gauge)
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
        # Les familles en un geste : la diffusion (Paging, SI) noie le reste,
        # « Signalisation » montre Location Update, appels, chiffrement…
        cats = Gtk.Box(spacing=6)
        self.f_cat, first = "all", None
        self.f_cat_btn = {}
        for key, label in (("all", "Tout"), ("sig", "Signalisation"), ("meas", "Mesures"),
                           ("bcast", "Diffusion"), ("sms", "SMS"), ("core", "Cœur")):
            b = Gtk.ToggleButton(label=label, css_classes=["fcat"], active=(key == "all"))
            if first:
                b.set_group(first)
            first = first or b
            b.connect("toggled", self._on_frames_cat, key)
            self.f_cat_btn[key] = b
            cats.append(b)
        v.append(Gtk.ScrolledWindow(child=cats, vscrollbar_policy=Gtk.PolicyType.NEVER,
                                    hscrollbar_policy=Gtk.PolicyType.EXTERNAL))
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

    def _on_frames_cat(self, btn, key):
        if btn.get_active():
            self.f_cat = key
            self.f_custom.changed(Gtk.FilterChange.DIFFERENT)

    def _frame_match(self, item):
        if self.f_cat != "all" and item.cat != self.f_cat:
            return False
        words = self.f_filter.get_text().lower().split()
        return all(w in item.haystack for w in words)

    def _frame_setup(self, _factory, li):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, css_classes=["frame-row"])
        top = Gtk.Box(spacing=8)
        box.title = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END, css_classes=["frame-title"])
        box.when = Gtk.Label(xalign=1, css_classes=["frame-when"])
        top.append(box.title); top.append(box.when)
        mid = Gtk.Box(spacing=6)
        box.proto = Gtk.Label(css_classes=["proto"], valign=Gtk.Align.CENTER)
        box.dir = Gtk.Label(css_classes=["dirchip"], valign=Gtk.Align.CENTER)
        box.where = Gtk.Label(xalign=0, hexpand=True, css_classes=["frame-meta"], ellipsize=Pango.EllipsizeMode.END)
        for w in (box.proto, box.dir, box.where):
            mid.append(w)
        box.detail = Gtk.Label(xalign=0, css_classes=["caption", "dim-label"], ellipsize=Pango.EllipsizeMode.END)
        box.append(top); box.append(mid); box.append(box.detail)
        li.set_child(box)

    def _frame_bind(self, _factory, li):
        box, it = li.get_child(), li.get_item()
        box.title.set_text(it.title)
        box.title.set_tooltip_text(it.info or None)
        box.when.set_text(it.when)
        box.proto.set_text(FAMILY.get(it.kind, it.proto))
        box.proto.set_tooltip_text(it.proto)
        for k in KINDS:
            box.proto.remove_css_class("kind-" + k); box.remove_css_class("kind-" + k)
        box.proto.add_css_class("kind-" + it.kind); box.add_css_class("kind-" + it.kind)
        (box.add_css_class if it.key else box.remove_css_class)("key")
        box.dir.remove_css_class("dir-ul"); box.dir.remove_css_class("dir-dl")
        if it.direction == "UL":
            box.dir.set_text("↑ MS → réseau"); box.dir.add_css_class("dir-ul")
        elif it.direction == "DL":
            box.dir.set_text("↓ réseau → MS"); box.dir.add_css_class("dir-dl")
        box.dir.set_visible(it.direction in ("UL", "DL"))
        box.where.set_text(it.meta)
        rest = it.full if it.full and it.full != it.title and not it.info.lower().startswith("mon:") else ""
        detail = " · ".join(x for x in (rest, it.detail) if x)
        box.detail.set_text(detail); box.detail.set_visible(bool(detail))

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
        self.h_gauge.set_value(m, val, lvl)

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
        # Une ligne sans objet dans la techno courante ne s affiche pas : « LAC — »
        # en LTE (c est le TAC qui compte), « TAC — » en 2G.
        for k in ("lac", "tac", "enb", "arfcn", "earfcn", "pci"):
            r[k].set_visible(r[k].value.get_text() != "—")
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
        renderer = Gsk.CairoRenderer.new()
        renderer.realize(None)

        def radio(name):
            return lambda: (self.tabs.set_visible_child_name("radio"), self.stack.show(name, False))

        def node(name):
            def go():
                for src, sp in self.spec.items():       # une cascade pleine, sans attendre
                    for _ in range(sp.ROWS):
                        self.node.t0 -= 0.25
                        sp.push(self.node.psd(src))
                self.tabs.set_visible_child_name("node")
                self.node_swiper.show(name, False)
                if name == "console":
                    self.on_vty("show subscriber all")
            return go
        steps = [("cellule", radio("cell")), ("signal", radio("signal")), ("voisines", radio("neighbors")),
                 ("trames", radio("frames")), ("journal", radio("log")),
                 ("spectre-bts", node("bts")), ("spectre-ms", node("ms")),
                 ("services", node("services")), ("console", node("console")),
                 ("rejoindre", lambda: self.tabs.set_visible_child_name("phone")),
                 ("telephone", lambda: (self.use_account(self.node.enrol("", "")), self.number.set_text("100102"),
                                        self.on_call("active", "100102"))),
                 ("messages", lambda: (self.on_call("ended", ""), self._bubble("100102", "Oui, 5 sur 5.", True),
                                       self.tabs.set_visible_child_name("messages"))),
                 ("compte", lambda: self.tabs.set_visible_child_name("account"))]

        def shoot(i):
            if i >= len(steps):
                renderer.unrealize()
                self.get_application().quit()
                return False
            steps[i][1]()
            GLib.timeout_add(1500, snap, i)
            return False

        def snap(i):
            scale = self.get_scale_factor()
            w, h = self.get_width(), self.get_height()
            snapshot = Gtk.Snapshot()
            snapshot.scale(scale, scale)
            Gtk.WidgetPaintable.new(self).snapshot(snapshot, w, h)
            node_ = snapshot.to_node()
            if node_ is None:            # pas encore dessine (surface cachee) : on reessaie
                GLib.timeout_add(1000, snap, i)
                return False
            tex = renderer.render_texture(node_, Graphene.Rect().init(0, 0, w * scale, h * scale))
            path = os.path.join(directory, "%02d-%s.png" % (i + 1, steps[i][0]))
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
                         self.frames_urls, demo=self.demo)
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
