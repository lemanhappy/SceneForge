# SceneForge 消息审核工作流方案 — 可行性评审与优先项改造方案

> 评审对象：`docs/消息审核与角色设计方案.md`
> 评审基准：`SceneForge` 实际代码（`pipelines/`、`agents/`、`agent_runtime/`、`tools/`、`interfaces/`、`configs/`）
> 日期：2026-06-20

---

## 第一部分：总体可行性结论

**可以实现，且整体属于渐进式增强，无需推翻底层架构。** 方案中描述的多数「现有机制」在代码中真实存在；部分被列为「需新建」的能力，SceneForge 已有雏形。

### 关键判断：方案低估了已有的 `agent_runtime`

方案 §5/§20 建议新建 `WorkflowEngine` / `workflow/engine.py` / `commands.py` / `review.py`，仿佛从零开始。但代码里 `agent_runtime/` 已实现这套骨架的相当一部分：

| 方案设想 | 已有实现 | 位置 |
|---|---|---|
| Session 管理 / 状态机 | `SessionIndex`：`create/active/set_active/update_stage/artifact_checklist/mark_stale/append_log`，持久化到 `.sceneforge/sessions.json` | `agent_runtime/session_index.py` |
| 审核暂停点（规划后停下等用户） | `plan_text_artifacts()` 专门把文本规划与渲染切开 | `pipelines/script2video_pipeline.py:76` |
| 修改闭环 + 下游失效 | `sceneforge_narrative_planning` 支持 `revision_target`/`revision_instruction`，`_stale_keys_for_revision()` 自动标记下游（storyboard→frames→clips→final）失效 | `agent_runtime/sceneforge_adapters.py:215, 674` |
| 命令→工作流执行层 | 三个工具适配器 `sceneforge_narrative_planning / sceneforge_novel_planning / sceneforge_render_video` + React/tsx TUI | `agent_runtime/sceneforge_adapters.py:40`、`ui/src/cli.tsx` |

**建议：扩展 `agent_runtime`，不要另起一套 `workflow/engine.py`，否则会复现方案 §4 自己警告的「两套入口、两套状态行为」。**

### 逐模块可行性评级

| 方案模块 | 现状 | 可行性 | 说明 |
|---|---|---|---|
| 工作流状态机 / Session（§5） | 已有雏形 | 🟢 高 | 扩充 `SessionIndex` 状态枚举即可 |
| 审核任务 ReviewTask（§7） | 已有暂停点+revise | 🟢 高 | 把 `update_stage` 字符串状态升级为结构化 `ReviewTask` |
| 固定角色·参考图（§8/§10/§22） | 高度兼容 | 🟢 高 | `generate_single_image/_video` 已接受 `reference_image_paths`；`character_portraits_registry` 已存在，替换为固定图最小改动。**见第二部分** |
| 单镜头重生成（§5/§11/§12） | 部分 | 🟡 中 | 产物已按 `shots/<idx>/` 分目录、靠 `os.path.exists` 续跑；但当前原地覆盖、无版本号。**见第三部分** |
| 中文字幕（§14/§15） | 数据已就绪 | 🟡 中 | `ShotDescription.audio_desc` 已是结构化 `[Speaker] X (emotion): line` / `[Sound Effect]`，可解析；提取/时间轴/.ass/烧录需新建 |
| 中文模式（§14） | 部分 | 🟡 中 | 现仅有「输出语言随输入」弱约束（`screenwriter.py`），需注入独立 `chinese_runtime_instruction` |
| Prompt 外置 md（§13） | 全内联 | 🟡 中（量大） | 提示词全部内联在 `agents/*.py` 字符串模板，用 `.format()`；迁移机械但量大，需保留 fallback |
| 成本护栏（§17） | 仅限频 | 🟡 中 | 现只有 `RateLimiter`（请求数），无按 shot/角色/调用次数的预算硬上限 |
| 消息通道 飞书/微信（§6/§9） | 不存在 | 🟠 中 | 纯新建。飞书官方 API 可行；个人微信有合规风险，建议默认关 |
| 托管回传链接（§18） | 不存在 | 🟢 高 | 纯新建但简单，本地静态托管即可起步 |
| 授权与限频（§19） | 部分 | 🟢 高 | 限频已有，授权白名单新建，难度低 |
| LoRA / 3D / 供应商角色 ID（§8） | 不存在 | 🔴 低 | 方案 §22 自述首版不做，正确 |

### 主要风险

1. **产物版本化是单镜头重生成的前提**：现所有产物原地覆盖，靠存在性续跑；要实现「重生成第 4 镜不重跑全片 + 保留旧版」必须先解决版本化，改动面最广。
2. **角色一致性受底层视频模型能力限制**：方案 §10 的退化方案（固定参考图→首尾帧→视频）务实可行，因为现有 Doubao Seedance/Veo 走的就是首尾帧路线；真正的强一致性是模型天花板，不能假设。
3. **自然语言命令解析（§4）是新东西**：现 `main_agent.py` 只有 `/compact` 等少量命令；把"通过/修改/重生成第3镜"解析成 `UserCommand` 需单独设计并绑定审核状态机。

### 对实施顺序（§21）的修正

- 把「产物版本化」从阶段 10 **提到阶段 1**：它是单镜头重生成、不覆盖旧版的地基，越晚做返工越大。
- 阶段 1 改为「**扩展 `agent_runtime`**」而非新建 `WorkflowEngine`，复用 `SessionIndex` / `sceneforge_adapters` / 现有 revise+stale 机制。
- 最务实最小闭环：固定角色参考图 + 现有 `reference_image_paths` 注入 + 复用现有审核暂停点 + 飞书单通道 + 本地托管回链。几乎全踩在已有能力上。

---

## 第二部分：优先项 A — 固定角色参考图注入（函数级改造）

### 设计核心：在「registry 生产端」注入，下游零改动

下游消费 `character_portraits_registry` 的代码是**统一**的（`script2video_pipeline.py:301-305` 和 `501-505`）：

```python
identifier_in_scene = characters[character_idx].identifier_in_scene
registry_item = character_portraits_registry[identifier_in_scene]
for view, item in registry_item.items():
    available_image_path_and_text_pairs.append((item["path"], item["description"]))
```

registry 结构固定为 `{identifier_in_scene: {view: {"path": ..., "description": ...}}}`。
**只要固定角色也生成同结构的 registry 条目（path 指向固定图片），首帧/尾帧生成、视频生成全链路无需任何修改。** 这是改动最小、风险最低的切入点。

### 改造点 1：新增角色资产注册表加载（新文件）

新建 `characters/asset_registry.py`：

```python
# 读取 assets/characters/registry.yaml，返回 {asset_id: CharacterAsset}
# CharacterAsset 首版只需支持 type=reference_images：
#   asset_id, display_name, aliases, description, visual_prompt,
#   assets: {front: path, side: path, back: path, ...}
class CharacterAssetRegistry:
    @classmethod
    def from_yaml(cls, path: str) -> "CharacterAssetRegistry": ...
    def get(self, asset_id: str) -> CharacterAsset | None: ...
    def match(self, name: str, description: str) -> CharacterAsset | None:
        # 首版：name/alias 精确或模糊匹配即可，相似度匹配后置
        ...
```

### 改造点 2：`Script2VideoPipeline.__init__` 注入 binding（`pipelines/script2video_pipeline.py:48`）

新增可选入参，向后兼容（默认 `None` 时行为完全不变）：

```python
def __init__(self, chat_model, image_generator, video_generator, working_dir,
             character_bindings: dict[str, str] | None = None,   # {identifier_in_scene: asset_id}
             asset_registry: "CharacterAssetRegistry | None" = None):
    ...
    self.character_bindings = character_bindings or {}
    self.asset_registry = asset_registry
```

`__call__`（line 149）和 `init_from_config`（line 133）相应透传；`agent_runtime/sceneforge_adapters.py:sceneforge_render_video`（line 352 处构造 `Script2VideoPipeline`）读 `character_bindings.json` 后传入。

### 改造点 3：`generate_portraits_for_single_character` 增加固定资产分支（line 632）

这是唯一需要实质改动的生成函数。在函数顶部、`os.makedirs` 之后插入：

```python
async def generate_portraits_for_single_character(self, character, style, progress=None):
    identifier = character.identifier_in_scene
    # —— 固定角色短路：绑定了 reference_images 资产则直接复用，跳过生成 ——
    asset_id = self.character_bindings.get(identifier)
    if asset_id and self.asset_registry:
        asset = self.asset_registry.get(asset_id)
        if asset and asset.type == "reference_images":
            return self._registry_entry_from_fixed_asset(character, asset)
    # —— 否则走原有自动生成（front/side/back）——
    character_dir = os.path.join(self.working_dir, "character_portraits", ...)
    ...
```

新增私有方法（同文件内），把固定图拷进 session 目录并产出与自动生成**完全同构**的 registry 条目：

```python
def _registry_entry_from_fixed_asset(self, character, asset):
    identifier = character.identifier_in_scene
    character_dir = os.path.join(self.working_dir, "character_portraits",
                                 f"{character.idx}_{safe_path_component(identifier)}")
    os.makedirs(character_dir, exist_ok=True)
    entry = {}
    for view, src_path in asset.assets.items():        # front/side/back/...
        dst = os.path.join(character_dir, f"{view}.png")
        if not os.path.exists(dst):
            shutil.copy(src_path, dst)                 # 复制进 session，保证产物自洽
        entry[view] = {
            "path": dst,
            "description": f"A {view} view of {identifier} ({asset.display_name}). "
                           f"Fixed character asset, MUST keep appearance: {asset.description}",
        }
    self.character_portrait_events[character.idx].set()
    return {identifier: entry}
```

> 关键点：description 里写入「MUST keep appearance」类约束，会随 registry 进入 `ReferenceImageSelector` 的 prompt 前缀（line 378-380、529-531），帮助锁定外观。这也是方案 §13「角色约束注入」的落地点之一。

### 改造点 4（可选，外观锁冲突处理，方案 §16）

当用户修改意见涉及已锁定角色（如"林老师更年轻一点"），在 `sceneforge_narrative_planning` 的 revise 分支（`sceneforge_adapters.py:215`）增加一步校验：若 `character_bindings.json` 中该角色 `locked=true`，则不直接改写，返回确认提示（"是否解除固定"）。首版可后置。

### 工作量与风险

- 实质代码改动只在 1 个函数 + 1 个新方法 + 1 个新注册表文件；下游链路零改动。
- 向后兼容：不传 binding 时行为与现状逐字节一致。
- 唯一外部依赖：固定图片需是底层图像模型可用的参考图（现 `reference_image_paths` 已支持）。

---

## 第三部分：优先项 B — 产物版本化与单镜头重生成（函数级改造）

### 现状与难点

所有产物原地写盘、靠 `os.path.exists(...)` 判断是否跳过（`script2video_pipeline.py` 中 `first_frame.png`、`last_frame.png`、`video.mp4`、`*_selector_output.json`、`new_camera_*.png`、`transition_video_*.mp4` 等）。
难点是**镜头间依赖**：camera tree 中子相机会以父镜头的 `first_frame` 作为构图参考（`parent_shot_idx`，line 308-352、424-425），且 `priority_shot_idxs`（line 799）就是这些被依赖的父镜头。**重生成一个父镜头必须连带重算其子镜头。**

### 推荐路线：先做「归档式重生成」（最小可行），再演进到全版本化

#### 方案 B1（推荐首版）：归档 + 重跑，复用现有存在性续跑逻辑

新增方法 `regenerate_shot`，不改动现有 `__call__` 的续跑语义：

```python
async def regenerate_shot(self, shot_idx: int, *, script, user_requirement, style,
                          characters=None, character_portraits_registry=None,
                          progress=None):
    # 1. 计算受影响的镜头集合（自身 + 依赖它的子镜头）
    camera_tree = await self.construct_camera_tree(shot_descriptions=..., quiet=True)
    affected = self._collect_dependent_shots(shot_idx, camera_tree)   # 含 shot_idx 本身

    # 2. 归档（不删除，满足"不覆盖旧版本"）：
    #    shots/<idx>/ -> shots/<idx>/_archive/v{n}/
    for idx in affected:
        self._archive_shot_dir(idx)     # 把 first_frame/last_frame/video/selector_output 等移入 _archive/v{n}

    # 3. 删除受影响镜头的 final_video.mp4 触发重新拼接
    final = os.path.join(self.working_dir, "final_video.mp4")
    if os.path.exists(final): os.remove(final)

    # 4. 重新调用 __call__：现有 os.path.exists 续跑逻辑会"只"重算被归档（缺失）的镜头
    return await self.__call__(script=script, user_requirement=user_requirement,
                               style=style, characters=characters,
                               character_portraits_registry=character_portraits_registry,
                               progress=progress)
```

辅助方法：

```python
def _collect_dependent_shots(self, shot_idx, camera_tree) -> list[int]:
    # 广度优先：所有 parent_shot_idx 链上以 shot_idx 为祖先的镜头
    ...

def _archive_shot_dir(self, shot_idx):
    shot_dir = os.path.join(self.working_dir, "shots", str(shot_idx))
    if not os.path.isdir(shot_dir): return
    n = 1
    while os.path.exists(os.path.join(shot_dir, "_archive", f"v{n}")): n += 1
    archive = os.path.join(shot_dir, "_archive", f"v{n}")
    os.makedirs(archive, exist_ok=True)
    for name in os.listdir(shot_dir):
        if name == "_archive": continue
        shutil.move(os.path.join(shot_dir, name), os.path.join(archive, name))
```

> 优点：复用现有「存在即跳过」续跑逻辑，几乎不改 `__call__`；满足「不覆盖旧版本」「局部重生成只影响指定镜头及其依赖」「稳定 shot_id」三条原则（方案 §12）。
> 注意：`shot_description.json` 是否一并归档取决于用户是改画面（保留 desc，只重出图/视频）还是改剧本（连 desc 一起重算）。建议 `regenerate_shot` 增 `keep_description: bool=True` 控制。

#### 方案 B2（演进目标）：真·版本化产物树

按方案 §12 的 `script_v1/v2`、`video_v1/v2` 全量版本化，需要引入产物版本指针（如每个 session 维护 `manifest.json` 记录各 artifact 当前版本），改动面波及 `agent_runtime` 的 `artifact_checklist` / `_stale_keys_for_revision`（已有失效模型，可扩展为版本递增）。建议在 B1 跑通、需求确认后再做。

### 与 `agent_runtime` 的衔接

- 新增工具适配器 `sceneforge_regenerate_shot(session_id, shot_idx, keep_description)`，与现有 `sceneforge_render_video` 并列（`sceneforge_adapters.py:40` 的 `build_sceneforge_adapter_specs`）。
- 复用 `session_index.mark_stale` 记录受影响产物，`append_log("regenerations", ...)` 记录重生成历史与次数（用于方案 §17 的 `max_shot_regenerations` 上限）。

### 工作量与风险

- B1：1 个新方法 + 2 个辅助函数 + 1 个工具适配器；不改现有续跑语义，风险低。
- 主要正确性风险在 `_collect_dependent_shots` 的依赖闭包是否完整——必须覆盖 `parent_shot_idx` 全链，否则会出现"重生成父镜头但子镜头仍引用旧首帧"的不一致。建议为该函数单独写单测（已有 `tests/` 目录）。

---

## 附：建议的最小落地顺序

1. **产物版本化 B1（归档式重生成）** — 地基，先行。
2. **固定角色参考图注入 A** — 改动最小、收益最直接（人物一致性）。
3. 扩展 `agent_runtime`：`ReviewTask` 结构化 + `sceneforge_regenerate_shot` 适配器。
4. 中文模式 `chinese_runtime_instruction` 注入 + 字幕服务（数据已就绪）。
5. 飞书单通道 + 本地静态托管回链。

其余（Prompt 全量外置、微信、成本护栏完整化、LoRA/3D）按方案 §21 后续阶段推进。

---

## 第四部分：实现状态（2026-06-20 已落地）

以下五级均已实现并带单测；**所有新能力默认关闭**，不开启配置时行为与原版逐字节一致。新增/相关测试全绿（5 个新测试文件）。

### ① 产物版本化与单镜头重生成（B1）
- `pipelines/script2video_pipeline.py`：`regenerate_shot()` 归档式重生成（`shots/<idx>/_archive/v{n}/`，不覆盖旧版）；`_collect_dependent_shots()`（静态）计算依赖闭包（相机内锚点→兄弟、跨相机 `parent_shot_idx`→子锚点）；`_archive_shot_dir()`。
- 测试：`tests/test_shot_regeneration.py`（9）。

### ② 固定角色参考图注入（A）
- 新模块 `characters/`：`CharacterAsset` + `CharacterAssetRegistry`（YAML 加载/路径解析/别名匹配/`match_characters`）。
- `script2video_pipeline.py`：`__init__` 增 `character_bindings`/`asset_registry`；`generate_portraits_for_single_character` 固定资产短路（`_resolve_fixed_asset` + `_build_fixed_registry_entry`，下游零改动，注入"keep appearance"约束）。
- `init_from_config` 经 `CharacterAssetRegistry.from_config` 读取；配置 + `assets/characters/registry.example.yaml`。
- 测试：`tests/test_character_assets.py`（11）。
- 已知缺口：idea2video 顶层自有 `generate_portraits_for_single_character`，固定角色注入暂只在 script2video 层；idea 模式需同样处理。

### ③ agent_runtime 扩展
- `agent_runtime/sceneforge_adapters.py`：`sceneforge_regenerate_shot` 工具（端到端暴露 B1，归档版本数无状态强制 `SCENEFORGE_MAX_SHOT_REGENERATIONS`，默认 3）；`sceneforge_publish` 工具。
- `prompts/workflow.md` 增加路由指引。
- 测试：`tests/test_sceneforge_adapters.py` 新增 regenerate/publish 共 7 项。
- 已知缺口：结构化 `ReviewTask` 仍用 `update_stage` 字符串状态，未对象化。

### ④ 中文模式与字幕服务
- 新模块 `subtitles/`：`SubtitleService`（`audio_desc` 结构化提取/`motion_desc` 兜底/按文本长度时间轴/`.ass`+`.srt`/ffmpeg 烧录优雅降级），已接入 `Script2VideoPipeline.__call__`（script 与 idea 两模式均正确）。
- 新模块 `prompting/`：`chinese_runtime_instruction` + `is_chinese_mode`。
- 配置 `language` + `subtitle`（默认关闭）。
- 测试：`tests/test_subtitles.py`（17）。
- 已知缺口：中文约束尚未注入 screenwriter/storyboard 内联 prompt（与 prompt 外置耦合，触及生成主路径）；构建器与配置已就绪。

### ⑤ 飞书通道与本地托管回链
- 新模块 `artifacts/`：`ArtifactHost`（local_static，稳定去重命名）+ `HostedArtifact`。
- 新模块 `channels/`：`MessagingChannel` 基类 + `format_review`、`ConsoleChannel`、`FeishuChannel`（真实 OpenAPI、token 缓存、注入式 session）、`WeChatChannel`（占位，未实现即报错）、`ChannelDispatcher`（`${ENV}` 展开/enabled 过滤/广播）。
- `sceneforge_publish` 工具：托管成片 + 回传链接（hosting/messaging 未配置时回退本地路径）。
- 配置 `hosting` + `messaging`（默认关闭）。
- 测试：`tests/test_channels_hosting.py`（14）+ publish 适配器测试（3）。
- 待办：飞书活连接联调需 `FEISHU_APP_ID/SECRET`；飞书 inbound（事件订阅 webhook）未做，`receive()` 返回 []。

## 第五部分：遗留项已完成（2026-06-20 同日补齐）

第四部分各"已知缺口"已逐项解决（测试总数 225 通过）：

- **遗留①（idea2video 固定角色）**：注入逻辑抽到 `characters/injection.py`（`resolve_fixed_asset`/`build_fixed_registry_entry`），`Script2VideoPipeline` 委托复用，`Idea2VideoPipeline.__init__/init_from_config/generate_portraits_for_single_character` 同步支持固定角色。测试见 `test_character_assets.py::TestIdeaPipelineFixedInjection`。
- **遗留③（中文约束注入生成）**：`Screenwriter`/`StoryboardArtist` 构造器增 `extra_system_instruction`（`_system()` 附加到 system prompt）；两个 pipeline 经 `init_from_config` 从 `language` 配置构建并注入（CLI 路径生效；adapter 规划路径与字幕/托管同样的 config 边界）。测试见 `test_subtitles.py::TestChineseInjectionWiring`。
- **遗留②（结构化 ReviewTask）**：`agent_runtime/review.py` 的 `ReviewTask` 模型 + `SessionIndex` 持久化 CRUD（`create/list/get/resolve_review_task`，存于 `review_tasks`）+ `sceneforge_review` 工具（create/list/resolve）。测试见 `test_sceneforge_adapters.py::SessionIndexReviewTaskTests / SceneForgeReviewToolTests`。
- **遗留④（飞书 inbound + 命令解析）**：`commands/`（`UserCommand` + `parse_user_command`，识别 通过/修改/重生成第N镜/状态/暂停/继续/取消，余者 new_topic）+ `channels/feishu_inbound.py`（challenge、`verify_signature` sha256、事件解码、`event_to_command`；加密事件经 lazy `cryptography`，缺库时明确报错——飞书 Encrypt Key 为可选）。测试见 `test_commands_inbound.py`。

仍待用户侧：飞书**活连接**联调需 `FEISHU_APP_ID/SECRET`；飞书 inbound **webhook 服务器**（HTTP 接收）属部署范畴，未包含；若启用 Encrypt Key 需安装 `cryptography`。

## 第六部分：入站编排 + 一致性 + 健壮性（同日补齐）

- **授权与限频（§19）**：`services/authorization.py` 的 `Authorizer`（`authorized_sources` 允许列表，未配置则放行）+ `InboundRateLimiter`（每用户每日上限，超限即拒，非阻塞）。配置 `security`/`rate_limits`（默认放行/不限）。
- **入站编排 `TriggerService`（§4/§6）**：`services/trigger_service.py` 把 `UserCommand` 路由到 SceneForge 工具，前置授权/限频闸：new_topic→narrative_planning、regenerate→regenerate_shot（"第 N 镜"1-based→0-based）、status→snapshot、approve→resolve 待审 ReviewTask（final 则触发 publish）、revise→标记 revised + 记录指令、pause/resume/cancel→stage。测试 `test_services.py`。
- **adapter 渲染路径 config 注入**：`sceneforge_render_video` 经 `_render_services` 从 `configs/*.yaml` 构建 `asset_registry`/`subtitle_service`/`character_bindings`（无显式 `character_bindings.json` 时按名/别名自动匹配）；`sceneforge_narrative_planning` 注入 `chinese_instruction`（中文台词在规划阶段生效）。`Idea2VideoPipeline` 增 `subtitle_service` 透传子 pipeline。至此 agent 驱动路径与 CLI 路径在中文/字幕/固定角色上行为一致。测试 `test_sceneforge_adapters.py::RenderConfigInjectionTests`。
- **跨平台并发健壮性修复**：`SessionIndex._locked` 原在 Windows（`fcntl` 为 None）退化为空操作致并发 `os.replace` 竞争；改为进程内按路径 `threading.Lock` 序列化（全平台）+ POSIX `fcntl` 跨进程锁。`test_robustness` 并发用例由此转绿。

全量测试 **241 passed, 0 failed**（此前两个 Windows 基线失败均已解决：并发是真实生产修复；todo 是测试自身 `read_text` 未指定 encoding，已补 `encoding="utf-8"`）。

### 飞书 webhook 服务（同日补齐）
- `services/feishu_server.py`：`FeishuWebhookHandler` 跑通整条入站链路 `HTTP POST → handle_event → event_to_command → TriggerService → SceneForge 工具`，含 URL challenge、可选验签（仅在配置 Encrypt Key 且带签名头时强制）、可选回执；`from_config` 一键装配。`serve()` 是 stdlib `http.server` 薄封装（核心 `handle_request` 与 socket 分离，已单测）。测试 `test_commands_inbound.py::TestFeishuWebhookHandler`。
- 启动示例：构建 `SessionIndex`+`SceneForgeAdapters`+`TriggerService`+`FeishuChannel`，`FeishuWebhookHandler.from_config(config, trigger, channel)` 后 `serve(handler, port=8080)`，把该地址填入飞书事件订阅。

### 分阶段审核工作流引擎 + 控制台驱动（同日补齐）
- `services/workflow_engine.py`：`WorkflowEngine` 实现文档 §5 的分阶段审核——`topic → 剧本 → 分镜 → 视频 → 成片`，每阶段生成后建 `ReviewTask` 暂停；`start_topic` 起新主题生成剧本，`approve` 推进下一阶段，`revise` 重做当前阶段（修改意见并入 user_requirement 重生成）。各阶段复用现有 pipeline 方法（`develop_story/extract_characters/write_script`、`plan_text_artifacts`、`generate_character_portraits` + 逐场景 `__call__` + 合成），与 SceneForge 既有产物约定一致。状态机与生成方法（`_gen_*`）分离，状态机用 fake 子类全量单测（`test_services.py::TestWorkflowEngineStateMachine`）。
- `TriggerService` 接入：设了 `workflow_engine` 时，`new_topic→start_topic`、`approve→approve`、`revise→revise`（未设则回退到原 `sceneforge_narrative_planning` 行为，旧测试不受影响）。
- `main_console.py`：控制台驱动器，与飞书 webhook 对称——同一条 `parse_user_command → TriggerService(+WorkflowEngine) → SceneForge 工具` 链路。已验证真实端到端：`做个短片` 实际调用 LLM 生成多场景剧本（模型凭证取自 `./configs/agent.local.yaml`）。
- 命令凭证说明：`_build_*` 模型构造固定从仓库根 `./configs/agent.local.yaml`（或 `SCENEFORGE_*` 环境变量）取 key；session 产物落在 `--workspace` 指定的工作区。

### 真实端到端控制台测试发现并修复的问题（同日）
用王云宝修仙主题在控制台真实跑通全流程并出片(2 场景 / 6 镜头 / `final_video.mp4` 7.6MB),过程中修复 3 个真实问题(均有单测):
1. **引擎阶段中途失败卡死 session**：`approve`/`revise` 原先先消费当前审核再生成,生成失败则无待审、无法重试。改为**生成成功后再消费审核**,失败时保留上一阶段待审、stage 回退到 `*_review_pending`,修复后重发『通过』即可重试。
2. **`_build_video_generator` 不支持 Seedance**：agent 渲染路径只认 Veo/OpenRouter。新增 `provider=seedance/doubao` 分支,并按模型名含 `seedance` **自动识别**(即使 provider 仍为 yunwu),接通 `VideoGeneratorDoubaoSeedanceYunwuAPI`。
3. **字幕 burn-in 在 Windows 失败**：ffmpeg `ass` 滤镜把盘符冒号 `D:` 当成选项分隔符(`Unable to parse … as image size`)。改为 **cwd=字幕目录 + 滤镜只用 basename**(`ass=final.ass`),彻底规避路径转义;在真实成片上验证可烧出带字幕 mp4。
> 经验:模型名要与 yunwu 账号实际渠道一致——图像 `gemini-2.5-flash-image`(非 `-preview`)、视频 `doubao-seedance-1-5-pro-251215`。

### 产品化第一步：角色工作台后端 API（同日，零新依赖）
为"更像产品"的目标先做角色工作台(v1 后端 only,前端后续接)：
- `characters/asset_registry.py` 增 `open_or_create / upsert / remove / save`(写回 registry.yaml,图片路径相对化,便携)。
- `characters/studio.py` 的 `CharacterStudio`：建/列/改角色 + `generate_view`(front 从描述生成、side/back 以 front 为参考保持一致;反复调用即"在页面上优化角色")。存好的角色即固定资产,视频生成绑定它→根治人物不一致。
- `server/character_api.py` 的 `CharacterStudioAPI`(stdlib http.server,zero-dep,与已有 feishu_server 一致;handler 与 socket 分离便于测试)+ `main_character_api.py` 入口。坏编码 body 返回干净 400 而非崩溃。
- 端点：`GET/POST /api/characters`、`GET/POST/DELETE /api/characters/{id}`、`POST /api/characters/{id}/generate`、`GET /api/characters/{id}/image/{view}`。
- 已真实起服务验证(创建/列出中文角色、写入 registry.yaml、坏编码 400);单测 `tests/test_character_studio.py`(6)。
- 启动：`python main_character_api.py --registry assets/characters/registry.yaml --port 8770`(模型 key 取自 configs/agent.local.yaml)。
- 后续已补齐(见下)。

### 产品化第二步：配置后台 API + 生产页 API + 网页前端 + 统一服务（同日，零新依赖）
按"依次补"全部完成:
- **配置后台 API**：`server/config_service.py` + `server/config_api.py`。读写 `configs/agent.local.yaml` 的 llm/image/video/embedding/reranker(model/base_url/api_key/provider);GET 时 api_key 脱敏(只露 set + 末4位 hint),PUT 时空 key 不覆盖;写后清 `load_agent_config` 缓存即时生效;video provider 从 base_url 派生只读展示。
- **生产页 API**：`server/production_api.py`。包 WorkflowEngine + regenerate-shot + publish:`POST /api/production/topic`、`GET /api/production[/{sid}]`、`POST .../approve|revise|regenerate-shot|publish`、`GET .../video`。
- **统一服务**：`server/app.py` 的 `AppAPI` 把三组 API(/api/config、/api/characters、/api/production)挂一个服务并托管静态前端;`serve()` 共享 stdlib socket 层(JSON + `_file` 二进制流 + CORS,坏编码→400)。
- **网页前端**：`frontend/` 中的 Vue 3 应用，经 Vite 构建到 `webui-dist/` 后由后端托管。
- **入口**：`python main_server.py`(默认 http://127.0.0.1:8770)。图像生成器惰性构建,无 key 也能起服务→先在设置页填 key 再生成。
- 已真实起服务验证(GET / 返回前端、/api/config 脱敏、/api/production、/api/characters);单测 `tests/test_web_apis.py`(8)+`test_character_studio.py`(6)。全量 268 passed。
- 仍可加强(非必需):前端用 React 重写、生产页 SSE 实时进度、配置页加字幕/语言/固定角色开关。

### 产品化第三步：后台任务化 + 出站通知（同日，#1 最关键的异步地基）
原来 approve→视频阶段同步阻塞十几分钟,会让网页请求超时、飞书 webhook 无法 3 秒内 ACK。改造:
- `services/job_runner.py` `JobRunner`:守护线程跑异步协程(各自事件循环)+ 状态查询 + **per-session 单飞**(同会话上一步未完成时拒绝重复"通过",顺带解决并发双跑)。
- `services/production_service.py` `ProductionService`:start_topic/approve/revise/regenerate_shot **提交为后台任务**,立即返回 job 记录;完成后若给了 target,经 notifier(ChannelDispatcher)**推送审核摘要**("【storyboard 已就绪】…请回复 通过/修改")。web 传 target=None→只轮询不推送;消息通道传 sender→推送。
- `ProductionAPI`:topic/approve/revise/regenerate 改为提交任务返回 `{job_id}`,新增 `GET /api/production/jobs/{id}` 轮询;snapshot 加 `busy` 字段。`webui` 前端改为提交→轮询 job→刷新。
- `TriggerService`:设了 production_service 时,new_topic/approve/revise/regenerate **立即 ACK**("已开始…完成后通知你")并后台执行+完成推送——这样飞书 webhook 能快速 ACK。
- `main_server`:装配 JobRunner + ProductionService + 从 messaging 配置构建的 dispatcher 作 notifier。
- 测试:`test_services.py`(JobRunner/ProductionService/Trigger 后台)+ `test_web_apis.py`(job 轮询流);全量 274 passed。

### 产品化第四步：飞书 webhook 挂统一服务 + 事件去重（#4）
- **挂载**:`server/app.py` 的 `serve()` 增 `feishu_handler`/`feishu_path` 参数;socket 层对 `/feishu/events` 特判,把**原始 body + headers**传给 `FeishuWebhookHandler.handle_request`(验签需要原始字节,绕开 JSON 解析),其余路由仍走 `AppAPI.handle`。一个服务一个端口同时服务浏览器 + 飞书。
- **去重**:`handle_event` 返回 `event_id`;`FeishuWebhookHandler` 用有界 LRU(线程安全)按 `event_id` 去重——飞书超时重发的同一事件**只触发一次**生成,重复直接 200 ACK。
- **快速 ACK**:消息→去重→`TriggerService.handle_command`(production_service 后台路径,秒回 note 作回执)→ webhook 立即 200;生成在后台跑、完成经 dispatcher 推送结果。
- `main_server` 仅在 messaging 配置启用飞书时,构建 `TriggerService(production_service)` + 从 dispatcher 取 FeishuChannel + `FeishuWebhookHandler`,挂到 serve。
- 已真实起服务验证:`/feishu/events` 的 url_verification challenge 握手返回 `{challenge}`、日志显示已挂载。单测含去重(同 event_id 只派发一次)。全量 276 passed。

### 产品化第五步：网页鉴权（#3）
- `server/app.py` 新增 `authorized(headers, token)`(常量时间比对,支持 `Authorization: Bearer` 或 `X-Auth-Token`,token 为空=鉴权关闭便于本地)。`serve()` 增 `auth_token`:对 `/api/*` 校验,失败 401;**静态页与 `/feishu/events` 豁免**(前者要先加载以输入令牌,后者有飞书签名)。
- `main_server` 从 `SCENEFORGE_WEB_TOKEN` 或 `--token` 读令牌,未设则打印警告(裸奔)。
- 前端 `api()` 自动带 `Authorization: Bearer <localStorage token>`,遇 401 弹框输入令牌并存。
- 真实验证:无/错令牌 `/api/*`→401,正确→200,静态 `/`→200(公开);单测 `authorized()`。全量 278 passed。
- 注意:这是单令牌共享鉴权(适合自托管/小团队);要多用户/角色需再加账号体系。

### 产品化第六步：审核内容接口（#5,可视化审核）
- `server/artifacts_reader.py`(纯函数):`read_script`(story+scene scripts+characters)、`read_storyboard`(各场景 shots 的 visual/audio_desc)、`build_manifest`(各镜头可用媒体的相对路径)、`resolve_file`(会话目录内、防路径穿越)。
- `ProductionAPI` 新增 `GET /{sid}/script | storyboard | artifacts | file?path=`(file 防穿越,按扩展名定 content-type)。
- **媒体鉴权**:`<img>`/`<a>` 不能带头,故 `authorized` 增 `?token=` query 参数支持;serve() 提取并校验。
- 前端:审核详情页拉取 script/storyboard/artifacts,渲染**剧本全文 + 分镜(视觉/音频描述)+ 首帧缩略图**;角色画像与成片链接也用 `mediaUrl()` 附 token。
- 真实验证:`/script` 返回内容、`/file?path=&token=` 200、无 token 401、路径穿越被挡。单测 `TestArtifactsReader` + 内容路由。全量 280 passed。

### 产品化第七步：成本护栏 + 并发上限（#6）
- `services/budget.py` `BudgetGuard`:`from_config` 读 `generation_budget`(max_scenes/max_total_shots/max_shot_regenerations)+ `rate_limits.global.max_concurrent_generations`;`check_render(scenes, shots)` 在**视频阶段前**卡场景/镜头数(视频成本随镜头数上升)。
- `WorkflowEngine`:approve 在 storyboard→video 闸前调 `budget.check_render`(从磁盘 storyboard 统计场景/镜头),超限则**保留 storyboard 待审**并返回 `budget_exceeded`+提示"回复 修改:减到 N 镜以内",不进入烧钱的视频生成。
- `JobRunner`:增全局 `max_concurrent` 并发上限,超额 submit 返回 `at_capacity`(per-session 单飞之外再加全局闸)。
- `main_server`:`BudgetGuard.from_config` 注入 engine,`JobRunner(max_concurrent=...)`。
- 配置 `generation_budget`(默认 max_scenes 3/max_total_shots 12/max_shot_regenerations 2)。单测覆盖 BudgetGuard、并发上限、引擎预算闸(超额保留待审)。全量 284 passed。

### 仍属部署/用户侧（非代码缺口）
- 飞书**活连接**需 `FEISHU_APP_ID/SECRET`、在开发者后台开启事件订阅并把回调地址填为 `http(s)://<公网>/feishu/events`、公网可达(内网穿透/部署)；Encrypt Key 若启用需 `cryptography`。
- 生产部署 webhook 建议放在 WSGI/ASGI（如 uvicorn/gunicorn）之后；`serve()` 的 stdlib 服务器仅适合本地/轻量场景。
- `Novel2MoviePipeline` 仍为部分实现（仓库原有 TODO，本次未涉及）。
