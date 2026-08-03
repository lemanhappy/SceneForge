# SceneForge 功能描述文档

> AI 短视频创作工作台 — 从一句创意/一段剧本/一整本小说，自动产出带配音、字幕、音乐、音效、合规标识的成片。
>
> 本文基于对当前代码库（`SceneForge`）的实地分析整理，描述**已落地的功能**。文档面向产品/运营/对接方，不含任何密钥或私密配置。

---

## 1. 产品概述

**SceneForge** 是在开源智能体视频框架 ViMax（HKUDS/ViMax，"Agentic Video Generation"）之上二次开发的**产品化 AI 短视频工作台**。它把"多智能体自动生成视频"的能力，包装成一套**可配置、可审核、可发布**的完整产品：

- **对创作者**：网页工作台，填创意/贴剧本/选角色 → 自动出片，中途可逐阶段审阅、逐镜头重生成。
- **对个人创作者**：支持本地 Web 工作台和可选消息入口驱动的分阶段创作流程。
- **产品定位关键词**：一句话成片、题材垂类化（短剧/解说/科普）、人物跨镜头一致、成片可直接发布（含配音+字幕+BGM+音效+AI 生成合规标识）。

产品由三层组成：

| 层 | 内容 |
|---|---|
| **核心生成引擎** | 多智能体流水线（编剧→分镜→机位→关键帧→图生视频→拼接），题材中立、provider 可插拔 |
| **产品能力层** | 角色库、音频后期链、字幕/转场/钩子/海报/合规标识、质量自评、领域包与风格 Skill |
| **应用层** | Web 前端 + HTTP 后端 + 后台作业系统 + 治理（预算/审核/成本/鉴权）+ 可选消息工作流 |

---

## 2. 系统架构总览

```
                          ┌───────────────────────────────────────┐
   创作者 (浏览器)  ─────▶ │  Web 前端 (Vue3, webui-dist/)          │
   飞书/控制台      ─────▶ │  5 页：创作 / 角色库 / Skill / 自动后期 / 设置 │
                          └───────────────────┬───────────────────┘
                                              │ /api/*  (Bearer Token 鉴权)
                          ┌───────────────────▼───────────────────┐
                          │  HTTP 后端 (main_server.py, 标准库)     │
                          │  AppAPI 聚合 10 组 API + SSE + 静态托管  │
                          └───────────────────┬───────────────────┘
        飞书 webhook ─────▶ TriggerService     │
        /feishu/events      (授权/限频/命令解析) │
                          ┌───────────────────▼───────────────────┐
                          │  ProductionService → JobRunner (后台作业)│
                          │  WorkflowEngine (分阶段审核状态机)       │
                          │  治理: 预算/审核/成本/清理/限流          │
                          └───────────────────┬───────────────────┘
                          ┌───────────────────▼───────────────────┐
                          │  生成引擎: 三条流水线 + 14 个智能体      │
                          │  RenderBackend → 图像/视频/LLM Provider  │
                          │  音频链 / 字幕 / 质量自评 / 领域包        │
                          └───────────────────────────────────────┘
```

- **零重框架**：后端用 Python 标准库 `http.server`（`ThreadingHTTPServer` + 每请求 `asyncio.run`），无 Flask/Django/FastAPI 依赖，便于打包与自托管。
- **产物权威**：`.working_dir/<session_id>/` 是每个创作会话的产物根目录；`.sceneforge/sessions.json` 仅为会话索引。管线按"产物文件是否已存在"跳过已完成阶段，**支持崩溃/重启后断点续跑**。

---

## 3. 四大核心能力

| 能力 | 输入 | 说明 |
|---|---|---|
| **Idea2Video（创意到视频）** | 一句创意 + 约束 + 风格 | 多智能体自动完成故事扩写、角色设计、分镜、成片。One-Prompt to Finished Video。 |
| **Script2Video（剧本到视频）** | 结构化剧本（单/多场景） | 直接按剧本出片，是整套系统的**核心引擎**，其它能力都复用它。 |
| **Novel2Video（小说到视频）** | 长篇小说全文 | 智能压缩 → 事件抽取 → RAG 检索 → 场景化 → 逐集视频，含跨事件角色追踪。 |
| **AutoCameo（自动客串）** | 用户/宠物照片 | 把真实人物作为"客串明星"跨脚本、跨场景出演（基于固定角色资产实现）。 |

---

## 4. 生成流水线与工作流

### 4.1 主工作流 DAG（Idea/Script）

```
input_idea → project_brief → characters → script → storyboard
→ shot_decomposition → camera_tree → frame_prompts → keyframes
→ video_clips → final_video
```

**Script2Video 完整阶段**（核心引擎，`pipelines/script2video_pipeline.py`）：

1. **抽取角色** — 分离静态特征（长相/体型）与动态特征（服饰/道具）
2. **生成角色三视图立绘** — 纯白底 前/侧/背 全身图（一致性参考），可绑定固定角色资产直接复用
3. **设计分镜** — 产出分镜列表（画面/音频/屏显文字）
4. **分解视觉描述** — 每镜拆成 首帧/尾帧/运动/变化程度
5. **构建机位树** — 父子机位覆盖关系，同机位画面继承保证空间一致
6. **生成关键帧** — 选参考图（≤8 张）→ 生成首帧/尾帧（跨机位用转场视频抽帧继承画面）
7. **图生视频** — 首尾帧驱动逐镜出片（第 6/7 步并发）
8. **拼接成片** — 含转场（硬切/交叉溶解/淡入淡出）
9. **音频后期** — 一趟 ffmpeg 混入 TTS 配音 + 音效 + BGM + 响度归一
10. **字幕/钩子/合规标识烧录** — 字幕、开场 3 秒钩子、剧情屏显文字、全时长 AI 生成角标
11. **导出封面海报**（可选）
12. **一致性自检与自动修复** — 逐镜打分，不达标镜头定向重生成

**Idea2Video** 在此之上再包一层：创意→扩写完整故事→抽全局角色→切分成多场景剧本→每场景跑一条 Script2Video 子流水线（共用同一批角色/立绘）→拼接为总成片；开场钩子只挂第 0 场景。

### 4.2 小说工作流 DAG（Novel）

```
novel_text → compressed_novel → events → relevant_chunks
→ scenes → global_characters → scene_scripts
```

分块压缩 → 顺序抽取事件链（含因果 process_chain）→ 对每个事件做 **FAISS 向量检索 + BGE reranker 重排** → 生成标准剧本场景 → 跨事件合并同一角色为统一身份 → 逐场景调 Script2Video 出片。

### 4.3 智能体职责表

| 智能体 | 职责 |
|---|---|
| **Screenwriter（编剧）** | 创意扩写成完整故事，并按时空切分成分场景剧本 |
| **StoryboardArtist（分镜师）** | 剧本拆分镜（画面/台词/屏显），每镜再分解首帧/尾帧/运动；支持单镜重写 |
| **CharacterExtractor（角色抽取）** | 从剧本抽角色，分离静态/动态特征 |
| **CharacterPortraitsGenerator（立绘生成）** | 角色前/侧/背三视图全身立绘 |
| **CameraImageGenerator（机位/关键帧）** | 构建机位树、转场抽帧继承、生成镜头首帧 |
| **ReferenceImageSelector（参考图选择）** | 为目标帧从立绘+历史帧选≤8 张参考图并生成合图 prompt |
| **BestImageSelector（关键帧选优）** | 对比角色/场景参考图、目标描述与多张候选图，选择人物、空间和语义一致性更高的一张；选优服务异常时保留首个成功候选 |
| **SceneExtractor（场景抽取）** | 小说改编：据事件+RAG 片段生成标准场景 |
| **EventExtractor（事件抽取）** | 从小说顺序抽取事件（含因果链，上限 50） |
| **ScriptPlanner（剧本规划）** | 意图路由（叙事/运动/蒙太奇）+ 三幕结构规划 |
| **ScriptEnhancer（剧本润色）** | 补感官细节、强化连续性与术语一致（不改剧情） |
| **GlobalInformationPlanner（全局角色）** | 跨场景/跨事件合并同一角色为统一身份 |
| **NovelCompressor（小说压缩）** | 长文分块压缩聚合，保留核心情节去冗余 |
| **HookWriter（钩子文案）** | 生成开场 3 秒钩子文字（多候选自动选优，中英双语） |
| **DomainPack（领域包）** | 按题材为各智能体注入风格化推理与视觉后缀 |

> 质量档位已接入关键帧和视频候选策略：省钱档为 1 张关键帧/1 个视频候选，均衡档为关键帧 2 选 1，高质量档为关键帧 3 选 1、视频 2 选 1。

---

## 5. 智能体运行时与 TUI

`agent_runtime/` 是一套**OpenAI 兼容的工具调用 Agent 循环 + 会话/上下文管理运行时**，`main_agent.py` 为 REPL/JSONL 入口，供终端 TUI（`sceneforge tui`）以 stdio 驱动。

- **AgentLoop**：事件流循环（组装 system prompt → 采样 → 执行工具 → 回填 → 循环），上限 50 轮工具调用；单轮失败不杀进程；事件（turn/token/tool_start/tool_progress/tool_result/done…）实时推 TUI。
- **上下文压缩**：token 超阈值（默认窗口 200k×0.90）时自动生成 8 段式交接摘要压缩历史，支持手动 `/compact`。
- **内置工具**：读写文件/JSON、glob、全文搜索、记忆读写、todo、sleep、run_shell（默认禁用、拒危险命令），全部受 workspace 沙箱限制。
- **SceneForge 高层工具**（把流水线暴露给 Agent）：`sceneforge_narrative_planning`（文本规划/修订）、`sceneforge_novel_planning`、`sceneforge_render_video`、`sceneforge_regenerate_shot`（单镜重生成）、`sceneforge_publish`（托管成片+回传链接）、`sceneforge_review`（五阶段人工审核门）。
- **行为契约**：未经工具结果或产物状态证明，不得声称已规划/渲染/写文件。

---

## 6. 角色库与固定角色一致性

跨镜头人物不一致是 AI 视频的核心痛点，本产品用"固定角色资产"根治：

- **角色库（CharacterStudio）**：角色 CRUD；每角色可生成 前/侧/背/表情 立绘（side/back 以 front 为参考编辑生成，锁定脸型/发型/服装）。
- **版本历史与回滚**：每次重生成前把当前图归档到 `_versions/{view}/v<n>.png`（非破坏性），可查看历史、回滚到任意版本（回滚本身也可逆）。
- **资产注册表**：YAML 持久化，图片路径相对化（资产库可迁移）；按 id/显示名/别名精确+子串匹配。
- **固定角色注入**：创作时勾选"出场角色"→ 把固定角色图注入会话、剧本沿用角色名、每镜复用同一参考图，实现跨镜头一致。这也是 **AutoCameo** 的实现基础。

---

## 7. 音频后期链

成片默认不再是"哑片"。`audio/` 提供完整音频后期，一趟 ffmpeg 混音、全程可选、失败自动降级为原视频：

- **配音（TTS）**：两家提供方
  - **OpenAI 兼容**（`/audio/speech`，tts-1 / tts-1-hd，多语种）
  - **MiniMax**（T2A v2，中文情感更强，speech-2.6-hd，含多个中文命名音色如"精英青年/霸道青年/御姐/少女"）
  - 音色目录 + 按目标语言自动选音 + **在线试听**；按配音真实时长微调镜头（避免切在半句）、字幕与配音严格同步。
- **背景音乐（BGM）**：曲库上传/选曲/音量，垫底循环；对白可作为侧链信号自动压低音乐，支持轻柔、标准、明显三档强度，无对白时保持普通混音。
- **音效（SFX）**：从分镜 `[Sound Effect]` 标记提取关键词，匹配素材库按镜头起始落位，无匹配静默跳过。
- **响度归一化**：EBU R128 loudnorm，成片响度统一。

---

## 8. 字幕 / 转场 / 钩子 / 封面 / 合规标识

- **字幕**：从分镜提取台词→排时间轴→渲染 ASS/SRT（颜色/位置/描边/字号可配）→ ffmpeg 烧录；中文字体回退（雅黑/黑体/Noto CJK）。终审页另提供项目级字幕时间线，可校正文本/起止时间、恢复生成版本及下载 SRT；该 sidecar 编辑不自动重烧已有 MP4。
- **转场**：硬切 / 交叉溶解 / 淡入淡出，拼接时应用。
- **开场钩子**：前 3 秒抖音爆款式浮层文字，可自动生成（best-of-N 选优）或手填。
- **封面海报**：从成片抓帧导出缩略图。
- **AIGC 合规标识**（默认启用）：全时长半透明角标（如"AI 生成"，九宫格位置可选）+ ffmpeg 元数据写入 `AIGC=true`，符合网信办生成式 AI 内容标识要求。

---

## 9. 质量自评（一致性 Critic）

`quality/consistency_critic.py` — 多模态 VLM 视觉质检门，四个维度独立打分与阈值：

| 维度 | 检查 |
|---|---|
| **identity 人物一致性** | 对比固定角色立绘，是否同一人 |
| **aesthetic 画面质量** | 清晰/构图/无畸变乱脸乱肢乱字 |
| **adherence 镜头贴合** | 是否符合分镜描述 |
| **temporal 镜内连贯** | 首尾帧同人同景不突变 |

- 阈值 0 = 该维禁用（默认仅开人物一致性）；任一启用维不达标即判不通过，失败原因**回灌重渲染 prompt** 做定向重生成。
- **Fail-open**：模型缺失/无图/回复不可解析一律判过，绝不卡住生产。前端以四维中文徽章展示。

---

## 10. 领域包与风格 Skill 市场

让同一套引擎产出不同垂类风格的视频：

- **内置领域包**（题材化推理，为编剧/分镜/钩子注入风格片段）：
  - `short_drama` 短剧/爽文（爽点前置、cliffhanger、反应镜头）
  - `explainer` 影视/事件解说（第三人称旁白、过渡钩子）
  - `knowledge` 知识科普（反直觉提问、问题→原理→举例）
  - `general` 通用（无操作，与上游逐字节一致）
- **用户风格 Skill**：纯文本 `.md`（YAML 头 + `## 剧本/分镜/视频/钩子` 分节，≤64KB），注入到编剧/分镜/视频提示词/钩子四个环节，**无可执行代码，导入安全**。
- **三类来源**：我的 Skill（`skills_user/`，可上传/删除）、内置风格（可 fork 成可编辑副本）、示例模板（`skills_examples/`：赛博朋克悬疑短剧/国风仙侠/暖心治愈，导入后可用）。
- **市场**：内置公共链接 + 可在设置配置自有 `market_url`；外部 SKILL.md 标准（面向编程 Agent）格式不同，通常不能直接导入，仅作灵感参考。

---

## 11. Web 应用

### 11.1 前端（Vue3，5 个页面）

品牌 **SceneForge · AI 短视频工作台**，左侧栏导航，`keep-alive` 保活各页。

| 页面 | 功能要点 |
|---|---|
| **🎬 创作** | 会话式主流程：新建（主题生成/导入剧本两模式）→ 逐阶段审阅（剧本/分镜/视频/终审）→ 逐镜看板（实时轮询、质量徽章、生产指标、返工原因、改提示词重生成、批量接受/恢复/返工、锁定约束、返工费用与节省预览、当前/历史版本并排对比、版本时间点批注）→ 终审轻量时间线（排序、入出点裁剪、基础转场、后台合成、恢复原片）、字幕时间线（文本/时间校正、恢复、SRT 下载）→ 下载/发布。含本片设置（语言/画幅/质量档位/领域/字幕/配音音色试听/BGM/出场角色）、模板记忆、成本估算与实际账单覆盖率、中间文件清理、断点续跑。 |
| **🎭 角色库** | 角色 CRUD、三视图立绘生成、历史版本/回滚、放大预览。 |
| **🎨 Skill 市场** | 风格 Skill 上传/导入/fork/删除，内置模板一键导入。 |
| **🪄 自动后期** | 对**外部导入视频**加字幕/配音/换旁白/BGM/合规角标（不做剪辑/拼接/口型同步，引导用剪映）。 |
| **⚙️ 设置** | 六类：模型 / 音频 / 画面 / 字幕与文案 / 合规与质量 / 扩展。模型厂商→型号级联、key 脱敏、数字项滑块+悬停说明。 |

> Web 界面仅维护 `frontend/` 下的 Vue 源码；生产环境由后端服务 Vue 构建产物 `webui-dist/`，不再保留旧版单文件前端。

### 11.2 后端 API（`/api/*`）

| 模块 | 前缀 | 能力 |
|---|---|---|
| ConfigAPI | `/api/config` | 读写模型配置（LLM/图像/视频/Embedding/Reranker），key 脱敏，厂商→型号目录 |
| CharacterStudioAPI | `/api/characters` | 角色 CRUD、立绘生成/取图、版本历史/回滚；asset_id 防路径穿越、重名 409 |
| ProductionAPI | `/api/production` | 生产主流程：会话列表/快照/删除、启动、作业状态、剧本/分镜读写、AI 重写单镜、产物清单、质量与成本指标、清理、审核（通过/修改/继续/退回）、单镜重生成、批量接受、批量返工影响/费用/节省预览、批量重生成、产物版本对比/批注/回滚/恢复上一版、终审剪辑方案读写/重新合成/恢复原片、项目字幕时间线读写/恢复/SRT 下载、发布、成片下载、SSE 流 |
| BgmAPI | `/api/bgm` | 背景音乐库：列表/选曲/音量/上传、对白自动压低开关与强度 |
| VoiceAPI | `/api/voice` | TTS 音色：目录/持久化/在线试听 |
| SfxAPI | `/api/sfx` | 音效库：开关/音量/列表/上传 |
| FeaturesAPI | `/api/features` | Schema 驱动的全局开关（转场/字幕/钩子/海报/AIGC/预算/审核词/质量阈值/Skill 市场地址） |
| TemplatesAPI | `/api/templates` | 偏好记忆与命名模板（批量创作复用） |
| EditAPI | `/api/edit` | 导入视频后期：上传/ASR 转写/一次性应用/下载 |
| SkillsAPI | `/api/skills` | 风格 Skill 列表/上传/导入/fork/删除 |

---

## 12. 消息驱动审核工作流（飞书）

把一次性流水线升级为**消息驱动、分阶段人工审核、可暂停恢复**的个人自动化闭环：

- **审核门序**：`主题 → [剧本] → 通过 → [分镜脚本] → 通过 → [分镜视频] → 通过 → [成片] → 发布`。每阶段生成后建审核任务暂停，"通过"推进、"修改：…"重做当前阶段、可"退回"作废下游。
- **飞书集成**：
  - **出站**：官方 OpenAPI 发送审核卡/成片链接（`ChannelDispatcher` → `FeishuChannel`）。
  - **入站**：`/feishu/events` webhook，含 URL challenge、按 `event_id` 去重、可选 Encrypt Key 签名校验；约 3 秒内 ACK（靠后台作业化），处理完回执。
- **命令解析**（自由文本→结构化）：`通过/批准/发布`→approve、`修改：…`→revise、`重生成第 N 镜`→regenerate、`状态/进度`→status、`暂停/继续/取消`→生命周期、其余→新建主题。
- **入站治理**：`Authorizer`（(渠道,用户) 白名单）+ `InboundRateLimiter`（按用户每日次数上限，仅对付费生成命令生效）。
- 微信渠道为占位（未实现）；控制台渠道可本地驱动同一链路（`main_console.py`）。

---

## 13. 后台作业与治理

| 组件 | 作用 |
|---|---|
| **JobRunner** | 后台作业执行器：守护线程内跑独立事件循环；**单飞**（同会话在跑则拒并发）；**全局并发上限**；进度快照供轮询；任务历史；刷新后仍可见上次结果 |
| **ProductionService** | 把工作流各阶段包成后台作业；Web（轮询）与消息渠道（推送）共用一实例；批量返工合并重复依赖，以一个 `workflow.regenerate_shots` 任务串行执行最小根镜头并统一重建成片；支持批量接受、批量恢复上一可用版本及版本级批注 |
| **WorkflowEngine** | 分阶段审核状态机；idea/script 双模式；复用管线产物、崩溃续跑；治理挂钩 |
| **BudgetGuard** | 视频阶段前按场景/镜头总数上限拦截；设置改动免重启生效 |
| **ContentModerator** | 可插拔内容审核：内置敏感词离线拦截，cloud 为预留扩展点；intake + 视频前双重校验 |
| **CostEstimator** | 成本**估算**（非账单）：前瞻（按场景/镜头×单价，审核门提示）+ 实际（按已生成产物计数） |
| **ProductionMetrics** | 镜头级生成事实与决策事件：模型/请求/重试/耗时/候选、人工通过与返工原因、版本选择与批注、局部返工累计节省、估算和供应商实际费用分离；形成项目指标并在样本充分后反馈模型路由 |
| **RegenerationPreview** | 合并连续性账本或机位树推导的受影响镜头，返回选中数、实际影响数、最小执行根、锁定维度和 Profile 估价区间；同时与整片重做比较预计节省镜头数、时间和费用；无静态单价时返回不可估价状态，不伪造金额 |
| **TimelineEditService** | 维护项目级非破坏剪辑方案：按分镜映射成片时间范围，校验镜头顺序、入出点和基础转场，在持久后台任务中从不可变源重新合成；成片或分镜重生成后识别旧方案失效，每次应用或恢复前归档当前版本 |
| **SubtitleTimelineService** | 聚合逐场配音时间轴或 SRT，并映射当前剪辑方案；校验项目级字幕文本与起止时间，保存/恢复时归档旧版，按需导出标准 SRT，读取操作不落盘 |
| **HousekeepingService** | 磁盘清理：回收中间产物，保留成片/海报/字幕/剧本；需成片存在且显式触发，先干跑预览 |
| **实时进度（SSE）** | `GET /api/production/jobs/<id>/stream`，服务端轮询快照推送，保活 ping，30 分钟安全上限 |

**Web 鉴权**：个人版使用单个 `--token` / `SCENEFORGE_WEB_TOKEN`；支持 `Authorization: Bearer` / `X-Auth-Token` / `?token=`（供 `<img>`/`<a>` 媒体），并使用恒定时间比对。空管理令牌表示管理 API 无鉴权，仅适合本地开发；静态 UI 与飞书 webhook 豁免。

---

## 14. 模型提供方矩阵

引擎通过 `RenderBackend.from_config()` 按配置里的 `class_path` 动态实例化生成器（`tools/protocols.py` 用 `Protocol` 定义鸭子接口），**可任意切换 provider**，并按 RPM/RPD 注入限流。

**图像生成**：豆包 Seedream（云雾网关）、Gemini/NanoBanana（Google 官方 或 云雾网关）
**视频生成**：豆包 Seedance（云雾）、Omni（云雾）、Veo（Google 官方 或 云雾）、OpenRouter（含 Veo，可带音频）
**LLM（chat/VLM）**：LangChain `init_chat_model`，OpenAI 兼容为主（默认多模态 VLM，用于选图/评审）；内置 MiniMax preset，其余 OpenAI 兼容透传
**Rerank**：SiliconFlow BGE reranker（小说线 RAG 检索用）

> 图像与视频均支持文本+参考图、首帧/首尾帧驱动、分辨率/时长/帧率/画幅等参数，网关模型统一走建任务→轮询模式并带指数退避重试。

---

## 15. 配置体系

两套配置并存：

- **Pipeline YAML**（`configs/idea2video.yaml`、`script2video.yaml`）：完整生产参数——`chat_model`/`image_generator`/`video_generator`（含 `class_path` 与限流）、`working_dir`、`character_assets`（固定角色）、`language`（目标语言/中文模式/画面文字策略）、`subtitle`、`compliance.aigc_label`、`moderation`、`quality.consistency`、`creative.domain`、`video`（画幅/转场/钩子/封面）、`audio`（tts/bgm/sfx/loudnorm）、`hosting`、`messaging`、`security`、`rate_limits`、`generation_budget`。
- **Agent runtime YAML**（`configs/agent.example.yaml` 模板 / `agent.local.yaml` 实填）：精简的 `llm/image/video/embedding/reranker` 五段密钥，供 Agent 运行时与 Web 模型配置读写。

绝大多数产品开关既可在 YAML 配置，也可在设置页可视化调整（写回同一 YAML）；**多数高级功能默认关闭**，不开配置时行为与上游 ViMax 一致。

---

## 16. 关键数据模型（`interfaces/`）

| 模型 | 说明 |
|---|---|
| **Scene** | 场景：idx / is_last / environment / characters / script |
| **EnvironmentInScene** | slugline（如 `INT. COFFEE SHOP - NIGHT`）+ 纯环境描述 |
| **Character（三级）** | 场景级 / 事件级 / 小说级角色身份，逐级聚合静态特征跨范围复用 |
| **ShotBriefDescription / ShotDescription** | 分镜简述与完整分镜（首/尾帧描述、可见角色、运动、变化程度、屏显文字） |
| **Camera** | 机位树节点（父机位/覆盖关系/缺失信息） |
| **Frame** | 帧（shot_idx / first\|last / 机位 / 可见角色） |
| **Event** | 事件（描述 + 因果 process_chain） |
| **ImageOutput / VideoOutput** | 统一图像/视频产物封装（b64/url/pil/np/bytes），统一 `save()` |

---

## 17. 部署与运行

- **一条命令启动整套 Web 应用**：`python main_server.py`（默认 `127.0.0.1:8770`）。
  - `--host 0.0.0.0` 对局域网开放；`--token` / `SCENEFORGE_WEB_TOKEN` 开鉴权；`--port` 改端口。
  - 图像生成器惰性构建，**未配 key 也能起服务**（先在设置页配 key，再生成）。
  - 仅服务 `webui-dist/`（Vue 构建产物）；缺失时启动失败并提示先执行前端构建。
- **前端开发热更新**：`cd frontend && npm run dev`（Vite :5173，代理 `/api`、`/feishu`、SSE 到 :8770）。生产用 `npm run build`。
- **终端 Agent**：`sceneforge tui`（new / resume）。
- **命令行直出**：`main_idea2video.py`、`main_script2video.py`（在文件内填创意/剧本与配置）。
- **控制台审核**：`main_console.py`（与飞书 webhook 同一驱动链路）。
- 环境：Python 3.12，`uv sync` 安装依赖；打包 exe 时只需 Python + `webui-dist/`，运行时不依赖 Node。

---

## 18. 已知边界与未完成项

- **口型同步**：不支持改换对白口型（缺 lip-sync 模型）；自动后期的"换旁白"是换音轨，非改口型。
- **微信渠道**：占位未实现（合规/稳定性风险）；飞书需 `FEISHU_APP_ID/SECRET` + 公网地址；加密事件需 `cryptography`。
- **多人协作**：当前运行版本不提供多人账号、项目成员权限、外部审核链接、评论负责人或协作通知；未来规划见 `docs/多人协作产品规划.md`。
- **内容审核云服务**：仅接入点预留，需选定服务商与凭证。
- **Novel2Movie**：渲染链在仓库中仍有原有 TODO；产品化重心在 idea/script 线。
- **候选择优边界**：关键帧选优依赖当前视觉语言模型的判断，不是像素级质量保证；选择服务失败时采用首个成功候选，避免整条生产任务中断。
- 需用户侧输入才能生效的项：飞书凭证、云审核凭证、BGM 音乐文件、SFX 素材库、最终音色选定。

---

*本文档由代码库实地分析生成，反映当前实现状态；具体参数与默认值以 `configs/` 与设置页为准。*
