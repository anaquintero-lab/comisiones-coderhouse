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


def render_html(rows):
    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        template = f.read()

    data_json = json.dumps(rows, ensure_ascii=False)
    today = datetime.datetime.now(TZ).strftime("%d/%m/%Y %H:%M")

    html = template.replace(
        "window.__COMISIONES_DATA__ = [];",
        f"window.__COMISIONES_DATA__ = {data_json};",
    )
    html = html.replace(
        "Instantánea estática — no se actualiza sola.",
        f"Datos actualizados automáticamente el {today} (hora Argentina).",
    )
    return html


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

    html = render_html(rows)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Listo: {OUTPUT_PATH} ({len(rows)} comisiones)", file=sys.stderr)


if __name__ == "__main__":
    main()
