# ECS 机房节点（乌兰察布 · 39.101.122.237）

2026-10-25 上线。**优选 IP 的主节点**：原先由 GitHub Actions 每 6 小时跑一次，
现在机房节点每 3 小时跑一轮，GitHub 流水线降级为"境外线路维护 + 兜底"。

## 部署位置（服务器）

| 内容 | 路径 |
|---|---|
| 探针（站点探测 + 会签板交叉核对） | `/opt/probe/probe.py`，`probe.timer` 每 5 分钟 |
| 优选 IP 主节点 | `/opt/ipopt/ipopt_ecs.py` + `alidns_rpc.py`，`ipopt.timer` 每 3 小时（00/3/6/…:20） |
| 配置 | `/opt/ipopt/env`（600）：`MODE=apply|publish`、`N_A=5` |
| CloudflareST | `/opt/cfst/CloudflareST`（v2.2.5，与 GitHub 侧同版本） |
| 日志 | `/var/log/probe.log`、`/var/log/ipopt.log` |

## 数据流

```
GitHub Actions（每3h）
  ├─ 境外测速 CloudflareST（US runner）
  ├─ 境内 ITDog（Playwright）
  ├─ 发布 _ip-opt-gh TXT  ← 机房实时读取，作为 ITDog 视角
  └─ 只在机房节点离线/非 apply 时维护 default 线路（服务降级，永不缺位）

ECS 机房节点（每3h）
  ├─ CloudflareST 本地扫描（机房自身环境）
  ├─ 读 _ip-opt-gh（ITDog 结果）
  ├─ 合并打分 0.65·机房延迟 + 0.35·ITDog 延迟
  ├─ 健康探测（HTTPS + 浏览器 UA，防 CF 403 误杀）
  ├─ 写 default 线路 A 记录 ×5（www/health/history/pimanager/starkeeper）
  └─ 发布 _ip-opt-ecs TXT
```

## 灰度开关

`/opt/ipopt/env` 里 `MODE=publish` 只观测（写会签记录，不碰 A 记录），
`MODE=apply` 才真正维护 DNS。GitHub 侧只有看到 `mode=apply` 且记录新鲜（4h 内）
才会让出 default 线路。

## 容灾守卫

- `www` default 线路若全属备站 IP 集（Vercel / GH Pages）→ 本轮不动 DNS；
- `starkeeper` 若是 CNAME → 跳过该子域；
- 任何一次写失败只告警，不阻断下一轮。
