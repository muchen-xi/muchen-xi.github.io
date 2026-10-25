// Pages Function: GET /api/hot —— 读 KV 里的本周热点
export async function onRequestGet({ env }) {
  const headers = {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'public, max-age=300',
    'access-control-allow-origin': '*',
  };
  try {
    const raw = await env.HOT_DATA.get('hot:latest');
    if (!raw) {
      return new Response(JSON.stringify({ items: [], sources: [], updated_at: null }), { headers });
    }
    return new Response(raw, { headers });
  } catch (e) {
    return new Response(JSON.stringify({ error: 'kv_unavailable' }), { status: 503, headers });
  }
}
