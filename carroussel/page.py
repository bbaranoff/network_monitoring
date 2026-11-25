#!/usr/bin/env python3
# page.py <dossier> - ecrit <dossier>/index.html : le carrousel des PNG du dossier.
# Fleches, clavier, balayage, vignettes, defilement automatique. Aucun reseau :
# la page et les images suffisent.
import json, os, sys

TITRES = {"telephone": "Téléphone", "messages-vide": "Messages", "sms-sortant": "SMS sortant",
          "sms-entrant": "SMS entrant", "appel-sortant": "Appel sortant", "appel-entrant": "Appel entrant",
          "appel-en-cours": "Appel en cours", "spectre-ms": "Spectre MS (montant)", "trames": "Trames",
          "cellule": "Cellule", "signal": "Signal", "voisines": "Voisines", "journal": "Journal",
          "spectre-bts": "Spectre BTS (descendant)", "services": "Services", "console": "Console VTY",
          "compte": "Compte", "rejoindre": "Rejoindre un nœud", "messages": "Messages"}

PAGE = """<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>network-monitor — captures</title>
<style>
*{box-sizing:border-box;margin:0}
html,body{height:100%}
body{background:radial-gradient(ellipse at top,#eef1f6,#cfd6e2 75%);color:#1d1d24;font:15px/1.4 system-ui,sans-serif;
 display:flex;flex-direction:column;align-items:center;justify-content:center;gap:16px;padding:18px;overflow:hidden}
h1{font-size:20px;font-weight:700;text-align:center}
h1 small{display:block;font-size:13px;font-weight:400;opacity:.6;margin-top:2px}
.stage{display:flex;align-items:center;justify-content:center;gap:18px;width:100%;max-width:900px}
.viewport{overflow:hidden;height:min(72vh,720px);aspect-ratio:var(--ratio);border-radius:26px;background:#fff;touch-action:pan-y;
 box-shadow:0 0 0 9px #15151a,0 0 0 10px #55555f,0 26px 60px #0006}
.track{display:flex;height:100%;transition:transform .45s cubic-bezier(.2,.8,.2,1)}
.track img{flex:0 0 100%;width:100%;height:100%;object-fit:cover;object-position:top;user-select:none;-webkit-user-drag:none}
.nav{flex:none;width:46px;height:46px;border-radius:50%;border:0;background:#0000001a;color:#1d1d24;font-size:22px;cursor:pointer}
.nav:hover{background:#00000030}
.cap{font-size:17px;font-weight:600;text-align:center;min-height:1.4em}
.cap span{opacity:.5;font-weight:400;margin-left:6px;font-variant-numeric:tabular-nums}
.dots{display:flex;gap:8px;flex-wrap:wrap;justify-content:center}
.dots button{width:9px;height:9px;padding:0;border-radius:50%;border:0;background:#00000030;cursor:pointer;transition:all .2s}
.dots button.on{background:#26a269;width:24px;border-radius:5px}
.thumbs{display:flex;gap:8px;overflow-x:auto;max-width:min(900px,96vw);padding:4px}
.thumbs img{height:64px;border-radius:6px;opacity:.5;cursor:pointer;border:2px solid transparent;transition:opacity .2s}
.thumbs img.on{opacity:1;border-color:#26a269}
@media (max-width:560px){.nav,.thumbs{display:none}}
</style></head><body>
<h1>network-monitor<small>moniteur radio et poste du réseau · postmarketOS / Phosh</small></h1>
<div class="stage">
 <button class="nav" id="prev" aria-label="Précédent">‹</button>
 <div class="viewport" id="vp" style="--ratio:__RATIO__"><div class="track" id="track"></div></div>
 <button class="nav" id="next" aria-label="Suivant">›</button>
</div>
<div class="cap" id="cap"></div>
<div class="dots" id="dots"></div>
<div class="thumbs" id="thumbs"></div>
<script>
const S=__SLIDES__;
const track=document.getElementById('track'),dots=document.getElementById('dots'),thumbs=document.getElementById('thumbs'),cap=document.getElementById('cap');
let i=0,timer;
S.forEach((s,k)=>{
 const im=new Image();im.src=s.src;im.alt=s.title;track.appendChild(im);
 const d=document.createElement('button');d.setAttribute('aria-label',s.title);d.onclick=()=>go(k);dots.appendChild(d);
 const t=new Image();t.src=s.src;t.alt=s.title;t.onclick=()=>go(k);thumbs.appendChild(t);
});
function go(k){
 i=(k+S.length)%S.length;track.style.transform=`translateX(${-100*i}%)`;
 cap.innerHTML=`${S[i].title}<span>${i+1} / ${S.length}</span>`;
 [...dots.children].forEach((d,n)=>d.classList.toggle('on',n===i));
 [...thumbs.children].forEach((t,n)=>t.classList.toggle('on',n===i));
 thumbs.children[i].scrollIntoView({block:'nearest',inline:'center',behavior:'smooth'});
 clearInterval(timer);timer=setInterval(()=>go(i+1),4000);
}
document.getElementById('prev').onclick=()=>go(i-1);
document.getElementById('next').onclick=()=>go(i+1);
addEventListener('keydown',e=>{if(e.key==='ArrowLeft')go(i-1);if(e.key==='ArrowRight')go(i+1);if(e.key===' '){e.preventDefault();clearInterval(timer)}});
let x0=null;const vp=document.getElementById('vp');
vp.addEventListener('pointerdown',e=>x0=e.clientX);
vp.addEventListener('pointerup',e=>{if(x0===null)return;const dx=e.clientX-x0;x0=null;if(Math.abs(dx)>40)go(i+(dx<0?1:-1))});
vp.addEventListener('mouseenter',()=>clearInterval(timer));
vp.addEventListener('mouseleave',()=>go(i));
go(0);
</script></body></html>"""


def taille_png(chemin):
    with open(chemin, "rb") as f:
        e = f.read(24)
    return int.from_bytes(e[16:20], "big"), int.from_bytes(e[20:24], "big")


def main():
    d = sys.argv[1] if len(sys.argv) > 1 else "."
    png = sorted(f for f in os.listdir(d) if f.lower().endswith(".png"))
    if not png:
        print("aucun PNG dans %s" % d, file=sys.stderr)
        return 1
    diapos = []
    for f in png:
        cle = f[:-4].split("-", 1)[-1] if f[:2].isdigit() else f[:-4]
        diapos.append({"src": f, "title": TITRES.get(cle, cle.replace("-", " ").capitalize())})
    w, h = taille_png(os.path.join(d, png[0]))
    page = PAGE.replace("__SLIDES__", json.dumps(diapos, ensure_ascii=False)).replace("__RATIO__", "%d/%d" % (w, h))
    with open(os.path.join(d, "index.html"), "w", encoding="utf-8") as f:
        f.write(page)
    print("%s : %d diapositives" % (os.path.join(d, "index.html"), len(diapos)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
