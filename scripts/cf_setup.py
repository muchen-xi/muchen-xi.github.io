"""
CF Dashboard 自动化脚本 — 移除 Worker 路由 + 绑定 KV + 部署 Pages

由 GitHub Actions workflow cf-auto-setup.yml 调用。
需要 CF_API_TOKEN 环境变量。
"""
import json
import os
import subprocess
import sys
import time

CF_ACCOUNT = "20a34acd60bfbc3705c9eb330428480a"
ZONE_NAME = "chenxiuniverse.top"
PAGES_PROJECT = "248200-xyz"
KV_NAMESPACE_ID = "132f0237e83845caa4325effad690cee"
KV_BINDING = "SITE_ANALYTICS"

TOKEN = os.environ["CF_API_TOKEN"]


def cf(method, path, data=None):
    """Call Cloudflare API."""
    url = f"https://api.cloudflare.com/client/v4/{path}"
    cmd = ["curl", "-s", "-X", method, url,
           "-H", f"Authorization: Bearer {TOKEN}",
           "-H", "Content-Type: application/json"]
    if data:
        cmd += ["-d", json.dumps(data)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    try:
        resp = json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"  API returned non-JSON: {result.stdout[:200]}", file=sys.stderr)
        return {"success": False, "result": None, "errors": [{"message": "non-JSON response"}]}
    if not resp.get("success"):
        errors = resp.get('errors', [])
        msgs = [e.get('message', str(e)) for e in errors]
        print(f"  API ERROR: {msgs}", file=sys.stderr)
    return resp


# ─── Step 1: Get Zone ID ───
print("=" * 50)
print("Step 1: Get Zone ID")
resp = cf("GET", f"zones?name={ZONE_NAME}")
zone_id = resp["result"][0]["id"]
print(f"  Zone ID: {zone_id}")

# ─── Step 2: List + Delete Worker Routes ───
print("=" * 50)
print("Step 2: List + Delete Worker Routes")
resp = cf("GET", f"zones/{zone_id}/workers/routes")
routes = resp.get("result") or []
print(f"  Found {len(routes)} route(s):")
deleted = 0
for r in routes:
    pattern = r.get("pattern", "")
    rid = r.get("id", "")
    print(f"    {pattern} (id={rid})")
    if "chenxiuniverse.top" in pattern:
        print(f"    → Deleting...")
        d = cf("DELETE", f"zones/{zone_id}/workers/routes/{rid}")
        if d.get("success"):
            print(f"    → DELETED")
            deleted += 1
if deleted == 0:
    print("  No chenxiuniverse.top routes to delete (may already be clean)")

# ─── Step 3: Bind KV to Pages ───
print("=" * 50)
print("Step 3: Bind KV namespace to Pages")

# First check current config
resp = cf("GET", f"accounts/{CF_ACCOUNT}/pages/projects/{PAGES_PROJECT}")
p = resp.get("result")
if not p:
    print("  FAILED: Cannot access Pages project (token may lack Pages:Read permission)")
    print(f"  API response: {json.dumps(resp, indent=2)[:500]}")
    sys.exit(1)
print(f"  Project: {p['name']}")
existing = p.get("deployment_configs", {}).get("production", {}).get("kv_namespaces", {})
print(f"  Existing KV bindings: {existing}")

# Patch with SITE_ANALYTICS
body = {
    "deployment_configs": {
        "production": {
            "kv_namespaces": {
                KV_BINDING: KV_NAMESPACE_ID
            }
        }
    }
}
resp = cf("PATCH", f"accounts/{CF_ACCOUNT}/pages/projects/{PAGES_PROJECT}", data=body)
if resp.get("success"):
    print(f"  KV binding configured: {KV_BINDING} -> {KV_NAMESPACE_ID}")
else:
    print(f"  FAILED: {resp.get('errors')}")
    sys.exit(1)

# ─── Step 4: Deploy with Wrangler ───
print("=" * 50)
print("Step 4: Deploy Pages with Wrangler")
result = subprocess.run(
    ["npx", "wrangler", "pages", "deploy", ".", "--project-name", PAGES_PROJECT,
     "--branch", "main", "--commit-dirty", "true"],
    capture_output=True, text=True, cwd=os.environ.get("GITHUB_WORKSPACE", "."))
print(result.stdout[-2000:] if len(result.stdout) > 2000 else result.stdout)
if result.returncode != 0:
    print(result.stderr[-1000:] if len(result.stderr) > 1000 else result.stderr)
    print("  WARNING: wrangler deploy returned non-zero (may be ok if no changes)")

# ─── Step 5: Wait + Verify ───
print("=" * 50)
print("Step 5: Wait 30s then verify")
time.sleep(30)

tests = [
    ("POST /ping", ["curl", "-s", "-w", "\nHTTP %{http_code}", "-X", "POST",
     "https://www.chenxiuniverse.top/ping?site=cf-setup-test"]),
    ("GET /_report_stats", ["curl", "-s", "-H",
     "Authorization: Bearer 207ddbc0c5376668adb2f5c225fae18ed0859c3cf86865ab",
     "https://www.chenxiuniverse.top/_report_stats?days=1&sites=www"]),
    ("Main site", ["curl", "-s", "-o", "/dev/null", "-w", "HTTP %{http_code}",
     "https://www.chenxiuniverse.top/"]),
]

for name, cmd in tests:
    r = subprocess.run(cmd, capture_output=True, text=True)
    print(f"\n--- {name} ---")
    print(r.stdout)

print("\n" + "=" * 50)
print("CF automation complete!")
