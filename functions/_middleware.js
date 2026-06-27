// Hostname-based routing middleware
const COUNTER = 'https://counter.m20081225.workers.dev';

export async function onRequest(context) {
  const { request, next } = context;
  const url = new URL(request.url);
  const hostname = url.hostname;

  // PV counter proxy (/ping public, /stats not proxied)
  if (url.pathname === '/ping') {
    return fetch(COUNTER + url.pathname + url.search);
  }
  if (url.pathname === '/stats') {
    return new Response('Not Found', { status: 404 });
  }

  // Internal report proxy: /_report_stats → counter /stats (needs Bearer)
  if (url.pathname === '/_report_stats') {
    const auth = request.headers.get('Authorization');
    if (!auth) return new Response('Not Found', { status: 404 });
    return fetch(COUNTER + '/stats' + url.search, request);
  }

  // Redirect apex to www
  if (hostname === 'chenxiuniverse.top') {
    return Response.redirect('https://www.chenxiuniverse.top' + url.pathname + url.search, 301);
  }

  // pimanager subdomain → /pimanager/ content
  if (hostname === 'pimanager.chenxiuniverse.top') {
    const newPath = '/pimanager' + url.pathname;
    return context.env.ASSETS.fetch(new URL(newPath, url.origin).toString());
  }

  return next();
}
