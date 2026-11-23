---
name: burner
description: VenusA/CSK 与 HISI/HI3863 双芯片统一烧录 Skill。按 profile 执行 preflight → boot → burn → verify → recover → receipt，统一资产 manifest（OSS 路径 + SHA-256）、北京时间无时区时间契约、dry-run / confirm-write / 硬件互斥锁；TCL、Media（美的）与 CSK6 CH344 dev01 台架严格隔离。用于台架固件烧录、已知基线回归和换版本验证。
---

# burner

一套 Skill 覆盖两种芯片的烧录：**VenusA/CSK（fw.img 全镜像）** 与 **HISI/HI3863（WS63 BurnTool）**。
不再按项目复制烧录 Skill——项目差异只写在 profile，工具与固件只登记在统一资产 manifest。

## 何时用

- 在台架上烧一套 TCL/Media 固件并做启动与版本校验。
- 用已知基线（CSK `V0.0.41` + HISI `V8-H363T20-WFMV206.111.1.16`）回归 Skill 或台架。
- 换版本验证：改 profile 的 `verify.expect` 或用 `--expect` 覆盖。

不用于：语音识别判定（交给 `lstest` / `tcl-test-sub`）、刷机方案设计、没有 profile 的“临时试一下”。

## 硬规则（违反即拒绝运行）

1. **没有 profile 不烧录**：`--profile` 必填，没有内置默认台架。端口、波特率、断电方式、期望版本都来自现场实测。
2. **写阶段必须 `--confirm-write`**：`boot:*` / `burn:*` / `recover:*` 都会改变设备状态。
3. **profile 不继承**：`inherits` / `extends` / `defaults_from` / `$ref` 在加载时直接拒绝；每个 profile 自己写全。
4. **族隔离**：`board_family: tcl` 必须显式禁用 `uut-reset` / `uut-csk-reset` / `uut-burn`；其他族使用 `uut-*` 必须显式写 `control.allow_tcl_commands: true`。
5. **draft profile 不能写**：`status: draft` 只允许 `plan` / dry-run / preflight（preflight 返回 BLOCKED 并列出待测量项）。唯一例外是**首跑 commissioning**：profile 内有真人写的 `commissioning` 块（`approved_by` / `approval_ref` / `pins`（每个 tool/image 的 asset + sha256）/ `allowed_steps` / `max_write_steps` / `max_runs=1` / `expires_at`，外加**二选一**：单台 `node` + `device`（serial + 端口），或多台 `benches[]`（每台自己的 `bench_id` / `node` / `serial` / `control_port` / `burn_ports` / `approval_ref`）），且命令行同时给 `--commission <approval_ref>`、`--node <approved node>`、`--confirm-write`（多台 profile 还要 `--bench <bench_id>`）；`--commission` 只接受 `all`，先跑 preflight，任一预门禁失败则设备零动作；**同一台架只写一次**（每台一个 ledger，路径含 node + bench_id），不自动重试，换 approval_ref 也不能重开同一台。跑完写 `commissioning.receipt.json`，再用 `promote` 由回执生成 measured profile（多台 profile 的 promote 只产出被选中那一台的 measured profile），状态不能手改。**审批有有效期**：`expires_at` 过期后 profile 仍可解析、`plan`/`preflight` 仍可用（plan 会打印 `[EXPIRED]`），但任何写步骤一律退出码 3；过期不是"重新跑一次就行"，要新的审批号/有效期。
6. **一台台架同时只允许一个烧录进程**：任何硬件阶段先抢 `%TEMP%\burner-<node>.lock`；被活进程持有时退出码 3，不杀进程、不抢端口。
7. **每步先落盘再判定**：step receipt + 工具日志 + 串口原始日志 + `reset_log.jsonl`；判定顺序 BLOCKED / FAIL / PASS。
8. **时间只写北京时间、不带时区**：`YYYY-MM-DD HH:MM:SS`（空格分隔、秒级）；`T`、小数秒、`+08:00`、`Z`、RFC3339 都是违约；设备原始时间只能原样进 `raw_*` 字段（见 `references/result-contract.md`）。
9. **凭据只来自环境变量**：Skill / manifest / 日志 / 回执里出现 token、密码、私钥即加载失败。
10. **设备端口只从 device-io 的 JSON 读取**：传 `--device-snapshot` 后端口全部来自该文档，解析失败即退出（码 2），绝不回落到 profile 里的旧端口；不传则保持原有 profile 行为。本 Skill 不自行枚举 USB/串口。

## 快速开始

```powershell
# 只看计划，不碰硬件
python scripts\burn.py plan --profile scripts\profiles\tcl-pvm1005.json --run-dir D:\zhsh\workspace\tasks\<task>\run

# 预检（只读）：枚举端口、读当前版本、校验资产 SHA-256
python scripts\burn.py preflight --profile ... --run-dir ... --material-dir ...\assets

# 全流程（写 flash + 断电），必须显式确认
python scripts\burn.py all --profile ... --run-dir ... --material-dir ...\assets --issue NOAH-44 --confirm-write

# 单步重跑（前序步骤必须 PASS）
python scripts\burn.py step verify:hisi --profile ... --run-dir ... --material-dir ...\assets --confirm-write

# 换一套固件：先改 profile 的 verify.expect，或临时覆盖期望版本
python scripts\burn.py all --profile ... --run-dir ... --material-dir ... --confirm-write `
  --expect venusa=V0.0.40 --expect hisi=V8-H363T20-WFMV206.110.1.15

# 首跑 commissioning（draft profile，必须有已批准 commissioning 块）：固定节点 + 单设备 + 钉住 SHA，只写一次
python scripts\burn.py all --profile ... --run-dir ... --material-dir ...\assets --issue NOAH-263 `
  --device-snapshot ...\port-map.json --commission NOAH-263 --node win003 --confirm-write

# 多台架逐台轮换（一台 = 一个 approval_ref = 一个 ledger）：先 plan 看选中哪台，再跑那一台
python scripts\burn.py plan --profile scripts\profiles\csk6-hisi-win003.json --run-dir ...\run --bench dev02
python scripts\burn.py all --profile scripts\profiles\csk6-hisi-win003.json --run-dir ...\run `
  --material-dir ...\assets --issue NOAH-267 --bench dev02 `
  --commission NOAH-267-win003-dev02 --node win003 --confirm-write

# 由 commissioning.receipt.json 生成 measured profile（缺任一分级证据即拒绝）
python scripts\burn.py promote --profile ...\csk6-hisi-dev01.json `
  --from ...\run\commissioning.receipt.json --out ...\csk6-hisi-dev01.measured.json
```

退出码：`0` 通过，`2` 用法/profile/资产/设备快照错误，`3` 守卫拒绝（draft、缺 `--confirm-write`、commissioning 块/引用不符、回执不全、台架被占用、驱动未实现），`4` 步骤 FAIL/BLOCKED。

## 设备端口：`--device-snapshot`

端口不再手写死：让 `device-io` 产出 JSON，本 Skill 只读该文档。参数在所有子命令
（`plan` / `preflight` / `step` / `all` / `receipt`）上都可用。

```powershell
# device-io 产出两种文档任选其一
device-io ports --format json --out device-snapshot.json
device-io roles --spec roles-spec.json --format json --save role-map.json

# 用快照跑 plan（只读）
python scripts\burn.py plan --profile scripts\profiles\tcl-pvm1005.json `
  --run-dir D:\zhsh\workspace\tasks\<task>\run --device-snapshot device-snapshot.json

# 用 role-map 跑全流程
python scripts\burn.py all --profile ... --run-dir ... --device-snapshot role-map.json --confirm-write
```

- **接受的 schema**：`ark.device-io.v1`（原始快照，按 profile 里已声明的端口名/interface 标签
  **精确、大小写不敏感**匹配 `ports[].device` / `ports[].interface`）与
  `ark.device-role-map.v1`（`device-io roles` 的结果，直接取 `roles[].port`）。
- **优先级**：给了 `--device-snapshot` 就以它为准，覆盖 `transport.log_port` /
  `transport.burn_port` 与 `control.port`；没给则完全不改变既有行为。
  一次运行里只要给了参数就**没有回落**：任何 role 解析不出来都直接失败。
- **role 命名**：双芯片 profile 用 `control`、`<chip>:log`、`<chip>:burn`
  （如 `venusa:log`、`hisi:burn`）；单芯片 profile 也接受裸 `log` / `burn`。
- **失败语义**：role-map 里 `ambiguous` / `missing` / `conflict`、缺 role、顶层 `ok:false`，
  或快照里 0 命中（`missing`）、多命中（`ambiguous`）都硬失败，退出码 **2**，
  错误信息含 role、state 与 `reason`。
- `plan` / `preflight` 输出带结构化块：`source`、`schema`、`generated_at`、`mode`、
  `roles`（role → 端口与 state），不打印整份快照。
- 细节与失败示例见 `references/profile-contract.md`。

## 统一状态机

步骤由 profile 推导，不在代码里写死项目：

| 步骤 | 动作 | 证据 |
|---|---|---|
| `preflight` | 枚举 COM、读当前版本、校验资产 hash | `steps/preflight.json`、`serial_logs/preflight_*.log` |
| `boot:<chip>` | 仅 `boot.required` 的芯片：断电 → boot strap → 上电 → 撤 strap | `steps/boot-*.json`、`reset_log.jsonl` |
| `burn:<chip>` | `venusa-uart`：`Uart_Burn_Tool -b -p -f -a`；`hisi-burntool`：`BurnTool -com -bin -signalbaud -chiptype -onlyburn` + 断电上电；`csk6-uart`：`Uart_Burn_Tool` + WRITE SIZE / 设备 MD5 / 4 标记判定（`burn.pre_power_cycle=true` 时按 `control.timing.power_sequence` 先上下电或先开串口再上下电，并镜像日志口） | `steps/burn-*.json`、`tool_logs/tool_*.log`、`serial_logs/burn-*.log`、BurnTool `optLog` |
| `verify:<chip>` | 断电上电，抓串口版本，按 `verify.expect` 判定 | `steps/verify-*.json`、`serial_logs/verify-*_*.log` |
| `recover:<chip>` | `ftm-clear`（`AT+FTM=0`）或 `power-cycle`；`before_verify` 决定顺序 | `steps/recover-*.json` |
| `receipt` | 汇总所有步骤 + 资产 + reset_log + 产物 SHA-256 | `run.receipt.json` |

执行严格串行：某步只能在前一步 PASS 后运行；FAIL/BLOCKED 后 `all` 立即停止，执行 `safe_recover`（按 profile 恢复供电并记录），并写一份 FAIL 回执。

## profile 分层

- `scripts/profiles/tcl-pvm1005.json`：TCL 台架实测值（COM6 控制、COM3 CSK、COM4 HISI、`uut-switch1`/`uut-csk-boot`、BurnTool 后 `115200 → AT+FTM=0 → 921600`）。
- `scripts/profiles/midea-csk6.json`：Media/美的 **draft**，声明自己的驱动方式与待测量项，不继承 TCL 的任何端口/命令/时序。
- `scripts/profiles/csk6-hisi-dev01.json`：CSK6 + HISI CH344 四串口 dev01 台架（win003/win005 **共用一台**）**draft**，单台 `device` 块（历史批准 `NOAH-266`，已消耗，保留供复现）。
  端口只是 win003 参考值：**跨节点禁止复用 COM 映射**，运行时必须逐节点枚举并传 `--device-snapshot`。
  CSK6 走 `csk6-uart`（WRITE SIZE + 设备 MD5 + 4 标记 + exit 0 才 PASS）；未测量项全在 `measurement_required`。
- `scripts/profiles/csk6-hisi-win003.json` / `csk6-hisi-win005.json`：同一台架族的 **4 台台架 × 每节点**（dev01–dev04）**draft**。
  `commissioning.benches[]` 每台一条：`bench_id` / `node` / `serial`（usb_parent）/ `control_port` / `log_ports` / `burn_ports` / `spare_port` / `approval_ref`（`NOAH-267-<node>-<bench>`）。
  端口角色按 NOAH-267 确认口径：A=`csk6:log`、B=`csk6:burn`（仅 CSK6）、C=`hisi:log_and_burn`（复用同一物理口）、D=spare（`port_roles` 声明）。
  `--bench <bench_id>` 选中一台；该台的端口就是批准里写的端口。每台一个 ledger，`%TEMP%\burner-<profile>-<node>-<bench_id>-commissioning.json`。
  端口来自 NOAH-268/269 只读清点（`usb_parent` 是主键，COM 会随重插变化），`control.timing.power_sequence=connect_then_power`。

TCL 与 Media 的差异、隔离规则和独立验收项见 `references/profile-contract.md`。

## 资产与 OSS

`assets/manifest.json` 是唯一的工具/固件登记处：

- 每条资产记录：`id`、`kind`（tool/firmware）、`target_chip`、`version`、`file`、`bytes`、`sha256`、OSS `key/object/download`、本地 `install.material_rel`/`cache_rel`、`provenance`。
- 工具不复制进项目 Skill：本机用节点缓存（`--asset-cache`）或本次材料树（`--material-dir`），SHA-256 决定本地副本是否可用。
- 凭据边界：只引用环境变量名（`ARK_OSS_ENDPOINT` / `ARK_OSS_BUCKET` / `ARK_OSS_ACCESS_KEY_ID` / `ARK_OSS_ACCESS_KEY_SECRET`），manifest 内出现字面量即拒绝加载；`--fetch-assets` 缺环境或缺 `ossutil` 时标 BLOCKED，不猜默认值。
- 详见 `references/asset-manifest.md`。

## 失败停止与恢复

- 任一步 FAIL/BLOCKED：停止后续步骤，不写 PASS。
- `safe_recover` 只做“把台架恢复到上电状态”，并把命令与回执写进 `reset_log.jsonl` 与 `recover/safe-recover.json`；它不会把 FAIL 改成 PASS。
- 台架被其他进程占用：退出码 3，等对方结束，不 kill、不抢串口。
- 资产缺失或 hash 不符：preflight BLOCKED，禁止烧录。

## 测试与门禁

```powershell
python -m unittest discover -s tests -v     # 单测（不碰串口）
python scripts\lint_scan.py --strict        # skill-git lint/scan：frontmatter、JSON、密钥扫描、compileall
python tools\time_lint.py --gate            # 时间契约：0 违规才算过（设备原始时间行用 time-lint:allow 标注）
```

## 已知坑（都有现场证据）

1. **HISI 烧完出现 “Flash Init Fail 0x80001341” 不是烧坏**：BurnTool 把芯片留在 FTM 工厂模式。先 `AT+FTM=0`（115200）再上电即可；本 Skill 把这一步做成 `recover:hisi`。
2. **fw.img 写 `0x0`**：16 MiB 全镜像；`0x40000` 是 `venusa.bin` 的分区地址。
3. **BurnTool 默认端口是 COM6（控制台）**：必须显式 `-com:4`，不要直接跑包里的 bat。
4. **BurnTool 的 stdout 为空是常态**：成功证据在 `BurnTool_Gold\optLog\optLog_*.txt`（“烧写完成成功”），不在 stdout。
5. **CSK 串口平时静默**：preflight 读不到版本不等于设备异常；版本证据以 `boot:venusa`/`verify:venusa` 后的启动横幅为准。
6. **两台并发 = 双方失败**：CSK 烧录需要独占 boot strap 与 COM3；由 `BenchLock` 兜底。
7. **CSK6 台架的 COM 编号跟着节点变**：同一台 CH344 dev01 在 win003 与 win005 上编号完全不同，
   把另一节点的映射抄过来会把日志口当烧录口（`kb-knowledge-99ced0adc4e3`）。运行时必须传 `--device-snapshot`。
8. **CSK6 烧录不看"像成功"**：`csk6-uart` 必须同时满足 WRITE SIZE = 实测字节数、设备 MD5 = 实测 MD5、
   4 个标记齐全、exit 0（`kb-knowledge-368ec0d51d2c`）；缺一项即 FAIL，不用其他项 PASS 抵消。
9. **设备版本串保留前导 `V`**：CSK6 打印 `venus ap appliction(V4.1.8@…)`，比较时按设备原文，
   不要在代码里偷偷去 `V`；真实差异（如 46.1.5 vs 46.1.8）照实记 FAIL/NEEDS_REVIEW。
10. **先上下电再开工具会错过 ROM 下载窗口**：NOAH-264/266 都是板子完整启动（`AP Boot Ready` /
    `venus ap appliction(...)`）之后 Uart_Burn_Tool 才开串口，连续 11 次 `CONNECT CHIP OVERTIME`。
    台架需要 `control.timing.power_sequence=connect_then_power`：先启动工具开 COM 等待 → 继电器断电/上电 →
    工具在 `tool_connect_timeout_s` 内出现 `CONNECT WITH CHIP SUCCESS` 才开始写；时序写进 `steps/burn-*.json`
    的 `timing` 与 `reset_log.jsonl` 的事件行，`checks.connect_marker` 不过即 FAIL。默认仍是 `power_then_connect`。
11. **一台台架的 approval_ref 不是重试许可证**：ledger 按 bench 记录，某台一旦 `write_started`，同一台换新
    `approval_ref` 也一律拒绝（NOAH-267：每台最多 1 次，失败切下一台）。

## 基线验收（2026-11-10 / pvm1005）

- profile：`tcl-pvm1005`；固件：CSK `V0.0.41`（fw.img `e2515c52…`）+ HISI `V8-H363T20-WFMV206.111.1.16`（`ed2eaf01…`）。
- 结果：`preflight → boot:venusa → burn:venusa → verify:venusa → burn:hisi → recover:hisi → verify:hisi → receipt` 全部 PASS。
- 版本回读：CSK `asr_dsp/SP-LVM141-A-ChXx-FX-V0.0.41`（boot `1.0.4`）、HISI `V8-H363T20-WFMV206.111.1.16`。
- 证据：任务目录 `tasks\noah-44\run`（step receipts、`tool_logs`、`serial_logs`、`reset_log.jsonl`、`run.receipt.json`）。
