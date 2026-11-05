#!/usr/bin/env python3
"""
Regenera index.html con todas las comisiones del Back Office de Coderhouse.

Lee las credenciales desde variables de entorno (nunca las escribe en el
HTML resultante):
  - CODERHOUSE_API_URL          (ej: https://api-prod.coderhouse.com/api)
  - CLAUDE_STUDENT_API_KEY       (scope: student-enrollment, platform-staff)
  - CLAUDE_FINANCE_API_KEY       (scope: finance-product)

Uso:
  python3 scripts/actualizar_comisiones.py
"""
import os
import sys
import json
import datetime
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from zoneinfo import ZoneInfo

import requests

API_URL = os.environ["CODERHOUSE_API_URL"].rstrip("/")
STUDENT_KEY = os.environ["CLAUDE_STUDENT_API_KEY"]
FINANCE_KEY = os.environ["CLAUDE_FINANCE_API_KEY"]

TZ = ZoneInfo("America/Argentina/Buenos_Aires")
DAY_NAMES = ["Domingo", "Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado"]
MONTH_NAMES = [
    "", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
    "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
]

# La API rechaza limit > 100 (400 Bad Request: "limit must not be greater than 100")
PAGE_SIZE = 100

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE_PATH = os.path.join(REPO_ROOT, "index.template.html")
OUTPUT_PATH = os.path.join(REPO_ROOT, "index.html")


def student_headers():
    return {"x-api-key": STUDENT_KEY}


def finance_headers():
    return {"x-api-key": FINANCE_KEY}


def get_all_cohorts():
    """Pagina GET /student/enrollment/m2m/admin/cohorts hasta traer todo.

    La respuesta tiene la forma {"data": [...], "pagination": {"page",
    "limit", "total", "totalPages", "hasNextPage", "hasPreviousPage"}}.
    """
    cohorts = []
    page = 1
    while True:
        resp = requests.get(
            f"{API_URL}/student/enrollment/m2m/admin/cohorts",
            headers=student_headers(),
            params={"page": page, "limit": PAGE_SIZE},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        items = data.get("data", [])
        cohorts.extend(items)

        pagination = data.get("pagination") or {}
        if not pagination.get("hasNextPage"):
            break
        page += 1
    return cohorts


def _product_title(product):
    """El título vive en localizations[], no en un campo plano."""
    locs = product.get("localizations") or []
    for loc in locs:
        if loc.get("language") == "es" and loc.get("isDefault"):
            return loc.get("title")
    for loc in locs:
        if loc.get("language") == "es":
            return loc.get("title")
    if locs:
        return locs[0].get("title")
    return product.get("title") or product.get("name")


def search_products(product_ids):
    """POST /finance/product/m2m/products/search — confirmado de solo lectura.

    Devuelve directamente una lista de productos (no envuelta en {"data": [...]}).
    """
    product_map = {}
    chunk_size = 50
    ids = list(product_ids)
    for i in range(0, len(ids), chunk_size):
        chunk = ids[i : i + chunk_size]
        resp = requests.post(
            f"{API_URL}/finance/product/m2m/products/search",
            headers=finance_headers(),
            json={"productIds": chunk},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        items = data if isinstance(data, list) else data.get("data", [])
        for p in items:
            pid = p.get("id") or p.get("productId")
            title = _product_title(p)
            if pid is not None and title:
                product_map[pid] = title
    return product_map


def get_professors_for_cohort(cohort_id):
    """GET /platform/staff/m2m/admin/assignments/cohort/{id} — devuelve una lista.

    Cada item trae `status`: ACTIVE | CANCELLED | COMPLETED. Solo los ACTIVE
    siguen asignados — CANCELLED es un profesor desasignado ("dado de baja").
    """
    try:
        resp = requests.get(
            f"{API_URL}/platform/staff/m2m/admin/assignments/cohort/{cohort_id}",
            headers=student_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        items = data if isinstance(data, list) else data.get("data", [])
        names = []
        for a in items or []:
            if a.get("status") == "ACTIVE" and a.get("professorName"):
                names.append(a["professorName"])
        return cohort_id, names
    except Exception:
        return cohort_id, []


def get_all_professors(cohort_ids):
    result = {}
    with ThreadPoolExecutor(max_workers=32) as ex:
        futures = [ex.submit(get_professors_for_cohort, cid) for cid in cohort_ids]
        for fut in as_completed(futures):
            cid, names = fut.result()
            result[cid] = names
    return result


def get_zoom_end_for_cohort(cohort):
    """Busca la primera clase con Zoom configurado (`providerClassLink` no
    nulo) de la comisión, y devuelve su `scheduledEndAt` (UTC ISO) o None si
    no hay ninguna clase con Zoom configurado todavía.

    GET /education/scheduling/m2m/cohorts/{id}/classes?fromDate=...&toDate=...
    (mismo header que student-enrollment/platform-staff).
    """
    cohort_id = cohort.get("id")
    start_dt = to_arg_dt(cohort.get("startDate"))
    end_dt = to_arg_dt(cohort.get("endDate"))
    if not start_dt:
        return cohort_id, None

    from_date = (start_dt - datetime.timedelta(days=30)).date().isoformat()
    to_date = (end_dt.date() if end_dt else (start_dt + datetime.timedelta(days=180)).date()).isoformat()

    try:
        resp = requests.get(
            f"{API_URL}/education/scheduling/m2m/cohorts/{cohort_id}/classes",
            headers=student_headers(),
            params={"fromDate": from_date, "toDate": to_date},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        items = data if isinstance(data, list) else data.get("data", [])
        con_zoom = [c for c in (items or []) if c.get("providerClassLink")]
        if not con_zoom:
            return cohort_id, None
        con_zoom.sort(key=lambda c: c.get("scheduledStartAt") or "")
        return cohort_id, con_zoom[0].get("scheduledEndAt")
    except Exception:
        return cohort_id, None


def get_all_zoom_ends(cohorts):
    result = {}
    with ThreadPoolExecutor(max_workers=32) as ex:
        futures = [ex.submit(get_zoom_end_for_cohort, c) for c in cohorts]
        for fut in as_completed(futures):
            cid, end_iso = fut.result()
            result[cid] = end_iso
    return result


def to_arg_dt(iso_str):
    if not iso_str:
        return None
    dt = datetime.datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    return dt.astimezone(TZ)


def build_rows(cohorts, product_map, professors_map, zoom_ends_map):
    """Arma UNA fila por cada comisión que trajo la API, sin excepción.

    No hay ningún filtro por `status` acá ni en `get_all_cohorts`: se
    procesan TODAS las comisiones devueltas por el Back Office, sea cual
    sea su estado (NOT_STARTED, IN_PROGRESS, COMPLETED o CANCELLED). El
    campo `estado` de cada fila sale siempre de `c.get("status")`, es
    decir, del valor que la API tiene en ESTE momento — si una comisión
    pasó a CANCELLED desde la última corrida, esta fila ya la refleja así.

    Lo mismo vale para `fecha_inicio`, `fecha_fin` y `horario`: cada
    corrida reconstruye el archivo entero desde cero a partir de la
    respuesta fresca de la API (no hay merge con un JSON/HTML anterior ni
    caché intermedia), así que una postergación o cambio de cursada en el
    Back Office siempre se refleja en la próxima actualización.

    Que una comisión no tenga profesores ACTIVOS asignados (o directamente
    ninguno) nunca la excluye: `professors_map.get(cohort_id, [])` cae a
    una lista vacía y la fila se arma igual, con "profesores": "".
    """
    rows = []
    for c in cohorts:
        cohort_id = c.get("id")
        product_id = c.get("productId")
        curso = product_map.get(product_id) or c.get("name", "—")

        start_dt = to_arg_dt(c.get("startDate"))
        end_dt = to_arg_dt(c.get("endDate"))

        week_days = c.get("weekDays") or []
        dias = ", ".join(DAY_NAMES[d] for d in week_days if 0 <= d <= 6)

        # La hora de INICIO sale siempre de startDate (Back Office, dato
        # fresco de esta corrida). La de FIN sale de la primera clase con
        # Zoom configurado si existe (scheduledEndAt); si no hay ninguna,
        # fallback a startDate + minutesPerClass (comportamiento anterior).
        hora_inicio = start_dt.strftime("%H:%M") if start_dt else ""
        zoom_end_iso = zoom_ends_map.get(cohort_id)
        fin_dt = to_arg_dt(zoom_end_iso) if zoom_end_iso else None
        if not fin_dt and start_dt:
            minutos = c.get("minutesPerClass") or 0
            if minutos:
                fin_dt = start_dt + datetime.timedelta(minutes=minutos)
        hora_fin = fin_dt.strftime("%H:%M") if fin_dt else ""
        horario = f"{hora_inicio} a {hora_fin}" if hora_inicio and hora_fin else hora_inicio

        mes = f"{MONTH_NAMES[start_dt.month]} {start_dt.year}" if start_dt else ""

        rows.append({
            "curso": curso,
            "comision": c.get("commissionNumber", cohort_id),
            "dia": dias,
            "horario": horario,
            "hora_inicio": hora_inicio,
            "mes": mes,
            # fecha_inicio/fecha_fin: siempre el startDate/endDate ACTUAL
            # de la API, nunca un valor arrastrado de una corrida previa.
            "fecha_inicio": start_dt.strftime("%d/%m/%Y") if start_dt else "",
            "fecha_fin": end_dt.strftime("%d/%m/%Y") if end_dt else "",
            # estado: siempre el status ACTUAL de la API (incluye CANCELLED).
            "estado": c.get("status", ""),
            "modalidad": c.get("modality", ""),
            "pais": c.get("country", ""),
            "inscriptos": c.get("currentStudents", 0),
            "cupo_max": c.get("maxStudents", 0),
            "profesores": ", ".join(professors_map.get(cohort_id, [])),
        })

    rows.sort(key=lambda r: (r["fecha_inicio"] or "", r["hora_inicio"] or ""))

    # Chequeo de integridad: tiene que haber exactamente una fila por
    # comisión traída de la API. Si esto alguna vez no se cumple, es señal
    # de que algo empezó a filtrar comisiones silenciosamente.
    assert len(rows) == len(cohorts), (
        f"Se armaron {len(rows)} filas pero la API trajo {len(cohorts)} "
        "comisiones — alguna se perdió en el camino."
    )

    return rows


def render_html(rows, precios):
    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        template = f.read()

    data_json = json.dumps(rows, ensure_ascii=False)
    precios_json = json.dumps(precios, ensure_ascii=False)
    today = datetime.datetime.now(TZ).strftime("%d/%m/%Y %H:%M")

    html = template.replace(
        "window.__COMISIONES_DATA__ = [];",
        f"window.__COMISIONES_DATA__ = {data_json};",
    )
    html = html.replace(
        "window.__PRECIOS_DATA__ = [];",
        f"window.__PRECIOS_DATA__ = {precios_json};",
    )
    html = html.replace(
        "Instantánea estática — no se actualiza sola.",
        f"Datos actualizados automáticamente el {today} (hora Argentina).",
    )
    return html


# ---------------------------------------------------------------------------
# Calculadora de precios: scraping de precios públicos de coderhouse.com
#
# No existe ningún precio real cargado en el módulo de pricing del API de
# Coderhouse (se probó en ~175 productos distintos, incluidas las 5
# materias de Python, y siempre devuelve price=null; /regions y
# /regions/countries también vuelven vacíos). Los precios públicos SÍ están
# en coderhouse.com: cada página de curso/carrera trae, para cada país, un
# bloque "Tachado" (precio de lista) y "Total" (precio final con el
# descuento vigente). Un mismo curso tiene una sola página pública aunque
# en el Back Office esté partido en varias materias/comisiones con nombres
# distintos (ej. "AI Engineering (V2) Diplomatura" y "AI Engineering" son
# la misma página pública "Curso de AI Engineering").
# ---------------------------------------------------------------------------

PRECIOS_SITEMAP_URL = "https://www.coderhouse.com/sitemap_es-AR.xml"

# País -> prefijo de locale en la URL. None = página sin prefijo (precio
# genérico "resto del mundo" en USD, que es el que efectivamente se le
# muestra a Estados Unidos y Venezuela).
PRECIOS_COUNTRY_PREFIX = {
    "AR": "ar", "UY": "uy", "CL": "cl", "CO": "co", "ES": "es",
    "MX": "mx", "PE": "pe", "US": None, "VE": None,
}

PRECIOS_AMOUNT_RE = __import__("re").compile(
    r'[\$€]\s*([\d.,]+)\s*([A-Z]{3})|S/\.\s*([\d.,]+)\s*([A-Z]{3})'
)

# Nombres de curso del Back Office que son B2B/demo/internos y nunca van a
# tener una página pública de precios en coderhouse.com.
PRECIOS_PALABRAS_NO_PUBLICAS = (
    "demo", "prueba", "b2b", "test", "adidas", "fenicio", "coresa",
    "dasa", "|", "interno", "staff", "capacitacion interna",
)

PRECIOS_STOPWORDS = {
    "de", "la", "el", "los", "las", "y", "a", "en", "para", "con", "del",
    "al", "un", "una", "online", "curso", "cursos", "carrera", "diplomatura",
    "diplomaturas", "v1", "v2", "v3", "en", "b2b", "coderhouse", "ch",
}


def _precios_normalizar(texto):
    import re
    import unicodedata
    texto = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
    texto = texto.lower()
    texto = re.sub(r"\(demo\)|\(diplomatura\)|\(diplotamtura\)|\(v\d+\)|\(en\)", " ", texto)
    texto = re.sub(r"[^a-z0-9]+", " ", texto)
    tokens = [t for t in texto.split() if t and t not in PRECIOS_STOPWORDS]
    return set(tokens)


def _precios_es_curso_publico(nombre):
    bajo = (nombre or "").lower()
    return not any(p in bajo for p in PRECIOS_PALABRAS_NO_PUBLICAS)


def _precios_descubrir_urls_catalogo():
    """Lee el sitemap de Argentina y devuelve los paths /cursos/slug y
    /carreras/slug (sin prefijo de país) que son páginas de producto reales."""
    import re
    resp = requests.get(PRECIOS_SITEMAP_URL, timeout=30)
    resp.raise_for_status()
    locs = re.findall(r"<loc>([^<]+)</loc>", resp.text)
    paths = []
    for loc in locs:
        m = re.match(r"https://www\.coderhouse\.com/ar/(cursos|carreras)/([^/]+)/?$", loc)
        if m:
            paths.append(f"{m.group(1)}/{m.group(2)}")
    return sorted(set(paths))


def _precios_extraer_par(html, label):
    import re
    m = re.search(rf'data-framer-name="{label}[^"]*"', html)
    if not m:
        return None
    seg = html[m.end():m.end() + 400]
    am = PRECIOS_AMOUNT_RE.search(seg)
    if not am:
        return None
    if am.group(1):
        monto_str, moneda = am.group(1), am.group(2)
    else:
        monto_str, moneda = am.group(3), am.group(4)
    try:
        return int(monto_str.replace(".", "").replace(",", "")), moneda
    except ValueError:
        return None


def _precios_extraer_titulo(html):
    import re
    m = re.search(r"<title[^>]*>([^<|]+)", html, re.IGNORECASE)
    return m.group(1).strip() if m else None


def _precios_fetch_pagina(path, prefix, intentos=3):
    """Trae una página pública de coderhouse.com. Reintenta ante fallas
    transitorias de red (timeout, 5xx, conexión cortada) con una pequeña
    espera creciente entre intentos, igual que scraper_precios_web.py: una
    página que falla por un hipo momentáneo de un solo país no debería
    dejar a ese curso sin precio para ese país en la Calculadora."""
    import time
    url = f"https://www.coderhouse.com/{prefix}/{path}" if prefix else f"https://www.coderhouse.com/{path}"
    ultimo_error = None
    for intento in range(1, intentos + 1):
        try:
            resp = requests.get(url, timeout=25, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            # coderhouse.com no declara charset en el header Content-Type, así
            # que requests adivina mal la codificación por defecto; la página
            # es UTF-8 (tiene <meta charset="utf-8">), forzarlo evita que los
            # acentos salgan mal (ej. "Administración" -> "AdministraciÃ³n").
            resp.encoding = "utf-8"
            return resp.text
        except Exception as e:
            ultimo_error = e
            if intento < intentos:
                time.sleep(1.5 * intento)
    print(f"  aviso: no se pudo traer {url} tras {intentos} intentos: {ultimo_error}", file=sys.stderr)
    return ""


def _precios_scrapear_curso(path):
    cache = {}
    titulo = None
    precios_por_pais = {}
    for pais, prefix in PRECIOS_COUNTRY_PREFIX.items():
        if prefix not in cache:
            cache[prefix] = _precios_fetch_pagina(path, prefix)
        html = cache[prefix]
        if not html:
            continue
        if titulo is None:
            titulo = _precios_extraer_titulo(html)
        tachado = _precios_extraer_par(html, "Tachado")
        total = _precios_extraer_par(html, "Total")
        if not tachado or not total:
            continue
        lista, moneda1 = tachado
        final, moneda2 = total
        moneda = moneda1 or moneda2
        if lista <= 0:
            continue
        descuento_pct = round((1 - final / lista) * 100) if lista else 0
        precios_por_pais[pais] = {
            "list": lista, "final": final, "currency": moneda, "discount_pct": descuento_pct,
        }
    return titulo, precios_por_pais


def _precios_es_pagina_de_curso(titulo, path):
    """Igual que en scripts/scraper_precios_web.py: descarta explícitamente
    cualquier página cuyo <title> sea inconfundiblemente de una carrera
    ("Carrera de …"), por más que la URL ya haya pasado el filtro de
    "cursos/" al leer el sitemap. Esto es intencionalmente solo por
    tipo/ruta de producto — NUNCA por el nombre del curso: un curso suelto
    puede (y debe) llamarse igual que una carrera (ej. "AI Automation",
    "Diseño UX/UI") y mostrar su propio precio individual sin problema."""
    if not titulo:
        return False
    titulo_norm = titulo.strip().lower()
    if titulo_norm.startswith("carrera de") or titulo_norm.startswith("carrera "):
        print(
            f"  aviso: {path} descartado — el título de la página es de una CARRERA "
            f"(\"{titulo.strip()}\"), no de un curso individual",
            file=sys.stderr,
        )
        return False
    return True


def scrape_catalogo_precios():
    """Trae precio de lista + final por país para cada CURSO individual
    público de coderhouse.com. Devuelve {path: {"titulo", "tipo", "precios"}},
    con "tipo" siempre "curso".

    IMPORTANTE: antes esta función también scrapeaba las páginas de
    /carreras/ y les ponía tipo="carrera", pero nada más adelante en el
    archivo (ni acá ni en construir_precios_data) filtraba ese tipo antes
    de mostrarlo en la Calculadora — por eso una carrera con el mismo
    nombre que uno de sus cursos (ej. "AI Automation", "Diseño UX/UI")
    terminaba mostrando el precio de la carrera completa, y volvía a
    aparecer en cada corrida de "Actualizar comisiones" sin importar cuántas
    veces se limpiara el archivo a mano. Ahora, igual que en
    scripts/scraper_precios_web.py, esta función solo trae páginas de
    "cursos/": las de "carreras/" ni se scrapean."""
    paths = [p for p in _precios_descubrir_urls_catalogo() if p.startswith("cursos/")]
    catalogo = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(_precios_scrapear_curso, p): p for p in paths}
        for fut in as_completed(futs):
            path = futs[fut]
            try:
                titulo, precios = fut.result()
            except Exception as e:
                print(f"  aviso: fallo scrapeando {path}: {e}", file=sys.stderr)
                continue
            if not titulo or not precios:
                continue
            if not _precios_es_pagina_de_curso(titulo, path):
                continue
            titulo_limpio = titulo.replace("Curso de ", "").replace("Carrera de ", "").strip()
            catalogo[path] = {"titulo": titulo_limpio, "tipo": "curso", "precios": precios}

    # Log de cobertura: igual que en scripts/scraper_precios_web.py, un
    # curso con menos países que el total esperado tuvo alguna página de
    # locale que no se pudo traer en esta corrida puntual (ver
    # fusionar_con_precios_publicados más abajo, que evita que este hueco
    # borre un precio de país ya publicado).
    total_paises = len(PRECIOS_COUNTRY_PREFIX)
    incompletos = sorted(
        (info["titulo"], path, sorted(set(PRECIOS_COUNTRY_PREFIX) - set(info["precios"])))
        for path, info in catalogo.items()
        if len(info["precios"]) < total_paises
    )
    if incompletos:
        print(
            f"  aviso: {len(incompletos)} cursos quedaron con cobertura de país incompleta en este scrape:",
            file=sys.stderr,
        )
        for titulo, path, faltantes in incompletos:
            print(f"    - {titulo} ({path}): faltan {', '.join(faltantes)}", file=sys.stderr)

    return catalogo


def leer_precios_publicados():
    """Lee el window.__PRECIOS_DATA__ que YA está publicado en
    index.html (si el archivo existe y tiene datos), indexado por id. Esto
    es lo que permite a fusionar_con_precios_publicados() rellenar, país
    por país, cualquier hueco puntual del scrape de hoy en vez de dejar
    ese país sin precio en la Calculadora. Nunca aborta la corrida: si el
    archivo no existe todavía (primera vez) o no se puede parsear, devuelve
    un diccionario vacío y la corrida sigue usando solo los datos frescos
    de hoy, como hacía antes."""
    if not os.path.exists(OUTPUT_PATH):
        return {}
    try:
        with open(OUTPUT_PATH, encoding="utf-8") as f:
            html = f.read()
        import re
        m = re.search(r"window\.__PRECIOS_DATA__\s*=\s*(\[.*?\]);", html, re.DOTALL)
        if not m:
            return {}
        publicados = json.loads(m.group(1))
        return {c["id"]: c for c in publicados if c.get("id")}
    except Exception as e:
        print(f"  aviso: no se pudo leer el index.html publicado para fusionar precios ({e}); se usa solo lo de hoy", file=sys.stderr)
        return {}


def fusionar_con_precios_publicados(precios_data):
    """Combina, PAÍS POR PAÍS, el precios_data recién armado con lo que ya
    está publicado en index.html: el país fresco de hoy pisa al publicado
    cuando está disponible, y el publicado se conserva solo para los
    países puntuales que faltaron en el scrape de hoy (en vez de
    desaparecer y mostrar "No disponible" en la Calculadora). A diferencia
    de scripts/scraper_precios_web.py (que refresca incrementalmente el
    index.html ya publicado), este script arma precios_data desde cero en
    cada corrida a partir del catálogo scrapeado hoy — por eso este merge
    es necesario acá también: sin él, un hueco de un solo país en una sola
    corrida de "Actualizar comisiones" bastaba para borrar ese precio."""
    publicados = leer_precios_publicados()
    if not publicados:
        return precios_data
    conservados = 0
    fusionado = []
    for curso in precios_data:
        anterior = publicados.get(curso["id"])
        if anterior and anterior.get("precios"):
            precios_ayer = anterior["precios"]
            precios_hoy = curso["precios"]
            faltantes_hoy = set(precios_ayer) - set(precios_hoy)
            if faltantes_hoy:
                conservados += len(faltantes_hoy)
                print(
                    f"  aviso: {curso['titulo']} ({curso['id']}) no trajo precio hoy para "
                    f"{', '.join(sorted(faltantes_hoy))} — se conserva el valor publicado anteriormente "
                    "para esos países en vez de mostrar \"No disponible\".",
                    file=sys.stderr,
                )
            curso = {**curso, "precios": {**precios_ayer, **precios_hoy}}
        fusionado.append(curso)
    if conservados:
        print(f"  {conservados} precios de país puntuales conservados de lo ya publicado por no haberse podido traer hoy", file=sys.stderr)
    return fusionado


def construir_precios_data(rows, catalogo):
    """Matchea los nombres de curso del Back Office contra el catálogo
    público scrapeado y arma la lista final para la calculadora. Un curso
    que no matchea con score >= 0.8 queda afuera (mejor no mostrar precio
    que mostrar uno incorrecto)."""
    nombres_curso = sorted({r["curso"] for r in rows if _precios_es_curso_publico(r["curso"])})

    catalogo_tokens = {}
    for path, info in catalogo.items():
        tokens = _precios_normalizar(info["titulo"]) | _precios_normalizar(path.split("/")[-1].replace("-", " "))
        catalogo_tokens[path] = tokens

    paths_usados = set()
    for nombre in nombres_curso:
        ctoks = _precios_normalizar(nombre)
        if not ctoks:
            continue
        mejor_path, mejor_score, mejor_cercania = None, 0, 0
        for path, tokens in catalogo_tokens.items():
            overlap = ctoks & tokens
            if not overlap:
                continue
            score = len(overlap) / len(ctoks)
            # Desempate determinístico entre cursos de la misma familia con
            # el mismo score (ej. "AI Automation" vs "AI Automation
            # Avanzado" para el nombre de Back Office "AI Automation"):
            # cercania castiga al catálogo que trae tokens de más (como
            # "avanzado"), así el curso base nunca termina matcheado al
            # avanzado (ni viceversa) por una carrera de hilos que llega
            # primero.
            cercania = len(overlap) / len(tokens | ctoks)
            if score > mejor_score or (score == mejor_score and cercania > mejor_cercania):
                mejor_score, mejor_cercania, mejor_path = score, cercania, path
        if mejor_path and mejor_score >= 0.8:
            paths_usados.add(mejor_path)

    precios_data = []
    for path in sorted(paths_usados, key=lambda p: catalogo[p]["titulo"]):
        info = catalogo[path]
        if info["tipo"] == "carrera":
            # Última barrera: nunca debería llegar hasta acá una carrera
            # (scrape_catalogo_precios ya las descarta por URL/tipo, no por
            # nombre), pero si alguna vez vuelve a colarse una, se corta
            # acá antes de mostrarla.
            continue
        precios_data.append({
            "id": path.split("/")[-1],
            "titulo": info["titulo"],
            "tipo": info["tipo"],
            "precios": info["precios"],
        })
    return precios_data


def main():
    print("Trayendo comisiones...", file=sys.stderr)
    cohorts = get_all_cohorts()
    print(f"  {len(cohorts)} comisiones encontradas", file=sys.stderr)

    product_ids = {c.get("productId") for c in cohorts if c.get("productId") is not None}
    print(f"Buscando {len(product_ids)} productos...", file=sys.stderr)
    product_map = search_products(product_ids)

    cohort_ids = [c.get("id") for c in cohorts]
    print("Buscando profesores asignados...", file=sys.stderr)
    professors_map = get_all_professors(cohort_ids)

    print("Buscando clases con Zoom configurado...", file=sys.stderr)
    zoom_ends_map = get_all_zoom_ends(cohorts)

    rows = build_rows(cohorts, product_map, professors_map, zoom_ends_map)

    # Desglose por estado para verificar en el log de cada corrida que
    # ninguna comisión (ni las CANCELLED) se está perdiendo en el camino.
    conteo_estados = Counter(r["estado"] or "(sin estado)" for r in rows)
    print("Comisiones por estado en esta corrida:", file=sys.stderr)
    for estado, cantidad in sorted(conteo_estados.items()):
        print(f"  {estado}: {cantidad}", file=sys.stderr)

    print("Scrapeando precios públicos de coderhouse.com...", file=sys.stderr)
    try:
        catalogo_precios = scrape_catalogo_precios()
        precios_data = construir_precios_data(rows, catalogo_precios)
        # Mergea país por país con lo que ya está publicado: evita que un
        # hueco puntual del scrape de hoy (una sola página de un solo país
        # que falló) borre un precio ya conocido para ese país en vez de
        # mostrarlo (ver fusionar_con_precios_publicados).
        precios_data = fusionar_con_precios_publicados(precios_data)
        print(f"  {len(precios_data)} cursos con precio público matcheado", file=sys.stderr)
    except Exception as e:
        # La calculadora de precios es una funcionalidad adicional: si el
        # scraping de coderhouse.com falla por completo (sitio caído,
        # cambio de HTML, etc.) no debe tirar abajo la actualización de
        # comisiones. Antes esto dejaba precios_data en una lista vacía,
        # lo que BORRABA TODA la Calculadora en el index.html resultante
        # (no solo un país de un curso, sino los precios de los ~59 cursos)
        # apenas se publicara esta corrida. Ahora, en su lugar, se conserva
        # lo que ya estaba publicado: una corrida con el scraping caído no
        # debe dejar la Calculadora peor que como estaba.
        publicados = leer_precios_publicados()
        precios_data = sorted(publicados.values(), key=lambda c: c["titulo"]) if publicados else []
        print(
            f"  aviso: no se pudieron traer precios ({e}); se conservan los {len(precios_data)} "
            "precios ya publicados en vez de vaciar la Calculadora esta corrida",
            file=sys.stderr,
        )

    html = render_html(rows, precios_data)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Listo: {OUTPUT_PATH} ({len(rows)} comisiones, {len(precios_data)} precios)", file=sys.stderr)


if __name__ == "__main__":
    main()
