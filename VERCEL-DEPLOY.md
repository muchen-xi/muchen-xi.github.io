# Vercel 备用站部署备忘

## 背景

- Vercel 为本站**备用站**（替换 GitHub Pages 成为容灾目标），主站仍为 Cloudflare Pages。
- 容灾由 `monitoring/scripts/failover-dns.py` 驱动：主站宕机时把 `www` 与 `health`（从属目标，跟随 www 一起切）的 A 记录切到 Vercel anycast IP `76.76.21.21`，恢复时切回保存的 CF 优选 IP。
- 根目录 `vercel.json` 已就位：安全头、缓存策略与 CF Pages 的 `_headers` 对齐，敏感路径（functions/、worker/、monitoring/ 等）统一返回 404。

## 部署方式 A：Vercel 控制台 Git 集成

1. Vercel Dashboard → Add New → Project → Import 仓库 `muchen-xi/muchen-xi.github.io`。
2. 纯静态站点，零构建：Framework Preset 选 Other（或留空），无需 Build Command / Output Directory 设置。
3. 直接 Deploy 即可，根目录 `vercel.json` 自动生效。

## 部署方式 B：Vercel CLI

```bash
npm i -g vercel
vercel login
vercel --prod   # 在仓库根目录执行
```

`vercel.json` 在 CLI 部署下同样生效。

## 域名绑定

1. 项目 Settings → Domains → 添加 `www.chenxiuniverse.top`。
2. **同时添加 `health.chenxiuniverse.top`**（2026-10-04 起 health 跟随 www 一起切到 Vercel；
   未绑定则该域名切过来时 TLS 无证书 = HTTP 000，是切换生效的**硬前置**）。
3. 按 Vercel 提示在阿里云 DNS 添加验证记录（两种方式任选其一）：
   - **TXT 验证**：添加 `_vercel` 的 TXT 记录，值为 Vercel 提供的验证串。
   - **CNAME 验证**：添加 `www` 的 CNAME 记录指向 `cname.vercel-dns.com`（以 Vercel 控制台提示为准）。
4. 验证通过后 Vercel 自动签发 TLS 证书。
5. 若证书未及时签发：首次 DNS 切换（failover）后会自动补签，短时 TLS 延迟可接受。
6. health 在 Vercel 上的路由由 `vercel.json` redirects 首条提供（host = health 域的 `/` → `/health`，307；
   用 redirect 而非 rewrite：rewrite 是 afterFiles 语义，`/` 会先被 index.html 命中）。
   绑定后实测：`curl -skI --resolve health.chenxiuniverse.top:443:76.76.21.21 https://health.chenxiuniverse.top/` 期望 `307` + `location: /health`。

## 容灾说明

- 主站宕机 → `failover-monitor.yml` 连续 3 次检测失败 → 自动执行 `backup` → `www` 与 `health` A 记录一起切到 `76.76.21.21`（Vercel 备用站）。
- 主站恢复（连续 3 次健康）→ 自动 `restore` 切回保存的 CF 优选 IP（health 复用 www 同线路已验证 IP）。
- 手动命令（需要环境变量 `ALI_KEY_ID` / `ALI_KEY_SECRET`）：

```bash
python3 monitoring/scripts/failover-dns.py status
python3 monitoring/scripts/failover-dns.py backup --dry-run
python3 monitoring/scripts/failover-dns.py backup
python3 monitoring/scripts/failover-dns.py restore --dry-run
python3 monitoring/scripts/failover-dns.py restore
```

## 已知降级（备用站预期行为）

以下依赖 CF Pages Functions / Worker 的能力在 Vercel 上不生效，页面静默降级：

- `/ping` 计数
- `/_report_stats`（health 面板的 PV 卡片会显示"数据不可用"，属预期）
- UA 黑名单
- 子域路由：`health.` 的根路径由 `vercel.json` 的 host 限定 redirect 提供（`/` → `/health`，307）；
  `pimanager.` / `history.` 在 Vercel 上仍不可用
- PV 计数仍打到主站绝对 URL（不受影响）
- health 面板 footer 文案 "Powered by Cloudflare Pages" 在备站上不准确（纯文案，不影响功能）

## 验证清单

部署后检查：

- 首页 / `health` / `evolution` / `topic` / `pimanager` 页面可访问。
- `curl -I https://www.chenxiuniverse.top/` 检查安全头（X-Frame-Options、X-Content-Type-Options、Referrer-Policy、Strict-Transport-Security、Permissions-Policy）。
- `curl -I` 检查缓存头（HTML 短缓存、图片/音频 immutable、CSS/JS 86400）。
- `curl -I` 检查敏感路径（`/worker.js`、`/_headers`、`/wrangler.toml` 等）返回 404。
