"""Focused checks for safe profile import and canonical skill installation."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
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
    assert "permissionMode" not in definition
    assert definition["promptFiles"] == [str(home / ".claude" / "agents" / "CFO.md")]
    assert len(definition["ruleFiles"]) == 2
    assert "Confidential body" not in definition["promptBody"]
    assert definition["identity"]["slack"]["credentialRef"] == "env:CFO_SLACK_BOT_TOKEN"
    assert "bypassPermissions" not in str(definition)
    assert definition["identity"] == IDENTITY
    assert "parent-assigned workspace" in definition["promptBody"]
    assert "pick-right-subagent-and-assign-task" in definition["promptBody"]
    assert "does not register a child" in definition["promptBody"]


def test_sdr_does_not_inherit_rules_scoped_only_to_c_level(tmp_path: Path) -> None:
    home = role_home(tmp_path)
    source = home / ".claude" / "agents"
    (source / "SDR.md").write_text((source / "CFO.md").read_text())
    (home / "agents" / "c-level-rules.md").unlink()
    definition = IMPORT.make_definition(home, "sdr", IDENTITY)
    assert definition["ruleFiles"] == [str(home / ".claude" / "rules" / "permissions.md")]


def test_import_requires_existing_shared_rules(tmp_path: Path) -> None:
    home = role_home(tmp_path)
    (home / "agents" / "c-level-rules.md").unlink()
    with pytest.raises(ValueError, match="Required rule file is missing"):
        IMPORT.make_definition(home, "cfo", IDENTITY)


def test_import_requires_explicit_configured_avatar_and_slack_identity(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="configured metadata"):
        IMPORT.make_definition(role_home(tmp_path), "cfo", {})


@pytest.mark.parametrize(
    "identity",
    [
        {**IDENTITY, "token": "private-token-material"},
        {**IDENTITY, "slack": {**IDENTITY["slack"], "token": "private-token-material"}},
        {**IDENTITY, "slack": {**IDENTITY["slack"], "credentialRef": "private-token-material"}},
        {**IDENTITY, "avatar": "https://example.org/avatar?token=private-token-material"},
    ],
)
def test_preview_rejects_credential_values_without_echo(identity: dict[str, object]) -> None:
    with pytest.raises(ValueError) as error:
        IMPORT.checked_identity(identity)
    assert "private-token-material" not in str(error.value)


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
    assert all(call.kwargs.get("scope") == "user" for call in calls)
    assert calls[3].args[3]["writeScope"] == "user"


def test_project_import_requires_explicit_scope_and_uses_project_revisions(tmp_path: Path) -> None:
    with patch.object(IMPORT, "grok_json", side_effect=[{"profiles": []}, {}, {}]) as native:
        IMPORT.apply_definitions("grok", tmp_path, {"cfo": {}}, False, scope="project")
    assert all(call.kwargs.get("scope") == "project" for call in native.call_args_list)
    assert native.call_args_list[-1].args[3]["expectedRevision"] is None
    assert native.call_args_list[-1].args[3]["writeScope"] == "project"


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


@pytest.mark.parametrize("prefix,newline", [("", "\n"), ("\ufeff", "\r\n")])
def test_frontmatter_inline_delimiters_preserve_model_and_effort(
    tmp_path: Path, prefix: str, newline: str
) -> None:
    home = role_home(tmp_path)
    source = home / ".claude" / "agents" / "CFO.md"
    source.write_bytes((prefix + newline.join([
        "---", 'description: "Finance --- reporting"',
        "model: catalog-exact", "effort: high", "---", "Source body --- untouched",
    ])).encode("utf-8"))
    definition = IMPORT.make_definition(home, "cfo", IDENTITY)
    assert definition["model"] == "catalog-exact"
    assert definition["effort"] == "high"
    assert definition["description"] == "Finance --- reporting"


@pytest.mark.parametrize("source", ["model: no-header", "---\nmodel: no-close", " ---\nmodel: wrong\n---"])
def test_frontmatter_requires_complete_delimiter_lines(tmp_path: Path, source: str) -> None:
    path = tmp_path / "role.md"
    path.write_text(source)
    with pytest.raises(ValueError, match="frontmatter"):
        IMPORT.frontmatter(path)


@pytest.mark.parametrize("avatar", ["💼", "👩‍💻", "⚙️", "https://example.org/cfo.png"])
def test_import_accepts_native_avatar_forms(avatar: str) -> None:
    identity = {**IDENTITY, "avatar": avatar}
    assert IMPORT.checked_identity(identity) == identity


@pytest.mark.parametrize("avatar", ["CFO", "", "💼 ", "💼" * 17, "file:///tmp/avatar.png", "https://user:secret@example.org/a", "https://[invalid"])
def test_import_refuses_invalid_avatar_without_echoing_it(avatar: str) -> None:
    with pytest.raises(ValueError, match="configured metadata") as error:
        IMPORT.checked_identity({**IDENTITY, "avatar": avatar})
    assert "secret" not in str(error.value)


def test_import_accepts_reference_only_one_password_identity() -> None:
    identity = {**IDENTITY, "slack": {**IDENTITY["slack"], "credentialRef": "op://Shared/CFO/token"}}
    assert IMPORT.checked_identity(identity) == identity


@pytest.mark.parametrize("catalog", [
    {}, {"profiles": None}, {"profiles": {}}, {"profiles": [None]},
    {"profiles": [{"revision": "r1"}]},
    {"profiles": [{"id": "cfo"}]},
    {"profiles": [{"id": "cfo", "revision": None}]},
    {"profiles": [{"id": "../cfo", "revision": "r1"}]},
    {"profiles": [{"id": "cfo", "revision": 1}]},
    {"profiles": [{"id": "cfo", "revision": "r1"}, {"id": "cfo", "revision": "r2"}]},
])
def test_malformed_catalog_refuses_before_validation_or_writes(tmp_path: Path, catalog: object) -> None:
    with patch.object(IMPORT, "grok_json", return_value=catalog) as native:
        with pytest.raises(ValueError, match="catalog.*malformed"):
            IMPORT.apply_definitions("grok", tmp_path, {"cfo": {}}, True)
    assert native.call_count == 1


@pytest.mark.parametrize("failure", [OSError("private-process-material"), subprocess.TimeoutExpired("private-process-material", 30)])
def test_partial_apply_report_survives_process_failure(tmp_path: Path, failure: Exception) -> None:
    with patch.object(IMPORT, "grok_json", side_effect=[{"profiles": []}, {}, {}, {}, failure]) as native:
        with pytest.raises(ValueError, match="stopped at cro; already applied: cfo") as error:
            IMPORT.apply_definitions("grok", tmp_path, {"cfo": {}, "cro": {}}, False)
    assert "private-process-material" not in str(error.value)
    assert native.call_count == 5
