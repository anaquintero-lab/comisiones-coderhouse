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
  2. Vuelve a scrapear el catálogo público de coderhouse.com: precio de
     lista, precio final y nombre de la promoción vigente (ej. "CODERWEEN
     💣"), por país, para cada CURSO individual con página pública.
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

IMPORTANTE — cursos vs. carreras: la calculadora de cambios muestra
precios de CURSOS individuales (un estudiante cambia de un curso a otro),
nunca de carreras completas. coderhouse.com también tiene una página
pública por cada carrera (ej. "Carrera de Data Analytics"), y bastantes
veces su título normalizado coincide con el de un curso individual que
forma parte de ella (ej. "Data Analytics" a secas). Si ambas páginas
conviven en la calculadora con el mismo título visible, el desplegable
"Curso" las muestra como dos opciones idénticas sin forma de distinguirlas
— y elegir una puede terminar mostrando el precio (mucho mayor) de la
carrera completa en vez del curso suelto. Por eso este script SOLO trae y
conserva páginas de tipo "curso": cualquier página de "carrera" se
descarta explícitamente, tanto al sumar cursos nuevos como al limpiar los
que ya hubiera quedado cargados de una corrida anterior.

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
from html import unescape
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


def _precios_extraer_promo(html):
    """Nombre/etiqueta de la campaña de descuento vigente en coderhouse.com
    (ej. "CODERWEEN 💣", "Black Friday"), si hay alguna activa. Se muestra
    en la franja de descuentos de cada página pública, en el bloque
    data-framer-name="Badge WW". Devuelve None si no hay ninguna campaña
    corriendo (la franja no está presente) o no se pudo leer."""
    m = re.search(r'data-framer-name="Badge WW"[^>]*>', html)
    if not m:
        return None
    seg = html[m.end():m.end() + 600]
    pm = re.search(r"<p[^>]*>([^<]*)</p>", seg)
    if not pm:
        return None
    texto = unescape(pm.group(1)).strip()
    return texto or None


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
    promo = None
    precios_por_pais = {}
    for pais, prefix in PRECIOS_COUNTRY_PREFIX.items():
        if prefix not in cache:
            cache[prefix] = _precios_fetch_pagina(path, prefix)
        html = cache[prefix]
        if not html:
            continue
        if titulo is None:
            titulo = _precios_extraer_titulo(html)
        if promo is None:
            promo = _precios_extraer_promo(html)
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
    return titulo, precios_por_pais, promo


def scrape_catalogo_precios():
    """Trae precio de lista + final + promo vigente por país para cada
    CURSO individual público de coderhouse.com (las páginas de carrera se
    descartan a propósito — ver nota al principio del archivo: evita que
    la calculadora confunda el precio de una carrera con el del curso
    suelto cuando comparten nombre). Devuelve
    {path: {"titulo", "tipo", "precios", "promo"}}, con "tipo" siempre
    "curso"."""
    paths = [p for p in _precios_descubrir_urls_catalogo() if p.startswith("cursos/")]
    catalogo = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(_precios_scrapear_curso, p): p for p in paths}
        for fut in as_completed(futs):
            path = futs[fut]
            try:
                titulo, precios, promo = fut.result()
            except Exception as e:
                print(f"  aviso: fallo scrapeando {path}: {e}", file=sys.stderr)
                continue
            if not titulo or not precios:
                continue
            titulo_limpio = titulo.replace("Curso de ", "").replace("Carrera de ", "").strip()
            catalogo[path] = {
                "titulo": titulo_limpio, "tipo": "curso", "precios": precios, "promo": promo,
            }
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
    mantiene su lugar y su id, pero con los precios (y la promo vigente)
    frescos del catálogo público cuando están disponibles (si no, conserva
    el precio anterior). Los cursos nuevos del catálogo que todavía no
    estaban cargados se agregan al final.

    `catalogo` ya viene filtrado a solo tipo "curso" (ver
    scrape_catalogo_precios): cualquier entrada de tipo "carrera" que haya
    quedado cargada de una corrida anterior a este arreglo se descarta acá,
    para no mezclar nunca el precio de una carrera con el de un curso
    individual del mismo nombre."""
    por_slug = {}
    for path, info in catalogo.items():
        slug = path.split("/")[-1]
        por_slug[slug] = info  # catalogo ya es solo "curso": un slug, una entrada

    actualizados = []
    slugs_vistos = set()
    cambiados = 0
    descartadas_carrera = 0
    for curso in precios_actuales:
        if curso.get("tipo") == "carrera":
            # Limpieza de una corrida anterior a este arreglo: la
            # calculadora de cambios es solo de cursos individuales.
            descartadas_carrera += 1
            continue
        slug = curso.get("id")
        slugs_vistos.add(slug)
        info_fresca = por_slug.get(slug)
        if info_fresca:
            if info_fresca["precios"] != curso.get("precios"):
                cambiados += 1
            actualizados.append({
                "id": slug,
                "titulo": info_fresca["titulo"],
                "tipo": "curso",
                "precios": info_fresca["precios"],
                "promo": info_fresca.get("promo"),
            })
        else:
            # No se encontró en el scraping de hoy (sitio caído para esa
            # página puntual, cambió de slug, etc.): conservamos el
            # último precio conocido en vez de dejar el curso sin precio.
            actualizados.append(curso)

    nuevos = 0
    for slug, info in por_slug.items():
        if slug in slugs_vistos:
            continue
        actualizados.append({
            "id": slug, "titulo": info["titulo"], "tipo": "curso",
            "precios": info["precios"], "promo": info.get("promo"),
        })
        nuevos += 1

    actualizados.sort(key=lambda c: c["titulo"])
    print(
        f"  {cambiados} cursos con precio distinto al anterior, {nuevos} cursos nuevos, "
        f"{descartadas_carrera} carreras descartadas (la calculadora es solo de cursos individuales)",
        file=sys.stderr,
    )
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
