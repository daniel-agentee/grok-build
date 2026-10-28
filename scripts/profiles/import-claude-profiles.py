#!/usr/bin/env python3
"""Import Claude seat references through Grok's validating, compare-and-swap CLI.

The default prints a secret-free plan. --apply writes through the native CLI;
existing definitions require --update-existing. Credential files are never read.
Requires PyYAML for the existing Claude frontmatter format.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any
from urllib.parse import urlsplit

import yaml


SEATS = {
    "cfo": ("CFO", "CFO"),
    "cos": ("CoS", "ChiefOfStaff"),
    "cro": ("CRO", "CRO"),
    "coo": ("COO", "COO"),
    "cto": ("CTO", "CTO"),
    "sdr": ("SDR", "SDR"),
}


def checked_identity(identity: dict[str, Any]) -> dict[str, Any]:
    """Allow references only, including in the default stdout preview."""
    refused = "Identity input must contain configured metadata and credential references only"
    if set(identity) - {"displayName", "avatar", "slack"}:
        raise ValueError(refused)
    if any(not isinstance(identity.get(key), str) for key in ("displayName", "avatar")):
        raise ValueError(refused)
    slack = identity.get("slack")
    if not isinstance(slack, dict) or set(slack) != {"botKey", "botUserId", "credentialRef"}:
        raise ValueError(refused)
    if any(not isinstance(value, str) for value in slack.values()):
        raise ValueError(refused)
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", slack["botKey"]) or not re.fullmatch(
        r"U[A-Z0-9]+", slack["botUserId"]
    ):
        raise ValueError(refused)
    reference = slack["credentialRef"]
    env_reference = re.fullmatch(r"env:[A-Z_][A-Z0-9_]*", reference)
    op_reference = re.fullmatch(r"op://[^/\r\n?#]+/[^/\r\n?#]+/[^/\r\n?#]+", reference)
    if not env_reference and not op_reference:
        raise ValueError(refused)
    avatar = urlsplit(identity["avatar"])
    if (
        avatar.scheme != "https"
        or not avatar.hostname
        or avatar.username
        or avatar.password
        or avatar.query
        or avatar.fragment
    ):
        raise ValueError(refused)
    return identity


def frontmatter(path: Path) -> dict[str, Any]:
    """Read the role metadata without copying its prompt or launch flags."""
    text = path.read_text(encoding="utf-8")
    sections = text.split("---", 2)
    if len(sections) != 3 or sections[0].strip():
        raise ValueError(f"Role has no YAML frontmatter: {path}")
    try:
        metadata = yaml.safe_load(sections[1])
    except yaml.YAMLError as exc:
        raise ValueError(f"Role YAML is malformed: {path}") from exc
    if not isinstance(metadata, dict):
        raise ValueError(f"Role frontmatter must be a mapping: {path}")
    return metadata


def make_definition(
    home: Path, seat: str, identity: dict[str, Any], model: str | None = None
) -> dict[str, Any]:
    """Create a native wrapper; the source prompt and rules remain authoritative."""
    identity = checked_identity(identity)
    role, workspace = SEATS[seat]
    source = home / ".claude" / "agents" / f"{role}.md"
    metadata = frontmatter(source)
    # The shared file explicitly binds the five C-level seats, not SDR.
    rules = [] if seat == "sdr" else [home / "agents" / "c-level-rules.md"]
    rules.extend(sorted((home / ".claude" / "rules").glob("*.md")))
    for path in rules:
        if not path.is_file():
            raise ValueError(f"Required rule file is missing: {path}")
    selected_model = model or metadata.get("model", "inherit")
    effort = metadata.get("effort", "medium")
    if not isinstance(selected_model, str) or not isinstance(effort, str):
        raise ValueError(f"Role model and effort must be strings: {source}")
    return {
        "name": seat,
        "description": str(metadata.get("description") or f"{role} specialist"),
        "model": selected_model,
        "effort": effort,
        "speed": "standard",
        "skills": [],
        "promptBody": (
            f"Use the referenced {role} instructions and shared rules for this task. "
            f"Role reference material is in {home / 'agents' / workspace}. "
            "The child's workspace remains the parent-assigned workspace. "
            "Grok platform adaptation: the canonical "
            "pick-right-subagent-and-assign-task skill governs Grok delegation, including "
            "model, reasoning and speed selection; provider-specific model hints in "
            "referenced Claude rules are historical platform guidance. "
            "Workspace cards are reference context; historical startup/model/permission "
            "commands there do not override this native profile's admitted settings. "
            "Referenced Slack push and watchdog descriptions belong to the original "
            "persistent seat; selecting this profile does not register a child for its "
            "inbox, transfer that seat's authority or authorize external messages. "
            "Use only the selected native identity through an authorized workflow."
        ),
        "promptFiles": [str(source.resolve())],
        "ruleFiles": [str(path.resolve()) for path in rules],
        "identity": identity,
    }


def grok_json(
    binary: str,
    cwd: Path,
    args: list[str],
    payload: dict[str, Any] | None = None,
    scope: str = "user",
) -> dict[str, Any]:
    """Invoke native control with configuration on stdin, never shell or secret argv."""
    result = subprocess.run(
        [binary, "profiles", "--cwd", str(cwd), "--scope", scope, *args, "--json"],
        input=json.dumps(payload) if payload is not None else None,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("Native Grok profile control returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("Native Grok profile control returned a non-object")
    if result.returncode == 0 and value.get("version") != 1:
        raise ValueError("Native Grok profile control requires the compatible version-1 API")
    validation = value.get("validation")
    if (
        result.returncode
        or value.get("error")
        or (isinstance(validation, dict) and validation.get("valid") is False)
    ):
        # Native error text can quote rejected configuration. Do not echo it or stderr.
        raise ValueError(
            "Native Grok refused the import; validate the selected model and source references"
        )
    return value


def apply_definitions(
    binary: str,
    cwd: Path,
    definitions: dict[str, dict[str, Any]],
    update: bool,
    scope: str = "user",
) -> list[str]:
    catalog = grok_json(binary, cwd, ["list"], scope=scope)
    existing = {entry["id"]: entry for entry in catalog.get("profiles", [])}
    collisions = sorted(set(definitions) & set(existing))
    if collisions and not update:
        raise ValueError(
            f"Existing profiles preserved: {', '.join(collisions)}; use --update-existing intentionally"
        )
    # Validate the complete batch before the first write. CAS still guards every profile.
    for profile_id, definition in definitions.items():
        grok_json(
            binary,
            cwd,
            ["validate"],
            {"id": profile_id, "definition": definition, "writeScope": scope},
            scope=scope,
        )
    applied = []
    for profile_id, definition in definitions.items():
        revision = existing.get(profile_id, {}).get("revision")
        try:
            grok_json(
                binary,
                cwd,
                ["apply"],
                {
                    "id": profile_id,
                    "expectedRevision": revision,
                    "definition": definition,
                    "writeScope": scope,
                },
                scope=scope,
            )
        except ValueError as exc:
            raise ValueError(
                f"Import stopped at {profile_id}; already applied: {', '.join(applied) or 'none'}. Refresh the catalog before retrying"
            ) from exc
        applied.append(profile_id)
    return applied


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument("--grok", default="grok")
    parser.add_argument(
        "--scope",
        choices=["user", "project"],
        default="user",
        help="User profiles apply across workspaces; project scope explicitly creates local overrides",
    )
    parser.add_argument("--seat", choices=sorted(SEATS), action="append")
    parser.add_argument(
        "--include-researcher", action="store_true",
        help="Also install the committed native Researcher template; General is a compiled native builtin",
    )
    parser.add_argument(
        "--identities",
        type=Path,
        required=True,
        help="Secret-free JSON mapping seat IDs to configured identity metadata",
    )
    parser.add_argument(
        "--model", help="Explicit catalog model override; omitted retains the role's model"
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--update-existing", action="store_true")
    args = parser.parse_args()
    try:
        identities = json.loads(args.identities.read_text(encoding="utf-8"))
        if not isinstance(identities, dict):
            raise ValueError("Identity input must map seat IDs to identity objects")
        for seat in args.seat or SEATS:
            if not isinstance(identities.get(seat), dict):
                raise ValueError(f"Identity input is missing {seat}")
        definitions = {
            seat: make_definition(args.home.resolve(), seat, identities[seat], args.model)
            for seat in (args.seat or SEATS)
        }
        if args.include_researcher:
            template = Path(__file__).resolve().parents[2] / "profiles/templates/researcher.json"
            definitions["researcher"] = json.loads(template.read_text(encoding="utf-8"))
        if args.apply:
            applied = apply_definitions(
                args.grok, args.cwd.resolve(), definitions, args.update_existing, args.scope
            )
            print(json.dumps({"applied": applied, "writeScope": args.scope}))
        else:
            print(
                json.dumps(
                    {"version": 1, "writeScope": args.scope, "definitions": definitions}, indent=2
                )
            )
    except json.JSONDecodeError:
        print("Import refused: malformed identity JSON", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"Import refused: {exc}", file=sys.stderr)
        return 1
    except (OSError, subprocess.TimeoutExpired):
        print(
            "Import refused: source or native control unavailable; no credentials were read",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
