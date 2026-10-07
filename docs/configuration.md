# 配置、资产与恢复

配置格式为 JSON，完整字段定义以 [types.py](../src/game_agent_lite/types.py) 为准，读取规则见 [cli.py](../src/game_agent_lite/cli.py)。未知字段会被拒绝。没有 YAML、配置继承或自动加载本地覆盖文件的机制。

## 本机配置与路径

在仓库根目录复制完整示例，然后显式传给 `start`：

```bash
cp examples/godot.json examples/godot.local.json
```

编辑 `godot.local.json` 中所需字段。macOS 应用路径示例（这些只是字段片段，不是完整配置）：

```json
{
  "godot": "/Applications/Godot.app/Contents/MacOS/Godot",
  "blender": "/Applications/Blender.app/Contents/MacOS/Blender"
}
```

- `codex/harbor/godot/blender/ffmpeg` 默认通过 PATH 查找，也接受文件路径；包含 `/` 的相对路径以配置文件目录为基准。
- `source`、`auth_file`、`assets.env_file` 相对配置文件所在目录解析，支持 `~`。示例的 `../.env.hunyuan` 指向仓库根目录。
- `entry` 相对游戏项目；HTML 默认 `index.html`，Godot 未指定时自动使用 `project.godot`。
- `--run-dir` 相对启动命令的工作目录。省略时创建在当前工作目录的 `runs/` 下。
- `build_command` 和 `check_command` 是参数数组，工作目录为候选游戏项目，不使用 shell 展开。含空格的参数独立写成一个数组元素。
- Saved file references are relative to the run directory. Absolute paths are resolved only for execution and Docker mounts. The default login refers to the current user.

`examples/*.local.json` 和 `.env.hunyuan` 被 Git 忽略。使用本地 Codex 时，将完整配置中的 `backend` 改为 `codex`；不需要修改角色或核心模块。

## 默认配置

| 字段 | 默认值 / 说明 |
|---|---|
| `engine` | 必填：`html` 或 `godot` |
| `brief` | 游戏需求；可通过 `--prompt` 提供并完整替换 |
| `backend` | `harbor`；另支持 `codex`、`mock` |
| `model` / `reasoning` | `gpt-6-astra` / `high`；需后端和账户支持 |
| `rounds` / `max_actions` | `null`：不限轮数 / 试玩请求数量预算 |
| `source` | `null`，可指定已有游戏目录；运行目录必须位于其外部 |
| `build_command` / `check_command` | `[]`，按需配置构建 / 功能检查 |
| `codex/harbor/godot/blender/ffmpeg` | 各自同名 PATH 命令 |
| `harbor_image` | `game-agent-lite/codex:0.153.4-godot-v1`，手动构建的本地镜像 |
| `codex_version` | `0.153.4`，需与镜像中的版本一致 |
| `auth_file` | `~/.codex/auth.json` |
| `seed` | `42`；实际重置语义由游戏实现 |
| `headless` | `true`，仅控制 HTML；Godot 试玩需要图形渲染 |
| `runtime.mode` | `local`；`docker` 将 HTML/Godot 构建试玩、Blender 与媒体处理放入统一运行镜像 |
| `runtime.build_network` | `none`；显式 `bridge` 仅允许构建阶段联网，试玩/Blender/媒体始终禁网 |
| `runtime.image` | `rsi-gamecreator/runtime:full-v1`，先手动构建 |
| `runtime.health_timeout_seconds` | `30`，worker 无心跳超时，非总时限 |
| `runtime.max_game_instances` | `1`; per-user host limit shared by controllers; the minimum active limit applies |
| `execution.parallel_reviews` | `2`; independent anonymous reviews; `1` is serial; legacy missing setting stays serial |
| `storage.keep_workspaces` | `false`; remove disposable projects after a valid Player report; retain failures and formal evidence |
| `player.trajectories_per_version` | `3`，每个版本的正式试玩次数 |
| `player.parallel_episodes` | `1`，同一版本同时运行的轨迹数；显式调大可并行 |
| Godot Player 后端 | 根据 `engine: godot` 自动使用引擎内 `policy.gd`；HTML 仍使用 Python Policy |
| `assets.env_file` | `../.env.hunyuan`，相对配置文件目录 |
| `assets.image_model` / `model_3d` | `hy-image-v3` / `hy-3d-3.1` |
| `assets.max_images/max_models/max_blender` | `null`：每轮数量不限；`0` 禁用对应能力 |
| `assets.face_count` | `30000`，3D 面数请求参数 |
| `assets.poll_seconds` / `http_timeout` | `10` / `120` 秒，轮询间隔 / 单次 HTTP 超时 |

`reasoning` 当前 Schema 接受 `low/medium/high/xhigh`；是否可用仍由所选模型决定。轮数、操作数的有限值为正整数，资产数量可为非负整数。JSON 中写 `null`，不写 `Infinity` 或字符串 `"unlimited"`。

正式示例沿用不限预算默认值。`godot-3rounds.json` 仅保留兼容文件名，实际 `rounds: null`。`mock.json` 的两轮和禁用资产仅用于离线模拟。解除总预算不取消单次通信超时，不保证任务永远自动重试。

普通 Godot 试玩也没有新增时长上限。独立的 `prepare-gamecraft` 命令仅对提交演示
补录应用 20 秒上限；这个限制不写入 run 配置，不影响正常 Player 或游戏规则。
参见 [Player 接口](player-policy.md)和 [GameCraft 导出](gamecraft-export.md)。

## 混元与 Blender

读取仓库根目录 `.env.hunyuan` 中的 `HUNYUAN_API_KEY`。可从 [.env.hunyuan.example](../.env.hunyuan.example) 复制；框架直接解析文件，不执行 shell、不向子进程传递密钥。不要把密钥写进 prompt、JSON 或游戏目录。

当前使用 TokenHub `https://tokenhub.tencentmaas.com/v1`：

- `/wand/hunyuan-image/v3-generation`：文生图，默认 `hy-image-v3`，尺寸参数 `1024x1024`，提示词最多 8192 字符；同步返回 `data[0].url` 并下载到本地。当前未实现图生图或局部编辑。
- `/api/3d/submit` 与 `/api/3d/query`：文生 / 图生 3D，默认 `hy-3d-3.1`，下载 GLB。
- Blender：执行草稿内 Python 脚本，`output_format` 支持 `glb` 和单张 `png`；缺省或 null 仍为 GLB。`runtime.mode: docker` 时在容器项目副本执行，本地模式在宿主执行。

图片接口参数依据腾讯云 [Hy 生图调用指南](https://cloud.tencent.com/document/product/1823/135745)。未显式指定的 `revise/seed` 使用服务端默认值。旧模型 `hy-image-v3.0` 的 `/images/generations` 与 `1024:1024` 格式仅为旧配置兼容和历史回执校验保留；新实验使用上述新模型和接口。

Designer 在 `design` 输出 `assets` 请求，Builder 在 `draft` 准备占位和 Blender 脚本，宿主在 `assets` 执行请求，最后 Builder 在 `integrate` 接入 `assets/generated/`。Reviewer 的建议不会直接触发生成。

请求示例：

```json
[
  {"id": "guardian_art", "kind": "image", "prompt": "青铜守卫角色插画，统一配色，无文字", "source": null},
  {"id": "guardian_mesh", "kind": "model", "prompt": "", "source": "guardian_art"},
  {"id": "table", "kind": "blender", "prompt": "制作战斗台", "source": "scripts/build_table.py"}
]
```

`model.source` 可引用同轮图片请求 id 或草稿图片的相对路径；图片先于模型生成。图生 3D 时不同时发送文字条件。Blender 脚本从 `--` 后读取 `--output /absolute/path/asset.glb`（或 `asset.png`）并导出/渲染；Hunyuan 请求的 `output_format` 为 null。同轮混元产物目前没有自动传给 Blender 的输入映射，不应当作已实现的完整后处理链。

没有混元密钥时，在完整配置的 `assets` 中将 `max_images` 和 `max_models` 设为 `0`。本地模式没有 Blender 时将 `max_blender` 设为 `0`；统一容器镜像已包含 Blender。这些是用户显式关闭能力，不改变正式示例的无限默认值。

## 停止、恢复和提交不确定性

`stop` 写停止标记；控制器保存阶段并清理其子进程。进行中的 HTTP 请求在返回或超时后响应停止。`resume` 从实验保存的 `config.json` 和阶段回执恢复，**不重新加载 examples**。

已完成阶段复用回执；未完成的角色调用、试玩会建立新尝试，保留旧内容，不恢复上次游戏的内存。New runs save relative paths. Preserve external dependency layout and configure authentication and matching images on the new machine. Historical absolute paths are not silently rewritten.

3D 作业收到 ID 后立即持久化，后续查询或下载失败可继续同一任务。提交结果不明确时暂停为 `needs_reconciliation`，不盲目重复付费。核对供应商结果后，可使用：

```bash
.venv/bin/game-agent resume runs/mygame --resolve-job 'guardian_mesh=已核实的任务ID'
.venv/bin/game-agent resume runs/mygame --resolve-file 'guardian_mesh=/绝对路径/已取回模型.glb'
```

二者按实际核对结果选择，只处理不确定提交。明确失败的作业保留 `failed`，不自动重新付费；修复原因后另建实验，或使用下面适用的显式迁移工具，不删除回执诱发重复提交。`job.json` 可能含短期下载 URL，实验目录不作为公共源码提交。

### 从 hy-image-v3.0 恢复未完成的资产阶段

旧图片模型下线后，仅更改示例配置不会改变已保存的实验。对已经停止、停在 `assets` 阶段且仍配置旧模型的实验，可执行：

```bash
.venv/bin/python scripts/migrate_hunyuan_v3.py runs/mygame
.venv/bin/game-agent resume runs/mygame
```

第一条命令只准备恢复，不加载密钥、不调用生成服务，也不启动 Agent；第二条才真正恢复执行。迁移工具持有运行锁，并先检查原请求指纹、草稿版本及已完成图片的 SHA-256。它保留已完成图片及原 `job.json`，将实验默认图片模型更新为 `hy-image-v3`，只将明确失败且没有生成结果的旧图片请求置为待提交。未提交的图片也使用新模型。后续资产清单仍会如实包含旧、新两种模型，不会把旧图片重新标注成新模型。

原配置、失败回执的完整文本与保留文件的哈希记录在 `migrations/hy-image-v3-round-NNNN.json`。若迁移写入中途被打断，可重复执行同一命令；一旦后续生成已改变待重试回执，工具会拒绝将它再次重置。未明确确认提交结果的 `uncertain/submitting` 以及仍待下载的图片会阻止迁移，应先核对或取回原结果。

## HTML、Godot 与 Blender 容器环境

使用 [html-docker.json](../examples/html-docker.json) 或 [godot-docker.json](../examples/godot-docker.json)，参见 [容器说明](runtime.md)。`runtime` 字段按默认值合并；未启用时仍为本地执行。构建与检查命令必须兼容 Linux，容器模式的 Godot/Blender/FFmpeg/Chromium 均由镜像提供，不依赖宿主工具路径。`build_network: bridge` 仅供显式构建命令安装依赖，正式试玩使用本地打包资源，不运行 dev server。恢复时不自动更换环境或迁移旧证据。
