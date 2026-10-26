/**
 * Cloudflare Worker — proxy de precios públicos de coderhouse.com.
 *
 * Por qué existe: el botón "🔄 Actualizar precios" de la Calculadora de
 * cambios quiere traer el precio FRESCO de coderhouse.com en el momento en
 * que lo tocás, no esperar a la próxima corrida programada de GitHub
 * Actions. Pero el navegador no puede pedirle directamente a
 * coderhouse.com esos datos (el sitio no manda las cabeceras CORS que
 * hacen falta para eso), así que hace falta un intermediario que sí pueda:
 * este Worker corre del lado del servidor (no en el navegador), así que no
 * tiene el problema de CORS al pedirle la página a coderhouse.com, y le
 * agrega a SU PROPIA respuesta la cabecera CORS que el navegador sí
 * necesita para poder leerla.
 *
 * No usa ninguna credencial de Coderhouse ni de GitHub: solo lee páginas
 * públicas, igual que scripts/scraper_precios_web.py (la misma lógica de
 * extracción, reescrita en JavaScript porque Cloudflare Workers corren JS,
 * no Python).
 *
 * Uso: GET /?curso=<slug>  (ej: /?curso=ai-agents)
 *   Devuelve los precios de ese curso en los 9 países, más la promo
 *   vigente, directo de coderhouse.com, calculados al momento.
 *
 * Deploy (gratis, sin tarjeta, 5 minutos, sin usar la terminal):
 *   1. Entrá a https://dash.cloudflare.com/ y creá una cuenta gratis (o
 *      iniciá sesión si ya tenés una).
 *   2. En el menú de la izquierda: Workers & Pages -> Create -> Create
 *      Worker. Ponele un nombre (ej. "coderhouse-precios") y Deploy.
 *   3. Click en "Edit code" (o "Quick edit"). Borrá todo el código de
 *      ejemplo que trae y pegá el contenido completo de este archivo.
 *      Click en "Save and deploy" (o "Deploy").
 *   4. Cloudflare te va a dar una URL del estilo
 *      https://coderhouse-precios.TU-SUBDOMINIO.workers.dev — esa es la
 *      URL que hay que pegar en index.html / index.template.html donde
 *      dice PRECIOS_WORKER_URL (buscá ese texto en el archivo: está en un
 *      solo lugar, cerca de la función actualizarPreciosEnVivo). Después
 *      de pegarla, volvé a subir index.html a GitHub.
 *   5. Listo — el botón ya hace el fetch en vivo. No hace falta tocar nada
 *      más nunca: el Worker queda corriendo solo, gratis, para siempre (el
 *      plan gratis de Cloudflare permite 100.000 pedidos por día, muy por
 *      encima de lo que este botón va a usar).
 */

const PAIS_PREFIJO = {
  AR: "ar", UY: "uy", CL: "cl", CO: "co", ES: "es",
  MX: "mx", PE: "pe", US: null, VE: null,
};

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", ...CORS_HEADERS },
  });
}

function extraerPar(html, label) {
  const re = new RegExp(`data-framer-name="${label}[^"]*"`);
  const m = re.exec(html);
  if (!m) return null;
  const desde = m.index + m[0].length;
  const seg = html.slice(desde, desde + 400);
  const am = /[$€]\s*([\d.,]+)\s*([A-Z]{3})|S\/\.\s*([\d.,]+)\s*([A-Z]{3})/.exec(seg);
  if (!am) return null;
  const [montoStr, moneda] = am[1] ? [am[1], am[2]] : [am[3], am[4]];
  const monto = parseInt(montoStr.replace(/\./g, "").replace(/,/g, ""), 10);
  return Number.isFinite(monto) ? { monto, moneda } : null;
}

function extraerPromo(html) {
  const m = /data-framer-name="Badge WW"[^>]*>/.exec(html);
  if (!m) return null;
  const desde = m.index + m[0].length;
  const seg = html.slice(desde, desde + 600);
  const pm = /<p[^>]*>([^<]*)<\/p>/.exec(seg);
  if (!pm) return null;
  const texto = pm[1]
    .replace(/&nbsp;/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&quot;/g, '"')
    .trim();
  return texto || null;
}

async function precioDeUnPais(curso, pais, prefijo) {
  const url = prefijo
    ? `https://www.coderhouse.com/${prefijo}/cursos/${curso}`
    : `https://www.coderhouse.com/cursos/${curso}`;
  let html;
  try {
    const resp = await fetch(url, {
      headers: { "User-Agent": "Mozilla/5.0" },
      cf: { cacheTtl: 0 },
    });
    if (!resp.ok) return { pais, error: `coderhouse.com respondió ${resp.status}` };
    html = await resp.text();
  } catch (e) {
    return { pais, error: "no se pudo contactar coderhouse.com" };
  }

  const tachado = extraerPar(html, "Tachado");
  const total = extraerPar(html, "Total");
  const promo = extraerPromo(html);
  if (!tachado || !total || tachado.monto <= 0) {
    return { pais, error: "no se encontró el precio en la página", promo };
  }
  const moneda = tachado.moneda || total.moneda;
  const discount_pct = Math.round((1 - total.monto / tachado.monto) * 100);
  return {
    pais,
    precio: { list: tachado.monto, final: total.monto, currency: moneda, discount_pct },
    promo,
  };
}

export default {
  async fetch(request) {
    if (request.method === "OPTIONS") {
      return new Response(null, { headers: CORS_HEADERS });
    }

    const url = new URL(request.url);
    const curso = (url.searchParams.get("curso") || "").trim();
    if (!curso || !/^[a-z0-9-]+$/.test(curso)) {
      return json({ error: "falta el parámetro ?curso=<slug-del-curso>" }, 400);
    }

    const resultados = await Promise.all(
      Object.entries(PAIS_PREFIJO).map(([pais, prefijo]) => precioDeUnPais(curso, pais, prefijo))
    );

    const precios = {};
    let promo = null;
    let encontrados = 0;
    for (const r of resultados) {
      if (r.promo && !promo) promo = r.promo;
      if (r.precio) {
        precios[r.pais] = r.precio;
        encontrados += 1;
      }
    }

    if (encontrados === 0) {
      return json(
        { error: "no se encontró precio público para ese curso en ningún país", curso },
        404
      );
    }

    return json({
      curso,
      promo,
      precios,
      fetched_at: new Date().toISOString(),
    });
  },
};
