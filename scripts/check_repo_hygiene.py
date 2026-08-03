#!/usr/bin/env python3
"""Fail when a proposed repository snapshot contains secrets or runtime data."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
MAX_TRACKED_BYTES = 10 * 1024 * 1024

SECRET_PATTERNS = {
    "OpenAI-style token": re.compile(rb"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}"),
    "GitHub token": re.compile(rb"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{20,}"),
    "Google API key": re.compile(rb"AIza[0-9A-Za-z_-]{20,}"),
    "AWS access key": re.compile(rb"AKIA[0-9A-Z]{16}"),
    "private key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
}

FORBIDDEN_TRACKED_PATHS = {
    ".web_token.txt",
    "assets/preferences.json",
    "configs/agent.local.yaml",
}
FORBIDDEN_TRACKED_PREFIXES = (
    ".public_artifacts/",
    ".sceneforge/",
    ".test_console/",
    ".tmp/",
    ".verify_output/",
    ".working_dir/",
    "assets/characters/",
    "assets/models/",
    "webui-dist/",
)
PRIVATE_FILE_SUFFIXES = {".key", ".p12", ".pfx", ".pem"}


def _git_paths(*args: str) -> list[str]:
    result = subprocess.run(
        ["git", *args, "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [item.decode("utf-8", errors="surrogateescape") for item in result.stdout.split(b"\0") if item]


def inspect_repository() -> list[str]:
    tracked = set(_git_paths("ls-files"))
    candidates = set(_git_paths("ls-files", "--cached", "--others", "--exclude-standard"))
    findings: list[str] = []

    for relative in sorted(tracked):
        normalized = relative.replace("\\", "/")
        if normalized in FORBIDDEN_TRACKED_PATHS or normalized.startswith(FORBIDDEN_TRACKED_PREFIXES):
            findings.append(f"forbidden tracked path: {normalized}")
        if Path(normalized).suffix.lower() in PRIVATE_FILE_SUFFIXES:
            findings.append(f"private-key file must not be tracked: {normalized}")

    for relative in sorted(candidates):
        path = ROOT / relative
        if not path.is_file():
            continue
        size = path.stat().st_size
        if relative in tracked and size > MAX_TRACKED_BYTES:
            findings.append(f"tracked file exceeds 10 MiB: {relative} ({size} bytes)")
        if size > MAX_TRACKED_BYTES:
            continue
        try:
            payload = path.read_bytes()
        except OSError as exc:
            findings.append(f"cannot read {relative}: {exc}")
            continue
        if b"\0" in payload:
            continue
        for label, pattern in SECRET_PATTERNS.items():
            for match in pattern.finditer(payload):
                line = payload.count(b"\n", 0, match.start()) + 1
                findings.append(f"{relative}:{line}: suspected {label}")

    return findings


def main() -> int:
    try:
        findings = inspect_repository()
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"repository hygiene check could not run: {exc}", file=sys.stderr)
        return 2
    if findings:
        print("Repository hygiene check failed:", file=sys.stderr)
        for finding in findings:
            print(f"- {finding}", file=sys.stderr)
        return 1
    print("Repository hygiene check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
