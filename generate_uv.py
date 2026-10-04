"""Consulta UVB de Meteochile y genera docs/index.html desde template.html.

- Usa solamente observaciones del día actual en Chile.
- Nunca reemplaza el día actual por ayer.
- Si todavía no existen observaciones de hoy, mantiene actual/max en null.
- Obtiene el pronóstico UV oficial de Meteochile para Biobío (reg=8a).
- El pronóstico se busca como rango oficial (1-2, 3-5, 6-7, 8-10, 11+).
- Si Meteochile entrega el pronóstico como imagen, se intenta leer esa imagen con OCR.
- Variables de entorno: DMC_USER, DMC_TOKEN.
"""
import os, re, sys, json, unicodedata, datetime as dt, requests, io, base64
from zoneinfo import ZoneInfo
from html.parser import HTMLParser
from urllib.parse import urljoin

print(">>> generate_uv.py VERSION 8 (diagnóstico del pronóstico oficial)")

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
        self.images = []
        self.others = []

    def handle_data(self, data):
        text = data.strip()
        if text:
            self.text_parts.append(text)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        t = tag.lower()
        if t == "img" and a.get("src"):
            self.images.append(a["src"])
        elif t in ("iframe", "embed", "source") and a.get("src"):
            self.others.append((t, a["src"]))
        elif t == "object" and a.get("data"):
            self.others.append((t, a["data"]))
        elif t == "a" and a.get("href") and re.search(r"\.(png|jpe?g|gif|webp|pdf|json)(\?|$)", a["href"], re.I):
            self.others.append((t, a["href"]))

    def get_text(self):
        return " ".join(self.text_parts)


RANGE_RE = re.compile(r"(?<!\d)(1\s*[-–]\s*2|3\s*[-–]\s*5|6\s*[-–]\s*7|8\s*[-–]\s*10|11\s*\+)(?!\d)")
OCR_RANGE_RE = re.compile(r"(?<!\d)(1\s*2|3\s*5|6\s*7|8\s*10)(?!\d)")


def forecast_range_candidates(text):
    found = []
    for match in RANGE_RE.finditer(text):
        value = re.sub(r"\s+", "", match.group(1)).replace("–", "-")
        found.append((match.start(), value))
    # OCR de la tabla oficial puede leer "3-5" como "35". Solo usamos
    # esta tolerancia para el texto OCR y la asociación posterior con la fecha/localidad.
    if not found:
        for match in OCR_RANGE_RE.finditer(text):
            raw = re.sub(r"\s+", "", match.group(1))
            if raw == "12": value = "1-2"
            elif raw == "35": value = "3-5"
            elif raw == "67": value = "6-7"
            elif raw == "810": value = "8-10"
            else: continue
            found.append((match.start(), value))
    return found


def range_upper(value):
    if not value:
        return None
    if "+" in value:
        return 11.0
    return float(value.split("-")[-1])


def choose_range_from_text(text, today):
    """Busca el rango asociado al día/localidad; nunca toma un número arbitrario."""
    if not text:
        return None
    normalized = norm(text)
    aliases = ["concepcion", "biobio", "los angeles"]
    date_variants = [
        today.strftime("%d-%m-%Y"),
        today.strftime("%d/%m/%Y"),
        today.strftime("%Y-%m-%d"),
        today.strftime("%d de %B de %Y"),
    ]

    date_positions = [normalized.find(norm(x)) for x in date_variants]
    date_positions = [p for p in date_positions if p >= 0]
    date_pos = date_positions[0] if date_positions else -1

    # Primero, si aparece Concepción/Biobío, buscar un rango cercano.
    for alias in aliases:
        start = max(0, date_pos - 2000) if date_pos >= 0 else 0
        pos = normalized.find(norm(alias), start)
        if pos < 0:
            continue
        context = text[max(0, pos - 1200):min(len(text), pos + 1800)]
        ranges = forecast_range_candidates(context)
        if ranges:
            # El rango más cercano a la localidad es el candidato.
            center = min(1200, len(context))
            return min(ranges, key=lambda x: abs(x[0] - center))[1]

    # Si la página regional ya está filtrada a Biobío, puede no repetir el nombre.
    # En ese caso exigimos que el rango esté cerca de la fecha de hoy.
    if date_pos >= 0:
        context = text[max(0, date_pos - 500):min(len(text), date_pos + 1800)]
        ranges = forecast_range_candidates(context)
        if ranges:
            return min(ranges, key=lambda x: abs(x[0] - 500))[1]

    return None


def ocr_image(image_bytes):
    """OCR opcional: GitHub-hosted Ubuntu normalmente dispone de tesseract."""
    try:
        from PIL import Image, ImageOps, ImageEnhance
        import pytesseract
        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        # Dos pasadas: original y escala de grises/contraste.
        texts = []
        for img in (image, ImageEnhance.Contrast(ImageOps.grayscale(image)).enhance(2.0)):
            for psm in (6, 11):
                try:
                    texts.append(pytesseract.image_to_string(img, config=f"--psm {psm}"))
                except Exception:
                    pass
        return "\n".join(texts)
    except Exception as exc:
        print(f"AVISO: OCR no disponible o falló: {exc}")
        return ""


def forecast_result(chosen, today, reg):
    return {"uv": range_upper(chosen), "rango": chosen, "fecha": today.isoformat(),
            "fuente": "Dirección Meteorológica de Chile",
            "tipo": "Pronóstico UV para día despejado", "region_param": reg}


def get_uv_forecast(today):
    print("Consultando pronóstico UV oficial de Meteochile para Biobío (reg=8a)...")
    reg = "8a"
    headers = {"User-Agent": "Mozilla/5.0 (compatible; ReporteUV/3.0)"}
    try:
        response = requests.get(FORECAST_URL, params={"reg": reg}, timeout=30, headers=headers)
        response.raise_for_status()
        response.encoding = "utf-8"
        html = response.text

        parser = ForecastParser()
        parser.feed(html)
        text = parser.get_text()

        # ---- DIAGNÓSTICO (datos públicos de la DMC; no incluye credenciales) ----
        print(f"[diag] URL final: {response.url} | estado: {response.status_code} | redirecciones: {[x.status_code for x in response.history]}")
        print(f"[diag] largo HTML: {len(html)} | texto visible: {text[:300]!r}")
        css_urls = re.findall(r"url\(['\"]?([^)'\"]+)", html)
        script_srcs = re.findall(r"<script[^>]+src=[\"']([^\"']+)", html)
        print(f"[diag] otros recursos (iframe/object/enlaces): {parser.others[:10]}")
        print(f"[diag] url() en CSS/HTML: {css_urls[:10]}")
        print(f"[diag] scripts externos: {script_srcs[:10]}")
        print(f"[diag] el HTML menciona 'biobio': {'biobio' in norm(html)} | 'concepcion': {'concepcion' in norm(html)}")
        # -------------------------------------------------------------------------

        # 1) Texto HTML, si la DMC lo entrega directamente.
        chosen = choose_range_from_text(text, today)
        if chosen:
            print(f"Pronóstico UV oficial encontrado en HTML: {chosen}")
            return forecast_result(chosen, today, reg)

        # 2) Imágenes (y otros recursos) que puedan contener la tabla del pronóstico.
        candidates = []
        for src in parser.images + [u for _, u in parser.others] + css_urls:
            if src not in candidates and (src.startswith("data:image") or re.search(r"\.(png|jpe?g|gif|webp)(\?|$)", src, re.I) or src in parser.images):
                candidates.append(src)
        print(f"Recursos de imagen candidatos: {len(candidates)}")

        for src in candidates:
            try:
                if src.startswith("data:image"):
                    content, ctype, label = base64.b64decode(src.split(",", 1)[1]), "data-uri", "data:image..."
                else:
                    label = urljoin(response.url, src)
                    img_response = requests.get(label, timeout=20, headers=headers)
                    img_response.raise_for_status()
                    ctype = img_response.headers.get("content-type", "")
                    content = img_response.content
                    if not ctype.startswith("image/"):
                        print(f"[diag] omitido (no es imagen): {label} · {ctype}")
                        continue
                print(f"[diag] imagen: {label[:120]} · {ctype} · {len(content)} bytes")
                ocr_text = ocr_image(content)
                print("[diag] OCR (inicio):", re.sub(r"\s+", " ", ocr_text)[:300])
                chosen = choose_range_from_text(ocr_text, today)
                if chosen:
                    print(f"Pronóstico UV oficial encontrado mediante imagen/OCR: {chosen}")
                    return forecast_result(chosen, today, reg)
            except (requests.RequestException, ValueError, IndexError) as exc:
                print(f"[diag] no se pudo procesar un recurso: {exc}")
                continue

        print("AVISO: Meteochile responde, pero el pronóstico no está expuesto como texto legible; no se inventará un valor.")
        return None

    except requests.RequestException as exc:
        print(f"AVISO: no fue posible consultar el pronóstico oficial: {exc}")
        return None
    except Exception as exc:
        print(f"AVISO: error procesando el pronóstico oficial: {exc}")
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

    common = {
        "name": name, "detail": detail,
        "forecast_uv": forecast["uv"] if forecast else None,
        "forecast_range": forecast["rango"] if forecast else None,
    }

    if not today:
        print(f"AVISO: todavía no hay datos de hoy ({hoy}) para {name} ({code})")
        stations.append({
            **common,
            "uv": None, "hora": None,
            "max_uv": None, "max_hora": None,
            "actual_uv": None, "actual_hora": None,
        })
        continue

    actual_t, actual_v = max(today, key=lambda item: item[0])
    max_t, max_v = max(today, key=lambda item: item[1])

    stations.append({
        **common,
        "uv": round(max_v, 1),
        "hora": f"{max_t:%H:%M}",
        "max_uv": round(max_v, 1),
        "max_hora": f"{max_t:%H:%M}",
        "actual_uv": round(actual_v, 1),
        "actual_hora": f"{actual_t:%H:%M}",
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
    print("  Pronóstico:", station["forecast_range"] or "no disponible")
print("========================================")
