"""Local rebuild pinning fails closed on source drift and build failures."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import pin_build


class PinBuildTests(unittest.TestCase):
    def fixture(self, directory):
        root = Path(directory)
        here = root / "tools/live-session-bridge"
        here.mkdir(parents=True)
        (root / "Cargo.lock").write_bytes(b"locked")
        (here / "resident-only-attach.patch").write_bytes(b"reviewed patch")
        manifest = {
            "patch_sha256": hashlib.sha256(b"reviewed patch").hexdigest(),
            "cargo_lock_sha256": hashlib.sha256(b"locked").hexdigest(),
            "base_commit": "reviewed-base",
            "rustc_version": "tested rustc",
            "cargo_version": "tested cargo",
            "target": "aarch64-apple-darwin",
            "build_environment": {"CARGO_INCREMENTAL": "0"},
            "build_command": ["cargo", "build", "--locked", "--offline"],
            "binary": "grok",
            "binary_sha256": "0" * 64,
            "cli_version": "1.0.45",
            "version_output": "grok 1.0.45 (old-revision) [alpha]",
        }
        (here / "build-manifest.json").write_text(json.dumps(manifest))
        (here / "launch.py").write_text('BINARY_SHA256 = "' + "0" * 64 + '"\n')
        (root / "grok").write_bytes(b"locally built artifact")
        return root, here, manifest

    def native_output(self, command, root):
        if command[:2] == ["git", "diff"]:
            return b"reviewed patch"
        if command[:2] == ["git", "ls-files"]:
            return b""
        if command[:2] == ["git", "rev-parse"]:
            return b"123456789abc" + b"d" * 28
        return {
            "rustc": b"tested rustc"
            if command[1] == "--version"
            else b"host: aarch64-apple-darwin",
            "cargo": b"tested cargo",
            str(root / "grok"): b"grok 1.0.45 (123456789abc) [alpha]",
        }[command[0]]

    def test_rebuild_pins_only_verified_source_and_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root, here, _ = self.fixture(directory)
            with (
                patch.object(pin_build, "output", side_effect=self.native_output),
                patch.object(pin_build.subprocess, "run") as build,
            ):
                digest = pin_build.rebuild_and_pin(root)
            build.assert_called_once()
            self.assertEqual(build.call_args.kwargs["env"]["CARGO_INCREMENTAL"], "0")
            self.assertEqual(digest, hashlib.sha256(b"locally built artifact").hexdigest())
            self.assertEqual(
                json.loads((here / "build-manifest.json").read_text())["binary_sha256"], digest
            )
            self.assertIn(digest, (here / "launch.py").read_text())
            final = json.loads((here / "build-manifest.json").read_text())
            self.assertEqual(final["version_output"], "grok 1.0.45 (123456789abc) [alpha]")
            self.assertEqual(final["artifact_checkout_commit"], "123456789abc" + "d" * 28)

    def test_changed_patch_or_lock_never_builds(self):
        for filename in ("Cargo.lock", "tools/live-session-bridge/resident-only-attach.patch"):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as directory:
                root, _, manifest = self.fixture(directory)
                (root / filename).write_bytes(b"unexpected")
                with patch.object(pin_build.subprocess, "run") as build:
                    with self.assertRaises(ValueError):
                        pin_build.verify_inputs(root, manifest)
                    build.assert_not_called()

    def test_source_untracked_compiler_and_target_drift_never_build(self):
        bad_outputs = (
            (["git", "diff"], b"unreviewed diff"),
            (["git", "ls-files"], b"crates/new.rs"),
            (["rustc", "--version"], b"different rustc"),
            (["rustc", "-vV"], b"host: different-target"),
        )
        for prefix, bad in bad_outputs:
            with self.subTest(prefix=prefix), tempfile.TemporaryDirectory() as directory:
                root, _, manifest = self.fixture(directory)

                def native(command, root):
                    return bad if command[:2] == prefix else self.native_output(command, root)

                with patch.object(pin_build, "output", side_effect=native):
                    with self.assertRaises(ValueError):
                        pin_build.verify_inputs(root, manifest)

    def git_fixture(self, directory):
        root, here, manifest = self.fixture(directory)
        native = root / "crates/agent.rs"
        native.parent.mkdir()
        native.write_text("// baseline\n")
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Bridge Test",
                "-c",
                "user.email=bridge-test@example.invalid",
                "commit",
                "-qm",
                "Fixture baseline",
            ],
            cwd=root,
            check=True,
        )
        manifest["base_commit"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip()
        native.write_text("// reviewed native change\n")
        patch_data = subprocess.check_output(
            ["git", "diff", "--binary", "HEAD", "--", *pin_build.NATIVE_PATHS], cwd=root
        )
        (here / "resident-only-attach.patch").write_bytes(patch_data)
        manifest["patch_sha256"] = hashlib.sha256(patch_data).hexdigest()
        return root, manifest

    def git_native_output(self, command, root):
        if command[0] == "git":
            return subprocess.check_output(command, cwd=root)
        return self.native_output(command, root)

    def test_only_named_workflow_metadata_is_excluded_from_native_patch(self):
        with tempfile.TemporaryDirectory() as directory:
            root, manifest = self.git_fixture(directory)
            workflow = root / ".github/workflows/mobile-bridge.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text("name: Tooling only\n")
            with patch.object(pin_build, "output", side_effect=self.git_native_output):
                pin_build.verify_inputs(root, manifest)
                subprocess.run(["git", "add", str(workflow)], cwd=root, check=True)
                pin_build.verify_inputs(root, manifest)

    def test_workflow_exception_still_rejects_compiled_source_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            root, manifest = self.git_fixture(directory)
            with patch.object(pin_build, "output", side_effect=self.git_native_output):
                (root / "crates/agent.rs").write_text("// unreviewed change\n")
                with self.assertRaisesRegex(ValueError, "Native source differs"):
                    pin_build.verify_inputs(root, manifest)
                (root / "crates/agent.rs").write_text("// reviewed native change\n")
                (root / "crates/new.rs").write_text("// untracked native source\n")
                with self.assertRaisesRegex(ValueError, "Untracked native source"):
                    pin_build.verify_inputs(root, manifest)

    def test_other_github_files_remain_subject_to_native_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            root, manifest = self.git_fixture(directory)
            other = root / ".github/injected.rs"
            other.parent.mkdir()
            other.write_text("// not exempt automation metadata\n")
            with patch.object(pin_build, "output", side_effect=self.git_native_output):
                with self.assertRaisesRegex(ValueError, "Untracked native source"):
                    pin_build.verify_inputs(root, manifest)
                subprocess.run(["git", "add", str(other)], cwd=root, check=True)
                with self.assertRaisesRegex(ValueError, "Native source differs"):
                    pin_build.verify_inputs(root, manifest)

    def test_failed_build_or_changed_version_leaves_pins_unchanged(self):
        for fail_build in (True, False):
            with self.subTest(fail_build=fail_build), tempfile.TemporaryDirectory() as directory:
                root, here, _ = self.fixture(directory)
                before = [
                    (here / name).read_bytes() for name in ("launch.py", "build-manifest.json")
                ]

                def native(command, root):
                    return (
                        b"wrong version"
                        if command[0] == str(root / "grok")
                        else self.native_output(command, root)
                    )

                with (
                    patch.object(pin_build, "output", side_effect=native),
                    patch.object(
                        pin_build.subprocess,
                        "run",
                        side_effect=subprocess.CalledProcessError(1, "cargo")
                        if fail_build
                        else None,
                    ),
                ):
                    with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                        pin_build.rebuild_and_pin(root)
                self.assertEqual(
                    before,
                    [(here / name).read_bytes() for name in ("launch.py", "build-manifest.json")],
                )

    def test_source_edit_during_build_does_not_pin_the_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root, here, _ = self.fixture(directory)
            before = [(here / name).read_bytes() for name in ("launch.py", "build-manifest.json")]
            with (
                patch.object(
                    pin_build, "verify_inputs", side_effect=[None, ValueError("Source changed")]
                ),
                patch.object(pin_build.subprocess, "run") as build,
            ):
                with self.assertRaisesRegex(ValueError, "Source changed"):
                    pin_build.rebuild_and_pin(root)
                build.assert_called_once()
            self.assertEqual(
                before,
                [(here / name).read_bytes() for name in ("launch.py", "build-manifest.json")],
            )


if __name__ == "__main__":
    unittest.main()
