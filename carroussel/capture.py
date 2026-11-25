#!/usr/bin/env python3
# capture.py - les captures du carrousel, prises DANS la VM postmarketOS.
#
# Lance par carroussel.sh, jamais a la main. Pilote la vraie appli (pas le
# mode demo) sur un compte SIP a part, et joue un scenario :
#   telephone, messages vides, SMS sortant, SMS entrant, appel sortant (600),
#   appel entrant, appel en cours, puis - pendant l appel, quand le mobile
#   emet - le spectre montant et les trames de signalisation, puis le reste.
# Les deux evenements entrants viennent du mobile GSM du banc : le script pose
# un drapeau (want-mt, want-call, want-hangup) dans CAPTURE_OUT, et l hote le
# lit et agit par le VTY du mobile.
# Phosh ne dessine que l appli au premier plan : la fenetre se remet devant a
# chaque etape - ne pas toucher au telephone pendant la capture.
import importlib.util,importlib.machinery,sys,os,time
APPLI=os.environ.get("CAPTURE_APPLI","/usr/bin/network-monitor")
PAIR=os.environ.get("CAPTURE_PAIR","100101")      # le mobile GSM du banc
l=importlib.machinery.SourceFileLoader("nm",APPLI);s=importlib.util.spec_from_loader("nm",l);m=importlib.util.module_from_spec(s);l.exec_module(m)
from gi.repository import Gtk, Gsk, GLib, Graphene
OUT=os.environ.get("CAPTURE_OUT","/tmp/vrai")
def start(self, directory):
    os.makedirs(OUT, exist_ok=True)
    r = Gsk.CairoRenderer.new(); r.realize(None)
    _t, self.toast = self.toast, (lambda *_a: None)     # une base vierge, sans la bulle « effacés » sur les photos
    self.on_wipe(); self.toast = _t
    def flag(n): open(OUT + "/" + n, "w").close()
    def radio(n): return lambda: (self.tabs.set_visible_child_name("radio"), self.stack.show(n, False))
    def node(n): return lambda: (self.tabs.set_visible_child_name("node"), self.node_swiper.show(n, False))
    def nbulles():
        n, c = 0, self.m_list.get_first_child()
        while c: n += 1; c = c.get_next_sibling()
        return n
    st = {}
    def online(): return "En ligne" in (self.a_state.get_subtitle() or "")
    def sms_mo():
        self.tabs.set_visible_child_name("messages")
        self.m_to.set_text(PAIR); self.m_text.set_text("Salut %s, ici %s" % (PAIR, self.account["msisdn"])); self.on_send(None)
    def sms_mt():
        st["n"] = nbulles(); flag("want-mt"); self.tabs.set_visible_child_name("messages")
    def sortant():
        self.tabs.set_visible_child_name("phone"); self.number.set_text("600"); self.on_dial()
    def entrant():
        self.sip.hangup(); GLib.timeout_add(4000, lambda: flag("want-call") and False)
    def trames():
        self.tabs.set_visible_child_name("radio"); self.stack.show("frames", False)
        self.f_cat_btn["sig"].set_active(True)
    def fin():
        self.sip.hangup(); flag("want-hangup"); self.tabs.set_visible_child_name("account")
    steps = [("telephone", lambda: self.tabs.set_visible_child_name("phone"), 2500, online),
             ("messages-vide", lambda: self.tabs.set_visible_child_name("messages"), 2500, None),
             ("sms-sortant", sms_mo, 20000, None),
             ("sms-entrant", sms_mt, 3000, lambda: nbulles() > st.get("n", 0)),
             ("appel-sortant", sortant, 3000, lambda: self.call_state in ("ringing", "active")),
             ("appel-entrant", entrant, 3000, lambda: self.call_state == "incoming"),
             ("appel-en-cours", lambda: self.sip.answer(), 2000, lambda: self.call_state == "active"),
             ("spectre-ms", node("ms"), 12000, None),
             ("trames", trames, 6000, lambda: self.f_model.get_n_items() >= 6),
             ("cellule", radio("cell"), 3000, None), ("signal", radio("signal"), 3000, None),
             ("voisines", radio("neighbors"), 3000, None), ("journal", radio("log"), 3000, None),
             ("spectre-bts", node("bts"), 12000, None), ("services", node("services"), 5000, None),
             ("console", node("console"), 3000, None), ("compte", fin, 3000, None)]
    def shoot(i):
        if i >= len(steps):
            r.unrealize(); self.get_application().quit(); return False
        self.present(); steps[i][1](); st["t0"] = time.monotonic(); GLib.timeout_add(steps[i][2], wait, i); return False
    def wait(i):
        cond = steps[i][3]
        if cond and not cond() and time.monotonic() - st["t0"] < 75:
            # la radio du banc rate parfois un SMS ou un appel : on redemande a 25 s et 50 s
            relance = {"sms-entrant": "want-mt", "appel-entrant": "want-call"}.get(steps[i][0])
            palier = int(time.monotonic() - st["t0"]) // 25
            if relance and palier in (1, 2) and st.get("relance") != (i, palier):
                st["relance"] = (i, palier); flag(relance); print("relance", steps[i][0], flush=True)
            GLib.timeout_add(1000, wait, i); return False
        if cond and not cond(): print("attente vaine", steps[i][0], flush=True)
        GLib.timeout_add(1500 if cond else 0, snap, i); return False
    def snap(i):
        sc = self.get_scale_factor(); w, h = self.get_width(), self.get_height()
        sn = Gtk.Snapshot(); sn.scale(sc, sc); Gtk.WidgetPaintable.new(self).snapshot(sn, w, h)
        nd = sn.to_node()
        if nd is None:
            self.present(); GLib.timeout_add(1000, snap, i); return False
        r.render_texture(nd, Graphene.Rect().init(0, 0, w*sc, h*sc)).save_to_png("%s/%02d-%s.png" % (OUT, i+1, steps[i][0]))
        print("capture", steps[i][0], flush=True); shoot(i+1); return False
    GLib.timeout_add(5000, shoot, 0)
m.Window.start_screenshots = start
sys.argv = ["network-monitor", "--screenshot", OUT]; m.main()
