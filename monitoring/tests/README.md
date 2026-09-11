# monitoring/tests — 双观察者容灾离线对抗推演

这套测试让 `dr_agent.py`（树莓派观察者）与 `dr_board.py`（云侧会签板 CLI）在
**不触碰真实阿里云 DNS / 不发邮件**的前提下，跑完"真实 HTTP API 交互 + 完整决策链 +
连续多轮探测"。真实 DNS 只读一次都不做：所有 `alidns.*.aliyuncs.com` 调用都被
`ALI_ENDPOINT` 环境变量改道到本机 `mock_alidns.py`。

```bash
# 一键全跑（约 5-6 分钟，含真实 CF/Vercel IP 探测）
python monitoring/tests/test_coop_scenarios.py

# 只跑指定场景（开发调试用）
python monitoring/tests/test_coop_scenarios.py S2 S12
```

- 纯标准库、**不依赖 pytest**；逐场景打印 ✅/❌，末尾汇总；任一断言失败 → `exit 1`。
- 每个场景独立临时目录做 config/state，绝不碰 `/etc`、`/var`；`DR_ALERT_ENABLED=0`
  不发邮件；AK 是假值。
- 网络前提：agent 的探测要打真实 CF/Vercel IP（`172.64.52.95` 等）与 223.5.5.5，
  离线环境会先打印 ⚠ 并在依赖联网的场景失败。mock 的循环不触发 peer 新鲜度问题。

## 文件

| 文件 | 说明 |
|---|---|
| `mock_alidns.py` | 假 Alidns 服务（内存 zone + 故障注入 + 调用统计），可内嵌可 CLI |
| `test_coop_scenarios.py` | S0-S14 场景与断言（本文件是唯一入口） |
| `test_starkeeper_safety.py` | 另一条独立回归：`monitoring/scripts/starkeeper_dns.py` 的"零记录空窗"防护（与本套无关，单独运行） |
| `README.md` | 本文档 |

## 场景一览

| 场景 | 验的是什么 | 关键断言（摘要） |
|---|---|---|
| **S0** mock 契约 | 假 API 本身是否可信 | 无匹配 `Record=[]`；缺 `Signature`→400 `MissingParameter`；`DomainRecords:null` 边界下 `dr_board mode www` 输出 `empty` 且 rc=0；重复 Add→`DomainRecordDuplicate`；超时注入客户端按自身 timeout 失败 |
| **S1** 健康 | 主站在线时不误切 | `--ticks 3` 后：无 `_dr-snap`；`_dr-pi` verdict=healthy/net=ok/mode=primary/fails=0/lines=200；www 记录 RecordId+Value 全未动；`dr_board mode www`→`primary` |
| **S2** 黑洞注入 | 连续 3 次不健康触发切换 | 注入 `203.0.113.1` 后 3 tick：日志有"满足切换条件/✅ 切换完成"；zone 两条线路都变 `76.76.21.21` 且总记录数仍为 2；`_dr-snap` 写入且**不含 IP 数组**（无 last_good_ips 时只写 ts/who/dir，黑洞不能当恢复目标）、dir=backup/who=pi；`mode www`→`backup`；primary 语义 `_dr-pi` 无 `live` |
| **S3** 拉锯防护（核心） | 云侧在对方摇头时不得恢复 | zone=Vercel、`_dr-pi` 是切换瞬间的 unhealthy/000 证据：`mode www`→`backup`、`peer _dr-pi --target www`→`unhealthy`；主站 IP 真实可达；契约 §四 的可执行复刻 `cloud_restore_allowed(backup, unhealthy)=False`（对照 healthy=True）；zone 仍是 Vercel |
| **S4** 阶段一不恢复 | `DR_ROLE=switch_only` 只告警 | zone=Vercel + 快照主站 IP 可达 → `--ticks 16`（跨 3 次完整探测）：输出"恢复条件满足但 DR_ROLE=switch_only"、streak≥3；zone 与 `_dr-snap` 完全未动；backup 语义 `lines`=主站路径（快照 CF IP→200）、`live`=当前入口（Vercel→200），`peer --target www`=healthy（闸门可放行） |
| **S5** Pi 失联降级 | 契约的"失联不阻断恢复" | `_dr-pi.ts` 40 分钟前 → `peer`=stale；删记录 → absent；`_dr-gh` 缺失 → absent；`cloud_restore_allowed` 对 stale/absent/unknown=True、对 unhealthy=False |
| **S6** 快照防污染 | 已有有效快照时不得把黑洞 IP 当恢复目标 | 预置含 CF IP 数组的 `_dr-snap`，再触发切换：数组原样保留，仅 ts/who/dir 更新为当前/pi/backup |
| **S7** 时钟防线 | 偏差超限拒绝 DNS 写 | skew=999（>300）：`allow_write()=False`；run_tick 后 www 记录与 RecordId 未动、无 `_dr-snap`；日志出现"时钟偏差…拒绝 DNS 写操作" |
| **S8** 备份语义静默 | backup 下不评估切换、不改任何记录 | zone=Vercel + 快照黑洞（制造 unhealthy）：无"拒绝覆盖/幂等保护/以下目标不切换"、无"切换完成"；www RecordId/Value 未动、无重复记录；`_dr-snap` 原文未变；`_dr-pi` 心跳照写（lines=主站 000、live=备站 200），`peer --target www`=unhealthy |
| **S9** API 故障降级 | API 挂了不误切、云侧可回退 | `Describe` 注入 500：agent rc=0、verdict=unknown、zone 未动、无快照/心跳；`dr_board mode www` rc=1 且 stdout 为空、stderr 带 ❌ |
| **S10** 验证档 | `DR_SWITCH_ENABLED=0` 零 DNS 风险 | 黑洞 + 3 tick：zone 仍黑洞且 RecordId/Value 未动、无 `_dr-snap`、日志"本应切换到备站…DR_SWITCH_ENABLED=0…仅告警"；`_dr-pi` 心跳 seq=3、ts 新鲜、verdict 照常。补充（进程内）：`DR_ROLE=full` + streak=3 直调 `maybe_restore` → 输出"本应恢复主站…DR_SWITCH_ENABLED=0"、发 `restore_disabled` 告警、zone 未动 |
| **S11** 心跳降频 | `DR_BOARD_WRITE_SECONDS` 生效且不过期 | 阶段 A（interval=15，8 tick）：`_dr-pi` 仅写 2 次（<8），次数符合 elapsed/interval，ts age≈15s；阶段 B（interval=120）：首轮必写；中途黑洞化后 51s 内立即补写（<120，变化即写），最后一次 verdict=unhealthy |
| **S12** starkeeper 安全 | 先建后删，任何失败不留裸域 | 断言1 非冲突 add 失败→A 原样、无 CNAME、有告警；断言2 删 A 失败→A+CNAME 并存（mixed）、CNAME 生效；断言3 成功→只剩 CNAME；断言4 恢复时 A 写回失败→CNAME 仍在。补充：冲突类错误才允许"删 A 后立即 add"，二次失败告警"无记录状态，需人工介入" |
| **S13** 缺陷 5（进程内） | backup 语义不评估切换、无告警噪音 | zone=Vercel + 快照主站黑洞，4 轮后 fails=4≥3（旧实现会进入"幂等保护拒绝"路径）：无"拒绝覆盖/幂等保护/以下目标不切换"warning、`AlertRecorder` 无 `switch_refused`、www 记录未动、`_dr-pi` 照写（verdict=unhealthy/lines=000/live=200）。对照：同进程把 zone 变黑洞（primary）跑 3 轮 → 出现"满足切换条件"且 zone 切到 Vercel |
| **S14** 缺陷 1（进程内） | 快照优先取 last_good_ips、绝不写当前记录 | A：预置 `last_good_ips` + 一份数组为黑洞的旧快照 → `execute_backup` 后快照数组 = last_good_ips（黑洞未进入，优先于旧快照）；B：无 last_good + 有效旧快照 → 旧数组原样保留；C：两者皆无 → 只写 ts/who/dir、无任何数组（切换照常执行） |

## mock_alidns.py

### CLI 手工调试

```bash
python monitoring/tests/mock_alidns.py --port 8899 --seed primary   # 或 --seed backup
# 另开终端（Git Bash）:
export ALI_ENDPOINT=http://127.0.0.1:8899/
export ALI_KEY_ID=mock ALI_KEY_SECRET=mock
python monitoring/scripts/dr_board.py mode www
python monitoring/pi-agent/dr_agent.py --config <你的测试配置> --state-dir <临时目录> --ticks 3 --dry-run
```

### 内嵌启动 + 辅助函数

```python
import mock_alidns
srv = mock_alidns.start_mock()          # 随机端口 + 后台线程，返回 MockAlidns
srv.url                                 # http://127.0.0.1:<port>/
srv.set_record("www", "A", "default", "172.64.52.95")   # 模块级同名函数操作"最近启动"的实例
srv.add_record("www", "A", "default", "162.159.44.17")  # 同组多条
srv.get_record(...) / srv.get_all(...) / srv.delete_record(...) / srv.clear_zone()
srv.dump_zone()                         # 全量记录副本（断言用）
srv.calls(action="UpdateDomainRecord", rr="_dr-pi")     # 调用次数
srv.call_log(action=None, rr=None)      # [{"ts","action","params"}]，S11 用它做时间线断言
srv.stop()
```

### 故障注入

```python
srv.set_fault("DescribeDomainRecords", code="InternalError", http_status=500, count=None)
srv.set_fault("AddDomainRecord", code="DomainRecordDuplicate", http_status=400, rr="starkeeper", count=1)
srv.set_fault("DescribeDomainRecords", mode="null_records", count=1)   # DomainRecords: null
srv.set_fault("DescribeDomainRecords", mode="empty_records", count=1)  # Record: []
srv.set_fault("DescribeDomainRecords", timeout=1.0, count=1)           # 挂起（触发客户端超时）
srv.clear_faults()
```

- `action=None` 匹配所有 Action；`rr` 可选（精确匹配响应里的 `RR`/请求里的 `RRKeyWord`）；
- `count=N` 只生效 N 次，`count=None` 持续到 `clear_faults()`；
- 未注入时：`Describe` 支持 `RRKeyWord`（子串、忽略大小写）/`TypeKeyWord`/`Line`/`PageSize`（默认 20，与真实 API 一致）；
- Add 重复（同 RR+Type+Line+Value）→ HTTP 400 `DomainRecordDuplicate`；未知 RecordId → `InvalidRecordId`；
- 签名只校验 `AccessKeyId`/`Signature`/`SignatureMethod`/`Timestamp` 存在且 Signature 非空，不重算 HMAC。

## 怎么加新场景

1. 在 `test_coop_scenarios.py` 里写函数并加装饰器，注册顺序即运行顺序：

   ```python
   @scenario("S15", "一句话标题")
   def s15(t):
       srv = mock_alidns.start_mock()
       tmp, cfg_path, state_dir = make_env(DR_ROLE="full")   # 关键字覆盖配置
       try:
           srv.set_record("www", "A", "default", BLACKHOLE)
           proc, elapsed = run_agent(cfg_path, state_dir, srv.url, ["--ticks", "3"], timeout=240)
           t.check(proc.returncode == 0, "agent 退出码 0")
           t.check("..." in (proc.stdout + proc.stderr), "输出断言")
           t.check_eq(zone_values(srv, "www", "A"), ["76.76.21.21"], "zone 断言")
       finally:
           srv.stop()
           shutil.rmtree(tmp, ignore_errors=True)
   ```

2. 可用工具：`run_agent` / `run_board` / `board_json` / `zone_values` /
   `zone_id_value_map` / `pi_write_log` / `cloud_restore_allowed` / `fresh_ts` /
   `ts_age_seconds` / `tcp_ok` / `AlertRecorder`（替换 `ctx.alerts` 收集告警）/
   `LogCollector`（配合 `_make_log_handler` 收集进程内日志）。
3. `@scenario(..., needs_net=False)` 标注不依赖外网的场景（仅用于前提提示）。

## 设计与边界说明（诚实清单）

- **S3 的"主站恢复可达"**：任务原文说"zone 改回真实 CF IP"，但该场景要断言的规则是
  `mode=backup + peer=unhealthy`，zone 必须保持 Vercel。测试的做法：zone 保持备站，
  把 `_dr-snap` 放进真实 CF IP 并实测其 TCP 可达（表示主站路径已恢复），`_dr-pi`
  保留切换瞬间 agent 真实写下的 unhealthy/000 证据。这样才同时满足"mode=backup"与
  "peer=unhealthy"两个前提。
- **S4 用 16 个 tick（约 75s）**：`DR_FULL_PROBE_SECONDS` 有 30s 硬下限，连续健康 3 次
  必须真实跨越两次 30s 间隔，无法压缩。
- **S7 用进程内 monkeypatch**：真实网络校时会用 HTTP Date 覆盖注入的 skew，所以冻结
  `probe_net` 与 `clock.refresh` 两个网络入口后直调 `run_tick`；这不经过 CLI，但走的是
  与生产完全相同的 `maybe_switch → execute_backup → allow_write` 路径。
- **时钟闸门只拦切换/恢复写**：`_dr-pi` 心跳是会签板 best-effort 写，不受
  `allow_write()` 限制（契约 §五：会签通道故障不得让容灾失效）。S7 因此断言的是
  A 记录/快照未动，而不是 `_dr-pi` 不存在。
- **S12 的冲突退化路径是破坏性的**：`DomainRecordDuplicate` 时才允许"删 A 后立即 add"，
  若二次 add 也失败会告警"无记录状态，需人工介入"（这是按需求保留的降级路径）。
- **S8 造 unhealthy 的手法**：zone=Vercel 本身健康，测试预置一份"主站 IP 不可达"的快照，
  让 agent 在 backup 语义下判 unhealthy（lines=主站路径 000、live=备站入口 200）；
  验证的是"backup 语义不评估切换"（缺陷 5）与闸门 `peer --target www`=unhealthy
  （缺陷 2），全是用 mock 数据构造的输入。
- **S11 阶段 B 的"立即写"上界**：切换触发依赖下一次完整探测（slow 模式最长
  `DR_FULL_PROBE_SECONDS`=30s），所以"立即"指"判定变化的当轮 tick 立即写"，测试用
  `间隔 < DR_BOARD_WRITE_SECONDS` 证明不是等心跳间隔到点。
- **未覆盖**：真实阿里云 API 的签名/限流/错误码差异；SMTP 发信链路；systemd/install.sh；
  GitHub workflow 对 `dr_board.py` 的编排（云侧规则在本套测试里是
  `cloud_restore_allowed` 这一可执行复刻，不是真跑 workflow）；`starkeeper` 混合态
  （mixed）后没有自动收敛重试（契约规定 mixed 保持现状，仅告警）；Pi 上 curl 缺失时的
  stdlib 探测分支；`--loop` 常驻循环本身（用 `--ticks` 覆盖了同一 run_tick 路径）。
