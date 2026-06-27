// /_report_stats → counter /stats 代理 (需 Bearer 认证)
const COUNTER = 'https://counter.m20081225.workers.dev';

export async function onRequest(context) {
  const { request } = context;
  const url = new URL(request.url);

  // 无认证 → 404 (不暴露端点存在)
  const auth = request.headers.get('Authorization');
  if (!auth) {
    return new Response('Not Found', { status: 404 });
  }

  const target = new URL('/stats' + url.search, COUNTER);
  return fetch(target, request);
}
