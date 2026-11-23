# Profile 契约与 TCL / Media 隔离

## 1. 一个 profile = 一台实测台架

profile 是完整的现场描述，**没有任何继承机制**。加载器拒绝 `inherits`、`extends`、
`defaults_from`、`parent_profile`、`$ref`、`include` 这些键，哪怕值是 `false`。

```json
{
  "schema": "burner-profile/v1",
  "name": "tcl-pvm1005",
  "node": "pvm1005",
  "board_family": "tcl",
  "board": "TCL-CSK7-HI3863",
  "status": "measured",
  "time_contract": "beijing-no-offset",
  "control": { "...": "见第 2 节" },
  "chips": [ { "...": "见第 3 节" } ]
}
```

| 字段 | 规则 |
|---|---|
| `board_family` | `tcl` / `midea` / `generic` |
| `status` | `measured` 才允许写阶段；`draft` 必须给 `measurement_required` |
| `time_contract` | 只接受 `beijing-no-offset` |
| `preflight_window_s` | preflight 读串口窗口，默认 6 s |

## 2. control 块

`control.kind`：

- `serial-relay`：本机控制串口发命令。必须有 `port`（COMx）、`baud`、`commands.power_cut`、
  `commands.power_restore`、`receipt_tokens`（按命令前缀匹配回执 token）和 `timing`。
  `commands` 只允许 `power_cut` / `power_restore` / `strap_on` / `strap_off`。
- `external-script`：项目自己的驱动脚本（Media 用 `auto_burn.py`），profile 只声明 `script`。
- `manual`：人工断电，写阶段由人确认（本 Skill 不自动执行）。

`timing`（measured 的 serial-relay 必填，单位秒）：`cut_to_strap_s`、`strap_to_restore_s`、
`restore_to_strap_off_s`、`restore_to_verify_s`、`cycle_hold_s`。

电源/连接时序（draft 与 measured 都校验形状）：

| 字段 | 说明 |
|---|---|
| `power_sequence` | `power_then_connect`（默认，先上下电再启动工具，等价旧行为）或 `connect_then_power`（先启动工具打开烧录口等待 → 继电器上下电 → 等工具连上 → 写） |
| `tool_connect_lead_s` | `connect_then_power` 专用：工具启动后等多久再动继电器，默认 0 |
| `tool_connect_timeout_s` | `connect_then_power` 必填且 > 0：上电后等 `CONNECT WITH CHIP SUCCESS` 的最长窗口 |
| `post_power_wait_s` | `power_then_connect` 专用：上电后、启动工具前的额外等待，默认 0 |

`connect_then_power` 的时序证据写进 `steps/burn-<chip>.json` 的 `timing`（`tool_started_at` /
`power_started_at` / `power_done_at` / `connected_at` / `connect_marker_seen` /
`connect_failure_marker_seen` / `connect_timeout_s`）以及 `reset_log.jsonl` 的 `event` 行；
`checks.connect_marker` 不过则该步 FAIL（NOAH-264/266 的 `CONNECT CHIP OVERTIME` 正是窗口错过）。

## 3. chips 块

数组顺序就是执行顺序。每个 chip：

| 字段 | 说明 |
|---|---|
| `id` | 步骤名后缀，小写，如 `venusa` / `hisi` / `csk6` |
| `chip` | 目标芯片描述（`CSK7 / VenusA`、`HI3863 / WS63`） |
| `boot.required` + `boot.method` | 需要 boot strap 时用 `control-strap` |
| `transport` | `log_port` / `log_baud` / `burn_port` / `burn_baud` |
| `burn.driver` | `venusa-uart` / `hisi-burntool` / `csk6-uart`（已实现）；`csk6-batch` 未实测，measured profile 直接拒绝 |
| `burn.tool` / `burn.image` | 资产 id，指向 `assets/manifest.json`，不是路径 |
| `verify` | `version_key`、`require_keys`、`expect`、`window_s`、可选 `cross_check` |
| `recover` | `required`、`driver`（`ftm-clear` / `power-cycle` / `none`）、`before_verify` |

驱动细节：

- `venusa-uart`：`Uart_Burn_Tool.exe -b <burn_baud> -p <burn_port> -f <image> [-l -m -d -s] -a <image_addr>`，
  `image_addr` 必须是 `0x...`；成功标记来自工具输出（`MD5 CHECK SUCCESS`、`FLASH DOWNLOAD SUCCESS`）。
- `hisi-burntool`：`BurnTool.exe -com:<n> -bin:<file> -signalbaud:<baud> -chiptype:<id> -onlyburn:<part>...`，
  成功/失败词来自 optLog（默认 `成功`/`完成`、`失败`）。
- `csk6-uart`：CSK6 CH344 台架的 `Uart_Burn_Tool`。默认参数与 `venusa-uart` 同族
  （`-b <burn_baud> -p <burn_port> -f <image> -a <image_addr>`），实测参数不同时用 `burn.argv_template`
  覆盖（占位符 `{tool}` `{baud}` `{port}` `{image}` `{addr}`）。measured profile 必须同时给出：
  `image_addr`（`0x...`）、`success_markers`、`expected_write_bytes`（WRITE SIZE）、`expected_md5`（设备 MD5）；
  判定 = exit 0 **且** 标记齐全 **且** WRITE SIZE 相同 **且** MD5 相同，缺一项即 FAIL
  （`kb-knowledge-368ec0d51d2c`）。
- `burn.pre_power_cycle`（`csk6-uart`，可选布尔）：为 `true` 时在写之前做一次断电 → 上电，并全程镜像
  `transport.log_port`，回执里记 `power.cycle`、`power.serial_log` / `bytes` / `lines` / `error`，
  且上下电回执不齐即 FAIL；该芯片同时必须有 `control.kind=serial-relay`。默认 `false`，行为与旧版一致。
  顺序由 `control.timing.power_sequence` 决定（见 §2）：默认 `power_then_connect` 与旧版完全一致，
  `connect_then_power` 先启动工具持有烧录口再上下电，并用 `burn.connect_markers` /
  `burn.connect_failure_markers` 判定是否真的连上。
  （来源：NOAH-264 首跑 `burn:csk6` 连续 11 次 `CONNECT CHIP OVERTIME`、COM35 6 s 0 字节，
  无法区分“板子没进下载窗口”和“端口接错”；NOAH-266 复跑同一现象。）

## 3.1 csk6 + hisi dev01 台架

`scripts/profiles/csk6-hisi-dev01.json` 描述 win003 / win005 **共用的同一台** CSK6 + HI3863 CH344 四串口台架
（单台 `device` 块，历史批准 `NOAH-266` 已消耗，保留用于复现；新开工不用它）。

- **端口逐节点识别，禁止跨节点复用**（`kb-knowledge-99ced0adc4e3`）：
  win003 `control COM36 / csk6:log COM35 / csk6:burn COM34 / hisi COM32`；
  win005 `control COM15 / csk6:log COM36 / csk6:burn COM37 / hisi COM39`。
  profile 里的 COM 只是 win003 参考值，运行时必须传 `--device-snapshot`（role：`control`、`csk6:log`、
  `csk6:burn`、`hisi:log`、`hisi:burn`），否则会把日志口当烧录口。
- **控制**：沿用 `uut-switch1.on/off` 继电器上下电，非 tcl 族，因此必须显式 `control.allow_tcl_commands: true`
  ——这是声明，不是从 `tcl-pvm1005` 继承时序。
- **状态**：`draft`。写阶段一律拒绝；升级 `measured` 要补齐 `measurement_required`
  （CSK6 工具 sha256、burn_baud、镜像地址、V40.1.11 的 WRITE SIZE/MD5、预期版本原文、HISI 分区集等）。
- **验收口径**：7 项独立判定，整机结论取最差项（`kb-method-cad7be93134e`）；`module_only` 拓扑下约 13 s
  周期复位记 `EXPECTED_TOPOLOGY_BEHAVIOR`，不进整机最差项。
- **固件包**：`csk6-v40.1.11-package`（`V40.1.11_20261119094552.7z`，bytes `38827193`、
  sha256 `b70eca75…`）目前是 `node-attachment`，`oss.download=local-only`——没有可取得位置就不编造 OSS 路径；
  解包后的写入镜像需另登记。

### 3.1.1 多台架：每节点 4 台，逐台审批

`scripts/profiles/csk6-hisi-win003.json` / `csk6-hisi-win005.json` 把同一台架族扩成**每节点 4 台**（dev01–dev04），
端口来自 NOAH-268/269 只读清点，**一台一个批准、一台一个 ledger**：

```json
"commissioning": {
  "approved_by": "郑保山",
  "approval_ref": "NOAH-267",
  "expires_at": "2026-11-26 23:59:59",
  "benches": [
    {"bench_id": "dev02", "node": "win003",
     "serial": "USB\\VID_1A86&PID_55D5\\6&1655da82&0&1",
     "topology": "UNVERIFIED",
     "control_port": "COM41",
     "log_ports": {"csk6": "COM38", "hisi": "COM39"},
     "burn_ports": {"csk6": "COM40", "hisi": "COM39"},
     "approval_ref": "NOAH-267-win003-dev02",
     "note": "NOAH-269 清点：hub 根口 2，无 iSerial"}
  ],
  "pins": { "…": "同单台写法" },
  "allowed_steps": ["burn:csk6", "verify:csk6", "burn:hisi", "verify:hisi"],
  "max_write_steps": 2,
  "max_runs": 1
}
```

| 规则 | 说明 |
|---|---|
| `device` 与 `benches` 二选一 | 同时出现即加载失败；单台旧块保持原样可解析 |
| `--bench <bench_id>` | 多台 profile 必填；选中哪台就驱动哪台的 `control_port` / `log_ports` / `burn_ports` |
| 每台自己的 `approval_ref` | `--commission` 必须等于**选中那台**的 ref，不匹配即退出码 3 |
| 每台自己的 ledger | `%TEMP%\burner-<profile>-<node>-<bench_id>-commissioning.json`；单台旧块的 ledger 名不变 |
| 一台只写一次 | 该 bench 的 ledger 一旦有 `write_started`，换新 `approval_ref` 也拒绝（NOAH-267：每台最多 1 次） |
| 台架身份 | `serial` 写 `usb_parent`（dev02–dev04 无真实 iSerial，COM 会变）；`usb_parent` 只在节点内唯一——win005 dev01 与 win003 dev03 是同串 |
| `--device-snapshot` 刷新端口 | 只接受带同一 `bench_id`（或同一 `usb_parent`）的文档；无身份信息、或指向另一台，一律拒绝（退出码 3），避免 COM 漂移被悄悄用于另一台设备 |
| `plan` | 不带 `--bench` 列出 4 台；带 `--bench` 渲染选中台的端口、时序开关与将执行的写步骤，全程零设备动作 |
| `promote` | 只产出**被选中那台**的 measured profile（`name` 加 `-<bench_id>`、端口换成该台、`nodes` 缩成该节点、删掉 `benches`） |

## 3.2 draft → measured：commissioning（首跑闭环）
draft profile 的写阶段一律拒绝，而 measured 必须给出的 `expected_write_bytes` / `expected_md5` /
`verify.expect`（以及 `control.timing.cycle_hold_s`）只有**真跑一次**才拿得到——直接用这两条规则会形成死循环。
闭环方式是**一次受控首跑**，证据来自这次运行本身，状态不手改：

1. profile 里由真人写一个 `commissioning` 块（`status` 必须是 `draft`，measured profile 带这个块直接报错）。
   块里的每一个字段都是审批范围的一部分：**只批一台 bench、每个 tool/image 的确定 sha256、一次首跑**
   （单台用 `node` + `device`，多台用 `benches[]` 每台一条，见 §3.1.1）：

```json
"commissioning": {
  "approved_by": "郑保山",
  "approval_ref": "NOAH-263",
  "approved_at": "2026-11-19 17:00:00",
  "expires_at": "2026-11-21 23:59:59",
  "node": "win003",
  "device": {
    "serial": "USB\\VID_1A86&PID_55D5\\0123456789",
    "topology": "module_only",
    "control_port": "COM36",
    "burn_ports": {"csk6": "COM34", "hisi": "COM32"}
  },
  "pins": {
    "csk6": {"tool": {"asset": "csk6-uart-burn-tool-v1.0.5", "sha256": "4bfdf680…"},
             "image": {"asset": "csk6-v40.1.11-fw-img", "sha256": "db0a618d…"}},
    "hisi": {"tool": {"asset": "hisi-burntool-gold", "sha256": "4bd51bc3…"},
             "image": {"asset": "hisi-ws63-v40.1.11-all-fwpkg", "sha256": "d973c3c7…"}}
  },
  "allowed_steps": ["burn:csk6", "verify:csk6", "burn:hisi", "verify:hisi"],
  "max_write_steps": 2,
  "max_runs": 1,
  "note": "首跑测量：只写一次，失败即停，不自动重试"
}
```

| 字段 | 规则 |
|---|---|
| `approved_by` | 审批人，非空；`promote` 会把它写进 measured profile 的 `verified.by` |
| `approval_ref` | 审批 Issue/记录号，必须与 `--commission` 完全一致 |
| `approved_at` | 可选，北京时间；只作审计 |
| `node` | 审批覆盖的执行节点；必须是 profile `nodes` 列表成员，运行时 `--node` 必须与它一致 |
| `device` | 单设备钉子：`serial`（USB hwid/serial）、`control_port`、每个芯片的 `burn_ports`；与 profile 实际端口不符即拒绝 |
| `pins` | 每个写入芯片的 `tool` / `image`：资产 id + sha256；资产 id 必须等于 profile 实际使用的，sha256 必须等于 manifest 声明值，运行前还要与实际文件复算一致 |
| `allowed_steps` | 本次允许的步骤（`preflight`/`receipt` 恒允许），白名单之外一步都不许跑 |
| `max_write_steps` | 写步骤上限，只允许 1 或 2 |
| `max_runs` | 固定为 1：首跑不是重试许可 |
| `expires_at` | 北京时间 `YYYY-MM-DD HH:MM:SS`；**过期是写门禁不是加载门禁**：profile 仍可解析、`plan`/`preflight` 仍可用（plan 打印 `[EXPIRED - writes refused until a new approval]`），写步骤退出码 3；过期要重新审批，不能靠重跑绕过 |
| `note` | 可选，只作说明 |

2. 首跑：`burn.py all --commission <approval_ref> --node <approved node> --confirm-write`（多台 profile 再加
   `--bench <bench_id>`）。条件缺一不可；
   `--commission` 只接受 `all`（`plan` / `preflight` 只读可用），因为它必须**从头按序**跑完整个 plan，
   `preflight` 先跑：枚举端口、核对资产 SHA、核对钉住的端口——任一不过即停，**设备零动作**。
   写第一个写步骤之前，先在每台 bench 的 ledger（`%TEMP%\burner-<profile>-<node>-<bench_id>-commissioning.json`，
   单台旧块为 `%TEMP%\burner-<profile>-<node>-commissioning.json`）登记
   `write_started`；同一台 bench 再次运行会被拒绝，代码里没有任何自动重试。
   `verify` 因为没有 `verify.expect` 而以“测量模式”判分：只要求版本键/启动行/上下电回执齐全，并把设备原文原样记下来。
3. 跑完在 run 目录落 `commissioning.receipt.json`（schema `burner-commissioning/v1`），记录 node、执行设备、
   pins、逐芯片 WRITE SIZE、设备 MD5、四个成功标记、exit、工具/镜像 SHA、实际 argv、
   HISI optLog（bytes + sha256）、期望版本原文、`window_s`、`reset_log.jsonl` 里量到的实际 `cycle_hold_s`
   与全部控制回执，以及 `resources`（证据体积、磁盘余量、工具 exit/超时、串口日志字节数）；
   `complete=false` + `missing` 会写明缺哪项。
   CSK6/VenusA 按 flash 数字判定（exit 0 + 4 标记 + WRITE SIZE + 设备 MD5）；
   HISI 按 optLog 成功 token + 无失败 token + 4 条上下电回执判定（BurnTool 的 exit code 只记录不判分）。
4. `burn.py promote --profile <draft> --from <commissioning.receipt.json> --out <measured.json>`：   只把回执里的值搬进 profile（`expected_write_bytes`、`expected_md5`、`verify.expect`、`window_s`、
   `control.timing.cycle_hold_s`、缺失的 `transport.burn_baud`/`burn_port`、`burn.image_addr`，以及 HISI 实际
   argv 里的 `-signalbaud` / `-chiptype` / `-onlyburn`），然后 `status` 改 `measured`、删掉 `commissioning`
   与 `measurement_required`、写 `verified`。任一分级证据缺失、回执来自另一个 profile 文件（SHA 不符）、
   approval_ref / node / device / pins 与审批不符，都会**拒绝生成**（退出码 3），且不留下半成品文件。
5. `promote` 产出的文件必须能通过 measured 的全部校验才会落盘；把它的 diff 走正常 PR 审阅，不手改状态。
   多台 profile 的 `promote` 按回执里的 `approval_ref` 找到那一台 bench，只产出该台的 measured profile（§3.1.1）。

口径补充：`control.timing` 的 `cut_to_strap_s` / `strap_to_restore_s` / `restore_to_strap_off_s` 只在有
`boot.required` 芯片时才是必填——没有 boot strap 的台架（如 dev01 双芯片）根本不会发 `strap_on/off`，
强行要求等于逼人编数字；`cycle_hold_s` 始终必填，由首跑实测。

## 4. 族隔离规则（加载时强制）

1. `board_family: tcl` 的 `control.forbidden_commands` 必须包含 `uut-reset`、`uut-csk-reset`、`uut-burn`；
   `control.commands` 里出现任何被禁命令即拒绝。
2. 非 tcl 族只要出现 `uut-*` 命令就拒绝，除非 profile **显式**写 `control.allow_tcl_commands: true`——
   这是“自己声明”，不是从 TCL 继承。
3. `boot.required` 的 chip 必须能在本 profile 的 `commands` 里找到 `strap_on`/`strap_off`。
4. 没有默认值：缺 `port`、`baud`、`expect` 等字段就报错，不会回落到任何其他 profile 的值。

## 5. TCL 与 Media（美的）差异

| 维度 | TCL (`tcl-pvm1005`) | Media (`midea-csk6`, draft) |
|---|---|---|
| 控制方式 | 本机控制串口 `COM6`，`uut-switch1.on/off` + `uut-csk-boot.on/off` | 项目自己的 `auto_burn.py`（`external-script`），不声明本机控制命令 |
| 目标芯片 | CSK7 + HI3863 双芯片 | CSK6（`csk6-batch`，未实测） |
| 串口/波特率 | COM3@921600 / COM4@921600，BurnTool `-com:4` | 全部为 `null`，待现场测量 |
| 烧录工具 | `Uart_Burn_Tool 1.0.10` + `BurnTool_Gold` | `CSK6_Burn.bat`（本 Skill 不内置驱动） |
| 工厂模式恢复 | `AT+FTM=0` @115200，且必须在 921600 校验前 | 不适用，禁止继承 TCL 的 FTM 时序 |
| 断电/上电时序 | `cut 2s → strap → 1s → restore → 8s → strap off` | 由 Media 脚本自行控制 |
| 状态 | measured（2026-11-10 回归 PASS） | draft，写阶段被拒绝 |

### Media 独立验收项（与 TCL 回归分开）

1. 在 Media 台架测出控制脚本路径、log/burn 端口与波特率、断电/恢复方式。
2. 把 CSK6 工具与镜像登记进 `assets/manifest.json`（含 SHA-256），再改 profile 引用。
3. 实现并单测 `csk6-batch` 驱动后，才能把 `status` 改为 `measured`。
4. 独立跑一次 Media 基线回归并生成自己的 receipt；不得复用 TCL 的 receipt 或归档。
5. 差异复核：Media profile 中不得出现 `uut-switch1`、`uut-csk-boot`、`COM6`、`AT+FTM=0` 等 TCL 值
   （`tests/test_profile.py::MediaIsolationTests` 守着）。

## 6. 新增一台台架的最小流程

1. 现场测端口、波特率、断电/恢复方式、启动版本串。
2. 复制最接近的 profile，改 `name`/`node`/`board_family`，`status` 先写 `draft`。
3. 把工具与固件登记到 `assets/manifest.json`（sha256 必须是实测值）。
4. `plan` + `preflight` 跑通后，补全期望版本，改 `status: measured`。
5. `all --confirm-write` 回归，证据进任务目录，receipt PASS 才算该台架可用。

## 7. 设备端口来源：`--device-snapshot`

主入口 `scripts/burn.py` 增加可选参数 `--device-snapshot FILE`，五个子命令都能用。
给了它就以文档为准，覆盖 profile 的 `transport.log_port` / `transport.burn_port` 与
`control.port`；**没给时行为与旧版完全一致**（端口仍取 profile）。

### 7.1 接受的文档

| schema | 来源 | 消费方式 |
|---|---|---|
| `ark.device-io.v1` | `device-io ports --format json --out FILE` | 原始快照，按 profile 已声明的端口名/interface 标签匹配 |
| `ark.device-role-map.v1` | `device-io roles --spec spec.json --format json --save FILE` | 直接取 `roles[role].port` |

按顶层 `schema` 分派；其他 schema 直接失败。解析只读 JSON，本 Skill 不枚举 USB/串口。

### 7.2 role 命名

| role | 覆盖的 profile 字段 |
|---|---|
| `<chip>:log` | `chips[].transport.log_port` |
| `<chip>:burn` | `chips[].transport.burn_port` |
| `control` | `control.port`（仅当 profile 声明了控制口） |

双芯片（`tcl-pvm1005`）用 `venusa:log`、`venusa:burn`、`hisi:log`、`hisi:burn`、`control`。
单芯片 profile（如 `midea-csk6`）额外接受裸 `log` / `burn`。双芯片 profile 里出现裸
`log` / `burn` 会硬失败并提示改用 `<chip>:log` / `<chip>:burn`。

### 7.3 role-map 解析规则

```json
{
  "schema": "ark.device-role-map.v1",
  "generated_at": "2026-11-11 18:00:00",
  "spec_schema": "ark.device-roles-spec.v1",
  "ok": true,
  "roles": {
    "venusa:log": {"state": "resolved", "port": "COM13", "reason": "matched interface SERIAL-A"},
    "venusa:burn": {"state": "resolved", "port": "COM13", "reason": "matched interface SERIAL-A"},
    "hisi:log": {"state": "resolved", "port": "COM15", "reason": "matched interface SERIAL-B"},
    "hisi:burn": {"state": "resolved", "port": "COM18", "reason": "matched interface SERIAL-C"},
    "control": {"state": "pinned", "port": "COM16", "reason": "caller pinned COM16"}
  }
}
```

- `state` 为 `resolved` / `pinned` 且 `port` 非空 → 采用该端口。
- `state` 为 `ambiguous` / `missing` / `conflict`，或 role 缺失，或顶层 `ok: false`
  → 硬失败，报错带 role、state、`reason`。
- 同芯片的 log 与 burn 允许是同一个物理口（TCL 的 VenusA 就是 COM3 同时做打印与烧录），
  端口重复本身不算错误；是否冲突由 device-io 的 `state` 判定。

### 7.4 原始快照解析规则

- 只用 profile 里**已经写下的**端口名（`COM6`、`/dev/ttyUSB0`）或 interface 标签，
  与 `ports[].device` / `ports[].interface` 做**大小写不敏感的精确相等**比较。
- `present: false` 的端口不参与匹配。
- BurnTool 的 `-com:<n>` 数字形式与 `COM<n>` 是同一个设备：profile 的 `burn_port` 写 `4`
  时按 `COM4` 匹配（只做这一种等价，不做前缀/序列号/模糊猜测）。
- 0 命中 → `missing`；多于 1 个不同 device 命中 → `ambiguous`；恰好 1 个 → 采用。
- profile 里该 role 没有声明端口（如 draft profile 的 `null`）→ 硬失败并提示改用 role-map。

### 7.5 输出与失败

`plan`（含 `--dry-run`）在文本末尾追加块，`preflight` 的 step JSON 增加同名键：

```
device snapshot: /path/role-map.json
  source: /path/role-map.json
  schema: ark.device-role-map.v1
  generated_at: 2026-11-11 18:00:00
  mode: role-map
  roles:
    venusa:log: COM13 (resolved)
    ...
```

```json
"device_snapshot": {
  "source": "/path/role-map.json",
  "schema": "ark.device-role-map.v1",
  "generated_at": "2026-11-11 18:00:00",
  "mode": "role-map",
  "roles": {"venusa:log": {"state": "resolved", "port": "COM13", "reason": "..."}}
}
```

不打印整份快照。**退出码沿用既有约定**：快照读不了、schema 不支持、任一 role 解析失败
都按 profile/资产类错误返回 **2**，stderr 只打一行可执行的错误；不会回落到 profile 端口。

```bash
python scripts/burn.py plan --profile scripts/profiles/tcl-pvm1005.json \
  --run-dir /home/zhsh/workspace/tasks/<task>/run --device-snapshot role-map.json
```
