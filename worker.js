/**
 * chenxiuniverse.top 容灾网关
 * 主: GitHub Pages (muchen-xi.github.io)
 * 备: CF Pages (chenxiuniverse-top.pages.dev)
 */

const PRIMARY = 'https://muchen-xi.github.io';
const SECONDARY = 'https://chenxiuniverse-top.pages.dev';

// 简单请求级容灾: 先试主站，挂了切备站
export default {
  async fetch(request) {
    const url = new URL(request.url);
    const path = url.pathname + url.search;

    // 健康检查端点
    if (path === '/__health') {
      const primaryOk = await check(PRIMARY);
      const secondaryOk = await check(SECONDARY);
      return new Response(JSON.stringify({
        primary: primaryOk ? 'ok' : 'down',
        secondary: secondaryOk ? 'ok' : 'down',
        active: primaryOk ? 'primary' : 'secondary',
      }), {
        headers: { 'Content-Type': 'application/json' },
      });
    }

    // 正常请求: 主站优先
    const resp = await tryFetch(PRIMARY + path, request);
    if (resp) return resp;

    // 主站挂了，切备站
    console.log(`主站不可用，切到备站: ${path}`);
    const fallback = await tryFetch(SECONDARY + path, request);
    if (fallback) return fallback;

    // 两个都挂了
    return new Response('暂时不可用，请稍后再试。Both backends are down.', { status: 503 });
  },
};

async function check(origin) {
  try {
    const r = await fetch(origin, { method: 'HEAD' });
    return r.ok;
  } catch {
    return false;
  }
}

async function tryFetch(url, request) {
  try {
    const resp = await fetch(url, {
      method: request.method,
      headers: request.headers,
      body: request.method === 'POST' ? request.body : undefined,
      redirect: 'follow',
    });
    if (resp.status < 500) return resp;
  } catch (e) {
    // 失败归 null
  }
  return null;
}
