// Hostname-based routing for Pages project
// 2026-06-26: 添加 /ping /stats 代理 → counter Worker (KV 访问计数)
const COUNTER = 'https://counter.m20081225.workers.dev';

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const hostname = url.hostname;

    // ── PV 计数器代理 (/ping 公开, /stats 不代理) ──
    if (url.pathname === '/ping') {
      const target = new URL(url.pathname + url.search, COUNTER);
      return fetch(target, request);
    }
    if (url.pathname === '/stats') {
      return new Response('Not Found', { status: 404 });
    }

    // Redirect apex to www
    if (hostname === 'chenxiuniverse.top') {
      const www = new URL(url.pathname + url.search, 'https://www.chenxiuniverse.top');
      return Response.redirect(www, 301);
    }

    // pimanager subdomain → /pimanager/ content
    if (hostname === 'pimanager.chenxiuniverse.top') {
      const newPath = '/pimanager' + url.pathname;
      return env.ASSETS.fetch(new Request(new URL(newPath, url.origin), request));
    }

    // Default: serve from root (www, 248200.xyz, cf-test, etc.)
    return env.ASSETS.fetch(request);
  },
};
