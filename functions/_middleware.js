/**
 * Pages Functions Middleware — Hostname-based routing
 *
 * 处理域名路由:
 *   health.chenxiuniverse.top  → /health.html
 *   history.chenxiuniverse.top → /evolution.html
 *   pimanager.chenxiuniverse.top → /pimanager/ (保留)
 *   chenxiuniverse.top (apex) → 301 www
 */
export async function onRequest(context) {
  const { request, next } = context;
  const url = new URL(request.url);
  const hostname = url.hostname;

  // history → evolution page
  if (hostname === 'history.chenxiuniverse.top') {
    return context.next('/evolution');
  }

  // health → health dashboard
  if (hostname === 'health.chenxiuniverse.top') {
    return context.next('/health');
  }

  return next();
}
