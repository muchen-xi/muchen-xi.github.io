/**
 * ⚠️ 已废弃 — 旧架构的"容灾网关"，已不在任何请求链路中，请勿使用或误以为它还在工作。
 *
 * 历史: 2026-06 境外加速时期，在 Account A (3c5c9230...) 部署为 cx-failover worker，
 *       workers.dev 地址: cx-failover.chenxi20081128.workers.dev。
 * 现状 (2026-08-01): Account A 境外加速已停用。
 *   - 路由由 Pages Functions `_middleware.js` 承担
 *     (chenxiuniverse.top 跳转 / pimanager / history / health 子域)。
 *   - 容灾由 cf-ip-optimizer 的 `failover-monitor.yml`（阿里云 DNS 切换至 GitHub Pages 备站）承担。
 *   - 本 worker 未绑定任何生效域名，不在 DNS 链路上，收不到流量。
 * 已知 bug: tryFetch 原样转发客户端 Host 头给 muchen-xi.github.io，GitHub Pages 按 Host
 *         虚拟主机返回 404，备站 fallback 永不触发。
 * 待办: 确认无流量依赖后删除本文件 + `worker/` 目录，并从 CF Dashboard 删除
 *       cx-failover / counter / health worker（详见 fix-report-2026-08-01.md 遗留待办）。
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
