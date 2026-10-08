# GameCraft 演示准备与导出

`export-gamecraft` 是离线文件导出命令，读取已有 Godot **policy 模式**的真实游玩证据，生成
GameCraft-Bench 的项目与输入轨迹。不会调用模型、运行策略或游戏、访问 rubric、
调用裁判，也不会修改原 run、游戏快照、policy.gd/policy.py、trajectory.jsonl 或进化流程。
HTML 和旧 GUI 格式暂不支持；不会把 HTML 项目转换成 Godot。

`prepare-gamecraft` 则是独立的短演示补录入口：复用已有 Godot `.gd` 策略，实际运行
游戏并录制最多 20 秒的新证据，再调用原导出器。**不是单纯复制文件，也不是官方重放**。
两者都不调用模型、生成服务或裁判，不读取评分 rubric；普通 Player 保持不限时。

## 准备新的短演示

先确认所选轮次已有新的 Godot `policy.gd`、`plan.json` 和对应试玩报告；原先保存的
Python Policy 不会自动翻译。先检查计划，不启动游戏、不写文件：

```bash
.venv/bin/game-agent prepare-gamecraft RUN_DIR \
    --round 0 --episode 1 --output exports/gamecraft-demo-round0 --dry-run
```

去掉 `--dry-run` 才会真实补录。可选 `--scenario 1=main`，但 `main` 必须已由游戏实现，
对应 Policy profile 也必须适合这个起点；框架不创建场景。可重复指定 episode/scenario。

如果接收端诊断确认“松开后立即再按”被官方窗口输入误判成自动重复，可显式增加
`--min-key-release-ms 120`。默认是 `0`（不调整），范围为 `0..2000` 毫秒。
仅当动作释放了至少一个已按住的键时，将**本次真实执行**的动作时长至少设为该值；
持续按住和原本没有按键的等待不变。新记录同时保存策略请求时长与实际执行时长，
导出仍使用实际事件时间，不拉伸旧 JSON。此选项只适用于新的 GameCraft 补录，
普通 Player 不受影响。20 秒上限保持不变，因此同样时限内可能只能展示更少内容。
释放间隔应根据具体游戏与重放环境选择，并通过新录制和重放结果核对。

```bash
.venv/bin/game-agent prepare-gamecraft RUN_DIR \
    --round 0 --episode 1 --scenario 1=main \
    --output exports/gamecraft-demo-round0
```

- 使用原 run 的引擎环境，Godot 以 1280×720 窗口启动，不调用会覆盖 scenario 的
  `agent_reset`。策略仍接收配置种子；这不等于替游戏实现 RNG 重置。
- 每条新演示最多 20 秒，到点释放输入并记 `truncated`；不改变游戏时钟，不加速，
  不自动截取长轨迹或重试。单次启动、资源导入和视频编码不计入这 20 秒操作窗口。
- 所选策略正常结束，或由专用补录器的20秒时间上限正常停止，且证据完整时可以导出。
  到时停止仍记录 `truncated`，`goal_verified=null`，不宣称策略完成或游戏通关。
  异常退出、人工中断、动作次数限制、未配对输入或证据缺失仍拒绝导出。
  引擎下一帧停止造成的毫秒级超出在导出时限定到600帧，原始时间保留，末尾释放按键/鼠标。
- 旧策略只复用/执行，不新增模型适配步骤；77秒的完整流程不会因此变成20秒可通关，
  只提交已录制的前20秒内容，后期机制可能需要已有 scenario 和匹配的短目标。
- 补录使用新任务目录；原 run、策略、游戏快照和历史证据均不修改。普通 Player 的
  三局并行、继承和恢复机制保留；这个一次性补录命令不自动续跑失败任务。

输出如下，`preparation.json` 记录原轮次、原 episode 编号和策略哈希。所选 episode
在新任务中从 1 重新编号，种子按 `config.seed + 新索引` 分配，记录新执行而非原轨迹：

```text
exports/gamecraft-demo-round0/
  preparation.json              原策略与所选 episode 的对应关系
  player/                       冻结 .gd、计划、新输入轨迹、状态、截图与录像
  status.json                   是否可导出、每条结束原因；不宣称官方重放成功
  submission/                   全部合规后才生成
    manifest.json
    game/demo_outputs/*.json
```

然后用 `scripts/score_gamecraft.py --submission exports/gamecraft-demo-round0/submission`
接入原评分流程。先按文末命令执行 `--dry-run` 或 `--smoke-test`；官方 verifier 才负责
重新启动游戏、按 JSON 输入重放、录屏抽帧。本接口不替换官方重放器。

## 用法

### 从已有补录记录离线导出

旧版补录如果因为20秒到时而拒绝导出，但已保存 `preparation.json` 与 `player/play.json`，
可以直接复用，不需要重跑 Policy，也不要把原始 `truncated` 手工改成 `completed`：

```bash
.venv/bin/python -m game_agent_lite.cli export-gamecraft RUN_DIR \
    --prepared-dir exports/gamecraft-demo-round0 \
    --output exports/gamecraft-player-submission
```

`--prepared-dir` 指补录根目录。默认使用其轮次；显式 `--round` 必须一致。
`--episode` 选择补录内编号，清单的 `source_episode` 保留原实验编号。
校验原游戏/策略哈希、episode映射、补录执行配置、实际输入反馈及录像；不重新执行游戏。
原 run、补录轨迹和旧 `status.json` 均保持不变，新提交包的清单才是本次导出的结果。

新清单逐条保存 `source_status`、`stop_reason`、`demo_time_limit_seconds` 和
`goal_verified=null`。停止时长、20秒导出范围与通关结果分别记录。
只在专用 GameCraft 补录配置与记录一致时放行时间截止；普通 Player 的不完整轨迹不放行。

### 从原实验导出

在仓库根目录执行，替换 RUN_DIR 为已有的 Godot 实验目录：

```bash
.venv/bin/python -m game_agent_lite.cli export-gamecraft RUN_DIR \
    --output exports/gamecraft-first
```

默认导出 state.json 中保存的最佳版本及其全部 episode。也可以导出某轮候选
（轮号从 0 开始）和指定的游玩记录（episode 从 1 开始）：

```bash
.venv/bin/python -m game_agent_lite.cli export-gamecraft RUN_DIR \
    --round 0 --episode 1 --episode 3 \
    --output exports/gamecraft-round0
```

需要该候选已有 check.json、played-candidate.json 和完整 policy-v1 报告。
不要求候选已经晋升；可以显式选择失败批次中已完成的 episode，但不会自动丢弃
失败 episode。completed 表示策略执行正常结束，不表示一定通关。

输出目录必须是 run 外部的新目录；已经存在则报错，不覆盖。输出结构：

```text
exports/gamecraft-round0/
  game/
    project.godot
    ...                         游戏代码与资产副本
    demo_outputs/
      0001.json                 官方帧事件格式
      0003.json
  manifest.json                 来源版本、策略/轨迹哈希、种子、初态和导出限制
```

原项目若已有 demo_outputs，只替换导出副本中的此目录，避免旧演示混入官方
按文件名取前 10 条的逻辑。复用框架快照过滤，排除 .env*、.godot、.codex、
.git、运行缓存等；不复制 run 配置或策略源码。保存的证据不变。

## 时间与输入转换

- 固定使用官方默认的 30 FPS 时间轴与 1280×720 输入坐标；与内部 2 FPS 录像无关。
- 仅转换已获得 feedback 的实际操作，并校验输入配对、状态衔接、时间顺序、
  版本与文件哈希。不接受不完整、失败、模拟或被修改的已选轨迹。
- 使用 action_start_seconds 保留初始等待和操作间隔，不把 duration_ms 简单相加。
- 连续按键集合转换为差分 key_down/key_up，保持同一个键时不重复按下；
  结束时释放仍按住的键。方向键、字母、数字等映射到官方 keycode；不支持的键报错。
- Godot 鼠标操作在 duration_ms 内保持按下，转换为 mouse_down/mouse_up；新 GDScript
  记录优先使用实际 `pointer_release_seconds`，历史 Python 记录保留请求时长近似；
  button=none 转为 mouse_move。归一化坐标转换为官方视口像素坐标。
- 最多导出 10 个 episode，超过时通过重复 --episode 显式选择，不自动取前 10 个。

按照任务文档，每条演示最多 20 秒。默认遇到长轨迹就报错；显式添加
`--clip-long` 才截取长 episode 的**前 20 秒**，短 episode 保持原长度：

```bash
.venv/bin/python -m game_agent_lite.cli export-gamecraft RUN_DIR \
    --round 0 --clip-long --output exports/gamecraft-round0-prefix
```

CLI 会提示发生了截取，manifest 记录原始时长、原始/导出动作数和 clipped 标志。
新增的 `export_range` 给出 `kind`（`full_episode` 或 `prefix`）、`start_seconds`（0）
与 `end_seconds`。`time_basis` 区分旧宿主 reset 后时钟和新引擎输入时钟。
`initial_state_check` 默认是 `unverified`；短演示补录为 `fresh_launch_recorded`，
仅表示本次确实从正常启动/scenario 录制，不等于与官方起点完全一致。
`source_video` 保存录像的路径和哈希；常规导出相对原 run，补录导出相对新 Player 任务，
由 `source_evidence_scope` 区分。
导出时核对录像文件，但不把录像复制进提交项目。评分接收方不需要访问这些原 run 文件。
截取不会加速动作，不代表展示了完整流程或后来发生的胜利。不会从中间随便裁一段
并假装它可以从新游戏开始重放，也不会替你实现后期场景。

## 初始状态与重放边界

**导出成功仅表示格式与来源检查通过，不代表官方重放已通过。** manifest 中
`replay_verified=false`、`official_score=null` 始终保持明确。

正常 Player 在 reset 后开始记录输入；官方会重新启动游戏，并不会调用这个 reset RPC。
新的短演示补录省略 reset，但双方仍各自等待窗口和录屏准备。游戏的标题页、RNG、镜头、
计时器等状态可能不一致。种子与初态保存在 manifest 用于核对，不会作为不存在的
官方 seed 字段写入轨迹，更不会自动修改游戏来重置状态。

如果游戏**已经实现**与记录初态匹配的 `--scenario <name>`，可以指定：

```bash
.venv/bin/python -m game_agent_lite.cli export-gamecraft RUN_DIR \
    --round 0 --episode 1 --scenario 1=main \
    --output exports/gamecraft-main
```

这个选项只把 scenario 写入官方 JSON；不会创建、执行或验证该场景。不同 episode
可重复指定各自映射。短演示补录的 scenario 会自动沿用，导出时不允许改成别的起点；
其他记录未指定时采用官方普通启动方式，不推断 scenario。

其他限制：旧宿主 action_start 时间不等于引擎实际收到输入的精确时间；旧鼠标松开用
请求时长近似。新 Godot 原生记录 `input_events.jsonl` 中每次实际注入的时间，
包括最终键鼠释放；旧记录的最终键盘释放仍使用 elapsed_seconds。
事件量化为 30 FPS 后可能同帧，保留释放/再按下的先后顺序；物理迭代与回放时钟
并未锁步。普通 Player 的 960×540 到官方 1280×720 的布局变化也可能影响点击，
补录入口使用 1280×720 减少这项差异。策略的闭环纠偏不会
被一条固定输入时间表保留。正式提交前需要在官方 Linux/Godot 环境重放核验。

当前鼠标每个动作结束后都会松开，不支持跨动作持续拖拽；导出不会补造未执行的交互。
生成与评分环境的 Godot 版本也可能不同。对于需要这些条件的游戏，格式转换不能保证
重放得到相同结果。

新原生 Player 的 `input_events.jsonl` 纳入证据哈希校验，导出器优先读取实际注入事件，
保留原顺序并将时间量化为 30 FPS。鼠标坐标记录为窗口归一化坐标，包含拉伸/黑边变换。
存在但损坏、时序/版本不符或缺少结束释放的文件会报错，不能悄悄用旧算法代替。

清单中每条演示新增：

- `input_source: "injected_events"`：来自事件文件；`source_input_events` 保存相对来源路径和哈希。
- `input_event_map`：导出事件索引对应原 `sequence`、`time_seconds`；前缀末尾合成释放标为 `prefix_release`，结束等待标为 `end_marker`。
- `input_source: "derived_actions"`：旧实验兼容路径，按动作记录推导事件。旧导出包仍可评分。

Godot 桥接与原生 Policy 共用键鼠执行逻辑，官方 xdotool 重放器保持原样。
实际注入不等于游戏已经响应，也不能消除开放循环重放、版本或初态差异。
`--smoke-test` 只检查流程，不自动比较游戏状态；先对照原始证据与官方新录像，
再使用分数讨论游戏质量。见 [试玩结果的解释与验证范围](player-policy.md#解释结果与验证范围)。

## 交给评分入口

导出后把整个包交给评分脚本，路径不用再手动追加 `/game`：

```bash
.venv/bin/python scripts/score_gamecraft.py \
    --task racing-drift-circuit \
    --submission exports/gamecraft-round0 \
    --benchmark benchmarks/gamecraft-bench \
    --dry-run
```

将题目 ID 换成导出游戏对应的任务。此命令仅检查导出包，不启动 Docker。
准备实际离线重放时，将 `--dry-run` 改成 `--smoke-test`；正式评分则去掉两者，
按[评分文档](gamecraft-scoring.md)配置裁判。`--submission` 与原来的 `--project` 互斥。

评分入口复用 `validate_gamecraft_submission()`：检查游戏内容哈希、每条轨迹哈希、
事件格式与时间、scenario 元数据，以及 `demo_outputs` 是否恰好只有清单列出的文件。
出错就拒绝接入，不回退到旧演示。旧 `gamecraft-export-v1` 清单缺少新增说明字段时仍可使用，
起点和结果仍视为未核验。校验函数不访问原 run，不执行 Policy，也不读取任务 rubric。

实际评分输出保存清单副本 `submission-manifest.json`，并在 `host-manifest.json` 中记录
任务、轮次、episode、项目版本和清单哈希。检查重放结果时对照该副本和官方新录的视频；
流程通过与通关结果分开判断。评分输出不写回导出包，`goal_verified` 保持 `null`。

常规本地测试使用合成记录和子进程替身验证转换、校验、入口与文件保护；另有本地
Godot 和可选 Docker 固定夹具测试实际输入、并行与录像。这些不调用生成模型或裁判，
不产生正式 benchmark 得分，也不证明任意长流程都能压进 20 秒。
