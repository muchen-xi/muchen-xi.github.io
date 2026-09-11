# 树莓派容灾观察者 dr-agent

契约：`monitoring/DR-OBSERVER-CONTRACT.md`（冻结版）。本目录全部文件由契约「实施边界」表归属 B 方维护。

`dr_agent.py` 是**单文件、纯标准库**（Python ≥ 3.9，无任何 pip 依赖）的常驻观察者，跑在
Pi Zero W（512MB / armv6 / 单核 1GHz）上，30 秒一个 tick：

- **slow 模式（常态）**：每 tick 对最近一次权威查询缓存的 IP 做 TCP 轻探（连接 443），
  每 `DR_FULL_PROBE_SECONDS`（默认 300s）做一次完整探测。
- **fast 模式（可疑）**：每 tick 都做完整探测。触发升频：TCP 轻探连续 2 次失败，
  或任一完整探测判不健康；**一轮判定健康即退回 slow**。
- **切换**：完整探测连续 3 次不健康 + 权威推导 `primary` + 自身网络正常 → 执行 DNS 切换
  （fast 模式下 ≈90 秒）。进程重启后回 slow、计数清零，第一轮强制完整探测重新建立判定
  （绝不拿重启前的旧证据切换）。

## 文件清单

| 文件 | 说明 |
|---|---|
| `dr_agent.py` | 观察者 / 切换执行器（纯标准库单文件） |
| `install.sh` | 一键安装（root，幂等） |
| `dr-agent.service` | systemd unit（`Restart=always` + 加固） |
| `config.env.example` | 配置模板（带去默认值的注释） |
| `README.md` | 本文档 |

## 部署步骤（树莓派）

```bash
# 1) 把 pi-agent/ 整个目录拷到树莓派（示例，在本机执行）
scp -r monitoring/pi-agent pi@<树莓派IP>:/tmp/

# 2) 树莓派上安装（交互录入阿里云 AK 与 SMTP）
ssh pi@<树莓派IP>
sudo bash /tmp/pi-agent/install.sh

# 3) 自检 + 单轮演练（先 dry-run，确认判定符合预期）
sudo /usr/bin/python3 /opt/dr-agent/dr_agent.py --selftest
sudo /usr/bin/python3 /opt/dr-agent/dr_agent.py --once --dry-run

# 4) 看服务与日志
systemctl status dr-agent
journalctl -u dr-agent -f
```

非交互安装（批量 / 自动化）：

```bash
sudo -E ALI_KEY_ID=xxx ALI_KEY_SECRET=yyy \
     SMTP_USERNAME=ops@example.com SMTP_PASSWORD=zzz REPORT_TO=you@example.com \
     bash install.sh
```

安装脚本做的事：检查 root / python3 ≥ 3.9 / curl → 建 `/opt/dr-agent` 与 `/var/lib/dr-agent`
→ 装 `/etc/dr-agent.env`（**chmod 600**，已存在则保留，除非 `--reset-config`）→ 装 unit →
`systemctl daemon-reload && enable --now`。重复执行只更新程序与 unit，**不动已有配置**。

## 配置说明（`/etc/dr-agent.env`）

见 `config.env.example`，全部键都有合理默认值，全部可被进程环境变量覆盖。要点：

| 键 | 默认 | 说明 |
|---|---|---|
| `DR_ROLE` | `switch_only` | 阶段一：只切不恢复（恢复判定只记录+告警）；`full` 才执行恢复 |
| `DR_TARGETS` | `www,starkeeper` | 参与推导与切换的目标 |
| `DR_TICK_SECONDS` / `DR_FULL_PROBE_SECONDS` / `DR_FAST_PROBE_SECONDS` | 30 / 300 / 30 | 节奏 |
| `DR_TCP_FAILS_TO_ESCALATE` | 2 | TCP 轻探连续失败升 fast（兼容旧拼写 `DR_TCP_FAILS_TO_ESCAPE`） |
| `DR_FAILS_TO_SWITCH` / `DR_STREAK_TO_RESTORE` | 3 / 3 | 切换 / 恢复阈值 |
| `DR_MIN_DWELL_SECONDS` / `DR_PEER_MAX_AGE_SECONDS` | 1800 / 1200 | 恢复闸门 |
| `DR_CLOCK_SKEW_MAX` | 300 | 超限拒绝一切 DNS 写（Pi Zero W 无 RTC） |
| `DR_ALERT_ENABLED` / `DR_DRY_RUN` | 1 / 0 | 开关 |
| `DR_SWITCH_ENABLED` | 1 | 0 = 只闸住 DNS 写：判定满足时只告警不切换（探测/告警/心跳照常，上线验证用） |
| `DR_BOARD_WRITE_SECONDS` | 300 | `_dr-pi` 心跳最小写入间隔；verdict/fails/fast/net/mode 变化时立即写 |
| `SMTP_*` / `REPORT_TO` | smtp.qiye.aliyun.com:465 | SMTP_SSL 告警 |

缺 `ALI_KEY_ID/SECRET` 时：`--selftest` 明确报 ❌，`--loop` 拒绝启动（有意保护）。

## 探测与判定（国内受众视角）

一次完整探测（`probe_full`）依次做：

1. **自身网络对照**：`223.5.5.5` 解析 `www.baidu.com` + 两个中立站点（baidu/aliyun）HTTPS +
   `vercel-test.chenxiuniverse.top`（独立备站验证域）。
   - 中立站点可达 → `net=ok`；中立站点全挂但备站验证域可达 → 仍 `net=ok`（确认是 CF 侧故障，可切）；
   - 连中立站点与备站都不通 → `net=broken` / `verdict=unknown`，**绝不切换**，只告警。
2. **权威记录 + 直连探测**：从阿里云 API 直读 A 记录（不经递归 DNS，防缓存失明），
   对每条线路的 A 记录用 `curl --resolve host:443:IP --connect-timeout 5 --max-time 8
   -A chenxiuniverse-monitor/1.0` 逐一探测；**该线路任一 IP 健康即视为该线路健康**
   （避免多记录轮换期的瞬时假故障）。CNAME 形态（starkeeper backup）走普通 HTTPS 探测。
3. **递归解析对照**：显式用 `223.5.5.5` 解析 `www.chenxiuniverse.top` 与权威记录比对。
   不一致 = 劫持/污染信号 → 告警；不一致**且递归结果不可达**（真实用户路径故障）→ 计入不健康。
   *仅在权威推导为 `primary` 时比对*，避免切换后 TTL 内的假告警。
4. **backup 语义**：若目标是 `backup`，额外用 `_dr-snap` 里的主站 IP 直连探测（恢复依据，
   与 failover-monitor workflow 行为一致）；主站仍不可达时不累积健康计数。

健康判据与仓库一致：**HTTP 2xx/3xx/4xx = 健康；`000` 或 5xx = 不健康**。
`verdict=unknown`（自身网络异常 / API 异常 / 无凭据）时**冻结计数**，不切换。

## 会签板

| 记录 | 行为 |
|---|---|
| `_dr-pi` | 心跳降频写（契约 2.1 schema，含 seq/verdict/net/mode/fast/fails/lines/temp/up，≤255B）：verdict/fails/fast/net/mode 任一变化立即写；无变化时按 `DR_BOARD_WRITE_SECONDS`（默认 300s）最小间隔写；重启后第一轮必写一次 |
| `_dr-snap` | 切换前 best-effort 写；已存在有效快照则**保留其 IP 数组**，只更新 ts/who/dir（防污染） |
| `_dr-gh` | 只读；按契约第三节 peer 语义（absent/stale/unknown/healthy/unhealthy）做恢复闸门 |

任一读写失败 → 记日志降级继续，**绝不让容灾因会签通道故障而失效**（契约第五节）。

## 测试接缝（仅供离线对抗推演，生产勿用）

`monitoring/tests/` 下有一套不依赖 pytest 的离线推演（`python monitoring/tests/test_coop_scenarios.py`）。
为支持它，`dr_agent.py` 提供两个**仅供测试**的接缝（生产路径判定逻辑不变）：

- 环境变量 `ALI_ENDPOINT`（如 `http://127.0.0.1:8899/`）覆盖阿里云 API 端点，配合
  `monitoring/tests/mock_alidns.py` 假 Alidns 服务；生产 `/etc/dr-agent.env` **不得**设置。
- `--ticks N`：同一进程内连续跑 N 轮 tick 后退出，用于累积"连续 N 次不健康"等跨轮计数
  （`--once` 语义不变，约等于 `--ticks 1`）。

## 升级方法

```bash
# 本机（仓库）→ 树莓派
scp monitoring/pi-agent/dr_agent.py pi@<树莓派IP>:/tmp/dr_agent.py
ssh pi@<树莓派IP> 'sudo install -m 0755 /tmp/dr_agent.py /opt/dr-agent/dr_agent.py && sudo systemctl restart dr-agent'
```

unit / 配置模板有变更时：重跑 `sudo bash /tmp/pi-agent/install.sh`（幂等，不动已有配置）。
升级后建议 `--selftest` + `--once --dry-run` 复核。

## 排障

```bash
systemctl status dr-agent                 # 服务状态
journalctl -u dr-agent -f                 # 实时日志（journald）
tail -f /var/lib/dr-agent/dr-agent.log    # 轮转日志（512KB × 2）
sudo /usr/bin/python3 /opt/dr-agent/dr_agent.py --status    # 上次判定 / 计数 / 时钟 / 告警
sudo /usr/bin/python3 /opt/dr-agent/dr_agent.py --selftest  # ✅⚠❌ 逐项自检
sudo /usr/bin/python3 /opt/dr-agent/dr_agent.py --once --dry-run  # 跑一轮但不写任何东西
```

常见现象：

| 现象 | 处理 |
|---|---|
| `--loop` 拒绝启动（缺 AK） | 编辑 `/etc/dr-agent.env` 填入 AK，然后 `sudo systemctl restart dr-agent` |
| 日志 `时钟偏差超限` | Pi 无 RTC，网络恢复后等一次校时（每 30 分钟）；仍异常则检查系统时间同步（`timedatectl`） |
| 日志 `会签板写入失败` | 不影响切换；检查 AK 权限（需 Alidns 读写）与 API 配额 |
| `verdict=unknown` 持续 | 看 `--status` 的 `last_detail`：自身网络异常 or API 异常（两者都不切换） |
| 站点被自己 WAF 403 | 确认探测 UA 为 `chenxiuniverse-monitor/1.0`（脚本已内置；改站点 middleware 时勿拉黑该 UA） |
| 想临时停掉自动切换 | `sudo systemctl stop dr-agent`（回滚章节）；或把 `DR_DRY_RUN=1` 后 restart |

## 手动恢复手册（阶段一 / 闸门拦下时）

阶段一（`DR_ROLE=switch_only`）**不会自动恢复**；恢复条件满足时只发告警。人工恢复步骤：

1. **看快照**（恢复目标 = 主站原始 IP，不是备站 IP）：

   ```bash
   python3 monitoring/scripts/dr_board.py get _dr-snap
   # 或阿里云 DNS 控制台 → 解析设置 → 查看 _dr-snap TXT
   ```

2. **核对主站 IP 可达性**（可选但推荐）：对快照里的每个 IP 执行

   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' -A chenxiuniverse-monitor/1.0 \
        --resolve www.chenxiuniverse.top:443:<IP> https://www.chenxiuniverse.top/
   ```

   只把返回 2xx/3xx/4xx 的 IP 写回。

3. **改回 DNS**（二选一）：

   - 控制台：`www` 的 default 与 oversea 线路 A 记录改为快照 IP；`starkeeper` 的 default
     删除 CNAME，添加 A 记录 = 快照 `starkeeper` IP；
   - 命令：`python3 monitoring/scripts/failover-dns.py restore`（需 `ALI_KEY_ID/SECRET`，
     脚本自带状态文件回退与逐 IP 校验）。

4. **确认并让观察者收敛**：`sudo systemctl restart dr-agent`（重启即重置状态机，
   第一轮完整探测确认 `mode=primary`）。

> 阶段二（`DR_ROLE=full`）会自动恢复：连续健康 ≥ `DR_STREAK_TO_RESTORE`、`mode=backup`
> （或 mixed 且主站 IP 可达）、peer(`_dr-gh`) ≠ unhealthy、距 `_dr-snap.ts` ≥
> `DR_MIN_DWELL_SECONDS`，且恢复前逐 IP 可达校验通过。

## 回滚

```bash
sudo systemctl stop dr-agent        # 立即停止观察与自动切换（DNS 维持现状，不自动改回）
sudo systemctl disable dr-agent     # 取消开机自启（彻底回滚）
# 如需恢复到主站，按上面「手动恢复手册」执行
```

回滚后如需清理：`sudo rm -rf /opt/dr-agent /var/lib/dr-agent /etc/dr-agent.env
/etc/systemd/system/dr-agent.service && sudo systemctl daemon-reload`。

## 设计备注

- **UA 是硬要求**：站点 middleware 会把 `curl`/`python-urllib` 默认 UA 403 掉，
  高频探测会被自己的 WAF 误判为不健康 → 误切。脚本对所有本站探测统一带
  `User-Agent: chenxiuniverse-monitor/1.0`。
- **curl 优先、stdlib 兜底**：curl 不存在时用 `ssl` + `socket` 手写 HTTPS 请求
  （证书校验失败时降级为不校验并记警告，保证仍能取到 HTTP 码）。
- **SD 卡保护**：`state.json` 只在内容变化时写（临时文件 + rename 原子替换），
  日志单文件 512KB × 2 份轮转。
- **切换动作**：`www` default/oversea A → `76.76.21.21`（Vercel，幂等收敛）；
  `starkeeper` default 先删光 A 再加 `CNAME starkeeper-bpw.pages.dev`。
  已处于 `backup/mixed/empty` 的目标拒绝覆盖并告警（幂等保护，不写快照）。
