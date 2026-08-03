"""Run the character studio backend API.

Usage:
  python main_character_api.py                              # default registry + port 8770
  python main_character_api.py --registry assets/characters/registry.yaml --port 8770

Model keys come from configs/agent.local.yaml (or SCENEFORGE_* env), same as the rest
of SceneForge. The studio writes fixed characters to the registry, which the video
pipeline can then reuse for consistent characters across shots.
"""

import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import argparse

from agent_runtime.sceneforge_adapters import _build_image_generator
from characters.studio import CharacterStudio
from server import CharacterStudioAPI, serve


def main() -> None:
    parser = argparse.ArgumentParser(description="SceneForge character studio API")
    parser.add_argument("--registry", default="assets/characters/registry.yaml", help="Path to the character registry yaml")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    args = parser.parse_args()

    studio = CharacterStudio(registry_path=args.registry, image_generator=_build_image_generator())
    serve(CharacterStudioAPI(studio), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
