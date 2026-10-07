# 代码策略 Player（轻量版本）

Player 先做基础启动预检，再读取源码、对应版本的设计与 Builder 摘要，编写或适配已有策略；框架运行
真实游戏并录制三次游玩；Player 最后回看状态、输入和画面，输出体验报告。
运行中的每个动作不调用大模型。Designer 每轮评估玩法、美术和一致性，Builder 做
改动相关的关键自测，广泛体验验证交给 Player。资产接口与 Reviewer 双顺序晋升规则不变。

## 设计、实现与策略继承

- Designer 继续输出 `hypothesis/changes/acceptance/assets`，明确资产是复用、修整还是
  生成/替换，不强制每轮生成或大改。历史反馈附带 Builder 摘要，但实现者陈述不是独立证据。
- Builder 的 `summary` 按实际改动、资产集成、已执行关键测试、剩余问题、Player 验证重点组织。
  integrate 收到 draft 摘要，避免重复全面试玩；没有新增自测时间或次数预算。
- policy Player 的 `handoff` 来自正在测试的版本：旧版使用旧设计和旧 Builder 摘要。
  缺少历史交接材料时字段可为空，保持旧运行兼容。
- 新候选优先从当前最佳版本继承 `policy.gd`（Godot）或 `policy.py`（HTML）、profile 计划和简短执行反馈；被拒候选
  不会自动成为基础。Player 在新任务副本中沿用或局部修改，历史策略与游戏保持不变。
- 游戏版本、执行设置、设计和实现上下文完全相同时，可以跳过策略编写；新版本仍调用
  一次 `policy_author` 检查适配。历史报告没有上下文记录时只作为参考，不自动跳过检查。
- 默认三个目的为核心流程回归、本轮设计重点、替代策略与边界恢复，数量仍由配置决定。
  policy 复用不等于录像复用：新试玩任务仍执行配置的所有 episode；仅同一任务恢复时
  复用已完成且校验通过的 episode。
- 集中分析明确交代玩法、美术、一致性、覆盖范围及策略变化。实际截图没有展示的内容
  标为未覆盖；策略变强不能直接等同游戏变好。失败记录保留，不重试至成功后挑选结果。

通用体验报告字段不变，未增加 Agent 或策略自动修复循环。HTML 的旧 GUI
模式保留原协议；Godot 的新执行统一使用引擎内 GDScript Policy。

## 配置与兼容性

新任务在调用 `policy_author` 前，使用正式配置的运行器在独立副本中执行
`start → reset → observe`，保存 `preflight/result.json`、`initial.png` 和 `engine/`
日志。原工程与正式存档不受影响；GameCraft fresh-launch 沿用其不 reset 的启动约定。
预检结果包含初始状态、实际视口及输入能力，不把这些框架字段塞进游戏的 `state`。

基础启动、桥接、reset、状态序列化或清理失败时，保存错误并在策略编写前停止。
能截图时保留诊断画面；启动过早失败可能没有截图。修复后需建立新的 Player 任务，
不覆盖失败记录。用户主动中断的预检会在恢复时归档并重做；已通过且校验一致的预检
只做一次。已有冻结策略和完成报告仍按原恢复路径处理。

预检不调用模型、不增加正式试玩条数，只验证正常初始状态。具体 scenario、策略输入
与完整玩法仍由正式 episode 检查；预检通过不代表试玩成功或满足 benchmark。

新建实验默认使用：

```json
{
  "player": {
    "mode": "policy",
    "trajectories_per_version": 3,
    "parallel_episodes": 3,
    "policy_call_timeout_seconds": 30
  }
}
```

这些字段放进正常的 HTML/Godot 配置即可，原 start/resume 命令不变。
只配置部分字段时，其余使用默认值。HTML 沿用已有 `mode` 设置；Godot 即使旧配置
写着 `gui`，新试玩也使用 GDScript。已有完整报告仍可只读恢复；需要继续执行的旧
Godot Python 策略会保留历史目录并建立新的 Player 任务，由 Player 编写/适配 `.gd`，
不会把 Python 当 GDScript 执行。不会修改正在运行的实验或已有游戏。

`trajectories_per_version` 是正式游玩次数，不是成功次数。每个版本默认三次；
存在 incumbent 时，本轮 candidate 和 incumbent 各三次，两次匿名比较复用证据。
种子按配置 `seed + episode_index` 分配。三种 profile 有不同目的，不保证实际
行为一定不同，Player 必须如实说明覆盖范围。

`parallel_episodes` 是同时运行的轨迹数，默认 1；显式设置为 2 或 3 可并行执行。
策略仍只生成一次，各轨迹使用独立引擎、策略实例与证据目录，全部结束后才统一分析。
Godot 策略与自己的游戏共用进程；HTML 策略仍使用独立 Python 工作进程。
超过并发数的轨迹排队，报告始终按计划编号排列，不按完成先后排列。
candidate 和 incumbent 仍顺序评测，并行发生在同一版本的各条轨迹内部。
恢复旧 policy 会话时，缺失此字段也使用默认并发数；并发数可调整，不重新生成策略。

`policy_call_timeout_seconds` 限制 HTML 策略工作进程响应、Godot 引擎 RPC 健康响应等待，
用于处理死循环或失联，不是游戏时长限制。普通 rollout 和每局没有新增固定时限；
显式 `benchmark.name=gamecraft` 的每局最多20秒，见下节。
现有 `max_actions` 在该模式下按每次游玩应用；达到上限记为 truncated，不记为胜利。
修改已有 policy 会话的种子、次数等设置会报错，避免混用测试条件。

## 可选 GameCraft 执行配置

配置、镜像准备及产物位置见 [README](benchmarks.md)。只有显式标记的任务
追加 `prompts/player_gamecraft.md`，普通 Player 提示词只负责通用游玩和四方面反馈。
Godot 策略仍是 `.gd` 决策代码，宿主把真实输入转换为官方 JSON，不让模型手写未经执行的轨迹。
计划包含 `goal` 和 nullable `scenario`。每条最多20秒，全部片段共同覆盖公开任务；不要求每条获胜。
仅 GameCraft 新计划增加 `coverage`，Player 先读取 `handoff.game.brief`，按公开需求分配 episode，
再实现策略。每项包含 `id`（R1…）、`requirement`、`episodes`（从1开始）、`expected`、
`omission_reason`（已安排时可为空；未安排必须解释）。每条 episode 至少对应一项需求，
编号越界、重复 ID、遗漏原因缺失会被拒绝；完全相同的分工只提醒，不自动重试。
失败保留，不筛掉差的轨迹。
原始记录结束后自动做官方离线重放，再统一分析两份证据；不自动调用付费裁判。
录制与重放固定同一 GameCraft 镜像 ID，但原生注入和 X11 输入不同，仍可能出现时序差异。

`gamecraft-goals.json` 保存 Player 基于证据的结果判断，宿主检查编号、路径、文件哈希和时间，
不把该判断说成独立通关验证。`won`、`not_won`、`unverified` 分别表示有证据支持获胜、
观察到未获胜、证据不足；策略 completed、JSON 导出成功和录像生成都不是胜利判断。
购买或导航等非获胜型目标使用覆盖结果，不能因 `not_won` 就认定游戏故障。

现有分析调用同时回填 `coverage-results.json`，按计划逐项记录 `recording` / `replay` 的
`shown`、`partial`、`not_shown`、`unverified`，以及原因和真实证据。`shown` / `partial`
至少引用对应执行的一张 PNG，不能拿原始成功画面替代重放证据；源码存在不等于实际展示。
宿主将相对证据路径和文件哈希绑定到版本及计划，并在 `play.json.gamecraft_coverage` 保存引用。
该报告属于 Player 的证据判断，不是官方分数或独立自动验收；不增加模型调用、自动补录或评分。
旧计划没有覆盖表时不要求新文件，仍可恢复；新建 GameCraft 作业补齐计划，可继续参考旧策略。
普通 HTML/Godot Player 的计划、提示词和时限不受影响。
Reviewer 与原有晋升规则保持不变。

## 执行环境

- 模型仍使用配置的 Harbor 或本地 Codex 后端。
- Godot 的 `.gd` 在游戏进程中执行；`runtime.mode=docker` 时复用已有运行镜像和 Xvfb，
  每局一个容器，不再额外启动 Python 策略容器。`local` 则使用本地 Godot。
- HTML 的真实 Python 策略需要 Docker，可复用包含 `python3` 的 `harbor_image`；
  即便模型后端是本地 Codex，也保留这一隔离。
- HTML 策略容器无网络、根文件系统只读、无额外 capabilities，使用非 root 用户。
  只挂载冻结的 `policy.py` 和框架工作进程脚本，不挂载游戏、凭据、证据或 Docker socket。
- Godot 策略只应通过 JSON 状态与动作接口控制游戏，框架对观测做深复制，禁止策略
  修改游戏节点、存档、资源或终局状态。这是生成代码的接口约定，不是 GDScript 内部
  权限沙箱；容器隔离宿主，原项目只读挂载，运行副本独立保存。
- 模型读取一份过滤过的源码副本。Harbor 以只读方式挂载；本地 Codex 不给该目录
  `--add-dir` 写权限。源文件另设只读权限并核对哈希；原始候选从不作为策略写入目录。
- Mock 后端运行固定的本地测试策略，其图像和录像明确标为模拟证据，不能作为真实试玩证据。
- 不静默拉取镜像；依赖不齐时记录失败。没有后台服务、策略训练或新 Agent。

## 策略接口

Godot Player 生成一个 `policy.gd`，继承 `RefCounted`，支持计划中的各个 profile：

```gdscript
extends RefCounted
var profile := ""

func reset(_seed: int, selected_profile: String):
    profile = selected_profile

func act(state: Dictionary):
    # 字段必须来自当前游戏的 agent_observe；这里只示意接口。
    if state.get("finished", false):
        return {"action": null, "done": true, "reason": "观察到目标完成"}
    return {"action": {"keys": ["ArrowRight"], "duration_ms": 100, "pointer": null},
            "done": false, "reason": "向右移动"}
```

框架在 Godot 物理更新中调用策略、保持输入并记录反馈；宿主只查询进度，不逐动作往返
Python RPC。动作时长使用实际经过时间，不改变游戏物理频率或 `Engine.time_scale`。
实际输入与鼠标松开时间由引擎记录，另保存物理帧编号用于排查；导出不把物理帧号
直接当成官方 30 FPS 录像帧号。

HTML Player 继续生成一个标准库 Python 文件：

```python
class Policy:
    def reset(self, seed, profile):
        # 每次游玩重新初始化状态。
        self.profile = profile

    def act(self, state):
        # 根据当前真实状态决定动作；以下只是返回格式示意。
        return {
            "action": {"keys": ["ArrowRight"], "duration_ms": 100, "pointer": None},
            "done": False,
            "reason": "当前测试目标需要向右移动"
        }
```

结束时返回 `{"action": null, "done": true, "reason": "实际结束依据"}`。
`state` 来自现有 HTML `GameAgentBridge.observe()` 或 Godot `agent_observe()`。
第一版不要求修改 Builder 或游戏状态 schema；Player 从源码理解实际字段。
字段不够时应报告测试覆盖限制，不能编造状态或跳过游戏逻辑。

按键集合持续保持到下一次更新；空集合释放。单次触发的按键需要先释放才能再触发。
鼠标坐标仍为 `0..1`，默认点击保持原有按下、等待、释放行为，新增可选 `phase`：

| phase | 行为 |
|---|---|
| 省略或 `click` | 按下并在本动作结束释放；`button: "none"` 只移动 |
| `down` | 按下并跨动作保持；重复同一个 down 不重复按下 |
| `move` | 移动并保持当前按钮状态，不产生点击 |
| `up` | 移动到给定位置并释放指定按钮 |

例如依次使用 `{"x":0.2,"y":0.3,"button":"left","phase":"down"}`、
`{"x":0.6,"y":0.7,"button":"none","phase":"move"}`、
`{"x":0.6,"y":0.7,"button":"left","phase":"up"}`。
`pointer: null` 保留正在按住的鼠标状态；当前接口最多保持一个鼠标按钮，不支持在
持续拖动中插入另一次 click。reset、异常或结束会释放全部输入。HTML 坐标对应浏览器
交互视口，Godot 对应逻辑视口，预检提供具体宽高与单位；录像分辨率可能不同。
GameCraft 对这些新动作使用实际输入事件导出；旧轨迹缺少事件文件时拒绝按点击推导拖动。

游戏的菜单、控制、碰撞和规则正常运行。
策略不能直接修改游戏坐标、生命、资源或终局标志。宿主机记录输入意图和成功执行后的反馈。

这是实时控制，没有实现加速仿真。第一版提供语法检查和正式执行后的诊断，未增加
自动策略修复循环或交互式调试工具；策略错误会保存并在回看中报告，失败批次不会晋升。

## 录屏与回看

状态读取不触发截图。策略控制、游戏物理更新和影像采样彼此分开。
保留原录像格式 **640×360、2 FPS**：Godot 定时采集，HTML 使用已有 Playwright
原生录像并在结束后转换为 2 FPS；本次没有更改浏览器原生录制机制。

保存每局首尾截图；录像完成后用 FFmpeg 每 10 秒提取一个关键帧，并保留视频时间戳。
Player 回看整个汇总、各局轨迹和关键帧，可自行在证据目录外从录像提取更多帧。
分析需要引用每个有画面 episode 的实际 PNG。引用校验只保证文件存在，不能证明模型
理解正确。没有视觉证据、版本不匹配、策略/引擎失败或截断的批次不能晋升。

Player 的 `evidence` 每项只能是相对于角色共享目录的一个文件路径，例如
`evidence/episodes/0001/frame-0000.png`；Reviewer 使用 `A/...`、`B/...`。
宿主在 `input.json` 的 `evidence_paths` 中列出已有文件，输出 schema 也将这些
相对路径作为可选值。不得附加绝对路径前缀、Markdown 或说明文字；解释放在报告正文。

轨迹使用 episode 相对时钟，录像帧使用 video 相对时钟；启动与 reset 可能导致零点不同。
报告明确区分它们，不承诺逐帧精确同步。2 FPS 影像不能证明实时帧率或细微卡顿。

## 保存与恢复

每轮 `jobs/player-<side>-*/` 下保存：

```text
policy.gd / policy.py            Godot / HTML 的冻结策略代码
plan.json                       各 profile 的测试目的
policy.json                     版本、配置、哈希和 inheritance（new/reused/adapted）
preparation.json                冻结的版本交接信息与父策略引用，新会话才有
inherited/                      基础版本的策略、profile 计划、执行摘要副本
author-*/                       Player 编写策略的模型任务与只读源码副本
episodes/0001/attempt-*/
  trajectory.jsonl              状态、实际输入、反馈和结束原因
  engine/                       原始截图与录像
    runtime.json                Godot 原版本、运行副本与实际用户数据目录
    runtime-*/project/          Godot 本次试玩副本（含该局 res:// 存档）
    policy-trajectory.jsonl     Godot 引擎内记录的原始输入与反馈
  keyframes/                    录像提取帧
  recorded.json                 录制完成检查点
  result.json                   单次游玩报告
episodes/0001/completed.json     完成凭据；其他 episode 同结构
analysis-*/                     Player 回看任务和证据副本
analysis.json                   最终分析
execution.json                  本次采用的并发数与轨迹总数
play.json                       全批次报告
```

中断恢复复用冻结策略和完整 episode，只重启未完成的一局。原失败/中断尝试保留。
如果所有 episode 已完成，只有报告引用格式失败，恢复先尝试离线修复已保存的
模型输出：核对原始证据哈希、完整操作轨迹和分析证据副本，将能唯一对应到文件的
旧绝对路径及冒号说明转换为纯相对引用，并检查每条轨迹的截图引用。
原模型输出不覆盖；`analysis-recovery.json` 保存引用转换记录。此过程不运行模型
或游戏，报告保留录制时的环境标识。缺失、被修改或有歧义的证据不会通过恢复。
只有这种完整记录的报告恢复允许使用历史环境；需要继续执行策略或游戏的会话仍
严格检查当前环境与冻结 settings 一致，不将旧录像改标为新环境运行结果。
录制完成后即使在提帧时中断，恢复也只继续后处理，不重新执行游戏。
每次游戏结束都会释放输入、关闭引擎及策略工作进程。容器所有权标记支持恢复时清理
该 Player 遗留的容器，不操作其他容器。

Godot 每个 attempt 都复制候选项目、安装宿主桥接脚本，并在副本中导入和运行。
`res://` 存档写入副本，`user://` 通过副本配置分配唯一的
`RSI-GameCreator/playtests/<id>` 用户数据目录；实际路径记录在 `runtime.json`。
同一 attempt 内允许保存和读档，不同 attempt 不共享运行时写入。
导入阶段也使用独立用户数据设置，原候选及其哈希不变。
此隔离覆盖 Godot 标准资源/用户目录，不是针对游戏任意绝对路径写入的系统沙箱。
用户数据保留用于排查；其位置遵循 [Godot 数据目录规则](https://docs.godotengine.org/en/stable/tutorials/io/data_paths.html)。

游戏/策略失败会记录为失败轨迹，其他轨迹继续执行；停止请求或完整性检查异常会取消
剩余任务，并等待各实例释放输入、关闭游戏和策略进程。已完成轨迹不会重跑。
并发实例会竞争 CPU/GPU，实际加速需测量；较高负载可降低 `parallel_episodes`。

原 Reviewer 通过现有匿名证据包读取多局报告、轨迹、画面和录像，不暴露源码与版本顺序。
新报告单独执行 `policy-v1` 校验，旧 GUI 报告的证据要求不放宽。

## GameCraft 的 20 秒演示

未启用 benchmark 的普通 Player 不加 20 秒限制。已有合规短轨迹可直接 `export-gamecraft`；另提供独立
`prepare-gamecraft`，复用保存的 `.gd`，从正常启动或已有 `--scenario` 开始补录，
仅这次补录最多 20 秒、1280×720 视口。补录不调用模型，不创建新场景或修改游戏。
专用补录正常到点未结束记为 `truncated`，证据完整时仍可导出，保留未验证通关的状态；
异常退出、人工中断或其他限制导致的截断不因此放行。不自动重试或声称通关。
旧补录可用 `export-gamecraft --prepared-dir` 离线导出，不重写原报告。
长流程的原策略不一定适合短演示，需要已有合适的 profile/scenario。旧 `.py` 可继续
用原导出器导出已有记录，但不能直接用于这个 Godot 补录入口。
用法与离线重放见 [GameCraft 演示与导出](gamecraft-export.md#准备新的短演示)。

Godot 普通桥接与原生 Policy 共用输入函数，统一保持/释放/再次按下、修饰键和鼠标事件。
原生 Policy 在 `trajectory.jsonl` 旁新增 `input_events.jsonl`，使用相同的引擎时间起点；
记录实际注入的按下/松开/移动事件，结束或受控异常时释放输入并关闭记录。
宿主保存其哈希，缺失/变更会使证据完整性检查失败。进程被强制杀死时只能保留已写记录，
不能保证写出最后释放或结束标记，这类残缺记录不会导出为完成演示。

导出优先使用这份事件记录，不再从动作集合重新推导键变化；旧实验没有该字段时保留旧路径。
不改变 `act(state)` 接口、决策策略或 HTML 试玩方式，也不自动调整执行节奏。
诊断确认松键过短导致窗口输入误判时，可在新的 `prepare-gamecraft` 中显式指定
`--min-key-release-ms 120`：只延长释放已有按键的动作，实际执行并重新录制；
默认不启用，普通 Player 不受影响，旧 JSON 不重新计时，20 秒上限不变。
原生输入与官方 xdotool 重放的结果尚未自动对照；离线流程通过不等于重放复现了原试玩。
参见 [初始状态与重放边界](gamecraft-export.md#初始状态与重放边界)。

## 并行与存档隔离验证（2026-09-18）

- 本次完整测试集：**234 passed, 1 skipped**。跳过项是需要另行指定 Python 环境的
  Harbor 兼容性测试。
- 本地真实 Godot 三实例并行夹具：各 30 次输入，独立端口、独立 `res://` / `user://`
  存档空间，原项目哈希不变；三段视频均核验为 640×360、2 FPS。
- 本地真实 Chromium 三实例并行夹具：各 30 次输入，独立反馈与录像，证据校验通过。
- 模拟测试覆盖并发数约束、乱序完成后的报告顺序、停止与子进程清理、单条失败保留、
  源码修改检测及已有策略/轨迹复用；真实无窗口 Godot 夹具另外验证各实例的存档读写隔离。
- 这些检查使用固定测试策略和模拟模型角色，没有启动新的真实模型 rollout 或调用生成服务。
  小夹具并发成功不代表大型游戏也能保持相同速度；并发负载需要结合实际项目评估。

## 解释结果与验证范围

- `completed` 表示策略自行结束，不等于宿主已经验证游戏胜利。实际进度见状态轨迹。
- 这是源码辅助的白盒测试，不代表普通玩家的发现过程或胜率。三局不是可靠难度统计。
- 各版本策略独立生成，种子相同但不保证技能水平相同；不宣称固定策略的公平难度比较。
- 游戏可玩性、策略是否擅长该游戏、视觉趣味性需要分别判断。
- 本地测试覆盖子进程策略执行、连续输入、状态/截图分离、多轨迹、停止恢复、匿名包、
  证据哈希与版本校验、Mock 全闭环以及可用时的 Godot/FFmpeg 本地夹具。
- 尚未运行真实 Codex/Harbor 策略生成、付费资产接口，或真实游戏的端到端效果评估。

本次本地验证（2026-09-18）：完整测试集 **192 passed, 1 skipped**；跳过项为需要
另行指定 Python 环境的 Harbor 兼容性测试。额外使用固定测试策略与模拟模型角色，
完成真实 Chromium + 隔离策略容器的三局夹具：每局 13 次输入，持续按键与释放生效，
各保存一段经 FFprobe 核验为 640×360、2 FPS 的录像及三张关键画面。
也验证了真实容器只读代码挂载、无网络配置及退出清理，以及 Godot 无窗口桥接夹具。
这些是框架联通性检查，不是生成模型游玩质量或完整 Godot 游戏测试。

## Godot 容器执行后端

`runtime.mode: docker` 将 HTML/Godot 游戏、录像和后置提帧放入独立 Linux 容器。Godot 使用 Xvfb，HTML 使用容器 Chromium；每局独立，复用共享环境镜像。policy 编写、三局并行与后置分析不变，宿主无需安装浏览器或 FFmpeg。镜像 ID 与框架哈希纳入冻结的 Player settings，恢复拒绝混用环境。参见 [运行与验证](runtime.md)。
