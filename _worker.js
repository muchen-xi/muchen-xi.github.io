/**
 * 248200-xyz Pages 高级模式 (Advanced Mode) Worker
 *
 * 原因: 标准 functions/_middleware.js 在此项目从未生效（部署标 funcs=True 但请求不进函数），
 *       改用 Advanced Mode `_worker.js` —— CF 直接把本文件当 Worker 跑，100% 接管所有请求。
 *
 * 功能（与原 _middleware.js 一致）:
 *   1. UA 黑名单拦截 (aiohttp/python-requests/curl/wget/sqlmap 等 → 403)
 *   2. 空 UA 拦截 → 403
 *   3. /ping — PV 计数 (KV: SITE_ANALYTICS, 限流 60/2min/IP)
 *   4. /_report_stats — PV 数据 (Bearer 鉴权, secret: COUNTER_SHARED_SECRET)
 *   5. 域名路由 — chenxiuniverse.top 跳转 / pimanager / history / health
 *   6. 静态资源回退 — 用 env.ASSETS 或默认回源
 *
 * 绑定 (CF Dashboard → 248200-xyz → Settings → Bindings):
 *   KV:     SITE_ANALYTICS → 132f0237e83845caa4325effad690cee
 *   Secret: COUNTER_SHARED_SECRET
 */

const CORS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type, Authorization',
};

const RATE_LIMIT_MAX = 60;
const RATE_LIMIT_TTL = 120;

const BLOCKED_UA_SUBSTR = [
  'aiohttp',
  'python-requests',
  'python-urllib',
  'httpx',
  'go-http-client',
  'libwww-perl',
  'scrapy',
  'curl',
  'wget',
  'sqlmap',
  'nikto',
  'masscan',
  'nmap',
  'zgrab',
  'zgrab2',
  'hydra',
];

function corsHeaders(extra = {}) {
  return { ...CORS, ...extra };
}

function dateStr(d = new Date()) {
  return d.toISOString().slice(0, 10);
}

function minuteWindow(d = new Date()) {
  const iso = d.toISOString();
  return iso.slice(0, 4) + iso.slice(5, 7) + iso.slice(8, 10)
       + iso.slice(11, 13) + iso.slice(14, 16);
}

const PIXEL_GIF = new Uint8Array([
  0x47, 0x49, 0x46, 0x38, 0x39, 0x61, 0x01, 0x00,
  0x01, 0x00, 0x80, 0x00, 0x00, 0xFF, 0xFF, 0xFF,
  0x00, 0x00, 0x00, 0x21, 0xF9, 0x04, 0x00, 0x00,
  0x00, 0x00, 0x00, 0x2C, 0x00, 0x00, 0x00, 0x00,
  0x01, 0x00, 0x01, 0x00, 0x00, 0x02, 0x02, 0x44,
  0x01, 0x00, 0x3B,
]);

async function handlePing(request, env) {
  const url = new URL(request.url);
  const method = request.method;
  const KV = env.SITE_ANALYTICS;

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
    } catch (_) {}
  }

  if (KV) {
    const site = url.searchParams.get('site') || 'www';
    const pvKey = `pv:${dateStr()}:${site}`;
    try {
      const raw = await KV.get(pvKey);
      const count = raw ? parseInt(raw, 10) : 0;
      await KV.put(pvKey, String(count + 1));
    } catch (_) {}
  }

  if (method === 'POST') {
    return new Response('ok', {
      status: 200,
      headers: corsHeaders({ 'Content-Type': 'text/plain' }),
    });
  }

  return new Response(PIXEL_GIF, {
    status: 200,
    headers: corsHeaders({
      'Content-Type': 'image/gif',
      'Cache-Control': 'no-cache, no-store, must-revalidate',
    }),
  });
}

async function handleReportStats(request, env) {
  const url = new URL(request.url);
  const KV = env.SITE_ANALYTICS;
  const hostname = url.hostname;
  const referer = request.headers.get('Referer') || '';
  const isInternal =
    hostname === 'health.chenxiuniverse.top' ||
    hostname.startsWith('localhost') || hostname.startsWith('127.0.0.1') ||
    /\/health(\/|$)/.test(referer);

  if (!isInternal) {
    const auth = request.headers.get('Authorization');
    const secret = env.COUNTER_SHARED_SECRET;
    if (!secret || !auth || auth !== `Bearer ${secret}`) {
      return new Response('Not Found', { status: 404 });
    }
  }

  const daysParam = parseInt(url.searchParams.get('days'));
  const days = Math.max(1, Math.min(isNaN(daysParam) ? 7 : daysParam, 90));
  const sitesRaw = url.searchParams.get('sites') || 'www,pimanager';
  const sites = sitesRaw.split(',').map(s => s.trim()).filter(Boolean);

  if (!KV) {
    return new Response(JSON.stringify({ error: 'KV namespace SITE_ANALYTICS is not bound' }), {
      status: 500,
      headers: corsHeaders({ 'Content-Type': 'application/json' }),
    });
  }

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

async function serveAsset(request, env, url) {
  if (env.ASSETS) {
    return env.ASSETS.fetch(request);
  }
  // 无 ASSETS 绑定（理论上 Advanced Mode 总有）— 兜底 404
  return new Response('Not Found', { status: 404 });
}

export default {
  async fetch(request, env) {
    try {
      const url = new URL(request.url);
      const hostname = url.hostname;

    // ---- CORS preflight ----
    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: corsHeaders() });
    }

    // ---- UA 黑名单 ----
    const ua = (request.headers.get('User-Agent') || '').toLowerCase();
    if (BLOCKED_UA_SUBSTR.some((s) => ua.includes(s))) {
      return new Response('Forbidden', {
        status: 403,
        headers: corsHeaders({ 'Content-Type': 'text/plain' }),
      });
    }

    // ---- 空 UA 拦截 ----
    if (!ua.trim()) {
      return new Response('Forbidden', {
        status: 403,
        headers: corsHeaders({ 'Content-Type': 'text/plain' }),
      });
    }

    // ---- /ping ----
    if (url.pathname === '/ping') {
      return handlePing(request, env);
    }

    // ---- /stats — 显式 404 ----
    if (url.pathname === '/stats') {
      return new Response('Not Found', { status: 404 });
    }

    // ---- /_report_stats ----
    if (url.pathname === '/_report_stats') {
      return handleReportStats(request, env);
    }

    // ---- 域名路由 ----
    if (hostname === 'chenxiuniverse.top') {
      return Response.redirect('https://www.chenxiuniverse.top' + url.pathname + url.search, 301);
    }
    if (hostname === 'pimanager.chenxiuniverse.top') {
      const assetReq = new Request(new URL('/pimanager' + url.pathname + url.search, url.origin), request);
      return serveAsset(request, env, assetReq);
    }
    if (hostname === 'history.chenxiuniverse.top') {
      if (url.pathname === '/' || url.pathname === '') {
        const assetReq = new Request(new URL('/evolution.html', url.origin), request);
        return serveAsset(request, env, assetReq);
      }
      return serveAsset(request, env, url);
    }
    if (hostname === 'health.chenxiuniverse.top') {
      if (url.pathname === '/' || url.pathname === '') {
        const assetReq = new Request(new URL('/health.html', url.origin), request);
        return serveAsset(request, env, assetReq);
      }
      return serveAsset(request, env, url);
    }

    // ---- 静态资源 ----
    return serveAsset(request, env, url);
    } catch (err) {
      // 任何未捕获错误 → 500，便于诊断（不静默回退静态）
      return new Response('Worker Error: ' + (err && err.message ? err.message : err), {
        status: 500,
        headers: corsHeaders({ 'Content-Type': 'text/plain' }),
      });
    }
  },
};
