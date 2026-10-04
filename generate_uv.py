"""Consulta getRecienteUvb (DMC) y genera docs/index.html responsivo.
Variables de entorno: DMC_USER, DMC_TOKEN. Nunca escribas el token en el código."""
import os, sys, json, unicodedata, datetime as dt, urllib.parse, requests
from zoneinfo import ZoneInfo

URL = "https://climatologia.meteochile.gob.cl/application/servicios/getRecienteUvb"
TZ = ZoneInfo("America/Santiago")
# Planta -> estación de referencia más cercana (editable)
# 370033 = María Dolores, Los Ángeles Ad. (Concepción sería 360019)
SITES = [("Nacimiento", "Planta Santa Fe", "370033"),
         ("Laja", "Planta Laja", "370033")]
CATS = [(2, "Bajo", "#2e9e4f", "🟢", "No requiere protección especial."),
        (5, "Moderado", "#b8960b", "🟡", "FPS 30+, lentes UV y cubrenuca."),
        (7, "Alto", "#e07b12", "🟠", "FPS 30+ cada 2 h, manga larga, evitar sol 12-16 h."),
        (10, "Muy alto", "#d63a2f", "🔴", "FPS 50+, protección total, pausas en sombra."),
        (99, "Extremo", "#8e44ad", "🟣", "FPS 50+, evitar tareas al sol en horas centrales.")]

norm = lambda s: unicodedata.normalize("NFD", str(s)).encode("ascii", "ignore").decode().lower()
cat = lambda v: next(c for c in CATS if v <= c[0])

def station_name(d):
    """Si el dict describe una estación (trae codigoNacional), devuelve 'código nombre'."""
    low = {k.lower(): v for k, v in d.items()}
    if "codigonacional" in low:
        return f"{low['codigonacional']} {low.get('nombreestacion', '')}"
    return None

def walk(node, name, out):
    """Recorre el JSON. El nombre de la estación puede venir en un dict hermano
    de la lista de mediciones, así que se busca entre los valores del nivel actual."""
    if isinstance(node, dict):
        name = station_name(node) or name
        for v in node.values():
            if isinstance(v, dict) and station_name(v):
                name = station_name(v)
        low = {k.lower(): v for k, v in node.items()}
        if "indiceuv" in low and not isinstance(low["indiceuv"], (list, dict)):
            ts = low.get("momento") or (f"{low['fecha']} {low.get('hora', '00:00:00')}" if "fecha" in low else None)
            try: out.append((name, ts, float(low["indiceuv"])))
            except (TypeError, ValueError): pass
        for v in node.values(): walk(v, name, out)
    elif isinstance(node, list):
        for v in node: walk(v, name, out)

def to_local(ts):
    s = str(ts).replace("Z", "").replace("T", " ")
    for f in ("%d-%m-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try: t = dt.datetime.strptime(s, f); break
        except ValueError: pass
    else: raise ValueError(f"Formato de fecha desconocido: {ts}")
    return t.replace(tzinfo=dt.timezone.utc).astimezone(TZ)  # la API entrega UTC

r = requests.get(URL, params={"usuario": os.environ["DMC_USER"], "token": os.environ["DMC_TOKEN"]}, timeout=60)
r.raise_for_status()
r.encoding = "utf-8"
recs = []; walk(r.json(), None, recs)
if not recs:
    sys.exit("No encontré 'indiceUV' en la respuesta; revisa la estructura:\n" + json.dumps(r.json(), ensure_ascii=False)[:1500])

rows = []
for town, plant, ref in SITES:
    mine = [(to_local(t), v) for n, t, v in recs if n and norm(ref) in norm(n) and t]
    if not mine:
        names = sorted({str(n) for n, _, _ in recs})
        print("Estaciones encontradas:", names)
        print("Muestra del JSON:", json.dumps(r.json(), ensure_ascii=False)[:1500])
        sys.exit(f"Sin datos para la estación {ref}")
    day = max(d.date() for d, _ in mine)                 # último día con datos
    peak = max((v, d) for d, v in mine if d.date() == day)
    rows.append((town, plant, ref, day, peak[0], peak[1], cat(round(peak[0]))))

hoy = dt.datetime.now(TZ)
msg = ["☀️ *REPORTE ÍNDICE UV*", f"📅 {hoy:%d-%m-%Y}", ""]
for town, plant, ref, day, v, t, c in rows:
    msg += [f"{c[3]} *{town} – {plant}*", f"IUV máx {v:.0f} ({c[1]}) · {day:%d-%m} {t:%H:%M} h · ref. {ref}", f"• {c[4]}", ""]
msg.append("Fuente: Dirección Meteorológica de Chile")
text = "\n".join(msg)

cards = "".join(f"""<article><div class="t" style="background:{c[2]}"><small>{plant} · ref. {ref}</small><h2>{town}</h2>
<div class="b"><b>{v:.0f}</b><span>{c[1]}</span></div></div><p>Máximo del {day:%d-%m} a las {t:%H:%M} h</p><p>{c[4]}</p></article>"""
                for town, plant, ref, day, v, t, c in rows)
html = f"""<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><title>Reporte UV</title>
<style>:root{{--bg:#f4f6f8;--c:#fff;--tx:#14202b}}@media(prefers-color-scheme:dark){{:root{{--bg:#0e151b;--c:#18222b;--tx:#eaf0f4}}}}
body{{margin:0;padding:16px;background:var(--bg);color:var(--tx);font-family:system-ui,sans-serif}}
main{{max-width:820px;margin:auto;display:grid;gap:14px}}@media(min-width:640px){{.g{{grid-template-columns:1fr 1fr}}}}
.g{{display:grid;gap:14px}}article{{background:var(--c);border-radius:16px;overflow:hidden}}
.t{{color:#fff;padding:14px 16px}}h2{{margin:0}}.b{{display:flex;gap:10px;align-items:baseline}}.b b{{font-size:3.5rem}}
article p{{margin:10px 16px}}a{{display:block;background:#25d366;color:#06240f;font-weight:600;text-align:center;padding:14px;border-radius:10px;text-decoration:none}}
small,.f{{opacity:.8}}.f{{font-size:.8rem}}</style></head><body><main>
<h1>☀️ Reporte Índice UV</h1><div class="f">Actualizado {hoy:%d-%m-%Y %H:%M} (hora Chile)</div>
<div class="g">{cards}</div>
<a href="https://wa.me/?text={urllib.parse.quote(text)}">Compartir por WhatsApp</a>
<div class="f">Fuente: Dirección Meteorológica de Chile. Valores medidos en la estación de referencia, no en la planta.</div>
</main></body></html>"""
os.makedirs("docs", exist_ok=True)
open("docs/index.html", "w", encoding="utf-8").write(html)
open("docs/mensaje.txt", "w", encoding="utf-8").write(text)
print(text)
