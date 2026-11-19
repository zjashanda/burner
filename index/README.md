# burner/index · 烧录能力固定索引

> Skill：`burner`（仓库 `zjashanda/burner`，frontmatter name `burner`）
> Schema：`ark.skill-index.v1`（机器）/ `ark.skill-route.v1`（单条能力）
> 维护：只加不删；改能力必须同时改 `index.json` 与 `SKILL.md` 的对应段落。

## 1. 智能体必须先读索引

```
burner/index/manifest.json → burner/index/index.json → 选中 capability → workflow → profile
```

禁止从 `SKILL.md` 正文猜 profile、工具或判定规则；`SKILL.md` 解释**为什么**，索引回答**用哪个**。

## 2. 分流：烧录 vs 测试

| 任务 | 归属 | 理由 |
|---|---|---|
| 写固件、换版本写入、烧后读版本、FTM 恢复 | `burner` | 只改变设备固件状态 |
| 烧后上下电验证、唤醒/命令词、OTA 后稳定性、音频、网络、资源监测 | `lstest` | 需要测试逻辑与判定 |
| 烧录 + 验证的组合任务 | `burner` → `lstest` | 组合只在任务编排层，能力不复制 |

**不新建** `csk6-hisi-burner` 之类按芯片命名的顶层 Skill；芯片差异写在 `chips` 与 profile 里。

## 3. 路由字段

每条 capability 固定 14 个字段：
`capability_id`、`skill`、`module`、`chip`、`project`、`firmware_family`、`profile`、`workflow`、
`inputs`、`outputs`、`preconditions`、`evidence_schema`、`version`、`status`。

路由顺序：`capability → skill → chip → project → profile → workflow → preconditions → execute → evidence → ark-kb`。

## 4. 当前能力

| capability_id | 适用 | workflow | 状态 |
|---|---|---|---|
| `burn.tcl-dual-baseline` | tcl-pvm1005 双芯片基线 | `wf.burn.tcl-dual-baseline` | active |
| `burn.single-chip-venusa` | 只烧 CSK7/VenusA | `wf.burn.single-venusa` | active |
| `burn.single-chip-hisi` | 只烧 HI3863/WS63 | `wf.burn.single-hisi` | active |
| `burn.version-swap-verify` | 换版本回归 | `wf.burn.version-swap` | active |
| `burn.recover-ftm-clear` | HISI 烧后 FTM 恢复 | `wf.burn.recover-only` | active |
| `burn.port-asset-preflight` | 只读预检 | `wf.burn.preflight-only` | active |
| `burn.csk6-hisi-dev01` | CSK6+HISI CH344 dev01 台架（win003/win005） | `wf.burn.csk6-hisi-dev01` | draft |

`midea-csk6` 只有 `burn.port-asset-preflight`：profile 是 `draft`，写阶段一律拒绝。

`csk6-hisi-dev01` 的写驱动 `csk6-uart` 已实现并有单测，但 profile 仍是 `draft`：
写阶段拒绝，只允许 `plan` / `preflight`；升级 `measured` 必须由节点实测证据驱动
（WRITE SIZE、设备 MD5、逐节点端口映射、启动版本原文）。判定拆成 7 项独立判定，
整机结论取最差项（`kb-method-cad7be93134e`）。

## 5. 前置检查（任何写阶段之前）

1. profile 存在且 `status=measured`；`draft` 只允许 `plan`/`preflight`。
2. 资产 `sha256` + `bytes` 全部通过；缺资产 → BLOCKED，不猜下载地址。
3. 台架互斥锁空闲；被占用退出码 3，不杀进程。
4. 写阶段显式 `--confirm-write`。
5. 端口优先来自 `device-io` 快照/role-map；给了就无回落。

## 6. 证据

- 产物：`steps/*.json`、`tool_logs/`、`serial_logs/`、`reset_log.jsonl`、`run.receipt.json`
- 回执 schema：`burner-receipt/v1`（长期） / `burner-phase/v1`（单步）
- 知识关联：`ark-kb` 条目的 `skill_ref=burner`、`capability`、`profile`、`workflow`

## 7. 新增能力的最小步骤

1. 在 `index.json` 的 `capabilities` 加一条（14 个字段全填）。
2. 需要新流程时加 `workflows` 条目，`entry` 必须是仓库里真实存在的命令。
3. 新芯片/新 profile 同步 `chips` / `projects` / `compatibility`。
4. 跑 `python -m unittest discover -s tests -v`、`python scripts/lint_scan.py --strict`、`python tools/time_lint.py --gate`。
