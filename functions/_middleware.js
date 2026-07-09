// Hostname-based routing + PV counting middleware — zero Worker dependency
//
// KV binding required in CF Dashboard:
//   Pages → 248200-xyz → Settings → Bindings → KV
//   Variable name: SITE_ANALYTICS
//   KV namespace:  SITE_ANALYTICS (132f0237e83845caa4325effad690cee)
//
// This middleware handles:
//   1. /ping          — PV counter (direct KV write, sendBeacon + img fallback)
//   2. /_report_stats — read PV data (Bearer auth, JSON)
//   3. Domain routing — chenxiuniverse.top, pimanager, history, health
//   4. CORS preflight — OPTIONS → 204
//
// The separate functions/ping.js and functions/_report_stats.js are now dead code
// (middleware returns a response directly without calling next()) and can be deleted.

const BEARER_TOKEN = '207ddbc0c5376668adb2f5c225fae18ed0859c3cf86865ab';

const CORS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type, Authorization',
};

const RATE_LIMIT_MAX = 120;
const RATE_LIMIT_TTL = 120; // seconds (2 min — covers the 1-min window with buffer)

// 1×1 transparent GIF (43 bytes, hardcoded — no atob dependency)
const PIXEL_GIF = new Uint8Array([
  0x47, 0x49, 0x46, 0x38, 0x39, 0x61, 0x01, 0x00,
  0x01, 0x00, 0x80, 0x00, 0x00, 0xFF, 0xFF, 0xFF,
  0x00, 0x00, 0x00, 0x21, 0xF9, 0x04, 0x00, 0x00,
  0x00, 0x00, 0x00, 0x2C, 0x00, 0x00, 0x00, 0x00,
  0x01, 0x00, 0x01, 0x00, 0x00, 0x02, 0x02, 0x44,
  0x01, 0x00, 0x3B,
]);

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Merge extra headers into the base CORS set. */
function corsHeaders(extra = {}) {
  return { ...CORS, ...extra };
}

/** "YYYY-MM-DD" for a given Date (defaults to today UTC). */
function dateStr(d = new Date()) {
  return d.toISOString().slice(0, 10);
}

/** "YYYYMMDDHHmm" — one bucket per minute. */
function minuteWindow(d = new Date()) {
  const iso = d.toISOString(); // "2026-07-01T12:34:56.789Z"
  return iso.slice(0, 4) + iso.slice(5, 7) + iso.slice(8, 10)
       + iso.slice(11, 13) + iso.slice(14, 16);
}

// ---------------------------------------------------------------------------
// Route handlers
// ---------------------------------------------------------------------------

/** POST → 200 plaintext (sendBeacon) | GET → 1×1 GIF (img fallback). */
async function handlePing(request, KV) {
  const url = new URL(request.url);
  const method = request.method;

  // ---- soft rate-limit (best-effort; KV eventual consistency is fine here) ----
  if (KV) {
    const ip = request.headers.get('CF-Connecting-IP') || '0.0.0.0';
    const rlKey = `rl:${ip}:${minuteWindow()}`;
    try {
      const raw = await KV.get(rlKey);
      const count = raw ? parseInt(raw, 10) : 0;
      if (count >= RATE_LIMIT_MAX) {
        return new Response('Too Many Requests', {
          status: 429,
          headers: corsHeaders({ 'Content-Type': 'text/plain' }),
        });
      }
      await KV.put(rlKey, String(count + 1), { expirationTtl: RATE_LIMIT_TTL });
    } catch (_) {
      // rate-limit KV failure → allow the request through
    }
  }

  // ---- increment PV counter ----
  if (KV) {
    const site = url.searchParams.get('site') || 'www';
    const pvKey = `pv:${dateStr()}:${site}`;
    try {
      const raw = await KV.get(pvKey);
      const count = raw ? parseInt(raw, 10) : 0;
      await KV.put(pvKey, String(count + 1));
    } catch (_) {
      // silent fail — never block the response for a broken counter
    }
  }

  // POST: sendBeacon path
  if (method === 'POST') {
    return new Response('ok', {
      status: 200,
      headers: corsHeaders({ 'Content-Type': 'text/plain' }),
    });
  }

  // GET (and anything else): img fallback
  return new Response(PIXEL_GIF, {
    status: 200,
    headers: corsHeaders({
      'Content-Type': 'image/gif',
      'Cache-Control': 'no-cache, no-store, must-revalidate',
    }),
  });
}

/** JSON report of PV counts — public for health.chenxiuniverse.top, Bearer for others. */
async function handleReportStats(request, KV) {
  const url = new URL(request.url);

  // ---- auth gate: skip for internal health panel, require Bearer for external ----
  const hostname = url.hostname;
  const isInternal = hostname === 'health.chenxiuniverse.top' || hostname.startsWith('localhost') || hostname.startsWith('127.0.0.1');
  if (!isInternal) {
    const auth = request.headers.get('Authorization');
    if (!auth || auth !== `Bearer ${BEARER_TOKEN}`) {
      return new Response('Not Found', { status: 404 });
    }
  }

  // ---- params ----
  const daysParam = parseInt(url.searchParams.get('days'));
  const days = Math.min(isNaN(daysParam) ? 7 : daysParam, 90);

  const sitesRaw = url.searchParams.get('sites') || 'www,pimanager';
  const sites = sitesRaw.split(',').map(s => s.trim()).filter(Boolean);

  if (!KV) {
    return new Response(JSON.stringify({ error: 'KV namespace SITE_ANALYTICS is not bound' }), {
      status: 500,
      headers: corsHeaders({ 'Content-Type': 'application/json' }),
    });
  }

  // ---- build result: { "YYYY-MM-DD": { "site": N, ... }, ... } ----
  const result = {};

  for (let i = 0; i < days; i++) {
    const d = new Date();
    d.setDate(d.getDate() - i);
    const day = dateStr(d);
    result[day] = {};

    for (const site of sites) {
      const pvKey = `pv:${day}:${site}`;
      try {
        const raw = await KV.get(pvKey);
        result[day][site] = raw ? parseInt(raw, 10) : 0;
      } catch (_) {
        result[day][site] = 0;
      }
    }
  }

  return new Response(JSON.stringify(result), {
    status: 200,
    headers: corsHeaders({ 'Content-Type': 'application/json' }),
  });
}

// ---------------------------------------------------------------------------
// Main entry
// ---------------------------------------------------------------------------

export async function onRequest(context) {
  const { request, next, env } = context;
  const KV = env.SITE_ANALYTICS; // undefined when the binding is not yet configured
  const url = new URL(request.url);
  const hostname = url.hostname;

  // ---- CORS preflight ----
  if (request.method === 'OPTIONS') {
    return new Response(null, { status: 204, headers: corsHeaders() });
  }

  // ---- /ping — PV counter (must be before domain routing) ----
  if (url.pathname === '/ping') {
    return handlePing(request, KV);
  }

  // ---- /stats — explicitly 404 (legacy endpoint, never existed publicly) ----
  if (url.pathname === '/stats') {
    return new Response('Not Found', { status: 404 });
  }

  // ---- /_report_stats — internal PV report (must be before domain routing) ----
  if (url.pathname === '/_report_stats') {
    return handleReportStats(request, KV);
  }

  // ---- Domain-based routing (preserved from original middleware) ----

  // Redirect apex to www
  if (hostname === 'chenxiuniverse.top') {
    return Response.redirect('https://www.chenxiuniverse.top' + url.pathname + url.search, 301);
  }

  // pimanager subdomain → /pimanager/ content
  if (hostname === 'pimanager.chenxiuniverse.top') {
    return env.ASSETS.fetch(new URL('/pimanager' + url.pathname, url.origin).toString());
  }

  // history subdomain → evolution page
  if (hostname === 'history.chenxiuniverse.top') {
    return env.ASSETS.fetch(new URL('/evolution.html', url.origin).toString());
  }

  // health subdomain → health page
  if (hostname === 'health.chenxiuniverse.top') {
    return env.ASSETS.fetch(new URL('/health.html', url.origin).toString());
  }

  // Everything else → static assets
  return next();
}
