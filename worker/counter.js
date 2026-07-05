/**
 * 晨曦的宇宙 · 页面访问计数器
 *
 * 记录 PV 到 KV (SITE_ANALYTICS), 通过 tracking pixel 触发,
 * 报告生成器通过 /stats 端点读取数据。
 *
 * 端点:
 *   POST /ping?site=www     sendBeacon 记录一次访问 (POST)
 *   GET  /ping?site=www     老式 img pixel 记录 (GET 兜底)
 *   GET  /stats?days=7      返回最近 N 天 JSON 数据 (需认证)
 *
 * KV key 格式: pv:YYYY-MM-DD:site
 *
 * 保护:
 *   - /ping: Origin/Referer 校验 + KV 简易频率限制 (120次/分钟/IP)
 *   - /stats: 需 Authorization: Bearer <SHARED_SECRET>
 *
 * 部署 (Account B: 20a34acd...):
 *   npx wrangler kv:namespace create SITE_ANALYTICS
 *   npx wrangler secret put SHARED_SECRET --name counter
 *   npx wrangler deploy worker/counter.js --name counter \\
 *     --compatibility-date 2025-01-01
 *   然后 CF Dashboard → Workers & Pages → counter → Settings →
 *     Bindings → KV → SITE_ANALYTICS → 绑定 namespace
 */

const PIXEL = 'R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7';

// 允许的来源域名 (反滥用)
const ALLOWED_ORIGINS = [
  'https://www.chenxiuniverse.top',
  'https://chenxiuniverse.top',
  'https://248200.xyz',
  'https://www.248200.xyz',
  'https://pimanager.chenxiuniverse.top',
  'https://pimanager.248200.xyz',
];

// /ping 频率限制: 每个 IP 每分钟最多 120 次
const RL_WINDOW_SEC = 60;
const RL_MAX_REQUESTS = 120;

function decodePixel() {
  return Uint8Array.from(atob(PIXEL), c => c.charCodeAt(0));
}

function getClientIP(request) {
  return request.headers.get('CF-Connecting-IP') || '0.0.0.0';
}

function isAllowedOrigin(request) {
  const origin = request.headers.get('Origin');
  if (origin && ALLOWED_ORIGINS.includes(origin)) return true;
  // Referer 兜底 (img pixel 不带 Origin)
  const referer = request.headers.get('Referer');
  if (referer) {
    try {
      const refOrigin = new URL(referer).origin;
      if (ALLOWED_ORIGINS.includes(refOrigin)) return true;
    } catch (_) {}
  }
  // 本地开发
  const host = request.headers.get('Host') || '';
  if (host.startsWith('localhost') || host.startsWith('127.0.0.1')) return true;
  return false;
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const path = url.pathname;

    // CORS 预检
    if (request.method === 'OPTIONS') {
      return new Response(null, {
        status: 204,
        headers: {
          'Access-Control-Allow-Origin': '*',
          'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
          'Access-Control-Allow-Headers': 'Authorization, Content-Type',
          'Access-Control-Max-Age': '86400',
        },
      });
    }

    // ── /ping — 记录一次页面访问 ──
    if (path === '/ping') {
      // Origin/Referer 校验
      if (!isAllowedOrigin(request)) {
        return new Response('Forbidden', { status: 403 });
      }

      // 简易频率限制 (非阻塞 — 异步写 KV)
      const ip = getClientIP(request);
      const now = Math.floor(Date.now() / 1000);
      const windowKey = Math.floor(now / RL_WINDOW_SEC);
      const rlKey = `rl:${ip}:${windowKey}`;

      const current = await env.SITE_ANALYTICS.get(rlKey);
      const rlCount = (parseInt(current) || 0) + 1;
      ctx.waitUntil(
        env.SITE_ANALYTICS.put(rlKey, rlCount.toString(), {
          expirationTtl: RL_WINDOW_SEC * 2,
        })
      );

      if (rlCount > RL_MAX_REQUESTS) {
        return new Response('Too Many Requests', { status: 429 });
      }

      // 正常计数
      const site = url.searchParams.get('site') || 'www';
      const today = new Date().toISOString().split('T')[0];
      const key = `pv:${today}:${site}`;

      const pvCurrent = await env.SITE_ANALYTICS.get(key);
      const count = (parseInt(pvCurrent) || 0) + 1;
      await env.SITE_ANALYTICS.put(key, count.toString());

      if (request.method === 'POST') {
        return new Response(null, {
          status: 200,
          headers: {
            'Access-Control-Allow-Origin': '*',
            'Cache-Control': 'no-store',
          },
        });
      }

      return new Response(decodePixel(), {
        status: 200,
        headers: {
          'Content-Type': 'image/gif',
          'Cache-Control': 'no-cache, no-store, must-revalidate',
          'Access-Control-Allow-Origin': '*',
        },
      });
    }

    // ── /stats — 读取统计数据 (需认证) ──
    if (path === '/stats') {
      const auth = request.headers.get('Authorization');
      const expected = `Bearer ${env.SHARED_SECRET}`;

      if (!auth || auth !== expected) {
        return new Response('Unauthorized', {
          status: 401,
          headers: { 'WWW-Authenticate': 'Bearer' },
        });
      }

      const days = Math.min(parseInt(url.searchParams.get('days') || '7'), 90);
      const sites = (url.searchParams.get('sites') || 'www,pimanager').split(',');

      const dates = [];
      for (let i = days - 1; i >= 0; i--) {
        const d = new Date();
        d.setDate(d.getDate() - i);
        dates.push(d.toISOString().split('T')[0]);
      }

      const stats = {};
      for (const date of dates) {
        stats[date] = {};
        for (const site of sites) {
          const kvKey = `pv:${date}:${site}`;
          const val = await env.SITE_ANALYTICS.get(kvKey);
          stats[date][site] = parseInt(val) || 0;
        }
      }

      return new Response(JSON.stringify(stats), {
        status: 200,
        headers: {
          'Content-Type': 'application/json',
          'Access-Control-Allow-Origin': '*',
          'Cache-Control': 'no-store',
        },
      });
    }

    return new Response('Not Found', { status: 404 });
  },
};
