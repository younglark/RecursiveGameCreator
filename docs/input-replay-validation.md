# 实际输入事件与离线重放验证（2026-09-20）

本次仅调整 Godot 输入桥接、原生 Policy 记录、证据完整性检查与 GameCraft 导出。
没有修改游戏、四个角色提示词或 benchmark 源码，没有模型 rollout、资产生成或付费评分。
普通试玩仍不限 20 秒；GameCraft 演示限制、录屏参数与已有 Policy 接口保持不变。

## 实现与验证

- 桥接 RPC 和原生 Runner 共用按键差分、修饰键状态与鼠标执行函数。
- 原生试玩保存 `input_events.jsonl`；每条含序号、动作索引、实际注入时间和版本/Policy 标识。
  宿主保存哈希；导出器检查顺序、时钟、版本、按下/释放平衡与修饰键状态。
- GameCraft 优先转换实际事件，清单保留来源与逐事件对应；旧记录继续兼容动作推导。
- 完整离线测试：435 passed、1 skipped；跳过项需要另行指定 Harbor Python 环境。
- 真实 Godot 无窗口用例核对 RPC/原生两条路径：长按不重复触发、释放后再次按下、
  Shift＋方向键后普通方向键、Ctrl＋鼠标按下/释放、正常结束/中断释放。
- 7 项 Linux 容器用例通过：5 种视口点击回归与 2 项官方重放对照。
  使用原版 GameCraft replayer，持续移动到达目标，组合键/重复按键/鼠标的
  接收次数、修饰键状态和坐标与原生记录一致。输入夹具首尾的等待只为取得足够的视频样本，
  没有给实际 Player 统一增加等待时间。

复现容器夹具（禁网，无模型）：

```bash
GAME_AGENT_RUNTIME_IMAGE=rsi-gamecreator/runtime:full-v1 \
GAME_AGENT_GAMECRAFT_IMAGE=rsi-gamecreator/gamecraft-eval:godot-4.6.2-v1 \
GAME_AGENT_GAMECRAFT_BENCHMARK=benchmarks/gamecraft-bench \
.venv/bin/python -m pytest \
  tests/test_runtime_integration.py::test_native_gamecraft_preparation_and_optional_upstream_replay -ra
```

## 初次已有游戏验收：存在差异

复用已有推箱子 round 0、episode 1 的 `.gd`，独立补录 20 秒，再通过 `--smoke-test`
执行官方重放。原项目、Policy 和原 Player 报告的哈希均保持不变。

| 检查 | 实际结果 |
|---|---|
| 原生补录 | 109 个动作，正常在 20 秒停止，状态轨迹进入第 4 关 |
| 官方重放 | 构建/录像流程通过，但最后画面停在第 2 关死亡界面 |
| 结果一致性 | **未通过**；没有声称通关或重放成功 |
| 评分 | `official_score=null`，没有调用真实裁判 |

本地证据（不随源码提交）：

- 新补录与导出包：`exports/sokoban-input-events-validation/`
- 官方录像与截图：`validation/sokoban-input-events-smoke/demos/0001/`
- 人工对照记录：`validation/sokoban-input-events-smoke/consistency-check.json`

小夹具通过不能推出所有游戏结果一致。输入注入、30 FPS 时间量化、系统窗口输入和实际游戏响应
仍是不同环节；此处没有锁步、自动存档恢复或通用通关识别。后续可用事件序号及时间映射
定位首次偏离，再决定是否需要单独调整补录节奏；不能默改旧轨迹或用 `smoke_passed` 代替结果核对。

## 首次偏离与重复重放验证

在临时项目副本中添加只读输入探针，调用未修改的官方 replayer（Godot 4.6.2），
记录游戏收到的按键、`echo`、物理键码、处理帧和离散游戏状态。原游戏、原策略、
旧轨迹与 benchmark 源码不修改；所有运行均禁网，无模型或付费裁判。

旧轨迹的首次偏离出现在输入序号 **29**、动作索引 **22**、原记录 **4.326564 秒**：
右键的松开与重新按下在重放的同一处理帧到达，新的按下被标为 `echo=true`。
游戏 `_input` 会忽略 echo，因而玩家本应移动到 `[7,5]`、回合 7，实际仍在 `[6,5]`、
回合 6；随后的推箱路线错位。124 个显式键盘事件都送到了，但 7 次新按下被误标，
说明“事件齐全”仍不能证明游戏按预期响应。

轻量处理是只对**新的 GameCraft 补录**显式设置 `--min-key-release-ms 120`。
默认仍为 0；当动作移除已按住的键时，真实执行至少 120 毫秒，记录请求/执行两种时长，
再导出实际事件。不是改写旧 JSON 或 benchmark，也不改变普通 Player 的默认节奏。

| 检查 | 新录制与验证结果 |
|---|---|
| 新原生补录 | 89 个动作、100 个显式键盘事件，正常在 20 秒到点停止 |
| 最短同键释放至再次按下 | 实测 123.206 毫秒 |
| 同一份新 JSON 连续官方重放 | 3/3 次显式键盘事件顺序一致，新的按下被误标为 echo 均为 0 |
| 可对齐键盘动作后的离散状态 | 每次 87 个检查点，3/3 次均无偏离；比较玩家、箱子、回合、机关、敌人与物品等字段 |
| 终态 | 原生与三次重放均为第 3 关、回合 10、玩家 `[4,3]`、存活 |
| 不加探针的官方 smoke-test | 构建/重放通过；录像末尾人工核对为第 3 关、回合 10 |
| 通关/评分 | 未通关，保留 `truncated`；没有调用真实裁判，`official_score=null` |

每次另收到 2 个长按 Enter 的系统自动重复事件，均正确标为 echo 并被游戏忽略；
这与“松开后再次按下被误标”不同。此对照不比较连续动画时间，也不是通用的自动通关检测。
放慢补录后，20 秒内进度从原生旧记录的第 4 关变为第 3 关；不能把稳定重放解读为
覆盖全部任务要求或分数必然提高。导出 manifest 的通关/重放默认标记不被手工改写，
具体样例验收另存报告并绑定输入 JSON 哈希。

补充回归验证：**442 passed、1 skipped**（Harbor 兼容性检查未配置专用 Python 环境）。
新增用例覆盖参数默认关闭/范围检查、dry-run 不启动引擎，以及真实 Godot 执行的松键
时长确实改变、保持按键和已松开动作不变、策略请求时长仍保留。

本地证据（不随源码提交）：

- 首次偏离：`validation/sokoban-replay-diagnosis/baseline-1/comparison.json`
- 新补录与提交包：`exports/sokoban-release120-validation/`
- 三次实际事件/状态对照：`validation/sokoban-replay-diagnosis/release120-{1,2,3}/`
- 验收摘要：`validation/sokoban-replay-diagnosis/summary.json`
- 不加探针的录像：`validation/sokoban-release120-smoke-1/demos/0001/0001.mp4`
