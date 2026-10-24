#!/usr/bin/env python3
"""
Actualiza SOLO los precios públicos de la Calculadora de cambios en
index.html, scrapeando las páginas públicas de coderhouse.com.

A diferencia de scripts/actualizar_comisiones.py, este script NO necesita
ninguna credencial de la API de Coderhouse (ni CLAUDE_STUDENT_API_KEY ni
CLAUDE_FINANCE_API_KEY): solo lee páginas públicas con requests. Por eso
puede correr solo, en un cron de GitHub Actions, sin ningún secreto
configurado y sin consumir tokens de Claude.

Qué hace, en orden:
  1. Lee los cursos que la calculadora ya tiene cargados hoy en
     index.html (window.__PRECIOS_DATA__).
  2. Vuelve a scrapear el catálogo público de coderhouse.com (precio de
     lista y precio final por país, para cada curso/carrera con página
     pública).
  3. Para cada curso ya cargado, refresca sus precios con lo que
     encontró en el sitio. Si un curso dejó de tener página pública (o el
     scraping falla para ese curso puntual), conserva el último precio
     conocido en vez de borrarlo — mejor un precio un poco viejo que
     ninguno.
  4. Suma a la lista cualquier curso nuevo que haya aparecido en el sitio
     público y todavía no estuviera cargado.
  5. Reescribe window.__PRECIOS_DATA__ en index.html con la lista
     actualizada, guarda una copia en precios_coderhouse.json, y actualiza
     el cartel de "última actualización" de la calculadora.

Uso:
  python3 scripts/scraper_precios_web.py

Se ejecuta automáticamente todos los días vía
.github/workflows/scraper.yml (sin secretos). También se puede
disparar a mano desde la pestaña "Actions" del repositorio en GitHub, o
con el botón "🔄 Actualizar precios web" de la propia calculadora, que
abre esa misma pantalla de Actions.
"""
import json
import os
import re
import sys
import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from zoneinfo import ZoneInfo

import requests

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX_PATH = os.path.join(REPO_ROOT, "index.html")
JSON_PATH = os.path.join(REPO_ROOT, "precios_coderhouse.json")
TZ = ZoneInfo("America/Argentina/Buenos_Aires")


# ---------------------------------------------------------------------------
# Scraping de precios públicos de coderhouse.com (idéntico al que usa
# scripts/actualizar_comisiones.py — ver ahí el detalle de por qué se
# scrapea el sitio público en vez de usar el API de pricing del Back
# Office: el API no tiene ningún precio real cargado).
# ---------------------------------------------------------------------------

PRECIOS_SITEMAP_URL = "https://www.coderhouse.com/sitemap_es-AR.xml"

# País -> prefijo de locale en la URL. None = página sin prefijo (precio
# genérico "resto del mundo" en USD, que es el que efectivamente se le
# muestra a Estados Unidos y Venezuela).
PRECIOS_COUNTRY_PREFIX = {
    "AR": "ar", "UY": "uy", "CL": "cl", "CO": "co", "ES": "es",
    "MX": "mx", "PE": "pe", "US": None, "VE": None,
}

PRECIOS_AMOUNT_RE = re.compile(
    r'[\$€]\s*([\d.,]+)\s*([A-Z]{3})|S/\.\s*([\d.,]+)\s*([A-Z]{3})'
)

PRECIOS_STOPWORDS = {
    "de", "la", "el", "los", "las", "y", "a", "en", "para", "con", "del",
    "al", "un", "una", "online", "curso", "cursos", "carrera", "diplomatura",
    "diplomaturas", "v1", "v2", "v3", "en", "b2b", "coderhouse", "ch",
}


def _precios_descubrir_urls_catalogo():
    """Lee el sitemap de Argentina y devuelve los paths /cursos/slug y
    /carreras/slug (sin prefijo de país) que son páginas de producto reales."""
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
    m = re.search(r"<title[^>]*>([^<|]+)", html, re.IGNORECASE)
    return m.group(1).strip() if m else None


def _precios_fetch_pagina(path, prefix):
    url = f"https://www.coderhouse.com/{prefix}/{path}" if prefix else f"https://www.coderhouse.com/{path}"
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
        print(f"  aviso: no se pudo traer {url}: {e}", file=sys.stderr)
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


def scrape_catalogo_precios():
    """Trae precio de lista + final por país para cada curso/carrera público
    de coderhouse.com. Devuelve {path: {"titulo", "tipo", "precios"}}."""
    paths = _precios_descubrir_urls_catalogo()
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
            tipo = "carrera" if path.startswith("carreras/") else "curso"
            titulo_limpio = titulo.replace("Curso de ", "").replace("Carrera de ", "").strip()
            catalogo[path] = {"titulo": titulo_limpio, "tipo": tipo, "precios": precios}
    return catalogo


# ---------------------------------------------------------------------------
# Refresco del archivo index.html ya publicado
# ---------------------------------------------------------------------------

def leer_precios_actuales():
    with open(INDEX_PATH, encoding="utf-8") as f:
        html = f.read()
    m = re.search(r"window\.__PRECIOS_DATA__\s*=\s*(\[.*?\]);", html, re.DOTALL)
    if not m:
        print("No se encontró window.__PRECIOS_DATA__ en index.html — abortando sin tocar nada.", file=sys.stderr)
        sys.exit(1)
    try:
        precios = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        print(f"window.__PRECIOS_DATA__ no es JSON válido ({e}) — abortando sin tocar nada.", file=sys.stderr)
        sys.exit(1)
    return html, precios


def refrescar_precios(precios_actuales, catalogo):
    """Devuelve la lista de precios actualizada: cada curso ya cargado
    mantiene su lugar y su id, pero con los precios frescos del catálogo
    público cuando están disponibles (si no, conserva el precio anterior).
    Los cursos nuevos del catálogo que todavía no estaban cargados se
    agregan al final, igual que hace construir_precios_data()."""
    # Indexa el catálogo por slug (último segmento del path), guardando
    # también el "tipo" para poder distinguir cursos/carreras que
    # compartan slug.
    por_slug = {}
    for path, info in catalogo.items():
        slug = path.split("/")[-1]
        por_slug.setdefault(slug, {})[info["tipo"]] = info

    actualizados = []
    slugs_vistos = set()
    cambiados = 0
    for curso in precios_actuales:
        slug = curso.get("id")
        slugs_vistos.add(slug)
        opciones = por_slug.get(slug)
        info_fresca = None
        if opciones:
            info_fresca = opciones.get(curso.get("tipo")) or next(iter(opciones.values()))
        if info_fresca:
            if info_fresca["precios"] != curso.get("precios"):
                cambiados += 1
            actualizados.append({
                "id": slug,
                "titulo": info_fresca["titulo"],
                "tipo": info_fresca["tipo"],
                "precios": info_fresca["precios"],
            })
        else:
            # No se encontró en el scraping de hoy (sitio caído para esa
            # página puntual, cambió de slug, etc.): conservamos el
            # último precio conocido en vez de dejar el curso sin precio.
            actualizados.append(curso)

    nuevos = 0
    for path, info in catalogo.items():
        slug = path.split("/")[-1]
        if slug in slugs_vistos:
            continue
        actualizados.append({
            "id": slug, "titulo": info["titulo"], "tipo": info["tipo"], "precios": info["precios"],
        })
        nuevos += 1

    actualizados.sort(key=lambda c: c["titulo"])
    print(f"  {cambiados} cursos con precio distinto al anterior, {nuevos} cursos nuevos", file=sys.stderr)
    return actualizados


def actualizar_timestamp(html, momento):
    """Actualiza (si existe) el cartel con id="precios-last-updated" de la
    calculadora. No falla si el elemento no está (versiones viejas del
    archivo)."""
    nuevo, n = re.subn(
        r'(id="precios-last-updated"[^>]*>)[^<]*(<)',
        rf'\g<1>Precios actualizados el {momento} (hora Argentina)\g<2>',
        html,
    )
    if n == 0:
        print('  aviso: no se encontró el cartel #precios-last-updated (se actualizaron los precios igual)', file=sys.stderr)
        return html
    return nuevo


def main():
    print("Leyendo precios actuales de index.html...", file=sys.stderr)
    html, precios_actuales = leer_precios_actuales()
    print(f"  {len(precios_actuales)} cursos cargados actualmente", file=sys.stderr)

    print("Scrapeando precios públicos de coderhouse.com...", file=sys.stderr)
    catalogo = scrape_catalogo_precios()
    print(f"  {len(catalogo)} páginas públicas con precio encontrado", file=sys.stderr)

    if not catalogo:
        print("El scraping no encontró ningún precio público; se deja index.html sin cambios.", file=sys.stderr)
        return

    precios_nuevos = refrescar_precios(precios_actuales, catalogo)

    precios_json = json.dumps(precios_nuevos, ensure_ascii=False)
    nuevo_html, n = re.subn(
        r"window\.__PRECIOS_DATA__\s*=\s*\[.*?\];",
        lambda _m: f"window.__PRECIOS_DATA__ = {precios_json};",
        html,
        count=1,
        flags=re.DOTALL,
    )
    if n == 0:
        print("No se pudo reemplazar window.__PRECIOS_DATA__ en index.html — abortando sin escribir.", file=sys.stderr)
        sys.exit(1)

    momento = datetime.datetime.now(TZ).strftime("%d/%m/%Y %H:%M")
    nuevo_html = actualizar_timestamp(nuevo_html, momento)

    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        f.write(nuevo_html)
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(precios_nuevos, f, ensure_ascii=False, indent=2)

    print(f"Listo: {INDEX_PATH} y {JSON_PATH} actualizados ({len(precios_nuevos)} cursos, {momento}).", file=sys.stderr)


if __name__ == "__main__":
    main()
