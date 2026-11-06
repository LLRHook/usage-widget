"""Build and stage the host Rust library beside the collector; never runs the app."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from usage_core import library_name, library_path, platform_key

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "native" / "Cargo.toml"


def main() -> int:
    # Cargo artifacts come from its JSON output, so CARGO_TARGET_DIR and a
    # repository-independent shared build cache both work without path guesses.
    if os.environ.get("CARGO_BUILD_TARGET"):
        raise SystemExit("Build with the host Rust toolchain; unset CARGO_BUILD_TARGET")
    process = subprocess.run([
        "cargo", "build", "--locked", "--release", "--manifest-path", str(MANIFEST),
        "--message-format=json-render-diagnostics",
    ], cwd=HERE, stdout=subprocess.PIPE, text=True, encoding="utf-8", check=True)
    artifacts = []
    for line in process.stdout.splitlines():
        event = json.loads(line)
        if event.get("reason") == "compiler-artifact" and event["target"]["name"] == "ai_usage_core":
            artifacts.extend(Path(name) for name in event["filenames"] if Path(name).name == library_name())
    if len(artifacts) != 1:
        raise SystemExit("Cargo did not produce exactly one host collector library")
    # Load the actual output before staging: rejects a wrong architecture, stale
    # ABI or missing exports without replacing a working staged library.
    import usage_core
    usage_core._load_library.cache_clear()
    original = usage_core.library_path
    try:
        usage_core.library_path = lambda: artifacts[0]
        usage_core._load_library()
    finally:
        usage_core.library_path = original
        usage_core._load_library.cache_clear()
    destination = library_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + f".{os.getpid()}.tmp")
    try:
        shutil.copyfile(artifacts[0], temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"Built native collector for {platform_key()}: {destination.relative_to(HERE)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
