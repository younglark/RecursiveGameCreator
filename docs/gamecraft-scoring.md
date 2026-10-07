# GameCraft：为已有游戏评分

这是一个独立的 Linux 容器评分入口，不启动生成/进化 rollout，不修改四个 Agent。
输入三选一：`--submission` 指定 Player JSON 导出包，`--project` 指定已完成 Godot 项目及
`demo_outputs/*.json`，或者 `--recordings` 指定新采集的 Player 原生截图包。
前两者直接调用官方 `gamecraft_bench.verifier` 重放；后者直接传递原始 PNG 给同一裁判，
沿用 rubric、逐项聚合和评分公式，省去 JSON 重放与视频抽帧。

原生包要求854×480、目标2 FPS、每条≤20秒且≤40张，包含对应版本游戏、真实输入和时间戳。
新重测入口默认生成此包，命令与文件结构见 [README](benchmarks.md)。
不能直接传入任意视频或把旧640×360截图放大充当新采集。新模式记录 `recording_score`，
`official_score=null`，须作为更换证据采集方式的评测变体单独报告。下面原有 `--submission`
与 `--project` 的说明仍指官方重放流程。

## 1. 准备环境

在 RSI-GameCreator 根目录执行。先按 README 在 `.venv` 中安装本仓库及 Python 依赖；实际重放还需要已启动 Docker Desktop。
如果已有本地 GameCraft 仓库，可直接通过 `--benchmark` 指定，不必重复克隆。

```bash
git clone https://github.com/FreedomIntelligence/gamecraft-bench benchmarks/gamecraft-bench
docker build -t rsi-gamecreator/gamecraft-eval:godot-4.6.2-v1 docker/gamecraft
```

镜像包含 Python 3.12、Godot 4.6.2（下载后校验官方 SHA512）、Xvfb、xdotool、
FFmpeg、Mesa 软件渲染、中文字体和官方裁判 SDK。这里使用 Debian 12；官方 README
的安装示例是 Ubuntu 22.04。本机 Apple Silicon 构建原生 Linux ARM64，x86 主机
构建 AMD64；做跨机器对比时应同时记录架构、镜像 ID、Godot 和裁判版本。

只安装评分需要的依赖，不安装生成 Agent、Harbor 或网页仪表盘。官方评分源码以只读
方式挂载，运行记录保存 benchmark commit、源码哈希、镜像 ID 和 `pip freeze`。
默认先寻找本仓库 `benchmarks/gamecraft-bench`，其次兼容旧的兄弟仓库路径；也可显式指定。

本机已经有旧版 Debian 12 Godot runtime 时，可以减少重复下载：

```bash
docker build --build-arg BASE_IMAGE=rsi-gamecreator/runtime:godot-4.7.1-v1 \
  -t rsi-gamecreator/gamecraft-eval:godot-4.6.2-v1 docker/gamecraft
```

评分镜像仍会重新安装并验证 **Godot 4.6.2**，不会使用基底的 4.7.1 评分。
后续升级 benchmark 或重建镜像可能改变环境；论文实验应固定上述记录。

## 2. 先做不联网验证

已有 Player 导出包时，推荐先检查其格式和文件哈希：

```bash
.venv/bin/python scripts/score_gamecraft.py \
  --task racing-drift-circuit \
  --submission exports/gamecraft-round2 \
  --dry-run
```

导出方式见[导出文档](gamecraft-export.md)。脚本自动使用包内 `game/`，
核对项目与轨迹哈希、事件协议和演示列表。错误不会回退到原项目。
只依赖包内文件，原 run 路径作为来源记录；已有 v1 清单兼容，缺少起点核对信息仍视为未核对。

准备实际离线重放时，执行：

```bash
.venv/bin/python scripts/score_gamecraft.py \
  --task racing-drift-circuit \
  --submission exports/gamecraft-round2 \
  --smoke-test
```

若明确要评测项目原有的演示，仍可将 `--submission ...` 换成
`--project runs/YOUR_RUN/rounds/0002/integrate-XXXX/project`。这条旧入口不会校验导出清单，
也不会自动读取 Player 的记录。

该命令使用 `--network none`，不传入密钥，用官方 stub judge 检查导入、构建、
按键/鼠标回放、录像、截图和评分文件输出。固定的 stub 数字**不是游戏分数**。
`evaluation.json` 中 `official_score` 保持 `null`，`reward.txt` 会改名为
`stub-reward.NOT-A-SCORE.txt`。`breakdown.json` 仍保留官方原始输出，里面的 stub 值
也不能用于论文或版本排名。
固定零分会让上游 CLI 的阈值退出码为 1，这是 stub 的预期行为；启动脚本按构建和回放
错误判断自检是否通过，并在通过时返回 0。

默认使用官方上限（通常最多 10 条演示），不会截取 Player 的长轨迹替代已有演示。
`--dry-run` 检查路径、导出包格式和哈希并展示调用计划，不启动 Docker、不创建输出目录。
环境验证通过只证明流程可以运行，不证明轨迹展示了全部要求或游戏能够通关。

## 3. 配置真正的模型裁判

首次创建配置；如果文件已存在，直接编辑，避免覆盖已有设置：

```bash
cp -n docker/gamecraft/judge.env.example .env.gamecraft
chmod 600 .env.gamecraft
```

用编辑器打开 `.env.gamecraft` 并填写 API key。模板选用本地官方源码的默认配置：

```dotenv
GAMECRAFT_BENCH_JUDGE=openai
GAMECRAFT_BENCH_JUDGE_MODEL=gpt-5.5
GAMECRAFT_BENCH_JUDGE_OPENAI_API_KEY=填写你的裁判API密钥
```

如果使用自有兼容端点，再配置 `GAMECRAFT_BENCH_JUDGE_OPENAI_BASE_URL`。
模型名称必须是该端点支持图片输入和此裁判调用方式的模型；模板默认值不是服务可用性保证。
这一步使用独立的裁判 API 配置，脚本不会读取本地 Codex 登录文件或混元密钥。
正式模型调用需要相应 API 权限；不联网检查不验证 API 的权限、额度或模型兼容性。

配置使用 Docker env-file 格式：每行 `KEY=value`，不加 `export` 或外层引号。
`.env.gamecraft` 已被 `.gitignore` 排除，不进入镜像、游戏副本或运行元数据。
也可使用上游支持的 `claude`、`opus`、`kimi`、`gemini` 后端及对应裁判密钥。

### Evolvent 的 GPT-5.5

Evolvent 使用 OpenAI 兼容协议，仍选择 `openai` 裁判，并显式覆盖接口地址：

```dotenv
GAMECRAFT_BENCH_JUDGE=openai
GAMECRAFT_BENCH_JUDGE_MODEL=gpt-5.5
GAMECRAFT_BENCH_JUDGE_OPENAI_BASE_URL=https://llmapi.evolventapi.com/v1
GAMECRAFT_BENCH_JUDGE_OPENAI_API_KEY=填写你的Evolvent密钥
```

`BASE_URL` 使用 `/v1`，不要附加 `/chat/completions`。密钥应填写在本地
`.env.gamecraft`，不要写入模板、命令行参数或游戏项目。
如果已经在兄弟目录的 `.env.agent` 中保存了 `VN_AGENT_API_KEY`，可私下复制该值；
评分脚本不会自动扫描或读取其他项目的凭据文件。

本机已于 2026-09-19 将这份配置接入 `.env.gamecraft`，使用 HTTPS，并设置文件权限为
`0600`。在评分容器中，原版 `OpenAIJudge` 使用两张已有赛车截图、两条连接测试要求
成功返回了分数和理由，原版 JSON 解析器通过。此测试验证了认证、图片输入、
`max_tokens=2048` 和响应解析的兼容性，**不构成完整任务评分**。
它没有运行九条正式演示的全部评分，也不能保证后续服务额度和可用性。
接入只修改本地凭据配置；没有修改官方裁判、rubric、生成 Agent 或游戏代码。
报告实验结果时，应注明调用渠道为 Evolvent、请求模型名为 `gpt-5.5`。

## 4. 正式评分

```bash
.venv/bin/python scripts/score_gamecraft.py \
  --task racing-drift-circuit \
  --submission exports/gamecraft-round2 \
  --env-file .env.gamecraft
```

脚本只读挂载源游戏和 benchmark，在容器的 `/workspace/game` 复制游戏、清理旧导入缓存，
用 4.6.2 重新导入，随后运行未经修改的官方 verifier。输入和录像在容器 Xvfb 中完成，
不会操作 Mac 的键盘、鼠标或窗口。输出默认放在新建的 `validation/gamecraft-...`。
用 `--output` 可指定新目录，禁止覆盖旧目录或把输出写入提交包/游戏/benchmark。

官方回放使用 **1280×720 输入坐标、30 FPS 时间轴、854×480 录像、每 0.5 秒采样图片**。
这些是评测专用参数；RSI Player 的 640×360 / 2 FPS 设置没有改变。
正式调用通常每条成功回放请求一次多模态裁判，错误时上游可能重试。

主要产物：

| 文件 | 用途 |
| --- | --- |
| `reward.txt` | 官方 verifier 给出的 0～1 总分 |
| `breakdown.json` | 逐项评分、聚合、错误和演示记录 |
| `judge_log.json` | 裁判理由和响应 |
| `evaluation.json` | 本次模式、验证结果、分数及相对路径的媒体索引 |
| `demos/<名称>/<名称>.mp4` | 官方回放录像 |
| `demos/<名称>/frames/*.png` | 实际提交给裁判的采样截图 |
| `import.log`、`build.log`、`verifier.log` | 导入、构建和评测日志 |
| `host-manifest.json`、`source-files.sha256.json`、`installed-packages.txt` | 环境和输入溯源 |
| `submission-manifest.json`（`--submission` 模式） | 原样保存导出清单；host-manifest 的 submission 字段关联其哈希、任务、轮次和 episode |

`--submission` 不修改提交包，也不会将流程通过写成通关成功。host-manifest 中的
`submission.goal_verified` 保持 `null`；`evaluation.json` 的 `smoke_passed` 表示离线流程结果，
预期游戏目标是否复现需结合新录的录像人工确认。完整 episode 与前缀可通过清单里的
`clipped` / `export_range` 区分。长记录不会被评分入口自动裁剪或加速。

先检查 `evaluation.json` 的 `errors`。API/回放错误可能让上游产生零分，不能把这种零分
直接解释成游戏质量差；脚本遇到这些错误会以非零码结束，`official_score` 保持 `null`。
正常完成但低于上游默认 0.5 阈值仍属于有效评分；脚本会保留 verifier 的退出码，
并将这种完成的评分视为成功执行。构建不通过会保留零分和失败日志。

对比 Round 0、1、2 时，分别指定各轮 sealed integrate 项目和独立输出目录，保持相同
benchmark、镜像和裁判设置。演示覆盖范围也会影响得分，应随结果报告演示数量。
不要把评测 rubric 或评分明细传回后续生成 Agent，以免污染 benchmark 测试。

## 本地验证记录（2026-09-19）

在 Apple Silicon / Docker Linux ARM64 上，Godot `4.6.2.stable.official.71f334935`
完成 APEX Round 2 的全部 **9 条**演示回放，生成 **9 段 854×480 / 30 FPS 录像**和
**343 张采样截图**。导入、官方构建检查、回放与 stub 评分文件输出通过，无回放错误；
三个原始版本的内容哈希均未变化。另检查了 dry-run、只读挂载、禁用网络、输出防覆盖
和无裁判密钥时提前失败。未调用真实模型裁判，未验证 API 权限或实际评分效果。
