// /ping → counter Worker 代理 (仅此路径触发 Function, 静态资源不经过)
const COUNTER = 'https://counter.m20081225.workers.dev';

export async function onRequest(context) {
  const { request } = context;
  const url = new URL(request.url);
  const target = new URL('/ping' + url.search, COUNTER);
  return fetch(target, request);
}
