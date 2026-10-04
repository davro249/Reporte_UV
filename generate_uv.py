"""Consulta UVB de Meteochile y genera docs/index.html desde template.html.

- Usa solamente observaciones del día actual en Chile.
- Nunca reemplaza el día actual por ayer.
- Si todavía no existen observaciones de hoy, mantiene actual/max en null.
- Intenta obtener además el pronóstico UV oficial de Meteochile para hoy.
- Variables de entorno: DMC_USER, DMC_TOKEN.
"""
import os, re, sys, json, unicodedata, datetime as dt, requests
from zoneinfo import ZoneInfo
from html.parser import HTMLParser

print(">>> generate_uv.py VERSION 6 (hoy + actual + máximo + pronóstico oficial por rango)")

URL = "https://climatologia.meteochile.gob.cl/application/servicios/getRecienteUvb"
FORECAST_URL = "https://www.meteochile.gob.cl/PortalDMC-web/otros_pronosticos/climatologia_pronostico_uv.xhtml"
CL = ZoneInfo("America/Santiago")
HERE = os.path.dirname(os.path.abspath(__file__))

STATIONS = [("Concepción", "Carriel Sur (360019)", "360019"),
            ("Los Ángeles", "María Dolores (370033)", "370033")]
PLANTS = [("Planta Santa Fe", "Nacimiento", "Los Ángeles"),
          ("Planta Laja", "Laja", "Los Ángeles")]

def norm(value):
    return unicodedata.normalize("NFD", str(value)).encode("ascii", "ignore").decode().lower()

def station_name(data):
    if not isinstance(data, dict):
        return None
    low = {str(k).lower(): v for k, v in data.items()}
    if "codigonacional" in low:
        return f"{low['codigonacional']} {low.get('nombreestacion', '')}".strip()
    return None

def walk(node, name, out):
    if isinstance(node, dict):
        name = station_name(node) or name
        for value in node.values():
            if isinstance(value, dict) and station_name(value):
                name = station_name(value)
        low = {str(k).lower(): v for k, v in node.items()}
        if "indiceuv" in low and not isinstance(low["indiceuv"], (list, dict)):
            timestamp = low.get("momento") or (f"{low['fecha']} {low.get('hora', '00:00:00')}" if "fecha" in low else None)
            try:
                out.append((name, timestamp, float(low["indiceuv"])))
            except (TypeError, ValueError):
                pass
        for value in node.values():
            walk(value, name, out)
    elif isinstance(node, list):
        for value in node:
            walk(value, name, out)

def source_tz(js):
    timezone_value = None
    if isinstance(js, dict):
        timezone_value = next((v for k, v in js.items() if str(k).lower() == "timezone"), None)
    print("timezone de la API:", timezone_value)
    try:
        return ZoneInfo(str(timezone_value))
    except Exception:
        return dt.timezone.utc

def parse_timestamp(timestamp, timezone):
    text = str(timestamp).replace("Z", "").replace("T", " ")
    for fmt in ("%d-%m-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d-%m-%Y %H:%M", "%Y-%m-%d %H:%M"):
        try:
            return dt.datetime.strptime(text, fmt).replace(tzinfo=timezone).astimezone(CL)
        except ValueError:
            pass
    raise ValueError(f"Formato de fecha desconocido: {timestamp}")

class ForecastParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text_parts = []
    def handle_data(self, data):
        text = data.strip()
        if text:
            self.text_parts.append(text)
    def get_text(self):
        return " ".join(self.text_parts)

def forecast_range_candidates(text):
    """Devuelve rangos oficiales del tipo 3-5, 6-7, 8-10 o 11+."""
    found = []
    for match in re.finditer(r"(?<!\d)(1\s*[-–]\s*2|3\s*[-–]\s*5|6\s*[-–]\s*7|8\s*[-–]\s*10|11\s*\+)(?!\d)", text):
        value = re.sub(r"\s+", "", match.group(1)).replace("–", "-")
        found.append((match.start(), value))
    return found

def range_upper(value):
    if not value:
        return None
    if "+" in value:
        return 11.0
    return float(value.split("-")[-1])

def get_uv_forecast(today):
    print("Consultando pronóstico UV oficial de Meteochile...")

    # La página oficial usa contenido dinámico. Probamos primero el identificador
    # que actualmente entrega la página oficial para este producto y luego el
    # identificador regional histórico de Biobío como respaldo.
    region_params = ["12", "8", "8a"]
    aliases = ["concepcion", "los angeles", "biobio"]
    date_variants = [
        today.strftime("%d-%m-%Y"),
        today.strftime("%d/%m/%Y"),
        today.strftime("%Y-%m-%d"),
    ]

    for reg in region_params:
        try:
            response = requests.get(
                FORECAST_URL,
                params={"reg": reg},
                timeout=30,
                headers={"User-Agent": "Mozilla/5.0 (compatible; ReporteUV/2.0)"},
            )
            response.raise_for_status()
            response.encoding = "utf-8"
            parser = ForecastParser()
            parser.feed(response.text)
            text = parser.get_text()
            if not text:
                continue

            normalized = norm(text)
            # Nunca elegimos simplemente el número mayor de la página.
            # Primero localizamos la fecha de hoy y una referencia explícita a
            # Concepción/Biobío/Los Ángeles, y solo entonces aceptamos un rango UV.
            date_pos = -1
            for date_text in date_variants:
                date_pos = normalized.find(norm(date_text))
                if date_pos >= 0:
                    break

            search_positions = []
            for alias in aliases:
                pos = normalized.find(norm(alias), max(0, date_pos - 1000) if date_pos >= 0 else 0)
                if pos >= 0:
                    search_positions.append(pos)

            if not search_positions:
                # Algunas versiones de la página no imprimen el nombre de la
                # localidad en el texto extraído. En ese caso no inventamos un
                # valor: seguimos con el siguiente identificador.
                continue

            for pos in search_positions:
                context = text[max(0, pos - 700):min(len(text), pos + 1200)]
                ranges = forecast_range_candidates(context)
                if not ranges:
                    continue

                # Preferimos el rango más cercano a la localidad encontrada.
                chosen = min(ranges, key=lambda item: abs(item[0] - min(700, len(context))))[1]
                upper = range_upper(chosen)
                if upper is None:
                    continue

                print(f"Pronóstico UV oficial encontrado: {chosen} (reg={reg})")
                return {
                    "uv": upper,
                    "rango": chosen,
                    "fecha": today.isoformat(),
                    "fuente": "Dirección Meteorológica de Chile",
                    "tipo": "Pronóstico UV para día despejado",
                    "region_param": reg,
                }

        except requests.RequestException as exc:
            print(f"AVISO: no fue posible consultar el pronóstico oficial (reg={reg}): {exc}")
        except Exception as exc:
            print(f"AVISO: error procesando el pronóstico oficial (reg={reg}): {exc}")

    print("AVISO: no se encontró un rango UV oficial asociado a Concepción/Biobío; no se mostrará un valor inventado.")
    return None

try:
    r = requests.get(URL, params={"usuario": os.environ["DMC_USER"], "token": os.environ["DMC_TOKEN"]}, timeout=60)
except Exception as exc:
    sys.exit(f"No fue posible consultar Meteochile: {exc}")

r.raise_for_status()
consultado = dt.datetime.now(CL)
r.encoding = "utf-8"
js = r.json()
recs = []
walk(js, None, recs)
if not recs:
    sys.exit("No encontré 'indiceUV' en la respuesta:\n" + json.dumps(js, ensure_ascii=False)[:1500])
tz = source_tz(js)
hoy = dt.datetime.now(CL).date()
print("Fecha actual en Chile:", hoy)
forecast = get_uv_forecast(hoy)
stations = []

for name, detail, code in STATIONS:
    mine = []
    for station, timestamp, value in recs:
        if not station or code not in norm(station) or not timestamp:
            continue
        try:
            mine.append((parse_timestamp(timestamp, tz), value))
        except ValueError:
            continue

    today = [(local_time, value) for local_time, value in mine if local_time.date() == hoy]

    if not today:
        print(f"AVISO: todavía no hay datos de hoy ({hoy}) para {name} ({code})")
        stations.append({
            "name": name, "detail": detail,
            "uv": None, "hora": None,
            "max_uv": None, "max_hora": None,
            "actual_uv": None, "actual_hora": None,
            "forecast_uv": forecast["uv"] if forecast else None,
        "forecast_range": forecast["rango"] if forecast else None,
        })
        continue

    actual_t, actual_v = max(today, key=lambda item: item[0])
    max_t, max_v = max(today, key=lambda item: item[1])

    stations.append({
        "name": name,
        "detail": detail,
        "uv": round(max_v, 1),
        "hora": f"{max_t:%H:%M}",
        "max_uv": round(max_v, 1),
        "max_hora": f"{max_t:%H:%M}",
        "actual_uv": round(actual_v, 1),
        "actual_hora": f"{actual_t:%H:%M}",
        "forecast_uv": forecast["uv"] if forecast else None,
        "forecast_range": forecast["rango"] if forecast else None,
    })

    print(f"{name}: IUV actual {actual_v:.1f} a las {actual_t:%H:%M} | IUV máximo {max_v:.1f} a las {max_t:%H:%M}")

data = {
    "fecha": hoy.isoformat(),
    "consultado": consultado.strftime("%Y-%m-%dT%H:%M"),
    "stations": stations,
    "plants": [{"name": n, "town": t, "ref": ref} for n, t, ref in PLANTS],
    "forecast": forecast,
    "demo": False,
}

template_path = os.path.join(HERE, "template.html")
try:
    with open(template_path, encoding="utf-8") as file:
        tpl = file.read()
except FileNotFoundError:
    sys.exit("No encontré template.html en: " + HERE)

block = "/* DATOS_INICIO */\nconst DATA=" + json.dumps(data, ensure_ascii=False) + ";\n/* DATOS_FIN */"
html, count = re.subn(r"/\* DATOS_INICIO.*?DATOS_FIN \*/", lambda match: block, tpl, flags=re.S)
if count != 1:
    sys.exit("No encontré exactamente un bloque DATOS_INICIO / DATOS_FIN en template.html")
os.makedirs(os.path.join(HERE, "docs"), exist_ok=True)
output_path = os.path.join(HERE, "docs", "index.html")
with open(output_path, "w", encoding="utf-8") as file:
    file.write(html)
print("Página generada:", output_path)
print("")
print("========================================")
print(" RESUMEN REPORTE UV")
print("========================================")
print("Fecha:", hoy)
print("Consultado:", consultado.strftime("%Y-%m-%d %H:%M"))
print("Pronóstico UV:", forecast["rango"] if forecast else "no disponible")
for station in stations:
    print("")
    print(station["name"], station["detail"])
    if station["actual_uv"] is None:
        print("  Actual: todavía sin mediciones de hoy")
    else:
        print(f"  Actual: {station['actual_uv']:.1f} a las {station['actual_hora']} h")
    if station["max_uv"] is None:
        print("  Máximo: todavía sin mediciones de hoy")
    else:
        print(f"  Máximo: {station['max_uv']:.1f} a las {station['max_hora']} h")
print("========================================")
