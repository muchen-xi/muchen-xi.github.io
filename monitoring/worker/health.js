/**
 * ⚠️ 已废弃 — 旧健康检查 Worker，已被客户端直连检查 + health_check.py 取代，请勿部署/使用。
 *
 * 历史: 2026-06 时代部署于 health.m20081225.workers.dev / health.248200.xyz (Account B)。
 * 2026-06-27 压测打爆配额引发 P0 后解构 worker 依赖 →
 *   CF IP 可用性验证改由本仓库 monitoring/scripts/health_check.py 用 curl --resolve
 *   直连 www.chenxiuniverse.top 承担；健康面板由 personal-site/health.html 客户端直连。
 *
 * 部署在 CF 上的同名 Worker 至今未删（残留，见 fix-report-2026-08-01 待办），
 * 但当前链路完全不经过它。本文件仅作历史存档保留。
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
