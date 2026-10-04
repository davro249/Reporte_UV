"""Consulta getRecienteUvb (DMC) y genera docs/index.html con el diseño de template.html.
Variables de entorno: DMC_USER, DMC_TOKEN. Nunca escribas el token en el código."""
import os, re, sys, json, unicodedata, datetime as dt, requests
from zoneinfo import ZoneInfo

print(">>> generate_uv.py VERSION 4 (plantilla template.html)")
URL = "https://climatologia.meteochile.gob.cl/application/servicios/getRecienteUvb"
CL = ZoneInfo("America/Santiago")
HERE = os.path.dirname(os.path.abspath(__file__))

# Estaciones a mostrar: (nombre, detalle, código nacional)
STATIONS = [("Concepción", "Carriel Sur (360019)", "360019"),
            ("Los Ángeles", "María Dolores (370033)", "370033")]
# Plantas: (nombre, localidad, estación de referencia)
PLANTS = [("Planta Santa Fe", "Nacimiento", "Los Ángeles"),
          ("Planta Laja", "Laja", "Los Ángeles")]

def station_name(d):
    low = {k.lower(): v for k, v in d.items()}
    if "codigonacional" in low:
        return f"{low['codigonacional']} {low.get('nombreestacion', '')}"

def walk(node, name, out):
    """Recorre el JSON sin asumir su estructura exacta (la estación es dict hermano de la lista)."""
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

def source_tz(js):
    """Usa el campo 'timezone' de la cabecera si existe; si no, UTC."""
    tzv = None
    if isinstance(js, dict):
        tzv = next((v for k, v in js.items() if k.lower() == "timezone"), None)
    print("timezone de la API:", tzv)
    try: return ZoneInfo(str(tzv))
    except Exception: return dt.timezone.utc

def parse(ts, tz):
    s = str(ts).replace("Z", "").replace("T", " ")
    for f in ("%d-%m-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try: return dt.datetime.strptime(s, f).replace(tzinfo=tz).astimezone(CL)
        except ValueError: pass
    raise ValueError(f"Formato de fecha desconocido: {ts}")

norm = lambda s: unicodedata.normalize("NFD", str(s)).encode("ascii", "ignore").decode().lower()

r = requests.get(URL, params={"usuario": os.environ["DMC_USER"], "token": os.environ["DMC_TOKEN"]}, timeout=60)
r.raise_for_status(); r.encoding = "utf-8"
js = r.json()
recs = []; walk(js, None, recs)
if not recs:
    sys.exit("No encontré 'indiceUV' en la respuesta:\n" + json.dumps(js, ensure_ascii=False)[:1500])
tz = source_tz(js)

stations, days = [], []
for name, detail, code in STATIONS:
    mine = [(parse(t, tz), v) for n, t, v in recs if n and code in norm(n) and t]
    if not mine:
        print(f"AVISO: sin datos para {name} ({code}); se omite"); continue
    day = max(d.date() for d, _ in mine)                      # último día con datos
    v, t = max((v, d) for d, v in mine if d.date() == day)    # máximo de ese día
    stations.append({"name": name, "detail": detail, "uv": round(v, 1), "hora": f"{t:%H:%M}"})
    days.append(day)
    print(f"{name}: IUV máx {v:.1f} el {day} a las {t:%H:%M}")

if not any(s["name"] == "Los Ángeles" for s in stations):
    sys.exit("Sin datos de Los Ángeles (referencia de las plantas); no se actualiza la página.")

now = dt.datetime.now(CL)
data = {"fecha": max(days).isoformat(), "actualizado": now.strftime("%Y-%m-%dT%H:%M"),
        "stations": stations,
        "plants": [{"name": n, "town": t, "ref": ref} for n, t, ref in PLANTS], "demo": False}

tpl = open(os.path.join(HERE, "template.html"), encoding="utf-8").read()
block = "/* DATOS_INICIO */\nconst DATA=" + json.dumps(data, ensure_ascii=False) + ";\n/* DATOS_FIN */"
html, n = re.subn(r"/\* DATOS_INICIO.*?DATOS_FIN \*/", lambda m: block, tpl, flags=re.S)
if n != 1: sys.exit("No encontré los marcadores DATOS_INICIO / DATOS_FIN en template.html")
os.makedirs("docs", exist_ok=True)
open("docs/index.html", "w", encoding="utf-8").write(html)
print("Página generada: docs/index.html")
