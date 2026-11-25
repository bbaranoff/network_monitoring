#!/usr/bin/env python3
# largeur.py <network-monitor.py> - la fenetre tient-elle dans un telephone ?
#
# A lancer SUR postmarketOS (ou tout GTK 4 + libadwaita), avant chaque paquet :
#   python3 verif/largeur.py /usr/bin/network-monitor
# Mesure la largeur et la hauteur minimales de la fenetre dans les trois modes
# et nomme les widgets qui depassent 360 px. Une page trop large, meme cachee,
# elargit TOUTE la fenetre : sur Phosh, tout se decentre. Deja arrive avec une
# rangee de pilules (console VTY), les marges du clavier, deux boutons de QR code.
# Sort en erreur si un mode depasse LARGEUR x HAUTEUR.
LARGEUR, HAUTEUR = 360, 673
import importlib.util,importlib.machinery,sys
l=importlib.machinery.SourceFileLoader("nm",sys.argv[1]);s=importlib.util.spec_from_loader("nm",l);m=importlib.util.module_from_spec(s);l.exec_module(m)
from gi.repository import Gtk, GLib
def large(w, d=0, out=None):
    out = [] if out is None else out
    mn = w.measure(Gtk.Orientation.HORIZONTAL, -1)[0]
    lab = ""
    for g in ("get_label","get_title","get_text"):
        if hasattr(w,g):
            try: lab = getattr(w,g)() or ""
            except Exception: pass
            if lab: break
    out.append((mn, d, type(w).__name__, str(lab)[:30]))
    c = w.get_first_child()
    while c: large(c, d+1, out); c = c.get_next_sibling()
    return out
pire = [0, 0]
def start(self, d):
    def go():
        for mode in (0,1,2):
            self.set_mode(mode)
            w, h = self.measure(Gtk.Orientation.HORIZONTAL,-1)[0], self.measure(Gtk.Orientation.VERTICAL,LARGEUR)[0]
            pire[0], pire[1] = max(pire[0], w), max(pire[1], h)
            print("MODE", mode, "fenetre min", w, "hauteur min", h)
            for name in ("phone","messages","radio","node","account"):
                page = self.tabs.get_child_by_name(name)
                if page is None: continue
                pm = page.measure(Gtk.Orientation.HORIZONTAL,-1)[0]
                if pm > LARGEUR:
                    print("  page", name, pm)
                    for r in sorted([x for x in large(page) if x[0] > LARGEUR], key=lambda x: x[1])[-3:]: print("     ", r)
        self.get_application().quit()
        if pire[0] > LARGEUR or pire[1] > HAUTEUR:
            print("TROP GRAND : %d x %d pour %d x %d" % (pire[0], pire[1], LARGEUR, HAUTEUR)); sys.exit(1)
        print("OK : %d x %d au plus, pour %d x %d" % (pire[0], pire[1], LARGEUR, HAUTEUR))
    GLib.timeout_add(1500, lambda: go() and False)
m.Window.start_screenshots = start
sys.argv = ["network-monitor","--demo","--screenshot","/tmp/mz"]; m.main()
