"""Run the unified SceneForge web backend (config + character studio + production)
and serve the static frontend.

Usage:
  python main_server.py                      # http://127.0.0.1:8770  (open in browser)
  python main_server.py --port 8770 --registry assets/characters/registry.yaml

Model keys are read from configs/agent.local.yaml; you can also set them from the
设置 page. The image generator is built lazily, so the server starts even before
any key is configured (configure keys first, then generate).
"""

import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import argparse
import os
from pathlib import Path

import yaml

from agent_runtime.session_factory import create_session_index
from agent_runtime.sceneforge_adapters import SceneForgeAdapters
from channels import ChannelDispatcher
from characters.studio import CharacterStudio
from characters.library_studio import AssetModelStudio
from infrastructure.sqlite import SQLiteAssetCatalogRepository, SQLiteDatabase, SQLiteJobQueue
from project_identity import state_directory
from services import (Authorizer, BudgetGuard, ContentModerator, CostEstimator, DurableJobRunner, FeishuWebhookHandler,
                      HousekeepingService, InboundRateLimiter, JobRunner, ProductionService, TriggerService,
                      WorkflowEngine, RemoteVideoRecovery, ProviderRegistry)
from server import (AppAPI, AssetModelAPI, BgmAPI, BgmService, CharacterStudioAPI, ConfigAPI, ConfigService,
                    EditAPI, FeaturesAPI, FeaturesService, PreferenceService, ProductionAPI, SfxAPI, SfxService,
                    SkillsAPI, SkillsService, TemplatesAPI, VoiceAPI, VoiceService, AppSettingsAPI,
                    AppSettingsService, LoraAPI, LoraService, SeriesAPI, SeriesService, serve)
from editing import VideoEditService


def _static_dir(base: Path) -> Path:
    """Return the required Vue production build directory."""
    dist = base / "webui-dist"
    if not (dist / "index.html").is_file():
        raise FileNotFoundError(
            "Vue frontend build not found: webui-dist/index.html. "
            "Run `cd frontend && npm ci && npm run build` before starting SceneForge."
        )
    return dist


def _is_loopback_host(host: str) -> bool:
    normalized = str(host or "").strip().lower().strip("[]")
    return normalized in {"127.0.0.1", "::1", "localhost"}


class _LazyImageGen:
    """Image generator for the character studio.

    Built fresh per call (not cached): the web server runs each request in its
    own ``asyncio.run`` event loop, and the underlying aiohttp client binds to the
    loop it's created in — caching it would fail later requests with
    "Event loop is closed". Construction is cheap (just stores key/model), so a
    fresh instance per generate is fine and also lets the server start before any
    key is configured (set it on the 设置 page, then generate)."""

    async def generate_single_image(self, *args, **kwargs):
        from agent_runtime.sceneforge_adapters import _build_image_generator
        return await _build_image_generator().generate_single_image(*args, **kwargs)


def main() -> None:
    parser = argparse.ArgumentParser(description="SceneForge web backend + frontend")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--workspace", default=".")
    parser.add_argument(
        "--session-backend",
        choices=("sqlite", "json"),
        default=os.environ.get("SCENEFORGE_SESSION_BACKEND", "sqlite").strip().lower(),
        help="Session metadata backend (default: sqlite)",
    )
    parser.add_argument("--registry", default="assets/characters/registry.yaml")
    parser.add_argument("--bgm-dir", default="assets/bgm", help="Background-music library directory")
    parser.add_argument("--sfx-dir", default="assets/sfx", help="Sound-effect library directory")
    parser.add_argument("--log-file", default=os.environ.get("SCENEFORGE_LOG_FILE", ""), help="Optional rotating log file path")
    parser.add_argument("--log-level", default=os.environ.get("SCENEFORGE_LOG_LEVEL", "INFO"))
    parser.add_argument("--config", default="configs/agent.local.yaml")
    parser.add_argument("--token", default=os.environ.get("SCENEFORGE_WEB_TOKEN", ""),
                        help="Web access token for /api/* (or set SCENEFORGE_WEB_TOKEN). Empty = no auth.")
    args = parser.parse_args()
    if not args.token and not _is_loopback_host(args.host):
        parser.error(
            "--token or SCENEFORGE_WEB_TOKEN is required when --host is not a loopback address"
        )

    from utils.logging_setup import configure_logging
    configure_logging(level=args.log_level, logfile=(args.log_file or None))

    root = Path(args.workspace).resolve()
    static_dir = _static_dir(Path(__file__).resolve().parent)
    registry_path = Path(args.registry)
    if not registry_path.is_absolute():
        registry_path = root / registry_path
    session_index = create_session_index(root, backend=args.session_backend)
    app_settings_service = AppSettingsService(root, session_index=session_index)
    adapters = SceneForgeAdapters(root, session_index)

    # Pipeline config (messaging / budget / rate_limits) from idea2video.yaml.
    cfg_path = Path("configs/idea2video.yaml")
    pipeline_cfg = {}
    if cfg_path.exists():
        try:
            pipeline_cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        except Exception:
            pipeline_cfg = {}

    catalog_database = SQLiteDatabase(state_directory(root) / "sceneforge.db")
    catalog_repository = SQLiteAssetCatalogRepository(catalog_database)
    series_service = SeriesService(catalog_database, session_index)
    budget = BudgetGuard.from_config(pipeline_cfg)
    moderator = ContentModerator.from_config(pipeline_cfg)
    cost_estimator = CostEstimator.from_config(pipeline_cfg)
    provider_registry = ProviderRegistry.from_workspace(root)
    lora_service = LoraService(root)
    engine = WorkflowEngine(session_index, root, adapters=adapters, budget=budget,
                            moderator=moderator, cost_estimator=cost_estimator,
                            provider_registry=provider_registry,
                            asset_catalog_repository=catalog_repository,
                            lora_service=lora_service)
    dispatcher = ChannelDispatcher.from_config(pipeline_cfg)

    async def notify(text, target):
        if dispatcher is not None and target:
            await dispatcher.broadcast_text(text, target=target)

    if args.session_backend == "sqlite":
        job_database = catalog_database
        runner = DurableJobRunner(
            SQLiteJobQueue(job_database),
            max_concurrent=budget.max_concurrent_generations,
            remote_reconciler=RemoteVideoRecovery(root, session_index),
        )
    else:
        runner = JobRunner(max_concurrent=budget.max_concurrent_generations)
    service = ProductionService(engine, runner, adapters, notifier=(notify if dispatcher else None))

    app = AppAPI(
        config_api=ConfigAPI(ConfigService(args.config)),
        character_api=CharacterStudioAPI(CharacterStudio(
            str(registry_path),
            _LazyImageGen(),
            catalog_repository=catalog_repository,
        )),
        asset_api=AssetModelAPI(AssetModelStudio(
            catalog_repository,
            _LazyImageGen(),
            root / "assets" / "models",
        )),
        production_api=ProductionAPI(session_index, service, adapters,
                                     cost_estimator=cost_estimator,
                                     housekeeping=HousekeepingService(),
                                     series_service=series_service),
        bgm_api=BgmAPI(BgmService(library_dir=args.bgm_dir,
                                  config_paths=["configs/idea2video.yaml", "configs/script2video.yaml"])),
        voice_api=VoiceAPI(VoiceService(config_paths=["configs/idea2video.yaml", "configs/script2video.yaml"],
                                        workspace_root=str(root))),
        features_api=FeaturesAPI(FeaturesService(config_paths=["configs/idea2video.yaml", "configs/script2video.yaml"])),
        sfx_api=SfxAPI(SfxService(library_dir=args.sfx_dir,
                                  config_paths=["configs/idea2video.yaml", "configs/script2video.yaml"])),
        templates_api=TemplatesAPI(PreferenceService(str(root / "assets" / "preferences.json"))),
        edit_api=EditAPI(VideoEditService(imports_dir=str(root / "assets" / "imports"), workspace_root=str(root))),
        skills_api=SkillsAPI(SkillsService(workspace_root=str(root),
                                           config_paths=["configs/idea2video.yaml", "configs/script2video.yaml"])),
        app_settings_api=AppSettingsAPI(app_settings_service),
        lora_api=LoraAPI(lora_service),
        series_api=SeriesAPI(series_service),
        static_dir=str(static_dir),
    )

    # Feishu inbound webhook (mounted on the same server at /feishu/events) — only
    # when a Feishu channel is enabled in messaging config.
    feishu_handler = None
    feishu_channel = None
    if dispatcher is not None:
        feishu_channel = next((ch for ch, _ in dispatcher.channels if getattr(ch, "type", None) == "feishu"), None)
    if feishu_channel is not None:
        trigger = TriggerService(
            session_index, adapters,
            authorizer=Authorizer.from_config(pipeline_cfg),
            rate_limiter=InboundRateLimiter.from_config(pipeline_cfg),
            production_service=service,
        )
        feishu_handler = FeishuWebhookHandler.from_config(pipeline_cfg, trigger, channel=feishu_channel)
        print("Feishu webhook mounted at /feishu/events")

    auth = args.token or None
    if auth:
        print("Web auth: ON")
    else:
        print("⚠️  Web auth: OFF (no SCENEFORGE_WEB_TOKEN/--token). Anyone reaching this port can read keys and trigger generation — set a token before exposing it.")

    try:
        serve(app, host=args.host, port=args.port, feishu_handler=feishu_handler, auth_token=auth)
    finally:
        if hasattr(runner, "stop"):
            runner.stop()


if __name__ == "__main__":
    main()
