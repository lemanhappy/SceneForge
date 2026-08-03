# Third-Party Notices

## Upstream source

SceneForge began as a derivative of [HKUDS/ViMax](https://github.com/HKUDS/ViMax), distributed under the MIT License. The upstream notice is retained in [LICENSE](LICENSE), with additional provenance in [docs/上游来源与许可证.md](docs/上游来源与许可证.md).

## Software dependencies

Python and Node.js dependencies are declared in `pyproject.toml`, `uv.lock`, `frontend/package-lock.json`, and `ui/package-lock.json`. Each dependency remains governed by its own license. Release builds should generate an SBOM and preserve all notices required by those packages.

FFmpeg is an external runtime dependency. Its applicable LGPL/GPL terms depend on the build and enabled codecs. SceneForge does not grant redistribution rights for an FFmpeg binary supplied by a distributor or end user.

## External model services

Model providers and API gateways are external services and are not included in the MIT license for this repository. Users must supply their own credentials and comply with each provider's terms, content rules, privacy policy, and billing terms.

## Media and model assets

User-created characters, references, LoRA files, generated audio, images, and videos are runtime data and are excluded from source control by default. A sample asset may be published only when its source, author, license, and redistribution permission are documented.
