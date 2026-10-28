#!/usr/bin/env python3
"""Install canonical routing skill links, preserving any previous local skills."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path


def install(source: Path, target: Path) -> Path | None:
    """Link one committed skill; never overwrite an existing local copy."""
    if not (source / "SKILL.md").is_file():
        raise ValueError(f"Missing committed skill: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() and target.resolve() == source.resolve():
        return None
    backup = None
    if target.exists() or target.is_symlink():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = target.parent.parent / "backups" / "skills" / f"{target.name}-{stamp}"
        backup.parent.mkdir(parents=True, exist_ok=True)
        target.rename(backup)
    try:
        target.symlink_to(source.resolve(), target_is_directory=True)
    except OSError:
        if backup is not None:
            backup.rename(target)
        raise
    return backup


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--grok-home", type=Path, default=Path.home() / ".grok")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2] / "profiles" / "skills"
    for name in ("pick-right-subagent-and-assign-task", "sub"):
        target = args.grok_home / "skills" / name
        backup = install(root / name, target)
        print(
            f"Installed {target}" + (f"; previous version preserved at {backup}" if backup else "")
        )


if __name__ == "__main__":
    main()
