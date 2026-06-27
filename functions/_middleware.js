// Hostname-based routing middleware
const COUNTER = 'https://counter.m20081225.workers.dev';

export async function onRequest(context) {
  const { request, next } = context;
  const url = new URL(request.url);
  const hostname = url.hostname;

  // DIAGNOSTIC: test path on history/health
  if (url.pathname === '/_diag') {
    return new Response(`hostname=${hostname}`, {
      headers: { 'Content-Type': 'text/plain' },
    });
  }

  // PV counter proxy
  if (url.pathname === '/ping') {
    return fetch(COUNTER + url.pathname + url.search);
  }
  if (url.pathname === '/stats') {
    return new Response('Not Found', { status: 404 });
  }

  // Internal report proxy
  if (url.pathname === '/_report_stats') {
    const auth = request.headers.get('Authorization');
    if (!auth) return new Response('Not Found', { status: 404 });
    return fetch(COUNTER + '/stats' + url.search, request);
  }

  // Redirect apex to www
  if (hostname === 'chenxiuniverse.top') {
    return Response.redirect('https://www.chenxiuniverse.top' + url.pathname + url.search, 301);
  }

  // history subdomain → evolution page
  if (hostname === 'history.chenxiuniverse.top') {
    const newUrl = new URL('/evolution.html', url.origin);
    return context.env.ASSETS.fetch(newUrl.toString());
  }

  // health subdomain → health page
  if (hostname === 'health.chenxiuniverse.top') {
    const newUrl = new URL('/health.html', url.origin);
    return context.env.ASSETS.fetch(newUrl.toString());
  }

  // pimanager subdomain → /pimanager/ content
  if (hostname === 'pimanager.chenxiuniverse.top') {
    return context.env.ASSETS.fetch(new URL('/pimanager' + url.pathname, url.origin).toString());
  }

  return next();
}
