# SceneForge

SceneForge 是一个面向 AI 短剧和自媒体视频的本地优先创作工作台。用户可以从主题创意或已有剧本开始，依次完成剧本创作、分镜设计、镜头生成和成片制作。

## 主要能力

- 四阶段审核工作流，支持修改、终止、继续和断点恢复。
- 角色、道具、场景、风格 Skill 与可选 LoRA 资源库。
- 逐镜头首帧生成、历史版本、回滚、质量检测和连续性约束。
- 字幕、配音、音效、背景音乐、成本估算和自动合成。
- 明暗主题以及可配置的媒体文件存储目录。
- 本地 Web 工作台、持久化任务队列和历史创作管理。

## 快速启动

需要 Python 3.12、`uv`、Node.js 22+、`npm`，并确保 `FFmpeg` 已加入 `PATH`。

```powershell
uv sync --frozen
cd frontend
npm ci
npm run build
cd ..
.\start.bat
```

macOS 或 Linux 请将最后一条命令替换为 `uv run python main_server.py`。默认访问地址为 `http://127.0.0.1:8770/`。

## 模型配置

模型服务可以在设置页面配置，也可以将 `configs/agent.example.yaml` 复制为已忽略的 `configs/agent.local.yaml`。还可以使用 `SCENEFORGE_API_KEY`、`SCENEFORGE_LLM_API_KEY`、`SCENEFORGE_IMAGE_API_KEY` 和 `SCENEFORGE_VIDEO_API_KEY` 等环境变量。不要把真实凭据写入公开流水线模板。

模型调用可能向外部供应商发送提示词、参考媒体和生成参数，并可能产生费用。使用前应确认供应商的隐私、数据保留、内容和计费条款。

## 数据目录

- 项目状态：`.sceneforge/`
- 生成媒体：默认 `.working_dir/`，可在设置中修改
- 用户资产：`assets/`
- 用户风格 Skill：`skills_user/`

旧版本的 `.vimax/` 数据和 `VIMAX_*` 环境变量会通过兼容层迁移或读取。

运行数据、本地模型配置、生成媒体、用户资产和用户 Skill 默认不会进入源码仓库。

## 安全

Web 服务预期绑定到 `127.0.0.1`。使用非回环地址前必须设置 `SCENEFORGE_WEB_TOKEN` 或传入 `--token`。漏洞报告和部署建议见 [SECURITY.md](SECURITY.md)。

## 开发与贡献

```bash
uv run python scripts/check_repo_hygiene.py
uv run pytest -q
cd frontend && npm run build
cd ../ui && npm test
```

提交代码或素材前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 上游与许可证

SceneForge 最初基于 [HKUDS/ViMax](https://github.com/HKUDS/ViMax) 进行产品化开发。目前项目已加入独立的分阶段工作流、桌面端 Web 界面、资产模型库、持久化队列、自动后期和一致性控制等能力。许可证与上游说明见 [LICENSE](LICENSE)、[NOTICE](NOTICE)、[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 和 [docs/上游来源与许可证.md](docs/上游来源与许可证.md)。
