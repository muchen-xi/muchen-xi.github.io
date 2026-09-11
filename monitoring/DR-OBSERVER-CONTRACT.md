# 容灾观察者会签板契约 v1

> 冻结于 2026-09-11。两个独立观察者（`gh` = GitHub Actions 云监控，`pi` = 树莓派）在**互不通信、无共享文件**的前提下保持一致。所有实现（`dr_board.py`、`dr_agent.py`、两个 workflow、加固脚本）必须严格遵守本契约。

## 一、事实来源：权威 A 记录

任何一方判断"现在处于什么状态"，一律**从阿里云 API 直读权威记录推导**，不读对方的状态、不读 git 文件、不依赖递归 DNS 缓存。

| 目标 | backup 判据 | primary 判据 | 其它 |
|---|---|---|---|
| `www` | default 与 oversea 两条线路的 A 记录**全部**属于备站 IP 集 | 两条线路 A 记录**都不**属于备站 IP 集 | 部分属于 = `mixed`；无记录 = `empty` |
| `starkeeper` | default 线路存在 **CNAME** 记录 | default 线路存在 **A** 记录 | A 与 CNAME 并存 = `mixed`；无记录 = `empty` |

备站 IP 集（与 `failover-dns.py` 保持一致）：
```
VERCEL_IPS   = ["76.76.21.21"]
GH_PAGES_IPS = ["185.199.108.153", "185.199.109.153", "185.199.110.153", "185.199.111.153"]
BACKUP_SET   = VERCEL_IPS ∪ GH_PAGES_IPS
```

## 二、TXT 会签板

三个记录，子域固定，**默认线路**，TTL 600，值 ≤255 字节（超长要裁剪，见下）：

| 记录 | 唯一写入方 | 用途 |
|---|---|---|
| `_dr-pi.chenxiuniverse.top` | 树莓派 | 心跳 + 判定（覆盖 www 与 starkeeper） |
| `_dr-gh.chenxiuniverse.top` | **仅** `failover-monitor.yml` | 云监控心跳 + 判定（www 视角） |
| `_dr-snap.chenxiuniverse.top` | **谁切换谁写** | 切换前快照 + 最小驻留时间依据 |

> ⚠️ 单一写入方原则：`_dr-gh` 只有 `failover-monitor.yml` 写；`starkeeper-monitor.yml` **只读不写**
> （避免两个 workflow 互相覆盖）。树莓派的判定是唯一覆盖全部目标的观察者，靠 `lines` 的 key 前缀区分目标。

### 2.1 观察者记录 schema（`_dr-pi` / `_dr-gh`）

单行紧凑 JSON，字段名不可改：

```json
{"v":1,"ts":"2026-09-11T10:00:00Z","who":"pi","seq":42,"verdict":"healthy","net":"ok","mode":"primary","fast":0,"fails":0,"lines":{"www.default":"200","www.oversea":"200","starkeeper":"200"},"temp":45.2,"up":3600}
```

| 字段 | 取值 | 说明 |
|---|---|---|
| `v` | `1` | 契约版本 |
| `ts` | UTC ISO8601 秒精度 | 新鲜度依据；格式 `%Y-%m-%dT%H:%M:%SZ` |
| `who` | `pi` / `gh` | 写入方身份 |
| `seq` | 整数，每轮 +1 | 判断卡死（同一 seq 长时间不变） |
| `verdict` | `healthy` / `unhealthy` / `unknown` | 站点判定；`unknown` = 自身网络异常无法判定 |
| `net` | `ok` / `broken` | 观察者自身出口网络状态 |
| `mode` | `primary` / `backup` / `mixed` / `empty` | 写入该条时**观察到的**权威状态 |
| `fast` | `0` / `1` | 是否处于快通道（可疑升频）模式 |
| `fails` | 整数 | 当前连续不健康计数 |
| `lines` | 对象 | **始终表示"主站路径"**的探测码（HTTP 码字符串；`000` = 不可达）。primary 语义下 = 权威记录直连探测码；backup/mixed 语义下 = `_dr-snap` 主站 IP 的直连探测码（恢复判定路径）。**规范 key**：`www.default`、`www.oversea`、`starkeeper`；快照无对应主站 IP 时该 key 省略（宁缺毋滥，绝不用备站入口码冒充主站） |
| `live` | 对象，可选 | backup/mixed 语义下的**当前入口**（备站 Vercel / CNAME）探测码，key 同 `lines`；仅展示，**不参与 `peer --target` 闸门判定**。primary 语义不写。超长裁剪时优先删除（先删 `starkeeper` 键） |
| `temp` | 数字，可选 | 树莓派温度（℃） |
| `up` | 整数，可选 | 进程连续运行秒数 |

**两种语义下的 `lines`**（2026-09-12 修复，演练缺陷 2）：

```json
primary: {"v":1,"ts":"...","who":"pi","seq":42,"verdict":"healthy","net":"ok","mode":"primary","fast":0,"fails":0,"lines":{"www.default":"200","www.oversea":"200","starkeeper":"200"}}
backup:  {"v":1,"ts":"...","who":"pi","seq":43,"verdict":"unhealthy","net":"ok","mode":"backup","fast":0,"fails":3,"lines":{"www.default":"000","www.oversea":"000"},"live":{"www.default":"200","www.oversea":"200"}}
```

> ⚠️ `lines` 在两种语义下都表示**主站路径**，这正是恢复闸门能防拉锯的前提：
> 若 backup 语义下 `lines` 写的是备站入口码（200），`peer --target www` 会返回
> `healthy`，云侧恢复闸门被误放行——当故障形态是"CF IP 境内被墙、境外正常"时，
> 云侧会立刻把站点恢复回坏 IP，来回拉锯。因此 `peer --target <t>` 的目标语义
> （§三）表达的是"**国内视角下主站路径是否恢复**"，备站入口状态只通过 `live`
> 供人查看。备份语义下"主站不健康"是预期状态，写入方不得据此进入切换评估。

`gh` 不写 `temp`/`up`/`live`。未知字段读取方一律忽略。

### 2.2 快照记录 schema（`_dr-snap`）

```json
{"v":1,"ts":"2026-09-11T10:00:00Z","who":"pi","dir":"backup","www":["172.64.52.95","162.159.44.17","162.159.39.168"],"www_oversea":["104.19.184.186","104.21.93.71","172.66.216.152"],"starkeeper":["172.64.52.95","162.159.39.168"]}
```

| 字段 | 取值 | 说明 |
|---|---|---|
| `ts` | UTC ISO8601 | **最近一次切换动作**的时间 → 最小驻留时间的计算基准 |
| `who` | `pi` / `gh` | 执行方 |
| `dir` | `backup` / `restore` | 最近一次动作方向 |
| `www` / `www_oversea` / `starkeeper` | IP 字符串数组 | **主站原始 IP**（恢复目标），不是备站 IP |

**写入语义**（2026-09-12 演练修复后的优先级，**不得回退**）
1. **优先用"最后一次健康时的主站 IP"**（`last_good_ips`：权威查询成功且该线路探测健康时记录；**IP 组里含备站 IP 则整组不记**，避免备站驻留期把 Vercel IP 记成主站 IP）。
2. 没有 `last_good_ips` 时，**保留已存在且有效的旧快照 IP 数组**（与 `.failover_state.json` 同规则），只更新 `ts`/`who`/`dir`。
3. 两者都没有 → **只写 `ts`/`who`/`dir`，不写 IP 数组** + `⚠` 日志。
   ⚠️ **绝不允许**把"切换瞬间的当前记录"当恢复目标——2026-09-12 演练实测：首次切换时那正是攻击注入的黑洞 IP，恢复方会把它当救命稻草。
- `restore` 时：写 `dir="restore"` + 新 `ts`，IP 数组保留（主站 IP 依然有效）。
- **写入失败绝不允许阻断 DNS 切换**（best-effort，只记日志）。

**长度裁剪顺序**（超过 255 字节时）：先删 `starkeeper` → 再把三个数组各截到 2 个 IP → 再截到 1 个。

**读取容错**：去掉首尾空白 → 若值被双引号包裹则剥掉引号 → **反转义 `\"` 与 `\\`** → JSON 解析失败视为不存在。

> ⚠️ **阿里云 TXT 往返会转义内层双引号**（2026-09-11 真实 API 实测）：
> 写入 `{"a":1}` 读回 `{\"a\":1}`；写入 `"quoted"` 读回 `quoted`（外层引号被剥掉）。
> 读取端若不做反转义，`json.loads` 必然失败 → `peer` 误判 `absent` → **恢复闸门失效、会签通道等于不通**。
> 因此反转义是双观察者能互相看见的前提，`dr_board.normalize_txt_value()` 与
> `dr_agent.read_txt()` 必须同时实现（已有 4 项离线断言锁住）。

## 三、`dr_board.py` CLI 契约

位置 `monitoring/scripts/dr_board.py`，纯标准库，环境变量 `ALI_KEY_ID` / `ALI_KEY_SECRET`（可选 `ALI_REGION`，默认 `cn-hangzhou`）。

| 命令 | 输出（stdout，单行） | 退出码 |
|---|---|---|
| `mode www` | `primary` / `backup` / `mixed` / `empty` | 0；API 或凭据失败 → 无输出 + 1 |
| `mode starkeeper` | 同上 | 同上 |
| `get <record>` | 记录的原始值（去引号） | 0；记录不存在 → 3；API 失败 → 1 |
| `set <record> <value>` | 无 | 0；失败 → 1（**记录不存在时自动创建**，TTL 600） |
| `peer <record> [--max-age 1200] [--target www\|starkeeper]` | `healthy` / `unhealthy` / `stale` / `absent` / `unknown` | 0（判定类命令不因缺记录失败）；API 失败 → 打印 `unknown` + 1 |

- `<record>` 只接受 `_dr-pi` / `_dr-gh` / `_dr-snap`（拒绝其它名字，防误写）。
- `peer` 语义：`absent` = 记录不存在或值为空；`stale` = `ts` 超过 `--max-age` 秒（默认 1200）；`unknown` = 新鲜但 `verdict != healthy/unhealthy`；其余取 `verdict` 原值。
- `peer --target <t>` 语义（目标感知，用于各自 workflow 的恢复闸门）：只看 `lines` 里**匹配 `<t>` 的条目**——
  key 等于 `<t>` 或以 `<t>.` 开头都算命中（`starkeeper` 与 `starkeeper.default` 等价）；
  全部为 2xx/3xx/4xx → `healthy`；任一条为 `000` 或 5xx → `unhealthy`；该目标的 key 一个都没有 → `unknown`。
  不带 `--target` 时退化为读整条 `verdict` 字段。
- 所有命令失败信息写 stderr，日志前缀沿用仓库风格（`❌` 错误 / `⚠` 警告）。

## 四、决策规则（两侧一致，不得各自发明）

```
切换（backup）条件，全部满足：
  1. 本地连续不健康计数 fails >= 3
  2. mode(权威推导) == primary
  3. 自身网络正常（net == ok）
  → 执行切换；切换后写 _dr-snap（best-effort）

恢复（restore）条件，全部满足：
  1. 本地连续健康计数 streak >= 3
  2. mode(权威推导) == backup（或 mixed 且主站 IP 可达）
  3. peer(_dr-对方观察者) != "unhealthy"      ← 对方明确说不健康则不恢复
  4. now - _dr-snap.ts >= 1800s（最小驻留时间）；_dr-snap 缺失视为满足
  → 执行恢复；恢复后写 _dr-snap(dir=restore)

强制恢复：workflow_dispatch 输入 force_restore=true → 跳过条件 3、4（保留人工最终权威）。

状态迁移时计数必须归零（2026-09-12 演练修复，两侧对称，不得回退）：
  当权威推导出的 mode 与上一次记录的 mode 不同（primary↔backup/mixed/empty）时，
  fails 与 streak 一律清零后再进入本轮计数。
  理由：演练实测云侧 healthy_streak 从故障前继承到 630、Pi 侧 fails 在 backup 期累积——
  前者让备份模式第一轮就恢复，后者让恢复回 primary 后 1 次探测即切换；两者都是"跳过连续 N 次确认"，
  会放大来回拉锯。归零后任何决策都必须重新积累满 N 次证据。
```

- **对方失联不阻断恢复**（`stale`/`absent` 视为通过），但要在告警里标注降级运行。
- **最小驻留（条件 4）的作用域**：只约束**执行恢复的那一方**。
  - 云侧（`gh`）的恢复闸门以条件 3（peer）为准，**不额外检查驻留时间**：云侧若因 `stale` 恢复，而树莓派随后仍看到国内不健康，树莓派会再次切备——这是正确行为（它有故障证据），不该被驻留时间拦住。
  - 树莓派在 `DR_ROLE=full` 下执行恢复时，条件 4 必须检查（防止在云侧刚切完、国内视角尚未稳定的窗口内反手恢复）。
- `mixed` / `empty` 一律按"保持现状、不切换"处理，只告警。
- 树莓派**阶段一**只执行切换，恢复判定只记录并告警（`DR_ROLE=switch_only`）；`DR_ROLE=full` 时才执行恢复，规则完全相同。

## 五、兼容与降级（硬要求）

1. 会签板任一读写失败 → 继续按现有逻辑走，**绝不能让容灾因会签通道故障而失效**。
2. 云监控推导状态失败（API 挂了）→ 回退读 `monitoring/.failover_count.json`（现行为）。
3. 推导出的状态与本地文件不一致时，**以推导为准并回写文件**（保证 `update-dns.py` 的现有守卫同步收敛）。
4. 树莓派读不到会签板时，切换不受阻；恢复判定按"对方 absent"处理。

## 六、本契约的实施边界（给并行 agent）

| 归属 | 文件 |
|---|---|
| A | `monitoring/scripts/dr_board.py`（新建） |
| B | `monitoring/pi-agent/**`（全部新建） |
| C | `.github/workflows/failover-monitor.yml`、`.github/workflows/starkeeper-monitor.yml` |
| D | `monitoring/scripts/update-dns.py`、`failover-dns.py`、`monitor_logger.py`、`report_mailer.py` |
| 主控 | `monitoring/DR-OBSERVER-CONTRACT.md`、`content/ops/**`、git 提交 |

只许改自己名下的文件；不得执行任何 git 命令；Python 目标版本 3.9+（不使用 `match` 等 3.10+ 语法）；树莓派侧脚本**只用标准库**。
