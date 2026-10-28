"""Focused checks for safe profile import and canonical skill installation."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest


def load_module(filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(filename, Path(__file__).parent / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


IMPORT = load_module("import-claude-profiles.py")
INSTALL = load_module("install-routing-skill.py")
IDENTITY = {
    "displayName": "Finance lead",
    "avatar": "https://example.org/finance.png",
    "slack": {
        "botKey": "finance",
        "botUserId": "UTESTFINANCE",
        "credentialRef": "env:CFO_SLACK_BOT_TOKEN",
    },
}


def role_home(tmp_path: Path) -> Path:
    agents = tmp_path / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "CFO.md").write_text(
        "---\nname: CFO\ndescription: Finance lead\nmodel: catalog-strong\neffort: medium\npermissionMode: bypassPermissions\n---\nConfidential body is kept at the source.\n",
    )
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "c-level-rules.md").write_text("Shared rule")
    rules = tmp_path / ".claude" / "rules"
    rules.mkdir()
    (rules / "permissions.md").write_text("Keep approvals")
    return tmp_path


def test_import_preserves_source_and_does_not_copy_permission_bypass(tmp_path: Path) -> None:
    home = role_home(tmp_path)
    definition = IMPORT.make_definition(home, "cfo", IDENTITY)
    assert definition["model"] == "catalog-strong"
    assert definition["effort"] == "medium"
    assert definition["speed"] == "standard"
    assert definition["permissionMode"] == "default"
    assert definition["promptFiles"] == [str(home / ".claude" / "agents" / "CFO.md")]
    assert len(definition["ruleFiles"]) == 2
    assert "Confidential body" not in definition["promptBody"]
    assert definition["identity"]["slack"]["credentialRef"] == "env:CFO_SLACK_BOT_TOKEN"
    assert "bypassPermissions" not in str(definition)
    assert definition["identity"] == IDENTITY
    assert "parent-assigned workspace" in definition["promptBody"]


def test_import_requires_existing_shared_rules(tmp_path: Path) -> None:
    home = role_home(tmp_path)
    (home / "agents" / "c-level-rules.md").unlink()
    with pytest.raises(ValueError, match="Required rule file is missing"):
        IMPORT.make_definition(home, "cfo", IDENTITY)


def test_import_requires_explicit_configured_avatar_and_slack_identity(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Explicit Slack identity"):
        IMPORT.make_definition(role_home(tmp_path), "cfo", {})


def test_malformed_yaml_error_does_not_echo_source_text(tmp_path: Path) -> None:
    path = tmp_path / "role.md"
    path.write_text("---\nmodel: [private-credential-material\n---\nBody")
    with pytest.raises(ValueError, match="Role YAML is malformed") as error:
        IMPORT.frontmatter(path)
    assert "private-credential-material" not in str(error.value)


def test_existing_profile_preserved_without_update_flag(tmp_path: Path) -> None:
    with patch.object(
        IMPORT, "grok_json", return_value={"profiles": [{"id": "cfo", "revision": "r1"}]}
    ) as native:
        with pytest.raises(ValueError, match="Existing profiles preserved"):
            IMPORT.apply_definitions("grok", tmp_path, {"cfo": {}}, False)
    assert native.call_count == 1


def test_validate_complete_batch_before_first_write_and_use_cas(tmp_path: Path) -> None:
    with patch.object(
        IMPORT,
        "grok_json",
        side_effect=[
            {"profiles": [{"id": "cfo", "revision": "r1"}]},
            {},
            {},
            {},
            {},
        ],
    ) as native:
        assert IMPORT.apply_definitions("grok", tmp_path, {"cfo": {}, "cro": {}}, True) == [
            "cfo",
            "cro",
        ]
    calls = native.call_args_list
    assert [call.args[2] for call in calls] == [
        ["list"],
        ["validate"],
        ["validate"],
        ["apply"],
        ["apply"],
    ]
    assert calls[3].args[3]["expectedRevision"] == "r1"
    assert calls[4].args[3]["expectedRevision"] is None


def test_refused_validation_writes_nothing(tmp_path: Path) -> None:
    with patch.object(
        IMPORT, "grok_json", side_effect=[{"profiles": []}, ValueError("Refused")]
    ) as native:
        with pytest.raises(ValueError, match="Refused"):
            IMPORT.apply_definitions("grok", tmp_path, {"cfo": {}}, False)
    assert all(call.args[2] != ["apply"] for call in native.call_args_list)


def test_stale_cas_stops_import_and_reports_partial_batch(tmp_path: Path) -> None:
    with patch.object(
        IMPORT,
        "grok_json",
        side_effect=[
            {"profiles": []},
            {},
            {},
            {},
            ValueError("Conflict"),
        ],
    ) as native:
        with pytest.raises(ValueError, match="stopped at cro; already applied: cfo"):
            IMPORT.apply_definitions("grok", tmp_path, {"cfo": {}, "cro": {}}, False)
    assert native.call_count == 5


def test_skill_install_preserves_local_source_and_is_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "canonical"
    source.mkdir()
    (source / "SKILL.md").write_text("canonical")
    target = tmp_path / "skills" / "sub"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("local user change")
    backup = INSTALL.install(source, target)
    assert backup is not None
    assert backup.parent == tmp_path / "backups" / "skills"
    assert (backup / "SKILL.md").read_text() == "local user change"
    assert (target / "SKILL.md").read_text() == "canonical"
    assert INSTALL.install(source, target) is None
