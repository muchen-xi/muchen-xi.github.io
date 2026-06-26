/**
 * 248200.xyz Health Check Worker
 *
 * 验证 CF IP 是否正常回源。
 * 返回请求来源的 CF colo 和连接 IP，帮助判断 IP 路由质量。
 *
 * 用法:
 *   curl https://health.248200.xyz
 *   → {"status":"ok","colo":"HKG","ip":"162.159.39.168","upstream":"ok"}
 *
 * 部署 (Account B: 20a34acd...):
 *   npx wrangler deploy worker/health.js --name health --compatibility-date 2025-01-01
 *   然后在 CF Dashboard → 248200.xyz → Workers Routes → 添加 health.248200.xyz/* → health
 */

const ORIGIN = 'https://chenxiuniverse-top.pages.dev';

export default {
  async fetch(request) {
    const cf = request.cf || {};

    // 只响应 GET/HEAD
    if (request.method !== 'GET' && request.method !== 'HEAD') {
      return new Response('Method Not Allowed', { status: 405 });
    }

    // 验证回源可达（用根路径，Pages 项目一定有 index.html）
    let upstream = 'unknown';
    try {
      const r = await fetch(ORIGIN, { method: 'HEAD' });
      upstream = r.ok ? 'ok' : `http_${r.status}`;
    } catch (e) {
      upstream = `error: ${e.message}`;
    }

    const healthy = upstream === 'ok';

    return new Response(JSON.stringify({
      status: healthy ? 'ok' : 'degraded',
      colo: cf.colo || 'unknown',
      ip: request.headers.get('CF-Connecting-IP') || 'unknown',
      asn: cf.asn || 0,
      country: cf.country || 'XX',
      upstream,
    }), {
      status: healthy ? 200 : 502,
      headers: {
        'Content-Type': 'application/json',
        'Access-Control-Allow-Origin': '*',
        'Cache-Control': 'no-store',
      },
    });
  },
};
