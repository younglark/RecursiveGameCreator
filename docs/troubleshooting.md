# 故障排查

先用 `game-agent status <运行目录>` 查看状态，再检查该实验的 `events.jsonl` 和当前轮次 `rounds/<轮次>/jobs/` 下的 `process.log`。`run.lock` 文件存在本身不代表仍在运行；不要用再次 `start` 同一目录的方式恢复。

## 安装或命令找不到

- 确认在仓库根目录执行 `sh scripts/setup.sh`，并按 [安装说明](../README.md#installation) 激活环境；仅安装 Python 依赖可用 `--minimal`。
- 通过 `command -v harbor` 检查宿主编排工具。引擎使用 Docker 模式时检查统一运行镜像；本地模式再检查 Godot/Blender/FFmpeg 的宿主 PATH 或配置路径。
- 复制目录不等于迁移虚拟环境。若 `.venv` 来自其他目录，重建它；用 `.venv/bin/python -c 'import game_agent_lite; print(game_agent_lite.__file__)'` 核对导入位置。
- `uv.lock` 只锁 Python 项目依赖；Harbor 和 Docker 单独准备；引擎、浏览器、Blender 和 FFmpeg 可由统一运行镜像提供。

## Docker 构建失败或 Harbor 无法启动

`auth.docker.io` / `registry` 超时发生在镜像下载环节，先检查 Docker Desktop、网络或代理。确认 `docker info`、`docker compose version` 成功，再构建 README 指定的镜像。不要依赖其他机器存在的私有本地镜像标签。

默认不自动拉取，因此镜像不存在时需运行完整安装脚本或按 [镜像说明](setup.md#docker-images) 构建。统一运行镜像为 `rsi-gamecreator/runtime:full-v1`，旧 Godot-only 镜像不包含 HTML/Blender 依赖。用 `docker image inspect game-agent-lite/codex:0.153.4-godot-v1` 检查。镜像中 Codex 版本必须与配置一致。

Harbor 适配基于 0.13.0。如果升级后失败，先确认版本，再查看角色 jobs 内的 `exception.txt` 和 trial 日志；外层 `Harbor role failed` 只是摘要。框架使用 `--no-delete` 保留共享镜像；不要把它改成会删除镜像的清理方式。

## 登录或模型不可用

默认认证文件为当前用户 `~/.codex/auth.json`。可重新执行 `codex login`，或在配置中显式设置 `auth_file`。如果登录只存在于其他存储位置，需要提供当前适配器能够读取的登录文件。

模型可配置不等于账户一定有权限。当前默认 `gpt-6-astra` / `high`；访问失败时查看后端原始错误，选择账户和后端实际支持的配置。框架不会自动切换模型。

## 混元没有调用或资产失败

先看 `rounds/<轮次>/design.json` 的 `assets`：为空时宿主不会生成。草稿阶段的 SVG 或占位图不代表后续一定没有生成，继续看资产回执和集成结果。

确认 `.env.hunyuan` 的路径相对配置文件正确，且使用 TokenHub 对应密钥。图片/3D 限制为 `0` 表示禁用。模型、服务权限、配额和网络错误都应根据资产回执处理；状态恢复规则见 [配置文档](configuration.md#停止恢复和提交不确定性)。

## Godot play 超时、断连或卡顿

本地 Godot 需要可渲染的桌面；容器模式使用 Xvfb，无需宿主桌面。`headless: true` 只作用于 HTML。检查当前试玩 `engine/` 下日志、Godot 退出或脚本错误、桥接连接，以及是否持续产生截图。通信错误、游戏性能问题和 Player 决策慢是不同原因。

当前录像和 Godot 观察图片为 640×360；录像目标为 2 FPS，不是游戏物理频率。PNG 编码仍在主线程，2 FPS 录像也不能验证精细跑跳手感。低分辨率没有保证所有超时都已解决。

修复运行环境后可 `resume`，未完成的试玩会重新开始。不能从“输入可能已执行但回复丢失”推断可以安全重发所有操作，不要通过修改已封存游戏或证据掩盖错误。

## 没有自动结束、重启，或没找到最佳版本

- `rounds: null` 表示持续迭代；`godot-3rounds.json` 的旧文件名不代表三轮限制。
- `nohup` 不自动重启异常退出的控制器；查看错误后手动 `resume`。
- `stop` 是停止请求，活动 HTTP 请求可能要等待返回。退出 `tail` 不会停止实验。
- `state.best` 为 `null` 表示尚无符合条件的基线；后续候选被拒时旧最佳不变。
- 每轮项目仍会保存。实际位置与查看方式见 [README](../README.md#inspect-and-resume)。

## 平台与测试边界

核心使用 POSIX 文件锁和进程组，暂不支持原生 Windows。macOS 优先验证；Linux 的离线测试成功不等于桌面 Godot 试玩已验证。测试中缺失外部 Godot、FFmpeg 或可选 Harbor 检查时会跳过相应项，见 [验证记录](../VALIDATION.md)。
