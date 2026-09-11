# 树莓派观察者 · 上线推进手册

> 目标：让树莓派成为容灾系统的**第二个观察者**（国内视角），并能独立完成切备。
> 分四档推进，每一档都有明确的通过标准。**任何一档出问题，`sudo systemctl stop dr-agent` 即可完全退出**。

---

## 0. 前置：两样东西

| 需要 | 从哪来 |
|---|---|
| 阿里云 AK（独立子账号） | 阿里云控制台新建 RAM 用户 `dr-pi`，只给云解析权限（见下方策略）。**不要用主账号 AK，也不要用 GitHub 那套** |
| SMTP 账号 | 与 `report_mailer.py` 同一套（GitHub Secrets 里的 `SMTP_USERNAME` / `SMTP_PASSWORD` / `REPORT_TO`） |

RAM 自定义策略（`DRAgentDNS`）：

```json
{
  "Version": "1",
  "Statement": [
    {"Effect": "Allow",
     "Action": ["alidns:DescribeDomainRecords", "alidns:DescribeDomainRecordInfo"],
     "Resource": ["*"]},
    {"Effect": "Allow",
     "Action": ["alidns:AddDomainRecord", "alidns:UpdateDomainRecord", "alidns:DeleteDomainRecord"],
     "Resource": ["acs:alidns:*:*:domain/chenxiuniverse.top"]}
  ]
}
```

> 若保存第二条时提示「不支持资源级授权」，把它的 `Resource` 改成 `["*"]`（退化为纯 DNS 子账号，与现有 `cf-dns-auto` 同级）。

---

## 1. 装机

```bash
# ① 用 PiManager 文件管理把 pi-agent/ 里的 5 个文件传到 Pi，例如 ~/dr-agent/
# ② 在 PiManager 终端执行：
cd ~/dr-agent && sudo bash install.sh
#    按提示粘贴 AK 与 SMTP（SMTP 那三项与 GitHub Secrets 一致）

# ③ 自检（缺什么会明确说）
sudo /usr/bin/python3 /opt/dr-agent/dr_agent.py --selftest
```

自检要求：**除「配置文件路径」外全部 ✅**。特别注意时钟项——Pi Zero W 没有 RTC，偏差 >300s 时 agent 会拒绝一切 DNS 写入。

> 配置文件在 `/etc/dr-agent.env`（600 权限）。改完统一用 `sudo systemctl restart dr-agent` 生效。

---

## 2. 四档推进

### 档 A · 观察（不改 DNS、不发邮件、不写 TXT）

```bash
sudo /usr/bin/python3 /opt/dr-agent/dr_agent.py --once --dry-run
```

**通过标准**：输出里 `verdict=healthy`、`mode=primary`、`lines` 里 www 与 starkeeper 的 HTTP 码都是 200（或 3xx）。
与本机浏览器访问 `https://www.chenxiuniverse.top` 一致即可。

### 档 B · 只告警（写 TXT 心跳 + 发邮件，**不碰 DNS**）

```bash
sudo sed -i 's/^DR_DRY_RUN=.*/DR_DRY_RUN=0/; s/^DR_SWITCH_ENABLED=.*/DR_SWITCH_ENABLED=0/' /etc/dr-agent.env
grep -q '^DR_SWITCH_ENABLED' /etc/dr-agent.env || echo 'DR_SWITCH_ENABLED=0' | sudo tee -a /etc/dr-agent.env
sudo systemctl restart dr-agent && sudo systemctl status dr-agent --no-pager
sudo journalctl -u dr-agent -n 30 --no-pager
```

**通过标准**（跑满 24 小时）：
1. `_dr-pi` TXT 记录出现在阿里云云解析控制台，且内容里的 `ts` 在持续更新
2. 每天 08:00 收到心跳邮件（温度 / 判定 / 线路码）
3. 日志里没有 ❌；判定与云监控一致（云监控每 5 分钟一轮，可在 GitHub Actions 里对比）

> 这一档验的是**凭据、签名、TXT 读写、SMTP 全链路**，零 DNS 风险。

### 档 C · 阶段一：可切备、不自动恢复

```bash
sudo sed -i 's/^DR_SWITCH_ENABLED=.*/DR_SWITCH_ENABLED=1/' /etc/dr-agent.env
sudo systemctl restart dr-agent
```

此时树莓派具备**独立切备能力**：两条线路连续 3 次完整探测不健康（≈90 秒）即把 www 切到 Vercel 备站、starkeeper 切到官方 CNAME，并写 `_dr-snap` 快照。
恢复仍由云监控执行，但云监控的恢复会被"树莓派说不健康"拦下——这正是防止境外视角把国内故障期间做的切换撤销的机制。

**通过标准**：跑一次受控演练（黑洞注入 → 观察是否切 → 恢复），见第 3 节。

### 档 D · 阶段二：对等自治（本次不上，跑稳后再开）

```bash
sudo sed -i 's/^DR_ROLE=.*/DR_ROLE=full/' /etc/dr-agent.env
sudo systemctl restart dr-agent
```

恢复规则：连续 3 次健康 + 对方（云监控）未说不健康 + 距上次切换 ≥30 分钟 + 逐 IP 可达校验。

---

## 3. 演练（进入档 C 后做）

```bash
# 演练前：记录当前 DNS
#   阿里云控制台 → 云解析 → www 的 A 记录（default / oversea 各 3 条）
# 注入：把 www 两条线路的 A 记录全改成 203.0.113.1（RFC5737，全球不可路由）
# 观察：
sudo journalctl -u dr-agent -f          # 应看到 TCP 轻探失败 → 升 fast → 连续 3 次不健康 → 切换
# 验证：
#   · 阿里云控制台 www 两条线路变成 76.76.21.21
#   · 浏览器访问 https://www.chenxiuniverse.top 正常（Vercel 备站）
#   · 收到切换告警邮件
# 恢复：在云监控 workflow 里手动 dispatch（或等主站 IP 恢复后由云监控自动恢复）
```

---

## 4. 排障速查

| 现象 | 先看这里 |
|---|---|
| 服务起不来 | `sudo journalctl -u dr-agent -n 50 --no-pager`；`--selftest` 会指出缺哪项 |
| 判定与浏览器不一致 | 看 `lines` 里的 HTTP 码；`000` = 连不上（检查家宽 / DNS 223.5.5.5 是否可达） |
| 心跳邮件没到 | `grep SMTP /etc/dr-agent.env`；`--selftest` 的 SMTP 项；企业邮箱是否开启 SMTP |
| 一直 `verdict=unknown` | `net` 字段：`broken` = 自身网络异常（agent 故意不切换，避免误判） |
| 时钟告警反复出现 | `sudo apt install -y chrony && sudo systemctl restart chrony` |
| 想临时停掉 | `sudo systemctl stop dr-agent`（退出容灾链路，DNS 无残留状态） |

日志：`/var/lib/dr-agent/dr-agent.log`（512KB 自动轮转，最多 2 份）
状态：`/var/lib/dr-agent/state.json`（仅变化时写盘，保护 SD 卡）

---

## 5. 回滚

```bash
sudo systemctl disable --now dr-agent     # 停服 + 取消开机自启
# 完全移除：sudo rm -rf /opt/dr-agent /var/lib/dr-agent /etc/dr-agent.env /etc/systemd/system/dr-agent.service
```

DNS 侧无残留：agent 只在故障时改 www / starkeeper 记录，`_dr-*` 三个 TXT 记录留着不影响任何服务（可作为下次上线的会签通道）。
