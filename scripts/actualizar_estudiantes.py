#!/usr/bin/env python3
"""
Trae el listado completo de estudiantes del Back Office de Coderhouse
(nombre, curso, comisión, fechas, día de cursada y estado de cada
inscripción) y lo escribe en `estudiantes_coderhouse.json`. La pestaña
"Estudiantes" de index.html lee ese archivo con un fetch() en el momento
en que se abre la página — este script NO toca index.html para nada, así
que no hay riesgo de que otra corrida (comisiones o precios) lo pise.

Lee las credenciales desde variables de entorno (nunca las escribe en los
archivos generados):
  - CODERHOUSE_API_URL          (ej: https://api-prod.coderhouse.com/api)
  - CLAUDE_STUDENT_API_KEY       (scope: student-enrollment, student-profile)
  - CLAUDE_FINANCE_API_KEY       (scope: finance-product)

Uso:
  python3 scripts/actualizar_estudiantes.py
"""
import os
import sys
import json
import time
import random
import threading
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

# La API rechaza limit > 100 (400 Bad Request: "limit must not be greater than 100")
PAGE_SIZE = 100

# Si la API acepta este enum para `status` en /enrollments cambia con el
# tiempo, basta con tocar esta lista — el resto del script no asume nada
# más sobre los valores.
ENROLLMENT_STATUSES = ["ACTIVE", "COMPLETED", "CANCELLED"]

# ---------------------------------------------------------------------------
# Límite de velocidad + reintentos.
#
# Con ~3000 comisiones, el script antes disparaba hasta 16 pedidos en
# paralelo sin ninguna pausa, y la API de Coderhouse terminaba devolviendo
# 429 (Too Many Requests) para la mayoría — esas comisiones quedaban sin
# inscriptos. Dos cambios para evitarlo:
#   1. Menos hilos en paralelo (antes 16, ahora 6).
#   2. Un "throttle" global: entre un pedido HTTP y el siguiente (sin
#      importar qué hilo lo haga) se espera como mínimo
#      MIN_INTERVALO_ENTRE_PETICIONES segundos.
#   3. Si igual llega un 429 (o un 5xx transitorio), se reintenta con
#      backoff exponencial (respetando el header Retry-After si la API lo
#      manda) en lugar de darlo por perdido al toque.
# ---------------------------------------------------------------------------
MAX_WORKERS_ENROLLMENTS = 6
MAX_WORKERS_PERFILES = 6
MIN_INTERVALO_ENTRE_PETICIONES = 0.12  # techo duro: ~8 pedidos/seg en total
MAX_REINTENTOS_429 = 6
ESPERA_BASE_SEGUNDOS = 1.5

# Opcional: si se define esta variable de entorno con un número de meses,
# se descartan las comisiones cuya fecha de fin sea más vieja que eso antes
# de pedir sus inscriptos — reduce drásticamente la cantidad de pedidos.
# Por defecto NO está activado: se trae el historial completo, como se
# pidió originalmente.
SOLO_ULTIMOS_MESES = os.environ.get("ESTUDIANTES_SOLO_ULTIMOS_MESES")

_rate_lock = threading.Lock()
_ultima_peticion = [0.0]


def _throttle():
    """Asegura que entre un pedido HTTP y el siguiente (de cualquier hilo)
    pase como mínimo MIN_INTERVALO_ENTRE_PETICIONES segundos."""
    with _rate_lock:
        ahora = time.monotonic()
        espera = _ultima_peticion[0] + MIN_INTERVALO_ENTRE_PETICIONES - ahora
        if espera > 0:
            time.sleep(espera)
        _ultima_peticion[0] = time.monotonic()


def _peticion_con_reintentos(fn, *args, **kwargs):
    """Envoltorio de requests.get/requests.post: aplica el throttle antes de
    cada intento y, si la respuesta es 429 o un 5xx transitorio, reintenta
    con backoff exponencial (+ jitter, para que varios hilos no reintenten
    todos en el mismo instante) en vez de devolver el error de una."""
    intento = 0
    while True:
        _throttle()
        resp = fn(*args, **kwargs)
        if resp.status_code not in (429, 500, 502, 503, 504):
            return resp
        intento += 1
        if intento > MAX_REINTENTOS_429:
            return resp  # se agota: que raise_for_status() lo reporte como error final
        retry_after = resp.headers.get("Retry-After") if resp.headers else None
        if retry_after:
            try:
                espera = float(retry_after)
            except ValueError:
                espera = ESPERA_BASE_SEGUNDOS * (2 ** (intento - 1))
        else:
            espera = ESPERA_BASE_SEGUNDOS * (2 ** (intento - 1))
        espera += random.uniform(0, espera * 0.3)
        print(
            f"  aviso: HTTP {resp.status_code} de la API, reintentando en {espera:.1f}s "
            f"(intento {intento}/{MAX_REINTENTOS_429})...",
            file=sys.stderr,
        )
        time.sleep(espera)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Archivo PÚBLICO: lo lee directamente el navegador (fetch) desde la pestaña
# "Estudiantes" — nunca lleva student_id/enrollment_id (son UUIDs internos
# del Back Office, sin uso para quien mira la página).
JSON_PATH = os.path.join(REPO_ROOT, "estudiantes_coderhouse.json")
# Archivo interno: guarda las mismas filas pero CON los IDs internos, solo
# para que la próxima corrida pueda detectar cambios comparando contra esta
# (nadie necesita abrirlo ni el sitio lo consulta).
HISTORIAL_PATH = os.path.join(REPO_ROOT, "estudiantes_historial.json")

MAX_CAMBIOS_GUARDADOS = 300


def student_headers():
    return {"x-api-key": STUDENT_KEY}


def finance_headers():
    return {"x-api-key": FINANCE_KEY}


# ---------------------------------------------------------------------------
# Comisiones + productos (mismo enfoque que actualizar_comisiones.py)
# ---------------------------------------------------------------------------

def get_all_cohorts():
    cohorts = []
    page = 1
    while True:
        resp = _peticion_con_reintentos(
            requests.get,
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
    product_map = {}
    chunk_size = 50
    ids = list(product_ids)
    for i in range(0, len(ids), chunk_size):
        chunk = ids[i : i + chunk_size]
        resp = _peticion_con_reintentos(
            requests.post,
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


def to_arg_dt(iso_str):
    if not iso_str:
        return None
    dt = datetime.datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    return dt.astimezone(TZ)


# ---------------------------------------------------------------------------
# Inscripciones por comisión
#
# El endpoint devuelto por el Back Office es
# GET /student/enrollment/m2m/admin/cohorts/{id}/enrollments?status=&page=&limit=
# No hay documentación pública de la forma exacta de cada item, así que acá
# se extraen los campos probando varios nombres posibles (ver
# `_extraer_campo_estudiante`) en vez de asumir uno solo — si el Back
# Office cambia el nombre de un campo, esto degrada con un aviso en el log
# en lugar de romper la corrida entera.
# ---------------------------------------------------------------------------

def _get(d, *paths, default=None):
    """Busca el primer valor no vacío entre varias rutas posibles dentro de
    un dict anidado. Cada "path" es una tupla de claves, ej. ("student",
    "firstName")."""
    for path in paths:
        cur = d
        ok = True
        for key in path:
            if not isinstance(cur, dict) or key not in cur:
                ok = False
                break
            cur = cur[key]
        if ok and cur not in (None, ""):
            return cur
    return default


def _extraer_campo_estudiante(enrollment):
    student_id = _get(
        enrollment,
        ("studentId",), ("student_id",),
        ("student", "id"), ("studentProfileId",),
    )
    first = _get(
        enrollment,
        ("firstName",), ("studentFirstName",),
        ("student", "firstName"), ("user", "firstName"), ("student", "user", "firstName"),
    )
    last = _get(
        enrollment,
        ("lastName",), ("studentLastName",),
        ("student", "lastName"), ("user", "lastName"), ("student", "user", "lastName"),
    )
    email = _get(
        enrollment,
        ("email",), ("studentEmail",),
        ("student", "email"), ("user", "email"), ("student", "user", "email"),
    )
    enrollment_id = _get(enrollment, ("id",), ("enrollmentId",))
    status = _get(enrollment, ("status",), default="")
    return student_id, first, last, email, enrollment_id, status


def _detalle_error_http(e):
    """Arma un mensaje de error legible, incluyendo el código de estado y el
    cuerpo de la respuesta si están disponibles (clave para distinguir un
    problema de autenticación/API key de uno de parámetros)."""
    resp = getattr(e, "response", None)
    if resp is not None:
        cuerpo = (resp.text or "")[:300]
        return f"HTTP {resp.status_code}: {cuerpo}"
    return str(e)


def get_enrollments_for_cohort(cohort_id):
    """Trae TODAS las inscripciones de una comisión, con o sin filtro de
    `status`. Primero intenta sin filtro (si el Back Office ya devuelve
    todo junto); si eso viene vacío O FALLA (por ejemplo porque el
    endpoint exige el filtro `status` y sin él responde 400), reintenta
    pidiendo cada estado de ENROLLMENT_STATUSES por separado y uniendo
    resultados (deduplicado por id)."""
    def _paginar(params_extra):
        items = []
        page = 1
        while True:
            resp = _peticion_con_reintentos(
                requests.get,
                f"{API_URL}/student/enrollment/m2m/admin/cohorts/{cohort_id}/enrollments",
                headers=student_headers(),
                params={"page": page, "limit": PAGE_SIZE, **params_extra},
                timeout=60,
            )
            resp.raise_for_status()
            data = resp.json()
            batch = data.get("data", data if isinstance(data, list) else [])
            items.extend(batch or [])
            pagination = data.get("pagination") if isinstance(data, dict) else None
            if not pagination or not pagination.get("hasNextPage"):
                break
            page += 1
        return items

    error_sin_filtro = None
    try:
        items = _paginar({})
    except Exception as e:
        # Antes, una excepción acá (ej. 400 porque el endpoint exige
        # `status`) cortaba toda la función y NUNCA se intentaba el
        # fallback con filtro por estado — esto dejaba la comisión
        # entera sin inscriptos sin que se note como error real.
        error_sin_filtro = _detalle_error_http(e)
        items = []

    if not items:
        try:
            vistos = {}
            for estado in ENROLLMENT_STATUSES:
                for it in _paginar({"status": estado}):
                    key = it.get("id") or it.get("enrollmentId") or id(it)
                    vistos[key] = it
            items = list(vistos.values())
        except Exception as e:
            detalle = _detalle_error_http(e)
            if error_sin_filtro:
                detalle = f"sin filtro -> {error_sin_filtro} | con filtro -> {detalle}"
            return cohort_id, [], detalle

    return cohort_id, items, None


def get_all_enrollments(cohort_ids):
    result = {}
    errores = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS_ENROLLMENTS) as ex:
        futures = [ex.submit(get_enrollments_for_cohort, cid) for cid in cohort_ids]
        for fut in as_completed(futures):
            cid, items, err = fut.result()
            result[cid] = items
            if err:
                errores.append((cid, err))
    if errores:
        print(f"  aviso: {len(errores)} comisiones fallaron al traer inscriptos (se las deja sin filas):", file=sys.stderr)
        for cid, err in errores[:10]:
            print(f"    {cid}: {err}", file=sys.stderr)
    return result


# ---------------------------------------------------------------------------
# Resolución de nombre/email por studentId cuando el enrollment no los trae
# embebidos (GET /student/profile/m2m/admin/profiles/{studentId})
# ---------------------------------------------------------------------------

def _resolver_perfil(student_id):
    try:
        resp = _peticion_con_reintentos(
            requests.get,
            f"{API_URL}/student/profile/m2m/admin/profiles/{student_id}",
            headers=student_headers(),
            timeout=30,
        )
        if resp.status_code == 404:
            return student_id, None, None, None
        resp.raise_for_status()
        data = resp.json()
        perfil = data.get("data", data) if isinstance(data, dict) else {}
        first = _get(perfil, ("firstName",), ("user", "firstName"))
        last = _get(perfil, ("lastName",), ("user", "lastName"))
        email = _get(perfil, ("email",), ("user", "email"))
        return student_id, first, last, email
    except Exception:
        return student_id, None, None, None


def resolver_perfiles_faltantes(student_ids):
    result = {}
    ids = [sid for sid in student_ids if sid]
    with ThreadPoolExecutor(max_workers=MAX_WORKERS_PERFILES) as ex:
        futures = [ex.submit(_resolver_perfil, sid) for sid in ids]
        for fut in as_completed(futures):
            sid, first, last, email = fut.result()
            result[sid] = (first, last, email)
    return result


# ---------------------------------------------------------------------------
# Armado de filas
# ---------------------------------------------------------------------------

def build_rows(cohorts, product_map, enrollments_map):
    filas_crudas = []  # (fila, student_id_sin_nombre?)
    student_ids_sin_nombre = set()

    for c in cohorts:
        cohort_id = c.get("id")
        product_id = c.get("productId")
        curso = product_map.get(product_id) or c.get("name", "—")

        start_dt = to_arg_dt(c.get("startDate"))
        end_dt = to_arg_dt(c.get("endDate"))
        week_days = c.get("weekDays") or []
        dias = ", ".join(DAY_NAMES[d] for d in week_days if 0 <= d <= 6)

        for e in enrollments_map.get(cohort_id, []):
            student_id, first, last, email, enrollment_id, estado = _extraer_campo_estudiante(e)
            nombre = " ".join(p for p in [first, last] if p).strip()
            if not nombre and student_id:
                student_ids_sin_nombre.add(student_id)

            filas_crudas.append({
                "student_id": student_id,
                "enrollment_id": enrollment_id,
                "nombre": nombre,
                "email": email or "",
                "curso": curso,
                "comision": c.get("commissionNumber", cohort_id),
                "dia": dias,
                "fecha_inicio": start_dt.strftime("%d/%m/%Y") if start_dt else "",
                "fecha_fin": end_dt.strftime("%d/%m/%Y") if end_dt else "",
                "estado": estado or "",
                "estado_comision": c.get("status", ""),
            })

    if student_ids_sin_nombre:
        print(f"  resolviendo nombre/email de {len(student_ids_sin_nombre)} estudiantes sin datos embebidos en el enrollment...", file=sys.stderr)
        perfiles = resolver_perfiles_faltantes(student_ids_sin_nombre)
        for fila in filas_crudas:
            if not fila["nombre"] and fila["student_id"] in perfiles:
                first, last, email = perfiles[fila["student_id"]]
                fila["nombre"] = " ".join(p for p in [first, last] if p).strip() or "(nombre no disponible)"
                fila["email"] = fila["email"] or (email or "")

    for fila in filas_crudas:
        if not fila["nombre"]:
            fila["nombre"] = "(nombre no disponible)"

    filas_crudas.sort(key=lambda f: (f["nombre"], f["curso"]))
    return filas_crudas


# ---------------------------------------------------------------------------
# Detección de cambios entre esta corrida y la anterior (sin depender de
# ningún endpoint de historial/auditoría, que no existe documentado): se
# compara la fila actual contra `estudiantes_coderhouse.json` tal como
# quedó la corrida pasada, ya commiteado en el repo.
# ---------------------------------------------------------------------------

def detectar_cambios(filas_nuevas, filas_previas):
    hoy = datetime.datetime.now(TZ).strftime("%d/%m/%Y %H:%M")
    prev_por_enrollment = {f["enrollment_id"]: f for f in filas_previas if f.get("enrollment_id")}
    prev_por_alumno_curso = {(f["student_id"], f["curso"]): f for f in filas_previas if f.get("student_id")}

    cambios = []
    for fila in filas_nuevas:
        prev = prev_por_enrollment.get(fila.get("enrollment_id")) if fila.get("enrollment_id") else None
        if prev is None and fila.get("student_id"):
            prev = prev_por_alumno_curso.get((fila["student_id"], fila["curso"]))

        if prev is None:
            continue  # inscripción nueva, no es un "cambio" sobre algo anterior

        if prev.get("estado") != fila.get("estado"):
            cambios.append({
                "detectado_el": hoy,
                "tipo": "estado",
                "nombre": fila["nombre"],
                "curso": fila["curso"],
                "comision": fila["comision"],
                "de": prev.get("estado") or "—",
                "a": fila.get("estado") or "—",
            })
        if prev.get("comision") != fila.get("comision"):
            cambios.append({
                "detectado_el": hoy,
                "tipo": "comision",
                "nombre": fila["nombre"],
                "curso": fila["curso"],
                "comision": fila["comision"],
                "de": prev.get("comision"),
                "a": fila.get("comision"),
            })
    return cambios


def cargar_json_previo():
    try:
        with open(HISTORIAL_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("estudiantes", []), data.get("cambios_recientes", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return [], []


def _filtrar_por_antiguedad(cohorts, meses):
    """Opcional (ESTUDIANTES_SOLO_ULTIMOS_MESES): descarta comisiones cuya
    fecha de fin sea más vieja que `meses` meses, para no pedirle inscriptos
    a comisiones cerradas hace mucho tiempo. Una comisión sin fecha de fin
    se conserva siempre (se la trata como vigente)."""
    limite = datetime.datetime.now(TZ) - datetime.timedelta(days=30 * meses)
    conservadas = []
    for c in cohorts:
        end_dt = to_arg_dt(c.get("endDate"))
        if end_dt is None or end_dt >= limite:
            conservadas.append(c)
    return conservadas


def main():
    print("Trayendo comisiones...", file=sys.stderr)
    cohorts = get_all_cohorts()
    print(f"  {len(cohorts)} comisiones encontradas", file=sys.stderr)

    if SOLO_ULTIMOS_MESES:
        try:
            meses = float(SOLO_ULTIMOS_MESES)
            antes = len(cohorts)
            cohorts = _filtrar_por_antiguedad(cohorts, meses)
            print(
                f"  ESTUDIANTES_SOLO_ULTIMOS_MESES={SOLO_ULTIMOS_MESES}: "
                f"se descartaron {antes - len(cohorts)} comisiones más viejas que eso "
                f"({len(cohorts)} quedan)",
                file=sys.stderr,
            )
        except ValueError:
            print(
                f"  aviso: ESTUDIANTES_SOLO_ULTIMOS_MESES={SOLO_ULTIMOS_MESES!r} no es un número, se ignora (se trae todo el historial)",
                file=sys.stderr,
            )

    product_ids = {c.get("productId") for c in cohorts if c.get("productId") is not None}
    print(f"Buscando {len(product_ids)} productos...", file=sys.stderr)
    product_map = search_products(product_ids)

    cohort_ids = [c.get("id") for c in cohorts]
    print("Trayendo inscriptos de cada comisión...", file=sys.stderr)
    enrollments_map = get_all_enrollments(cohort_ids)
    total_inscriptos = sum(len(v) for v in enrollments_map.values())
    print(f"  {total_inscriptos} inscripciones encontradas en total", file=sys.stderr)

    # Diagnóstico: si todo vino vacío, no hay forma de saber por qué solo
    # mirando el conteo. Mostramos las claves (no los valores, para no
    # volcar datos de estudiantes al log) del primer item crudo que haya
    # devuelto la API, para confirmar si el formato coincide con lo que
    # espera _extraer_campo_estudiante.
    if total_inscriptos == 0 and cohorts:
        print(
            "  AVISO: 0 inscripciones en TODAS las comisiones — si esto no es "
            "normal, revisá los 'aviso: N comisiones fallaron' de arriba (ahí "
            "va el código HTTP y el cuerpo de la respuesta de la API).",
            file=sys.stderr,
        )
    else:
        for items in enrollments_map.values():
            if items:
                print(f"  ejemplo de claves de un item crudo de /enrollments: {sorted(items[0].keys())}", file=sys.stderr)
                break

    filas = build_rows(cohorts, product_map, enrollments_map)

    conteo_estados = Counter(f["estado"] or "(sin estado)" for f in filas)
    print("Inscripciones por estado en esta corrida:", file=sys.stderr)
    for estado, cantidad in sorted(conteo_estados.items()):
        print(f"  {estado}: {cantidad}", file=sys.stderr)

    filas_previas, cambios_previos = cargar_json_previo()
    cambios_nuevos = detectar_cambios(filas, filas_previas)
    if cambios_nuevos:
        print(f"  {len(cambios_nuevos)} cambios detectados respecto de la corrida anterior:", file=sys.stderr)
        for cmb in cambios_nuevos[:20]:
            print(f"    {cmb['nombre']} — {cmb['curso']} — {cmb['tipo']}: {cmb['de']} -> {cmb['a']}", file=sys.stderr)
    else:
        print("  sin cambios respecto de la corrida anterior", file=sys.stderr)

    cambios_totales = (cambios_nuevos + cambios_previos)[:MAX_CAMBIOS_GUARDADOS]

    hoy_iso = datetime.datetime.now(TZ).isoformat()

    # Historial interno: filas COMPLETAS (con student_id/enrollment_id) para
    # que la próxima corrida pueda seguir detectando cambios.
    with open(HISTORIAL_PATH, "w", encoding="utf-8") as f:
        json.dump(
            {"generado_el": hoy_iso, "estudiantes": filas, "cambios_recientes": cambios_totales},
            f, ensure_ascii=False, indent=2,
        )

    # Archivo público: las mismas filas pero sin los IDs internos — es el
    # que la pestaña "Estudiantes" trae con fetch().
    filas_publicas = [{k: v for k, v in f.items() if k not in ("student_id", "enrollment_id")} for f in filas]
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(
            {"generado_el": hoy_iso, "estudiantes": filas_publicas, "cambios_recientes": cambios_totales},
            f, ensure_ascii=False, indent=2,
        )

    print(f"Listo: {JSON_PATH} y {HISTORIAL_PATH} actualizados ({len(filas)} inscripciones, {len(cambios_totales)} cambios en el historial).", file=sys.stderr)


if __name__ == "__main__":
    main()
