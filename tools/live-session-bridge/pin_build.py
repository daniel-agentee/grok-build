"""Verify pinned native inputs, rebuild locally, and pin the resulting artifact.

Build hashes can differ between checkout paths or linkers. This is an explicit
rebuild, never a checksum bypass for an arbitrary existing executable.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
# This workflow cannot contribute compiled source. Keep the exception exact so
# arbitrary files under .github still fail native source verification.
NATIVE_PATHS = [".", ":(exclude)tools", ":(exclude).github/workflows/mobile-bridge.yml"]


def output(command: list[str], root: Path) -> bytes:
    return subprocess.check_output(command, cwd=root)


def verify_inputs(root: Path, manifest: dict) -> None:
    patch = (root / "tools/live-session-bridge/resident-only-attach.patch").read_bytes()
    if hashlib.sha256(patch).hexdigest() != manifest["patch_sha256"]:
        raise ValueError("Native patch differs from the pinned manifest")
    if (
        hashlib.sha256((root / "Cargo.lock").read_bytes()).hexdigest()
        != manifest["cargo_lock_sha256"]
    ):
        raise ValueError("Cargo.lock differs from the pinned manifest")
    native_diff = output(
        ["git", "diff", "--binary", manifest["base_commit"], "--", *NATIVE_PATHS],
        root,
    )
    if native_diff != patch:
        raise ValueError("Native source differs from the exact reviewed base plus patch")
    if output(
        ["git", "ls-files", "--others", "--exclude-standard", "--", *NATIVE_PATHS], root
    ).strip():
        raise ValueError("Untracked native source files must be reviewed before rebuilding")
    for tool in ("rustc", "cargo"):
        if output([tool, "--version"], root).decode().strip() != manifest[f"{tool}_version"]:
            raise ValueError(f"{tool} version differs from the tested compiler")
    host = output(["rustc", "-vV"], root).decode().splitlines()
    if f"host: {manifest['target']}" not in host:
        raise ValueError("Compiler host differs from the tested target")


def rebuild_and_pin(root: Path = ROOT) -> str:
    here = root / "tools/live-session-bridge"
    manifest_path = here / "build-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    verify_inputs(root, manifest)
    # Read the launcher before building, and require exactly one existing pin.
    launcher_path = here / "launch.py"
    launcher = launcher_path.read_text()
    pattern = r'(?m)^BINARY_SHA256 = "[0-9a-f]{64}"$'
    if len(re.findall(pattern, launcher)) != 1:
        raise ValueError("Launcher must contain one explicit artifact pin")
    environment = os.environ.copy()
    environment.update(manifest["build_environment"])
    subprocess.run(manifest["build_command"], cwd=root, env=environment, check=True)
    # Recheck inputs to detect source edits made while the compiler was running.
    verify_inputs(root, manifest)
    binary = root / manifest["binary"]
    version = output([str(binary), "--version"], root).decode().strip()
    commit = output(["git", "rev-parse", "HEAD"], root).decode().strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Cannot identify the build checkout revision")
    expected = f"grok {manifest['cli_version']} ({commit[:12]}) [alpha]"
    if version != expected:
        raise ValueError("Built executable reports an unexpected version")
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    manifest["version_output"] = version
    manifest["artifact_checkout_commit"] = commit
    manifest["binary_sha256"] = digest
    launcher_path.write_text(re.sub(pattern, f'BINARY_SHA256 = "{digest}"', launcher))
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    return digest


if __name__ == "__main__":
    try:
        digest = rebuild_and_pin()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"Build pin unchanged: {error}") from None
    print(f"Pinned locally rebuilt Grok artifact SHA256 {digest}.")
    print(
        "Run the targeted tests and live acceptance before treating this local build as verified."
    )
