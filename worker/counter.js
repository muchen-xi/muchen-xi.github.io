/**
 * 晨曦的宇宙 · 页面访问计数器
 *
 * 记录 PV 到 KV (SITE_ANALYTICS), 通过 tracking pixel 触发,
 * 报告生成器通过 /stats 端点读取数据。
 *
 * 端点:
 *   POST /ping?site=www     sendBeacon 记录一次访问 (POST)
 *   GET  /ping?site=www     老式 img pixel 记录 (GET 兜底)
 *   GET  /stats?days=7      返回最近 N 天 JSON 数据
 *
 * KV key 格式: pv:YYYY-MM-DD:site
 *
 * 部署 (Account B: 20a34acd...):
 *   npx wrangler kv:namespace create SITE_ANALYTICS
 *   npx wrangler deploy worker/counter.js --name counter \
 *     --compatibility-date 2025-01-01
 *   然后 CF Dashboard → Workers & Pages → counter → Settings →
 *     Bindings → KV → SITE_ANALYTICS → 绑定 namespace
 */

const PIXEL = 'R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7';

function decodePixel() {
  return Uint8Array.from(
    atob(PIXEL),
    c => c.charCodeAt(0)
  );
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname;

    // ── /ping — 记录一次页面访问 ──
    if (path === '/ping') {
      const site = url.searchParams.get('site') || 'www';
      const today = new Date().toISOString().split('T')[0];
      const key = `pv:${today}:${site}`;

      // KV 读-改-写 (低流量下丢计数风险可忽略)
      const current = await env.SITE_ANALYTICS.get(key);
      const count = (parseInt(current) || 0) + 1;
      await env.SITE_ANALYTICS.put(key, count.toString());

      // 返回 1×1 透明 GIF (img pixel) 或空 200 (beacon)
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

    // ── /stats — 读取统计数据 ──
    if (path === '/stats') {
      const days = Math.min(parseInt(url.searchParams.get('days') || '7'), 90);
      const sites = (url.searchParams.get('sites') || 'www,pimanager').split(',');

      // 生成日期列表
      const dates = [];
      for (let i = days - 1; i >= 0; i--) {
        const d = new Date();
        d.setDate(d.getDate() - i);
        dates.push(d.toISOString().split('T')[0]);
      }

      // 批量读 KV
      const stats = {};
      for (const date of dates) {
        stats[date] = {};
        for (const site of sites) {
          const key = `pv:${date}:${site}`;
          const val = await env.SITE_ANALYTICS.get(key);
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

    // ── 其他路径 — 404 ──
    return new Response('Not Found', { status: 404 });
  },
};
