"""Consulta UVB de Meteochile y genera docs/index.html desde template.html.

- Usa solamente observaciones del día actual en Chile.
- Nunca reemplaza el día actual por ayer.
- Si todavía no existen observaciones de hoy, mantiene actual/max en null.
- Intenta obtener además el pronóstico UV oficial de Meteochile para hoy.
- Variables de entorno:
    DMC_USER
    DMC_TOKEN

Nunca escribir el token directamente en este archivo.
"""

import os
import re
import sys
import json
import unicodedata
import datetime as dt
import requests
from zoneinfo import ZoneInfo
from html.parser import HTMLParser


print(">>> generate_uv.py VERSION 5 (hoy + actual + máximo + pronóstico)")

# ============================================================
# CONFIGURACIÓN
# ============================================================

URL = (
    "https://climatologia.meteochile.gob.cl/"
    "application/servicios/getRecienteUvb"
)

# Página oficial del pronóstico UV de Meteochile.
FORECAST_URL = (
    "https://www.meteochile.gob.cl/"
    "PortalDMC-web/otros_pronosticos/"
    "climatologia_pronostico_uv.xhtml"
)

CL = ZoneInfo("America/Santiago")
HERE = os.path.dirname(os.path.abspath(__file__))


# ============================================================
# ESTACIONES
# ============================================================

# (nombre, detalle, código nacional)
STATIONS = [
    ("Concepción", "Carriel Sur (360019)", "360019"),
    ("Los Ángeles", "María Dolores (370033)", "370033"),
]


# ============================================================
# PLANTAS
# ============================================================

# (nombre, localidad, estación de referencia)
PLANTS = [
    ("Planta Santa Fe", "Nacimiento", "Los Ángeles"),
    ("Planta Laja", "Laja", "Los Ángeles"),
]


# ============================================================
# UTILIDADES
# ============================================================

def norm(value):
    """Normaliza texto para comparar nombres/códigos."""
    return (
        unicodedata
        .normalize("NFD", str(value))
        .encode("ascii", "ignore")
        .decode()
        .lower()
    )


def station_name(data):
    """
    Obtiene un identificador legible de estación desde un dict
    de la respuesta de Meteochile.
    """
    if not isinstance(data, dict):
        return None

    low = {
        str(k).lower(): v
        for k, v in data.items()
    }

    if "codigonacional" in low:
        return (
            f"{low['codigonacional']} "
            f"{low.get('nombreestacion', '')}"
        ).strip()

    return None


def walk(node, name, out):
    """
    Recorre recursivamente el JSON de Meteochile sin asumir
    una estructura fija.
    """

    if isinstance(node, dict):

        name = station_name(node) or name

        # A veces la estación está en un dict hermano.
        for value in node.values():
            if isinstance(value, dict) and station_name(value):
                name = station_name(value)

        low = {
            str(k).lower(): v
            for k, v in node.items()
        }

        # Registro UV
        if (
            "indiceuv" in low
            and not isinstance(
                low["indiceuv"],
                (list, dict)
            )
        ):
            timestamp = (
                low.get("momento")
                or (
                    f"{low['fecha']} "
                    f"{low.get('hora', '00:00:00')}"
                    if "fecha" in low
                    else None
                )
            )

            try:
                value = float(low["indiceuv"])
                out.append(
                    (
                        name,
                        timestamp,
                        value
                    )
                )
            except (TypeError, ValueError):
                pass

        for value in node.values():
            walk(value, name, out)

    elif isinstance(node, list):

        for value in node:
            walk(value, name, out)


def source_tz(js):
    """
    Obtiene la zona horaria indicada por la API.

    Si Meteochile informa UTC, se utiliza UTC.
    Si no existe el campo, se asume UTC.
    """

    timezone_value = None

    if isinstance(js, dict):

        timezone_value = next(
            (
                value
                for key, value in js.items()
                if str(key).lower() == "timezone"
            ),
            None
        )

    print("timezone de la API:", timezone_value)

    try:
        return ZoneInfo(str(timezone_value))
    except Exception:
        return dt.timezone.utc


def parse_timestamp(timestamp, timezone):
    """
    Convierte una fecha/hora entregada por Meteochile
    a hora local de Chile.
    """

    text = str(timestamp)

    text = (
        text
        .replace("Z", "")
        .replace("T", " ")
    )

    formats = [
        "%d-%m-%Y %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%d-%m-%Y %H:%M",
        "%Y-%m-%d %H:%M",
    ]

    for fmt in formats:

        try:

            return (
                dt.datetime
                .strptime(text, fmt)
                .replace(tzinfo=timezone)
                .astimezone(CL)
            )

        except ValueError:
            pass

    raise ValueError(
        f"Formato de fecha desconocido: {timestamp}"
    )


# ============================================================
# PRONÓSTICO UV
# ============================================================

class ForecastParser(HTMLParser):
    """
    Parser sencillo de HTML para obtener el texto visible
    de la página oficial de pronóstico UV.

    No depende de BeautifulSoup.
    """

    def __init__(self):
        super().__init__()

        self.text_parts = []

    def handle_data(self, data):
        text = data.strip()

        if text:
            self.text_parts.append(text)

    def get_text(self):
        return " ".join(self.text_parts)


def number_candidates(text):
    """
    Extrae posibles valores numéricos de IUV.

    El pronóstico UV trabaja con valores aproximadamente
    entre 0 y 15.
    """

    values = []

    pattern = r"(?<!\d)(\d{1,2}(?:[.,]\d)?)(?!\d)"

    for match in re.finditer(pattern, text):

        raw = match.group(1)

        try:
            value = float(
                raw.replace(",", ".")
            )
        except ValueError:
            continue

        if 0 <= value <= 15:
            values.append(value)

    return values


def get_uv_forecast(today):
    """
    Intenta obtener el pronóstico UV oficial publicado
    por Meteochile.

    IMPORTANTE:
    La página de pronóstico de DMC no expone necesariamente
    un JSON público estable como getRecienteUvb.

    Por eso esta función es deliberadamente tolerante:
    si la estructura cambia o no puede obtenerse el dato,
    simplemente devuelve None.

    La página principal seguirá funcionando con las
    observaciones UV.
    """

    print("Consultando pronóstico UV oficial de Meteochile...")

    try:

        # Se utiliza la región de Biobío.
        # El parámetro puede cambiar si Meteochile modifica
        # la estructura de su portal.
        response = requests.get(
            FORECAST_URL,
            params={"reg": "8a"},
            timeout=30,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 "
                    "(compatible; ReporteUV/1.0)"
                )
            },
        )

        response.raise_for_status()

        response.encoding = "utf-8"

        parser = ForecastParser()
        parser.feed(response.text)

        text = parser.get_text()

        if not text:
            print(
                "AVISO: la página de pronóstico no "
                "entregó texto visible."
            )
            return None

        normalized = norm(text)

        # Buscamos contexto asociado a pronóstico UV.
        forecast_terms = [
            "pronostico uv",
            "indice uv",
            "indice ultravioleta",
            "ultravioleta",
        ]

        found_term = None

        for term in forecast_terms:

            if norm(term) in normalized:
                found_term = term
                break

        if not found_term:
            print(
                "AVISO: no se encontró una sección reconocible "
                "de pronóstico UV en la página oficial."
            )
            return None

        # ----------------------------------------------------
        # Intento de localizar el día actual.
        # ----------------------------------------------------

        date_variants = [
            today.strftime("%d-%m-%Y"),
            today.strftime("%d/%m/%Y"),
            today.strftime("%Y-%m-%d"),
        ]

        position = -1

        for date_text in date_variants:

            position = normalized.find(
                norm(date_text)
            )

            if position >= 0:
                break

        # ----------------------------------------------------
        # Si no encontramos la fecha explícita, buscamos
        # alrededor de la sección de pronóstico UV.
        # ----------------------------------------------------

        if position < 0:

            position = normalized.find(
                norm(found_term)
            )

        if position < 0:
            position = 0

        # Ventana de texto alrededor del dato.
        start = max(0, position - 1000)
        end = min(len(text), position + 3000)

        context = text[start:end]

        values = number_candidates(context)

        if not values:

            print(
                "AVISO: no se encontraron valores numéricos "
                "de IUV en el contexto del pronóstico."
            )

            return None

        # Evitamos devolver valores absurdamente altos.
        values = [
            round(v, 1)
            for v in values
            if 0 <= v <= 15
        ]

        if not values:
            return None

        # ----------------------------------------------------
        # Como respaldo, utilizamos el mayor valor encontrado
        # dentro del contexto del pronóstico.
        #
        # Esto representa el máximo UV pronosticado del día.
        # ----------------------------------------------------

        forecast_uv = max(values)

        print(
            f"Pronóstico UV encontrado: "
            f"{forecast_uv:.1f}"
        )

        return {
            "uv": forecast_uv,
            "fecha": today.isoformat(),
            "fuente": "Dirección Meteorológica de Chile",
            "tipo": "Pronóstico UV para día despejado",
        }

    except requests.RequestException as exc:

        print(
            "AVISO: no fue posible consultar "
            f"el pronóstico UV oficial: {exc}"
        )

        return None

    except Exception as exc:

        print(
            "AVISO: error procesando el pronóstico UV: "
            f"{exc}"
        )

        return None


# ============================================================
# CONSULTA DE OBSERVACIONES UV
# ============================================================

try:

    r = requests.get(
        URL,
        params={
            "usuario": os.environ["DMC_USER"],
            "token": os.environ["DMC_TOKEN"],
        },
        timeout=60,
    )

except Exception as exc:

    sys.exit(
        "No fue posible consultar Meteochile: "
        f"{exc}"
    )


# Si Meteochile responde con error,
# NO se modifica docs/index.html.
r.raise_for_status()


# ============================================================
# MOMENTO DE CONSULTA
# ============================================================

consultado = dt.datetime.now(CL)


# ============================================================
# PROCESAR JSON
# ============================================================

r.encoding = "utf-8"

js = r.json()

recs = []

walk(
    js,
    None,
    recs
)

if not recs:

    sys.exit(
        "No encontré 'indiceUV' en la respuesta:\n"
        + json.dumps(
            js,
            ensure_ascii=False
        )[:1500]
    )


tz = source_tz(js)


# ============================================================
# FECHA ACTUAL EN CHILE
# ============================================================

hoy = dt.datetime.now(CL).date()

print(
    "Fecha actual en Chile:",
    hoy
)


# ============================================================
# PRONÓSTICO
# ============================================================

forecast = get_uv_forecast(hoy)


# ============================================================
# ESTACIONES
# ============================================================

stations = []

for name, detail, code in STATIONS:

    mine = []

    for station, timestamp, value in recs:

        if not station:
            continue

        if code not in norm(station):
            continue

        if not timestamp:
            continue

        try:

            local_time = parse_timestamp(
                timestamp,
                tz
            )

            mine.append(
                (
                    local_time,
                    value
                )
            )

        except ValueError:

            continue

    # --------------------------------------------------------
    # NO HAY NINGUNA MEDICIÓN DE LA ESTACIÓN
    # --------------------------------------------------------

    if not mine:

        print(
            f"AVISO: sin datos para "
            f"{name} ({code})"
        )

        stations.append(
            {
                "name": name,
                "detail": detail,

                "uv": None,
                "hora": None,

                "max_uv": None,
                "max_hora": None,

                "actual_uv": None,
                "actual_hora": None,

                "forecast_uv": (
                    forecast["uv"]
                    if forecast
                    else None
                ),
            }
        )

        continue

    # --------------------------------------------------------
    # SOLO DATOS DEL DÍA ACTUAL
    #
    # IMPORTANTE:
    # Nunca se usa ayer.
    # --------------------------------------------------------

    today = [
        (local_time, value)
        for local_time, value in mine
        if local_time.date() == hoy
    ]

    # --------------------------------------------------------
    # TODAVÍA NO HAY MEDICIONES DE HOY
    #
    # Esto ocurre normalmente durante la madrugada.
    # NO es un error.
    # --------------------------------------------------------

    if not today:

        print(
            f"AVISO: todavía no hay datos de hoy "
            f"({hoy}) para {name} ({code})"
        )

        stations.append(
            {
                "name": name,
                "detail": detail,

                "uv": None,
                "hora": None,

                "max_uv": None,
                "max_hora": None,

                "actual_uv": None,
                "actual_hora": None,

                "forecast_uv": (
                    forecast["uv"]
                    if forecast
                    else None
                ),
            }
        )

        continue

    # --------------------------------------------------------
    # ÚLTIMA MEDICIÓN DE HOY
    # --------------------------------------------------------

    actual_t, actual_v = max(
        today,
        key=lambda item: item[0]
    )

    # --------------------------------------------------------
    # MÁXIMO OBSERVADO DE HOY
    # --------------------------------------------------------

    max_t, max_v = max(
        today,
        key=lambda item: item[1]
    )

    stations.append(
        {
            "name": name,
            "detail": detail,

            # Compatibilidad con el diseño actual
            "uv": round(max_v, 1),
            "hora": f"{max_t:%H:%M}",

            # Máximo del día
            "max_uv": round(max_v, 1),
            "max_hora": f"{max_t:%H:%M}",

            # Última medición
            "actual_uv": round(actual_v, 1),
            "actual_hora": f"{actual_t:%H:%M}",

            # Pronóstico oficial
            "forecast_uv": (
                forecast["uv"]
                if forecast
                else None
            ),
        }
    )

    print(
        f"{name}: "
        f"IUV actual {actual_v:.1f} "
        f"a las {actual_t:%H:%M} | "
        f"IUV máximo {max_v:.1f} "
        f"a las {max_t:%H:%M}"
    )


# ============================================================
# DATOS FINALES
# ============================================================

data = {
    "fecha": hoy.isoformat(),

    "consultado": consultado.strftime(
        "%Y-%m-%dT%H:%M"
    ),

    "stations": stations,

    "plants": [
        {
            "name": name,
            "town": town,
            "ref": reference,
        }
        for name, town, reference in PLANTS
    ],

    "forecast": forecast,

    "demo": False,
}


# ============================================================
# GENERAR docs/index.html
# ============================================================

template_path = os.path.join(
    HERE,
    "template.html"
)

try:

    with open(
        template_path,
        encoding="utf-8"
    ) as file:

        tpl = file.read()

except FileNotFoundError:

    sys.exit(
        "No encontré template.html en: "
        + HERE
    )


block = (
    "/* DATOS_INICIO */\n"
    "const DATA="
    + json.dumps(
        data,
        ensure_ascii=False
    )
    + ";\n"
    "/* DATOS_FIN */"
)


html, count = re.subn(
    r"/\* DATOS_INICIO.*?DATOS_FIN \*/",
    lambda match: block,
    tpl,
    flags=re.S
)


if count != 1:

    sys.exit(
        "No encontré exactamente un bloque "
        "DATOS_INICIO / DATOS_FIN en template.html"
    )


os.makedirs(
    os.path.join(HERE, "docs"),
    exist_ok=True
)


output_path = os.path.join(
    HERE,
    "docs",
    "index.html"
)


with open(
    output_path,
    "w",
    encoding="utf-8"
) as file:

    file.write(html)


print(
    "Página generada:",
    output_path
)


# ============================================================
# RESUMEN
# ============================================================

print("")
print("========================================")
print(" RESUMEN REPORTE UV")
print("========================================")
print("Fecha:", hoy)
print("Consultado:", consultado.strftime("%Y-%m-%d %H:%M"))

if forecast:

    print(
        "Pronóstico UV:",
        forecast["uv"]
    )

else:

    print(
        "Pronóstico UV: no disponible"
    )

for station in stations:

    print("")
    print(
        station["name"],
        station["detail"]
    )

    if station["actual_uv"] is None:

        print(
            "  Actual: todavía sin mediciones de hoy"
        )

    else:

        print(
            f"  Actual: "
            f"{station['actual_uv']:.1f} "
            f"a las "
            f"{station['actual_hora']} h"
        )

    if station["max_uv"] is None:

        print(
            "  Máximo: todavía sin mediciones de hoy"
        )

    else:

        print(
            f"  Máximo: "
            f"{station['max_uv']:.1f} "
            f"a las "
            f"{station['max_hora']} h"
        )

print("========================================")
