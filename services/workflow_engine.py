"""Staged, review-gated production workflow (design §5).

Drives a topic through gated stages, pausing for user confirmation at each:

    topic -> [script] -> 通过 -> [storyboard] -> 通过 -> [video] -> 通过 -> [final] -> 通过 -> 发布

Each stage reuses the existing pipeline building blocks (Idea2VideoPipeline /
Script2VideoPipeline methods) and writes the same on-disk artifacts, so it stays
compatible with the rest of SceneForge. The state machine (start/approve/revise +
ReviewTask creation) is separated from the generation methods (`_gen_*`) so it
can be unit-tested with a fake subclass that overrides generation.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, List, Optional

from services.stage_handlers import StageHandlerRegistry, split_script_into_scenes as _split_script_into_scenes

# review gate order; approving gate N runs the generation for gate N+1
GATES = ["script", "storyboard", "shot_video", "final"]

_PROJECT_CONFIG_SECTIONS = (
    "language",
    "subtitle",
    "video",
    "compliance",
    "quality",
    "audio",
    "creative",
    "generation",
    "character_assets",
)
_SECRET_CONFIG_KEYS = {"api_key", "app_secret", "access_token", "secret"}


def _scene_number(name: str) -> int:
    try:
        return int(str(name).rsplit("_", 1)[1])
    except (IndexError, TypeError, ValueError):
        return -1

class WorkflowEngine:
    def __init__(self, session_index, workspace_root: str | Path, adapters: Any = None, budget: Any = None,
                 moderator: Any = None, cost_estimator: Any = None,
                 provider_registry: Any = None,
                 asset_catalog_repository: Any = None,
                 lora_service: Any = None,
                 stage_handlers: StageHandlerRegistry | None = None,
                 artifact_versions: Any = None):
        self.session_index = session_index
        self.workspace_root = Path(workspace_root).resolve()
        self.adapters = adapters  # optional SceneForgeAdapters, used for publish
        self.budget = budget  # optional BudgetGuard, gates the video stage
        self.moderator = moderator  # optional ContentModerator, gates topic intake
        self.cost_estimator = cost_estimator  # optional CostEstimator, for plan-ahead
        self.provider_registry = provider_registry
        self.asset_catalog_repository = asset_catalog_repository
        self.lora_service = lora_service
        self.stage_handlers = stage_handlers or StageHandlerRegistry.default()
        self.artifact_versions = artifact_versions or self._default_artifact_versions()

    def _default_artifact_versions(self):
        state_store = getattr(self.session_index, "state_store", None)
        database = getattr(state_store, "database", None)
        if database is None:
            return None
        from infrastructure.sqlite.artifact_repository import SQLiteArtifactRepository
        from services.artifact_versions import ArtifactVersionService

        return ArtifactVersionService(
            SQLiteArtifactRepository(database),
            self.workspace_root,
            external_roots_provider=self._artifact_storage_roots,
        )

    def _artifact_storage_roots(self) -> list[Path]:
        roots = [Path(getattr(self.session_index, "working_root", self.workspace_root / ".working_dir"))]
        for record in self.session_index.list_sessions():
            value = str(record.get("storage_root") or "").strip()
            if value:
                roots.append(Path(value))
        return roots

    # ----- config helpers ----------------------------------------------

    def _config(self) -> dict:
        import yaml
        path = self.workspace_root / "configs" / "idea2video.yaml"
        if not path.exists():
            return {}
        try:
            return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            return {}

    def _chinese_instruction(self) -> str:
        from prompting import runtime_language_instruction
        return runtime_language_instruction(self._config())

    @staticmethod
    def _without_secrets(value):
        if isinstance(value, dict):
            return {
                key: WorkflowEngine._without_secrets(item)
                for key, item in value.items()
                if str(key).lower() not in _SECRET_CONFIG_KEYS
            }
        if isinstance(value, list):
            return [WorkflowEngine._without_secrets(item) for item in value]
        return value

    def _project_config_snapshot(self) -> dict:
        """Freeze non-secret creative/render defaults for one project."""
        import copy

        config = self._config()
        return {
            section: self._without_secrets(copy.deepcopy(config[section]))
            for section in _PROJECT_CONFIG_SECTIONS
            if section in config
        }

    def _live_moderator(self):
        """Reload policy settings so Settings changes apply without a restart."""
        from services.moderation import ContentModerator

        config = self._config()
        if "moderation" in config:
            return ContentModerator.from_config(config)
        return self.moderator

    def _live_budget(self):
        """The budget guard to enforce, rebuilt from the CURRENT config so edits
        in 设置→生成预算 apply without a server restart. Falls back to the
        startup-injected guard when the config carries no ``generation_budget``
        (e.g. tests with no config file)."""
        from services.budget import BudgetGuard
        cfg = self._config()
        if cfg.get("generation_budget"):
            return BudgetGuard.from_config(cfg)
        return self.budget

    def budget_preview(self, session_id: str) -> Optional[dict]:
        """Shot/scene counts vs the budget limits at the storyboard stage, so the
        UI can warn BEFORE the user clicks 通过 (instead of only after a rejected
        approve). ``None`` when no storyboard yet or no budget is configured."""
        bg = self._live_budget()
        if bg is None:
            return None
        try:
            scenes, shots = self._count_storyboard(session_id)
        except Exception:
            return None
        if not shots:
            return None
        ok, msg = bg.check_render(scenes, shots)
        return {"scenes": scenes, "shots": shots, "max_scenes": bg.max_scenes,
                "max_total_shots": bg.max_total_shots, "ok": ok, "note": msg,
                "per_scene": self._per_scene_shots(session_id)}

    def _per_scene_shots(self, session_id: str) -> list:
        """Per-scene shot counts ``[n0, n1, ...]`` from the storyboards on disk, so
        the review UI can show '场景1: 5镜、场景2: 8镜' and let the user rebalance."""
        import json as _json
        idea = self._idea_dir(session_id)
        counts = []
        if idea.exists():
            for scene_dir in sorted(idea.glob("scene_*")):
                sb = scene_dir / "storyboard.json"
                if sb.exists():
                    try:
                        counts.append(len(_json.loads(sb.read_text(encoding="utf-8"))))
                    except Exception:
                        counts.append(0)
        return counts

    def _effective_config(self, session: dict) -> dict:
        """Global config with this session's per-video overrides (target_language /
        aspect_ratio) applied — so each video uses its own language/画幅 while the
        rest stays the global default. ``None`` on a session field = no override."""
        import copy
        session = session or {}
        cfg = copy.deepcopy(self._config())
        snapshot = session.get("config_snapshot")
        if isinstance(snapshot, dict):
            for section, value in snapshot.items():
                cfg[section] = copy.deepcopy(value)
        tl = session.get("target_language")
        if tl is not None:
            cfg.setdefault("language", {})["target_language"] = tl
        asp = session.get("aspect_ratio")
        if asp:
            cfg.setdefault("video", {})["aspect_ratio"] = asp
        ov = session.get("overrides") or {}
        if ov.get("subtitle_enabled") is not None:
            cfg.setdefault("subtitle", {})["enabled"] = bool(ov["subtitle_enabled"])
        if ov.get("subtitle_burn_in") is not None:
            cfg.setdefault("subtitle", {})["burn_in"] = bool(ov["subtitle_burn_in"])
        if ov.get("tts_enabled") is not None:
            cfg.setdefault("audio", {}).setdefault("tts", {})["enabled"] = bool(ov["tts_enabled"])
        if ov.get("voice"):
            tts = cfg.setdefault("audio", {}).setdefault("tts", {})
            if str(tts.get("provider") or "openai").lower() == "minimax":
                tts["voice_id"] = ov["voice"]
            else:
                tts["voice"] = ov["voice"]
        bgm_track = ov.get("bgm_track")
        if bgm_track == "__none__":          # this video: no background music
            cfg.setdefault("audio", {}).setdefault("bgm", {})["enabled"] = False
        elif bgm_track:                       # this video: a specific library track
            from audio.bgm_library import BgmLibrary
            bdir = (cfg.get("audio", {}).get("bgm", {}) or {}).get("dir") or str(self.workspace_root / "assets" / "bgm")
            p = BgmLibrary(bdir).track_path(bgm_track)
            if p:
                bgm = cfg.setdefault("audio", {}).setdefault("bgm", {})
                bgm["enabled"], bgm["path"] = True, p
        from services.quality_profiles import apply_quality_profile
        return apply_quality_profile(cfg, session.get("quality_tier"))

    def provider_route_preview(self, session_id: str) -> dict:
        if self.provider_registry is None:
            return {"ok": True, "routes": [], "note": "未启用模型能力注册表"}
        refresh = getattr(self.provider_registry, "refresh", None)
        if callable(refresh):
            refresh()
        session = self.session_index.get(session_id)
        if session is None:
            return {"ok": False, "routes": [], "note": "项目不存在"}
        from domain.providers import MediaType, ModelRequirement, QualityTier
        from services.provider_registry import NoCompatibleProviderError

        tier = QualityTier(str(session.get("quality_tier") or "balanced"))
        try:
            characters = self._load_characters(self._idea_dir(session_id))
        except (FileNotFoundError, OSError, ValueError):
            characters = []
        character_count = len(characters)
        reusable_count = len(self._reusable_assets(session))
        total_reference_count = character_count + reusable_count
        selected_assets = self._registry_for(session)
        enabled_bindings = [
            binding
            for asset in (selected_assets.all() if selected_assets else [])
            for binding in asset.enabled_render_bindings()
        ]
        project_loras = list(session.get("lora_bindings") or [])
        requires_native_lora = any(
            str(item.get("application_mode") or "native") == "native"
            for item in project_loras if item.get("enabled", True)
        )
        aspect = str(session.get("aspect_ratio") or "landscape")
        requirements = [("image", ModelRequirement(
            media_type=MediaType.IMAGE,
            text_to_image=True,
            image_to_image=True,
            multi_reference=total_reference_count > 1,
            multi_character_reference=character_count > 1,
            provider_character_id=any(item.kind == "provider_character_id" for item in enabled_bindings),
            lora=any(item.kind == "lora" for item in enabled_bindings) or requires_native_lora,
            aspect_ratio=aspect,
            reference_count=max(1, total_reference_count),
        ))]
        planned_durations = self._storyboard_durations(session_id)
        # Providers commonly expose only a few duration presets. The pipeline
        # normalizes each storyboard duration to a supported preset at submit
        # time, so capability validation must not reject an otherwise valid job.
        requirements.append(("video", ModelRequirement(
            media_type=MediaType.VIDEO,
            image_to_video=True,
            first_last_frame=True,
            multi_reference=True,
            aspect_ratio=aspect,
            reference_count=2,
        )))
        routes = []
        try:
            for purpose, requirement in requirements:
                route = {
                    "purpose": purpose,
                    **self.provider_registry.route(requirement, quality_tier=tier).to_dict(),
                }
                if purpose == "video":
                    route["planned_durations"] = planned_durations
                routes.append(route)
        except NoCompatibleProviderError as exc:
            return {
                "ok": False,
                "routes": routes,
                "quality_tier": tier.value,
                "note": "当前模型不支持这个项目的生成要求，请调整画幅、镜头时长或专业角色绑定。",
                "detail": str(exc),
            }
        return {"ok": True, "routes": routes, "quality_tier": tier.value,
                "note": "模型能力校验通过"}

    def selected_video_route(self, session: dict | None) -> dict:
        """Resolve the secret-free video route stored on a project."""
        session = session or {}
        latest = self.session_index.get(str(session.get("session_id") or "")) or session
        explicit = str(latest.get("video_profile_id") or "").strip()
        route = latest.get("provider_route")
        if not isinstance(route, dict):
            route = self.provider_route_preview(str(latest.get("session_id") or ""))
        for item in route.get("routes") or []:
            if isinstance(item, dict) and item.get("purpose") == "video":
                if not explicit or str(item.get("profile_id") or "").strip() == explicit:
                    return dict(item)
        if explicit and self.provider_registry is not None:
            for item in self.provider_registry.public_catalog():
                if str(item.get("profile_id") or "").strip() == explicit:
                    return {"purpose": "video", **item}
        return {"purpose": "video", "profile_id": explicit} if explicit else {}

    def selected_video_profile_id(self, session: dict | None) -> str | None:
        """Resolve the routed profile stored on a project, or preview it on demand."""
        profile_id = str(self.selected_video_route(session).get("profile_id") or "").strip()
        return profile_id or None

    def _storyboard_durations(self, session_id: str) -> list[int]:
        durations = set()
        for path in sorted(self._idea_dir(session_id).glob("scene_*/storyboard.json")):
            try:
                shots = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            for shot in shots if isinstance(shots, list) else []:
                raw = shot.get("duration_sec") if isinstance(shot, dict) else None
                try:
                    value = int(round(float(raw)))
                except (TypeError, ValueError):
                    continue
                if value > 0:
                    durations.add(value)
        return sorted(durations)

    def _lang_instruction(self, session: dict) -> str:
        from prompting import runtime_language_instruction
        return runtime_language_instruction(self._effective_config(session))

    def _domain(self, session: dict) -> str:
        """Domain-specific reasoning key (短剧/解说/科普…): per-session choice from
        the production form wins, else the config default (creative.domain)."""
        config = self._effective_config(session or {})
        return str((session or {}).get("domain") or (config.get("creative") or {}).get("domain") or "")

    def _asset_registry(self, config: dict | None = None):
        from characters import AssetCatalog
        return AssetCatalog.from_config(config or self._config())

    def _registry_for(self, session: dict):
        """Resolve the character asset registry for a session.

        When the user explicitly picked cast members on the production form
        (``character_asset_ids``), load the studio registry *regardless* of the
        global ``character_assets.enabled`` flag and narrow it to just those
        assets — explicit selection is opt-in and shouldn't require flipping a
        global switch, and narrowing keeps auto-matching from binding unrelated
        saved characters. Otherwise fall back to the config-driven auto registry."""
        ids = (session or {}).get("character_asset_ids") or []
        if not ids:
            return self._asset_registry(self._effective_config(session))
        from characters import AssetCatalog
        path = ((self._effective_config(session).get("character_assets") or {}).get("registry_path")
                or "assets/characters/registry.yaml")
        p = Path(path) if os.path.isabs(path) else (self.workspace_root / path)
        if not p.exists():
            return None
        full = AssetCatalog.from_yaml(str(p))
        selected = {aid: full.get(aid) for aid in ids if full.get(aid) is not None}
        if not selected:
            return None
        return AssetCatalog(selected, base_dir=full.base_dir, registry_path=full.registry_path)

    def _cast_brief(self, session: dict) -> str:
        """An '出场角色' block built from the explicitly-selected cast, instructing
        the writer to reuse these exact names so they match their fixed assets."""
        reg = self._registry_for(session)
        ids = (session or {}).get("character_asset_ids") or []
        if reg is None or not ids:
            return ""
        lines = []
        for asset in reg.all():
            desc = (asset.description or "").strip()
            bible = asset.bible_constraint() if hasattr(asset, "bible_constraint") else ""
            details = ". ".join(part for part in (desc, bible) if part)
            lines.append(f"- {asset.display_name}: {details}" if details else f"- {asset.display_name}")
        if not lines:
            return ""
        return ("出场角色（必须在剧本与台词中沿用以下角色名，不要改名或新增主要角色）：\n"
                + "\n".join(lines))

    def _reusable_assets(self, session: dict) -> list:
        if self.asset_catalog_repository is None:
            return []
        selected = [
            *(session.get("prop_asset_ids") or []),
            *(session.get("scene_asset_ids") or []),
        ]
        assets = []
        for asset_id in selected:
            try:
                asset = self.asset_catalog_repository.get_asset(str(asset_id))
            except Exception:
                asset = None
            if asset is not None:
                assets.append(asset)
        return assets

    def _reusable_asset_brief(self, session: dict) -> str:
        assets = self._reusable_assets(session)
        if not assets:
            return ""
        lines = [f"- {asset.prompt_constraint()}" for asset in assets]
        return (
            "固定资产模型（在剧本、分镜和画面中沿用同一名称与外观；未出镜时不要强行加入）：\n"
            + "\n".join(lines)
        )

    def _reusable_reference_pairs(self, session: dict) -> list[tuple[str, str]]:
        pairs = []
        for asset in self._reusable_assets(session):
            path = (asset.assets or {}).get("reference")
            if path and Path(path).is_file():
                pairs.append((
                    str(path),
                    f"[{asset.asset_type}] {asset.prompt_constraint()}",
                ))
        return pairs

    def _continuity_source(self, session: dict, scene_index: int) -> tuple[dict, dict]:
        """Resolve the previous scene, or the selected previous episode for scene 0."""
        current_session_id = str(session.get("session_id") or "")
        if int(scene_index) > 0:
            source_session_id = current_session_id
            source_scene_index = int(scene_index) - 1
            scene_dir = self._idea_dir(source_session_id) / f"scene_{source_scene_index}"
        else:
            source_session_id = str(session.get("continuity_source_session_id") or "").strip()
            if not source_session_id:
                return {}, {}
            source_root = self._idea_dir(source_session_id)
            candidates = [
                path for path in source_root.glob("scene_*")
                if path.is_dir() and (
                    (path / "continuity_ledger.json").is_file()
                    or (path / "continuity_contracts.json").is_file()
                )
            ]
            if not candidates:
                return {}, {}
            scene_dir = max(candidates, key=lambda path: _scene_number(path.name))
            source_scene_index = _scene_number(scene_dir.name)
        ledger = self._load_continuity_source_ledger(scene_dir, source_session_id)
        if not ledger.get("shots"):
            return {}, {}
        return ledger, {
            "source_session_id": source_session_id,
            "source_scene_index": source_scene_index,
            "source_kind": "previous_scene" if source_session_id == current_session_id else "previous_episode",
        }

    def _load_continuity_source_ledger(self, scene_dir: Path, source_session_id: str) -> dict:
        from quality import load_continuity_ledger

        ledger = load_continuity_ledger(scene_dir / "continuity_ledger.json")
        if ledger.get("shots"):
            return ledger
        try:
            contracts = json.loads((scene_dir / "continuity_contracts.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        source_session = self.session_index.get(source_session_id) or {}
        character_ids = list(source_session.get("character_asset_ids") or [])
        prop_ids = list(source_session.get("prop_asset_ids") or [])
        scene_ids = list(source_session.get("scene_asset_ids") or [])
        shots = {}
        for key, contract in (contracts.get("shots") or {}).items():
            if not isinstance(contract, dict):
                continue
            try:
                shot_idx = int(contract.get("shot_idx", key))
            except (TypeError, ValueError):
                continue
            reference = contract.get("continuity_reference_shot_idx")
            shots[str(shot_idx)] = {
                "shot_idx": shot_idx,
                "depends_on_shot_idxs": [int(reference)] if reference is not None else [],
                "character_asset_ids": character_ids,
                "prop_asset_ids": prop_ids,
                "scene_asset_ids": scene_ids,
                "initial_state": contract.get("initial_state") or {},
                "final_state": contract.get("final_state") or {},
                "transitions": contract.get("action_transitions") or [],
                "issue_codes": list(contract.get("prompt_issue_codes") or []),
            }
        return {
            "version": 0,
            "legacy_source": "continuity_contracts",
            "asset_bibles": {},
            "asset_usage": {},
            "shots": shots,
            "summary": {"shot_count": len(shots)},
        }

    def _continuity_reference_pair(self, session: dict, scene_index: int) -> tuple[str, str] | None:
        from quality import continuity_handoff

        ledger, source = self._continuity_source(session, scene_index)
        if not ledger:
            return None
        handoff = continuity_handoff(ledger)
        shot_idx = handoff.get("source_shot_idx")
        if shot_idx is None:
            return None
        source_dir = self._idea_dir(str(source["source_session_id"])) / f"scene_{source['source_scene_index']}"
        shot_dir = source_dir / "shots" / str(shot_idx)
        target = next(
            (path for path in (shot_dir / "last_frame.png", shot_dir / "first_frame.png") if path.is_file()),
            None,
        )
        if target is None:
            return None
        return str(target), (
            "[continuity] 上一场/集收尾画面；首镜必须沿用其中的人物外观、服装、"
            "道具持有状态、环境时间与光线，除非剧本明确写出变化"
        )

    def _continuity_inheritance_brief(self, session: dict) -> str:
        if not session.get("continuity_source_session_id"):
            return ""
        from quality import continuity_handoff

        ledger, source = self._continuity_source(session, 0)
        if not ledger:
            return ""
        handoff = continuity_handoff(ledger)
        final_state = handoff.get("final_state") or {}
        prop_lines = []
        for prop in final_state.get("props") or []:
            if not isinstance(prop, dict):
                continue
            holder = prop.get("holder_character_idx")
            location = f"由角色 {int(holder) + 1} 持有" if holder is not None else str(prop.get("support") or "位置未指定")
            prop_lines.append(f"{prop.get('label') or prop.get('prop_id')}：{location}")
        lines = [
            "上一集连续性状态（新剧本必须从此状态开始；若要改变，需写出明确过渡）：",
            f"- 来源项目：{source.get('source_session_id')}，场景 {int(source.get('source_scene_index', 0)) + 1}，镜头 {int(handoff.get('source_shot_idx', 0)) + 1}",
        ]
        if handoff.get("character_asset_ids"):
            lines.append("- 收尾角色资产：" + "、".join(handoff["character_asset_ids"]))
        if handoff.get("scene_asset_ids"):
            lines.append("- 收尾场景资产：" + "、".join(handoff["scene_asset_ids"]))
        if prop_lines:
            lines.append("- 收尾道具状态：" + "；".join(prop_lines))
        return "\n".join(lines)

    def _subtitle_service(self):
        from subtitles import SubtitleService
        return SubtitleService.from_config(self._config())

    def _idea_dir(self, session_id: str) -> Path:
        return self.session_index.working_dir(session_id) / "idea2video"

    def _load_characters(self, idea_dir: Path):
        from interfaces import CharacterInScene
        path = idea_dir / "characters.json"
        if not path.exists():
            return []
        return [CharacterInScene.model_validate(c) for c in json.loads(path.read_text(encoding="utf-8"))]

    def _load_scripts(self, idea_dir: Path) -> List[str]:
        path = idea_dir / "script.json"
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [s if isinstance(s, str) else json.dumps(s, ensure_ascii=False) for s in data]
        return [data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)]

    # ----- state machine -----------------------------------------------

    async def start_topic(self, idea: str, user_requirement: str = "", style: str = "", domain: str = "",
                          character_asset_ids: Optional[List[str]] = None, mode: str = "idea",
                          prop_asset_ids: Optional[List[str]] = None,
                          scene_asset_ids: Optional[List[str]] = None,
                          lora_ids: Optional[List[str]] = None,
                          script: str = "", target_language: Optional[str] = None,
                          aspect_ratio: Optional[str] = None, overrides: Optional[dict] = None,
                          quality_tier: str = "balanced",
                          continuity_source_session_id: Optional[str] = None,
                          series_id: Optional[str] = None, episode_number: Optional[int] = None,
                          episode_title: str = "", episode_outline: str = "",
                          previous_episode_id: Optional[str] = None,
                          series_context: Optional[dict] = None,
                          progress=None) -> dict:
        mode = "script" if str(mode) == "script" else "idea"
        moderator = self._live_moderator()
        if moderator is not None:
            # In script mode the screenplay itself is the user content to vet.
            verdict = moderator.check(script if mode == "script" else idea)
            if not verdict.get("ok"):
                return {"ok": False, "stage": "rejected", "error": "moderation",
                        "note": f"内容未通过审核：{verdict.get('reason')}"
                                + (f"（{'、'.join(verdict.get('matched', []))}）" if verdict.get("matched") else "")}
        selected_lora_ids = list(dict.fromkeys(str(item).strip() for item in (lora_ids or []) if str(item).strip()))
        lora_bindings = self.lora_service.resolve(selected_lora_ids) if selected_lora_ids and self.lora_service else []
        if selected_lora_ids and not self.lora_service:
            raise ValueError("LoRA 资源库尚未启用")
        source_session_id = str(continuity_source_session_id or "").strip()
        source_session = self.session_index.get(source_session_id) if source_session_id else None
        if source_session_id and source_session is None:
            raise ValueError(f"连续性来源项目不存在：{source_session_id}")
        if source_session is not None:
            character_asset_ids = list(character_asset_ids or source_session.get("character_asset_ids") or [])
            prop_asset_ids = list(prop_asset_ids or source_session.get("prop_asset_ids") or [])
            scene_asset_ids = list(scene_asset_ids or source_session.get("scene_asset_ids") or [])
        session = self.session_index.create(idea=idea, user_requirement=user_requirement,
                                            style=style or "Cinematic, coherent", domain=domain,
                                            character_asset_ids=character_asset_ids or [], mode=mode, script=script,
                                             prop_asset_ids=prop_asset_ids or [],
                                             scene_asset_ids=scene_asset_ids or [],
                                             lora_ids=selected_lora_ids, lora_bindings=lora_bindings,
                                             target_language=target_language, aspect_ratio=aspect_ratio,
                                             overrides=overrides, quality_tier=quality_tier,
                                             config_snapshot=self._project_config_snapshot(),
                                             continuity_source_session_id=source_session_id or None,
                                             series_id=series_id, episode_number=episode_number,
                                             episode_title=episode_title, episode_outline=episode_outline,
                                             previous_episode_id=previous_episode_id,
                                             series_context=series_context)
        sid = session["session_id"]
        self.session_index.update_stage(sid, "script_generating",
                                        "Importing script" if mode == "script" else "Generating script from topic")
        try:
            summary = await self._gen_script(session)
        except Exception as exc:
            self.session_index.update_stage(sid, "error", f"Script generation failed: {exc}")
            return {"ok": False, "session_id": sid, "stage": "script", "error": str(exc),
                    "note": "剧本生成失败，请修复后重新提交主题。"}
        task = self.session_index.create_review_task(sid, stage="script", summary=summary)
        self.session_index.update_stage(sid, "script_review_pending", "Script awaiting review")
        return {"ok": True, "session_id": sid, "stage": "script", "review_id": task["review_id"], "summary": summary}

    async def approve(self, session_id: str, progress=None) -> dict:
        session = self.session_index.get(session_id)
        if session is None:
            return {"ok": False, "reason": "no_session"}
        pending = self._latest_pending(session_id)
        if pending is None:
            return {"ok": True, "note": "no pending review to approve"}
        gate = pending["stage"]

        if gate in ("shot_video", "final"):
            readiness = self._shot_video_readiness(session_id)
            if readiness["enforced"] and not readiness["ok"]:
                note = (
                    "分镜视频产物不完整或已损坏，不能进入终审或发布。"
                    f"缺失/损坏：{', '.join(readiness['issues'])}"
                )
                if gate == "final":
                    self.session_index.resolve_review_task(
                        session_id, pending["review_id"], "superseded"
                    )
                    task = self.session_index.create_review_task(
                        session_id, stage="shot_video", summary=note
                    )
                    self.session_index.update_stage(
                        session_id, "shot_video_review_pending", note
                    )
                    return {
                        "ok": False,
                        "stage": "shot_video",
                        "error": "incomplete_shot_video_artifacts",
                        "note": note,
                        "review_id": task["review_id"],
                        "readiness": readiness,
                        "rolled_back": True,
                    }

                self._archive_incomplete_render_outputs(session_id)
                self.session_index.update_stage(
                    session_id, "shot_video_generating", "Recovering incomplete shot video artifacts"
                )
                try:
                    summary = await self._run_gate(
                        "shot_video", session, progress=progress
                    )
                except Exception as exc:
                    self.session_index.update_stage(
                        session_id,
                        "shot_video_review_pending",
                        f"shot_video recovery failed: {exc}",
                    )
                    return {
                        "ok": False,
                        "stage": "shot_video",
                        "error": str(exc),
                        "note": "分镜视频恢复生成失败；修复上游问题后再次点击页面右上角的阶段按钮即可重试。",
                        "readiness": readiness,
                    }
                recovered_readiness = self._shot_video_readiness(session_id)
                if recovered_readiness["enforced"] and not recovered_readiness["ok"]:
                    retry_note = (
                        "恢复生成结束，但仍有不完整产物："
                        f"{', '.join(recovered_readiness['issues'])}"
                    )
                    self.session_index.update_stage(
                        session_id, "shot_video_review_pending", retry_note
                    )
                    return {
                        "ok": False,
                        "stage": "shot_video",
                        "error": "incomplete_shot_video_artifacts",
                        "note": retry_note,
                        "readiness": recovered_readiness,
                    }
                self.session_index.resolve_review_task(
                    session_id, pending["review_id"], "superseded"
                )
                task = self.session_index.create_review_task(
                    session_id, stage="shot_video", summary=summary
                )
                self.session_index.update_stage(
                    session_id,
                    "shot_video_review_pending",
                    "shot_video awaiting review (recovered)",
                )
                return {
                    "ok": True,
                    "stage": "shot_video",
                    "review_id": task["review_id"],
                    "summary": summary,
                    "recovered": True,
                }

        # Generate the next stage BEFORE consuming the current review, so a
        # generation failure leaves the current gate still pending (re-approvable)
        # instead of stranding the session.
        if gate == "final":
            try:
                summary = await self._do_publish(session)
            except Exception as exc:
                self.session_index.update_stage(session_id, "final_review_pending", f"Publish failed: {exc}")
                return {"ok": False, "stage": "final", "error": str(exc), "note": "发布失败，修复后可点击页面右上角的阶段按钮重试。"}
            self.session_index.resolve_review_task(session_id, pending["review_id"], "approved")
            self.session_index.update_stage(session_id, "completed", "Published")
            return {"ok": True, "stage": "completed", "summary": summary}

        next_gate = GATES[GATES.index(gate) + 1]

        # Moderation gate: re-check the generated script before the expensive
        # video stage (the topic passed at intake, but the LLM-expanded script
        # may have drifted into disallowed content).
        moderator = self._live_moderator()
        if next_gate == "shot_video" and moderator is not None:
            verdict = moderator.check("\n".join(self._load_scripts(self._idea_dir(session_id))))
            if not verdict.get("ok"):
                note = f"剧本未通过内容审核：{verdict.get('reason')}" + (
                    f"（{'、'.join(verdict.get('matched', []))}）" if verdict.get("matched") else "")
                self.session_index.update_stage(session_id, f"{gate}_review_pending", note)
                return {"ok": False, "stage": gate, "error": "moderation", "note": note}

        # Budget gate: before the expensive video stage, cap scenes/shots.
        if next_gate == "shot_video" and self._live_budget() is not None:
            scenes, shots = self._count_storyboard(session_id)
            ok, msg = self._live_budget().check_render(scenes, shots)
            if not ok:
                self.session_index.update_stage(session_id, f"{gate}_review_pending", msg)
                return {"ok": False, "stage": gate, "error": "budget_exceeded", "note": msg}

        if next_gate == "shot_video" and self.provider_registry is not None:
            route_preview = self.provider_route_preview(session_id)
            if not route_preview.get("ok"):
                note = str(route_preview.get("note") or "当前模型不支持该镜头要求")
                self.session_index.update_stage(session_id, f"{gate}_review_pending", note)
                return {"ok": False, "stage": gate, "error": "provider_capability_mismatch",
                        "note": note, "route_preview": route_preview}
            self.session_index.update_metadata(session_id, provider_route=route_preview)

        self.session_index.update_stage(session_id, f"{next_gate}_generating", f"Generating {next_gate}")
        try:
            summary = await self._run_gate(next_gate, session, progress=progress)
        except Exception as exc:
            self.session_index.update_stage(session_id, f"{gate}_review_pending", f"{next_gate} generation failed: {exc}")
            return {"ok": False, "stage": gate, "failed_stage": next_gate, "error": str(exc),
                    "note": f"{next_gate} 生成失败，已保留上一阶段审核；修复后点击页面右上角的阶段按钮即可重试。"}
        # Plan-ahead: at the storyboard gate (right before the costly video stage)
        # tell the user the production plan + estimated cost, so "通过" is informed.
        if next_gate == "storyboard" and self.cost_estimator is not None:
            try:
                scenes, shots = self._count_storyboard(session_id)
                if shots:
                    summary = f"{summary}\n\n{self.cost_estimator.plan_summary(scenes, shots, str(session.get('quality_tier') or 'balanced'))}"
            except Exception:
                pass

        if gate == "shot_video":
            try:
                from services.production_metrics import (
                    rebuild_provider_performance,
                    record_stage_acceptance,
                )

                record_stage_acceptance(self.session_index.working_dir(session_id))
                rebuild_provider_performance(self.workspace_root, self.session_index)
            except Exception:
                logging.exception("Failed to record shot-video acceptance metrics")

        self.session_index.resolve_review_task(session_id, pending["review_id"], "approved")
        task = self.session_index.create_review_task(session_id, stage=next_gate, summary=summary)
        self.session_index.update_stage(session_id, f"{next_gate}_review_pending", f"{next_gate} awaiting review")
        return {"ok": True, "stage": next_gate, "review_id": task["review_id"], "summary": summary}

    async def preview_keyframes(
        self,
        session_id: str,
        progress=None,
        scene_index: int | None = None,
        shot_index: int | None = None,
        force: bool = False,
    ) -> dict:
        session = self.session_index.get(session_id)
        if session is None:
            return {"ok": False, "error": "no_session", "note": "项目不存在"}
        pending = self._latest_pending(session_id)
        if pending is None or pending.get("stage") != "storyboard":
            return {
                "ok": False,
                "error": "invalid_stage",
                "note": "关键帧预览只能在分镜确认阶段生成",
            }
        route_preview = self.provider_route_preview(session_id)
        if not route_preview.get("ok"):
            return {
                "ok": False,
                "error": "provider_capability_mismatch",
                "note": route_preview.get("note"),
            }
        handler = self.stage_handlers.get("shot_video")
        preview = getattr(handler, "preview_keyframes", None)
        if preview is None:
            return {"ok": False, "error": "preview_unavailable", "note": "当前视频处理器不支持关键帧预览"}
        targeted = scene_index is not None or shot_index is not None
        if targeted:
            try:
                scene_index, shot_index = int(scene_index), int(shot_index)
            except (TypeError, ValueError):
                return {"ok": False, "error": "bad_index", "note": "场景或镜头序号无效"}
            storyboard_path = self._idea_dir(session_id) / f"scene_{scene_index}" / "storyboard.json"
            try:
                shots = json.loads(storyboard_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                shots = []
            if not isinstance(shots, list) or not any(int(item.get("idx", -1)) == shot_index for item in shots):
                return {"ok": False, "error": "bad_index", "note": "指定的分镜不存在"}
            summary = await preview(
                self,
                session,
                progress=progress,
                scene_index=scene_index,
                shot_index=shot_index,
                force=bool(force),
            )
            preview_meta = dict(session.get("keyframe_preview") or {})
            completed = set(preview_meta.get("completed_shots") or [])
            completed.add(f"{scene_index}_{shot_index}")
            preview_meta.update({"summary": summary, "completed_shots": sorted(completed)})
            self.session_index.update_metadata(session_id, keyframe_preview=preview_meta)
        else:
            summary = await preview(self, session, progress=progress)
            self.session_index.update_metadata(
                session_id,
                keyframe_preview={"ready": True, "summary": summary},
            )
        return {"ok": True, "stage": "storyboard", "summary": summary}

    async def revise(self, session_id: str, instruction: str, progress=None) -> dict:
        session = self.session_index.get(session_id)
        if session is None:
            return {"ok": False, "reason": "no_session"}
        pending = self._latest_pending(session_id)
        if pending is None:
            return {"ok": True, "note": "no pending review to revise"}
        gate = pending["stage"]
        self.session_index.update_stage(session_id, f"{gate}_revision_requested", f"Revising {gate}")
        try:
            summary = await self._run_gate(gate, session, instruction=instruction, progress=progress)
        except Exception as exc:
            self.session_index.update_stage(session_id, f"{gate}_review_pending", f"{gate} revision failed: {exc}")
            return {"ok": False, "stage": gate, "error": str(exc), "note": f"{gate} 重做失败，原审核仍待确认，可重试。"}
        self.session_index.resolve_review_task(session_id, pending["review_id"], "revised")
        self.session_index.append_log("revisions", {"session_id": session_id, "stage": gate, "instruction": instruction})
        task = self.session_index.create_review_task(session_id, stage=gate, summary=summary)
        self.session_index.update_stage(session_id, f"{gate}_review_pending", f"{gate} awaiting review (revised)")
        return {"ok": True, "stage": gate, "review_id": task["review_id"], "summary": summary, "revised": True}

    @staticmethod
    def _interrupted_gate(stage: str) -> str | None:
        """The gate left mid-generation by a crash/restart (stage ends in
        ``_generating``/``_revision_requested`` but no job is running), or None."""
        for suf in ("_generating", "_revision_requested"):
            if stage.endswith(suf):
                return stage[: -len(suf)]
        return None

    async def resume_generation(self, session_id: str, progress=None) -> dict:
        """Resume a generation interrupted by a server restart/crash. Re-runs the
        gate that was mid-generation; the pipeline skips artifacts already on disk
        (os.path.exists), so it continues from the break rather than restarting."""
        session = self.session_index.get(session_id)
        if session is None:
            return {"ok": False, "reason": "no_session"}
        gate = self._interrupted_gate(str(session.get("stage", "")))
        if gate is None or gate not in GATES:
            return {"ok": True, "note": "无中断的生成可继续"}
        try:
            summary = await self._run_gate(gate, session, progress=progress)
        except Exception as exc:
            self.session_index.update_stage(session_id, f"{gate}_review_pending", f"{gate} resume failed: {exc}")
            return {"ok": False, "stage": gate, "error": str(exc), "note": f"{gate} 继续生成失败，可重试。"}
        # mirror the approve/revise tail: clear any stale pending, open this gate's review
        pending = self._latest_pending(session_id)
        if pending is not None:
            self.session_index.resolve_review_task(session_id, pending["review_id"], "approved")
        task = self.session_index.create_review_task(session_id, stage=gate, summary=summary)
        self.session_index.update_stage(session_id, f"{gate}_review_pending", f"{gate} awaiting review (resumed)")
        return {"ok": True, "stage": gate, "review_id": task["review_id"], "summary": summary, "resumed": True}

    def edit_script(self, session_id: str, text: str) -> dict:
        """Persist a manual, in-place script edit at the script review gate.

        The article shown by the web UI is saved as both the readable story and
        the scene list consumed by storyboard generation. Any downstream
        storyboard/render artifacts are invalid after a script change.
        """
        record = self.session_index.get(session_id)
        if record is None:
            return {"ok": False, "error": "no_session"}
        if not str(record.get("stage", "")).startswith("script_review"):
            return {
                "ok": False,
                "error": "wrong_stage",
                "note": "只能在『剧本』审核阶段直接编辑；若已进入后续阶段，请先退回到剧本。",
            }
        script_text = str(text or "").strip()
        if not script_text:
            return {"ok": False, "error": "empty", "note": "剧本正文不能为空。"}
        moderator = self._live_moderator()
        if moderator is not None:
            verdict = moderator.check(script_text)
            if not verdict.get("ok"):
                return {
                    "ok": False,
                    "error": "moderation",
                    "note": f"内容未通过审核：{verdict.get('reason')}",
                }
        scenes = _split_script_into_scenes(script_text)
        if not scenes:
            return {"ok": False, "error": "empty", "note": "未识别到可保存的剧本内容。"}

        from utils.atomic import atomic_write_text

        idea = self._idea_dir(session_id)
        self._invalidate_downstream(session_id, "script")
        atomic_write_text(idea / "story.txt", script_text, encoding="utf-8")
        atomic_write_text(
            idea / "script.json",
            json.dumps(scenes, ensure_ascii=False, indent=4),
            encoding="utf-8",
        )
        self.session_index.update_metadata(session_id, script=script_text)
        self.session_index.append_log(
            "revisions",
            {"session_id": session_id, "stage": "script", "instruction": "manual_edit"},
        )
        return {"ok": True, "scenes": len(scenes), "text": script_text}

    def edit_storyboard(self, session_id: str, scenes: list) -> dict:
        """Manually replace shots for one or more scenes at the 分镜脚本 review
        stage (add / delete / edit shots by hand). Renumbers idx/cam_idx, validates,
        writes each scene's storyboard.json, then invalidates the derived caches
        (per-shot decomposition + camera tree + any downstream video) so video
        generation rebuilds from the edits. Only allowed at the storyboard review."""
        from interfaces import ShotBriefDescription
        from prompting import target_language
        from agents.storyboard_artist import chinese_review_field_issues
        record = self.session_index.get(session_id)
        if record is None:
            return {"ok": False, "error": "no_session"}
        if not str(record.get("stage", "")).startswith("storyboard_review"):
            return {"ok": False, "error": "wrong_stage",
                    "note": "只能在『分镜脚本』审核阶段手动编辑分镜；若已生成视频，请先『退回修改·分镜脚本』。"}
        idea = self._idea_dir(session_id)
        require_chinese = target_language(self._effective_config(record)) == "zh"
        incoming = {int(s["scene_index"]): (s.get("shots") or [])
                    for s in (scenes or []) if s.get("scene_index") is not None}
        if not incoming:
            return {"ok": False, "error": "empty", "note": "没有要保存的分镜。"}
        written = 0
        changed_shots: dict[int, set[int]] = {}
        for scene_index, shots in incoming.items():
            scene_dir = idea / f"scene_{scene_index}"
            storyboard_path = scene_dir / "storyboard.json"
            if not storyboard_path.exists():
                continue  # only edit scenes that already exist (no adding scenes here)
            try:
                previous = json.loads(storyboard_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                previous = []
            previous = previous if isinstance(previous, list) else []
            if not shots:
                return {"ok": False, "error": "empty_scene",
                        "note": f"场景 {scene_index} 至少要保留一个镜头。"}
            n, norm = len(shots), []
            for i, raw in enumerate(shots):
                raw = raw or {}
                vd = str(raw.get("visual_desc", "") or "").strip()
                if not vd:
                    return {"ok": False, "error": "missing_visual",
                            "note": f"场景 {scene_index} 第 {i + 1} 个镜头缺少画面描述(visual_desc)。"}
                payload = dict(raw)
                payload.update({
                    "idx": i,
                    "is_last": i == n - 1,
                    "cam_idx": i,
                    "visual_desc": vd,
                    "audio_desc": str(raw.get("audio_desc", "") or ""),
                    "screen_text": ((str(raw.get("screen_text")).strip() or None)
                                    if raw.get("screen_text") else None),
                    "screen_text_pos": raw.get("screen_text_pos") or None,
                })
                try:
                    shot = ShotBriefDescription.model_validate(payload)
                except (TypeError, ValueError) as exc:
                    return {"ok": False, "error": "invalid_shot",
                            "note": f"场景 {scene_index} 第 {i + 1} 个镜头参数无效：{exc}"}
                if require_chinese:
                    issues = chinese_review_field_issues(shot)
                    prior_issues = chinese_review_field_issues(previous[i]) if i < len(previous) else {}
                    introduced = [
                        name for name, value in issues.items()
                        if prior_issues.get(name) != value
                    ]
                    if introduced:
                        labels = {
                            "visual_desc": "画面提示词",
                            "director_desc": "导演稿",
                            "audio_desc": "台词与声音",
                            "visual_style": "画面风格",
                            "avoid": "避免项",
                        }
                        display = list(dict.fromkeys(
                            "执行节拍" if name.startswith("beats[") else labels.get(name, name)
                            for name in introduced
                        ))
                        return {
                            "ok": False,
                            "error": "non_chinese_storyboard",
                            "note": (
                                f"场景 {scene_index + 1} 镜头 {i + 1} 的中文审核字段不能新增英文内容："
                                + "、".join(display)
                            ),
                        }
                norm.append(shot.model_dump())
            from domain.artifacts import compute_input_hash
            changed = {
                index for index in range(max(len(previous), len(norm)))
                if index >= len(previous)
                or index >= len(norm)
                or compute_input_hash(previous[index]) != compute_input_hash(norm[index])
            }
            if not changed:
                written += 1
                continue
            if self.artifact_versions is not None:
                for shot_index in changed:
                    self._capture_existing_shot_artifacts(
                        session_id, scene_index, shot_index, scene_dir)
                    if shot_index < len(previous) and not self.artifact_versions.list_versions(
                        session_id, scene_index, shot_index, "storyboard"):
                        self.artifact_versions.record_json_item(
                            session_id,
                            scene_index,
                            shot_index,
                            previous[shot_index],
                            live_path=storyboard_path,
                            input_values={"shot": previous[shot_index], "source": "pre_m2_import"},
                        )
            storyboard_path.write_text(
                json.dumps(norm, ensure_ascii=False, indent=4), encoding="utf-8")
            if self.artifact_versions is not None:
                for shot_index in changed:
                    if shot_index < len(norm):
                        self.artifact_versions.record_json_item(
                            session_id,
                            scene_index,
                            shot_index,
                            norm[shot_index],
                            live_path=storyboard_path,
                            input_values={
                                "shot": norm[shot_index],
                                "style": str(record.get("style", "")),
                                "requirement": str(record.get("user_requirement", "")),
                            },
                        )
                    else:
                        self.artifact_versions.mark_inputs_changed(
                            session_id,
                            scene_index,
                            shot_index,
                            "storyboard",
                            {"deleted": True},
                            reason="manual_storyboard_delete",
                        )
            changed_shots[scene_index] = changed
            written += 1
        if not written:
            return {"ok": False, "error": "no_match", "note": "未找到可编辑的场景。"}
        # Drop only the changed shots' render caches; immutable version snapshots
        # remain available for history and rollback.
        if self.artifact_versions is not None:
            self._invalidate_storyboard_shots(session_id, changed_shots)
        else:
            self._invalidate_downstream(session_id, "storyboard")
        self.session_index.append_log("revisions",
                                      {"session_id": session_id, "stage": "storyboard", "instruction": "manual_edit"})
        scenes_n, shots_n = self._count_storyboard(session_id)
        return {"ok": True, "scenes": scenes_n, "shots": shots_n}

    def _capture_existing_shot_artifacts(
        self, session_id: str, scene_index: int, shot_index: int, scene_dir: Path
    ) -> None:
        if self.artifact_versions is None:
            return
        shot_dir = scene_dir / "shots" / str(shot_index)
        for artifact_type, name in (("keyframe", "first_frame.png"), ("video", "video.mp4")):
            path = shot_dir / name
            if path.is_file():
                self.artifact_versions.record_file(
                    session_id,
                    scene_index,
                    shot_index,
                    artifact_type,
                    path,
                    input_values={"source_sha256": self._file_sha256(path)},
                    metadata={"captured_before": "manual_storyboard_edit"},
                )

    @staticmethod
    def _file_sha256(path: Path) -> str:
        import hashlib

        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _invalidate_storyboard_shots(
        self, session_id: str, changed_shots: dict[int, set[int]]
    ) -> None:
        import shutil

        idea = self._idea_dir(session_id)
        for final in idea.glob("final_video*.mp4"):
            final.unlink(missing_ok=True)
        for scene_index, shot_indexes in changed_shots.items():
            if not shot_indexes:
                continue
            scene_dir = idea / f"scene_{scene_index}"
            for shot_index in shot_indexes:
                shutil.rmtree(scene_dir / "shots" / str(shot_index), ignore_errors=True)
            (scene_dir / "camera_tree.json").unlink(missing_ok=True)
            for video in scene_dir.glob("*.mp4"):
                video.unlink(missing_ok=True)

    async def rewrite_shot_description(self, session_id: str, scene_index, shot_index,
                                       shots: list = None, instruction: str = "") -> dict:
        """AI-rewrite a SINGLE shot's visual/audio description at the 分镜脚本 review
        stage (does not persist — the UI fills the editor, the user then saves).
        ``shots`` is the scene's current shot list from the editor (to honour unsaved
        edits); falls back to storyboard.json on disk."""
        from agent_runtime.sceneforge_adapters import _build_chat_model
        from agents.storyboard_artist import StoryboardArtist
        from agents.domain_packs import resolve_domain
        record = self.session_index.get(session_id)
        if record is None:
            return {"ok": False, "error": "no_session"}
        if not str(record.get("stage", "")).startswith("storyboard_review"):
            return {"ok": False, "error": "wrong_stage",
                    "note": "只能在『分镜脚本』审核阶段重写分镜；若已生成视频，请先『退回修改·分镜脚本』。"}
        try:
            scene_index, shot_index = int(scene_index), int(shot_index)
        except (TypeError, ValueError):
            return {"ok": False, "error": "bad_index", "note": "镜头序号无效。"}
        idea = self._idea_dir(session_id)
        scripts = self._load_scripts(idea)
        if scene_index < 0 or scene_index >= len(scripts):
            return {"ok": False, "error": "bad_scene", "note": "场景不存在。"}
        if not shots:
            sb = idea / f"scene_{scene_index}" / "storyboard.json"
            try:
                shots = json.loads(sb.read_text(encoding="utf-8")) if sb.exists() else []
            except Exception:
                shots = []
        if not isinstance(shots, list) or shot_index < 0 or shot_index >= len(shots):
            return {"ok": False, "error": "bad_shot", "note": "镜头不存在。"}
        chinese = self._lang_instruction(record)
        pack = resolve_domain(self._domain(record))
        artist = StoryboardArtist(chat_model=_build_chat_model(),
                                  extra_system_instruction=pack.instruction_for("storyboard", chinese))
        try:
            resp = await artist.rewrite_shot(
                script=str(scripts[scene_index]), shots=shots, target_index=shot_index,
                characters=self._load_characters(idea),
                user_requirement=str(record.get("user_requirement", "") or ""),
                instruction=str(instruction or ""))
        except Exception as exc:
            return {"ok": False, "error": str(exc), "note": "重写失败，请重试。"}
        shot = resp.model_dump()
        shot["screen_text"] = shot.get("screen_text") or ""
        shot["screen_text_pos"] = shot.get("screen_text_pos") or ""
        return {"ok": True, "shot": shot}

    def _invalidate_downstream(self, session_id: str, gate: str) -> None:
        """Drop artifacts downstream of ``gate`` so a reopened session truly
        regenerates instead of reusing stale files (the pipeline resumes from disk
        via os.path.exists). Keeps the gate's own artifact + characters/portraits."""
        import shutil
        idea = self._idea_dir(session_id)
        if not idea.exists():
            return
        for f in idea.glob("final_video*.mp4"):
            try: f.unlink()
            except OSError: pass
        for scene_dir in idea.glob("scene_*"):
            if not scene_dir.is_dir():
                continue
            shutil.rmtree(scene_dir / "shots", ignore_errors=True)
            (scene_dir / "camera_tree.json").unlink(missing_ok=True)
            for f in scene_dir.glob("*.mp4"):
                try: f.unlink()
                except OSError: pass
            if gate == "script":            # going back to 剧本 also invalidates 分镜脚本
                (scene_dir / "storyboard.json").unlink(missing_ok=True)

    def reopen(self, session_id: str, gate: str) -> dict:
        """Re-open a finished (or any) session at an earlier gate for revision.
        Invalidates downstream artifacts and opens a fresh review at ``gate`` so the
        user can 修改 (regenerate that gate) or 通过 (regenerate everything below)."""
        if gate not in ("script", "storyboard"):
            return {"ok": False, "error": "只能退回到 剧本 或 分镜脚本"}
        session = self.session_index.get(session_id)
        if session is None:
            return {"ok": False, "reason": "no_session"}
        self._invalidate_downstream(session_id, gate)
        # clear any lingering pending review so exactly one (this gate's) is open
        for t in self.session_index.list_review_tasks(session_id):
            if t.get("status") == "pending":
                self.session_index.resolve_review_task(session_id, t["review_id"], "superseded")
        summary = ("已退回到剧本。修改后『通过』将重新生成分镜与视频（下游已作废）。" if gate == "script"
                   else "已退回到分镜脚本。修改后『通过』将重新生成分镜视频（下游已作废）。")
        task = self.session_index.create_review_task(session_id, stage=gate, summary=summary)
        self.session_index.update_stage(session_id, f"{gate}_review_pending", f"reopened at {gate}")
        return {"ok": True, "stage": gate, "review_id": task["review_id"], "summary": summary, "reopened": True}

    def _count_storyboard(self, session_id: str):
        """(scene_count, total_shot_count) from the idea-mode storyboards on disk."""
        import json as _json
        idea = self._idea_dir(session_id)
        scenes = 0
        shots = 0
        if idea.exists():
            for scene_dir in sorted(idea.glob("scene_*")):
                sb = scene_dir / "storyboard.json"
                if sb.exists():
                    scenes += 1
                    try:
                        shots += len(_json.loads(sb.read_text(encoding="utf-8")))
                    except Exception:
                        pass
        return scenes, shots

    def _latest_pending(self, session_id: str):
        pending = [t for t in self.session_index.list_review_tasks(session_id) if t.get("status") == "pending"]
        return pending[-1] if pending else None

    def _shot_video_readiness(self, session_id: str) -> dict:
        """Validate current live shot artifacts before final review or publish.

        Historical files under ``_archive`` never count. Projects without an
        on-disk storyboard (lightweight adapters and state-machine tests) keep
        the legacy behavior because there is no reliable expected-shot set.
        """
        idea_dir = self._idea_dir(session_id)
        expected: list[tuple[int, int]] = []
        issues: list[str] = []
        for scene_dir in sorted(idea_dir.glob("scene_*")):
            if not scene_dir.is_dir():
                continue
            storyboard_path = scene_dir / "storyboard.json"
            if not storyboard_path.is_file():
                continue
            try:
                storyboard = json.loads(storyboard_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                issues.append(f"{scene_dir.name}/storyboard.json")
                continue
            if not isinstance(storyboard, list):
                issues.append(f"{scene_dir.name}/storyboard.json")
                continue
            try:
                scene_index = int(scene_dir.name.split("_", 1)[1])
            except (IndexError, ValueError):
                continue
            for position, shot in enumerate(storyboard):
                raw_idx = shot.get("idx", position) if isinstance(shot, dict) else position
                try:
                    shot_index = int(raw_idx)
                except (TypeError, ValueError):
                    shot_index = position
                expected.append((scene_index, shot_index))

        if not expected:
            return {"ok": not issues, "enforced": False, "expected_shots": 0, "issues": issues}

        for scene_index, shot_index in expected:
            shot_dir = idea_dir / f"scene_{scene_index}" / "shots" / str(shot_index)
            frame_path = shot_dir / "first_frame.png"
            video_path = shot_dir / "video.mp4"
            if not self._valid_image(frame_path):
                issues.append(f"scene_{scene_index}/shot_{shot_index}/first_frame.png")
            if not self._valid_video(video_path):
                issues.append(f"scene_{scene_index}/shot_{shot_index}/video.mp4")

        final_path = idea_dir / "final_video.mp4"
        if not self._valid_video(final_path):
            issues.append("idea2video/final_video.mp4")
        return {
            "ok": not issues,
            "enforced": True,
            "expected_shots": len(expected),
            "issues": issues,
        }

    @staticmethod
    def _valid_image(path: Path) -> bool:
        if not path.is_file() or path.stat().st_size <= 0:
            return False
        try:
            from PIL import Image

            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                image.load()
                return image.width > 0 and image.height > 0
        except (OSError, ValueError):
            return False

    @staticmethod
    def _valid_video(path: Path) -> bool:
        if not path.is_file() or path.stat().st_size < 1024:
            return False
        try:
            from subtitles.timeline import probe_duration

            return float(probe_duration(str(path))) > 0.1
        except Exception:
            return False

    def _archive_incomplete_render_outputs(self, session_id: str) -> Optional[str]:
        """Move stale aggregate renders aside before rebuilding from live shots."""
        idea_dir = self._idea_dir(session_id)
        sources = list(idea_dir.glob("final_video*.mp4"))
        for scene_dir in sorted(idea_dir.glob("scene_*")):
            if not scene_dir.is_dir():
                continue
            sources.extend(scene_dir.glob("final_video*.mp4"))
            quality_path = scene_dir / "quality.json"
            if quality_path.is_file():
                sources.append(quality_path)
        sources = [path for path in sources if path.is_file()]
        if not sources:
            return None

        archive_base = idea_dir / "_archive" / "incomplete_recovery"
        version = 1
        while (archive_base / f"v{version}").exists():
            version += 1
        archive_dir = archive_base / f"v{version}"
        for source in sources:
            target = archive_dir / source.relative_to(idea_dir)
            target.parent.mkdir(parents=True, exist_ok=True)
            source.replace(target)
        return str(archive_dir)

    async def _run_gate(self, gate: str, session: dict, instruction: str = "", progress=None) -> str:
        if gate == "script":
            return await self._gen_script(session, instruction)
        if gate == "storyboard":
            return await self._gen_storyboard(session, instruction)
        if gate == "shot_video":
            return await self._gen_video(session, instruction, progress=progress)
        if gate == "final":
            return await self.stage_handlers.run("final", self, session, instruction, progress)
        raise ValueError(f"Unknown gate: {gate}")

    # ----- generation stages (override in tests) -----------------------

    def _augment_requirement(self, session: dict, instruction: str) -> str:
        base = str(session.get("user_requirement", "") or "")
        series = self._series_episode_brief(session)
        cast = self._cast_brief(session)
        reusable = self._reusable_asset_brief(session)
        inherited = self._continuity_inheritance_brief(session)
        out = "\n".join(p for p in (base, series, cast, reusable, inherited) if p)
        return f"{out}\n修改意见：{instruction}".strip() if instruction else out

    @staticmethod
    def _series_episode_brief(session: dict) -> str:
        if not session.get("series_id"):
            return ""
        context = session.get("series_context") or {}
        number = int(session.get("episode_number") or 1)
        lines = [
            "连续短剧约束（本集必须服从作品设定，并与前后集保持叙事一致）：",
            f"- 作品：{context.get('title') or session.get('series_id')}，第 {number} 集"
            + (f"《{session.get('episode_title')}》" if session.get("episode_title") else ""),
        ]
        if context.get("premise"):
            lines.append("- 整体故事：" + str(context["premise"]))
        if session.get("episode_outline"):
            lines.append("- 本集剧情目标：" + str(session["episode_outline"]))
        duration = context.get("episode_duration_sec")
        if duration:
            lines.append(f"- 本集目标时长：约 {int(duration)} 秒")
        bible = context.get("bible") or {}
        if bible:
            lines.append("- 作品设定：" + json.dumps(bible, ensure_ascii=False, separators=(",", ":")))
        return "\n".join(lines)

    async def _gen_script(self, session: dict, instruction: str = "") -> str:
        return await self.stage_handlers.run("script", self, session, instruction)

    async def _gen_storyboard(self, session: dict, instruction: str = "") -> str:
        return await self.stage_handlers.run("storyboard", self, session, instruction)

    async def _gen_video(self, session: dict, instruction: str = "", progress=None) -> str:
        return await self.stage_handlers.run(
            "shot_video", self, session, instruction, progress)

    async def rebuild_after_shot_regeneration(self, session_id: str, progress=None) -> dict:
        """Re-run full project finalization after a targeted raw-shot render.

        The low-level regeneration adapter only replaces affected frames/clips and
        re-concatenates the scene. Re-entering the normal video stage restores the
        session's asset bindings, quality critic, audio, subtitles, and top-level
        project concat without regenerating artifacts that are already fresh.
        """
        session = self.session_index.get(session_id)
        if session is None:
            raise ValueError(f"Unknown session: {session_id}")

        idea_dir = self._idea_dir(session_id)
        final_path = idea_dir / "final_video.mp4"
        archived_path = None
        if final_path.exists():
            archive_root = idea_dir / "_archive" / "shot_regenerations"
            version = 1
            while (archive_root / f"v{version}").exists():
                version += 1
            archive_dir = archive_root / f"v{version}"
            archive_dir.mkdir(parents=True, exist_ok=False)
            archived_path = archive_dir / final_path.name
            final_path.replace(archived_path)

        try:
            summary = await self._gen_video(session, progress=progress)
        except Exception:
            self.session_index.update_stage(
                session_id,
                "shot_video_review_pending",
                "shot_video finalization failed after regeneration",
            )
            raise

        pending = self._latest_pending(session_id)
        if pending is None or pending.get("stage") != "shot_video":
            if pending is not None:
                self.session_index.resolve_review_task(
                    session_id, pending["review_id"], "superseded"
                )
            self.session_index.create_review_task(
                session_id,
                stage="shot_video",
                summary=summary,
            )

        self.session_index.update_stage(
            session_id,
            "shot_video_review_pending",
            "shot_video awaiting review (regenerated)",
        )
        return {
            "summary": summary,
            "final_video_path": self._portable_path(final_path)
            if final_path.exists()
            else None,
            "archived_final_video_path": self._portable_path(archived_path)
            if archived_path is not None
            else None,
        }

    def _portable_path(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.workspace_root))
        except ValueError:
            return str(path.resolve())

    def _finalize(self, session: dict) -> str:
        final_path = self._idea_dir(session["session_id"]) / "final_video.mp4"
        exists = final_path.exists()
        return ("成片已就绪，请回复「通过」发布并回传链接。" if exists
                else "未找到成片文件，请先完成视频阶段。")

    async def _do_publish(self, session: dict) -> str:
        if self.adapters is not None and hasattr(self.adapters, "sceneforge_publish"):
            result = await self.adapters.sceneforge_publish({"session_id": session["session_id"]})
            payload = getattr(result, "metadata", None) or {}
            return f"已发布。链接：{payload.get('url') or payload.get('final_video_path')}"
        return "成片完成（未配置托管/消息通道，未生成外链）。"
