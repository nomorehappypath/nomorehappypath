# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""One rename rule for every path manifest (defects #1 and #7, 2026-09-24).

A rebuilt hashed asset deletes ``index-OLD.js`` and adds a near-identical
``index-NEW.js``. Git's rename detection pairs them into one path wherever it
is on, so different manifest sites disagreed (two paths on the request side,
one in the fold guard) and the harness refused its own correct candidate.
"""
from __future__ import annotations

from pathlib import Path
import re
import tempfile
import unittest

from harness import accepted_bytes, board, contract, control, cto
from harness.git_broker import BrokerError
from tests.requirements_support import agreed_requirements
from tests import test_git_broker, test_subtask_pipelining

# Referenced through their modules so the loader does not collect the parent
# fixtures again from this module's namespace.


def _own_tests_only(subclass: type, parent: type) -> type:
    """Reuse a fixture class without re-running the parent's tests here."""
    for name in dir(parent):
        if name.startswith("test") and name not in vars(subclass):
            setattr(subclass, name, None)
    return subclass

HARNESS = Path(__file__).resolve().parents[1] / "harness"
BUNDLE = "".join(f"export const line{index} = {index};\n" for index in range(40))


class RenameShapedPipelineTests(test_subtask_pipelining.SubtaskPipeliningTests):
    """The real pipeline: request-review, verdict, fold, on a rename-shaped commit."""

    def setUp(self):
        # The parent's setUp begins the task on its baseline commit, and the
        # first bundle must already be in that baseline, so the fixture is
        # rebuilt here with the bundle committed before the task begins.
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "code"
        self.root.mkdir()
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.name", "Fixture")
        self._git("config", "user.email", "fixture@example.invalid")
        # The owner's git (2.50) pairs similar delete+add into a rename by
        # default; pin it on so the reproduction never depends on the host.
        self._git("config", "diff.renames", "true")
        (self.root / ".gitignore").write_text(".harness/\n", encoding="utf-8")
        (self.root / "test_smoke.py").write_text(
            "import unittest\n\nclass Smoke(unittest.TestCase):\n"
            "    def test_passes(self): self.assertTrue(True)\n",
            encoding="utf-8",
        )
        (self.root / "alpha").mkdir()
        (self.root / "alpha" / "index-OLD.js").write_text(BUNDLE, encoding="utf-8")
        self._git("add", ".gitignore", "test_smoke.py", "alpha/index-OLD.js")
        self._git("commit", "-q", "-m", "baseline with the first bundle")
        session = control.create(self.root, "codex_delivery")
        self.delivery = board.register(
            self.root, "development", board.AWAITING_OWNER_DIRECTION,
            vendor="OpenAI", session_id=session["id"],
        )
        board.record_owner_direction(self.root, session["id"], "Rebuild the bundled application")
        board.begin_task(self.root, self.delivery["id"], "PIPELINE")
        contract.create_contract(self.root, "PIPELINE", "Rebuild the bundled application", ["delivery"])
        agreed_requirements(self.root, self.delivery["id"], "Rebuild and independently verify the bundle.")
        board.define_delivery_plan(
            self.root, self.delivery["id"], "application",
            "Independent product capabilities can use isolated worktrees.",
        )

    def test_a_rebuilt_hashed_asset_passes_request_review_verdict_and_fold(self):
        self.declare()
        reviewer = self.reviewer()
        board.start_subtask(self.root, self.delivery["id"], "alpha")
        workspace = self.workspace("alpha")
        (workspace / "alpha" / "index-OLD.js").unlink()
        (workspace / "alpha" / "index-NEW.js").write_text(BUNDLE + "export const rebuilt = true;\n", encoding="utf-8")
        candidate = board.broker_stage_commit(
            self.root, self.delivery["id"], ["alpha/index-OLD.js", "alpha/index-NEW.js"],
            "rebuild the bundle", subtask="alpha",
        )
        self.assertEqual(candidate["manifest"], ["alpha/index-NEW.js", "alpha/index-OLD.js"])

        request = self.request("alpha")
        self.assertEqual(request["reviewed_files"], ["alpha/index-NEW.js", "alpha/index-OLD.js"])
        self.assertEqual(request["accepted_byte_manifest"]["paths"], ["alpha/index-NEW.js", "alpha/index-OLD.js"])

        passed = self.pass_request(reviewer, request, "alpha")

        state = board.snapshot(self.root)
        task_workspace = Path(state["task_workspaces"]["PIPELINE"])
        self.assertTrue(passed["integrated_commit"])
        self.assertFalse((task_workspace / "alpha" / "index-OLD.js").exists())
        self.assertTrue((task_workspace / "alpha" / "index-NEW.js").read_text().endswith("rebuilt = true;\n"))
        self.assertEqual(
            self._git("-C", str(task_workspace), *accepted_bytes.name_only_arguments(
                f"{state['task_baselines']['PIPELINE']['head']}..{passed['integrated_commit']}")).split(),
            ["alpha/index-NEW.js", "alpha/index-OLD.js"],
        )


_own_tests_only(RenameShapedPipelineTests, test_subtask_pipelining.SubtaskPipeliningTests)


class RenameShapedAcceptMergeTests(test_git_broker.GitBrokerTests):
    """accept-merge compares the certified manifest against the Git range."""

    def test_accept_merge_honours_a_rename_shaped_certified_manifest(self):
        self._git(self.repository, "config", "diff.renames", "true")
        (self.repository / "index-OLD.js").write_text(BUNDLE, encoding="utf-8")
        self._git(self.repository, "add", "index-OLD.js")
        self._git(self.repository, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "bundle")
        self.base_commit = self._git(self.repository, "rev-parse", "HEAD").strip()
        self.state["task_baselines"]["TASK"]["head"] = self.base_commit
        _, workspace = self.create_task_workspace()
        (workspace / "index-OLD.js").unlink()
        (workspace / "index-NEW.js").write_text(BUNDLE + "export const rebuilt = true;\n", encoding="utf-8")
        committed = self.broker.stage_commit("delivery", 2, ["index-OLD.js", "index-NEW.js"], "rebuild")
        self.assertEqual(committed["manifest"], ["index-NEW.js", "index-OLD.js"])
        _, pinned, _ = self.pin_candidate(committed)
        self.state["release_decisions"]["TASK"] = {"decision": "accepted"}
        # The release check records the manifest the owner's Accept later hands
        # to accept-merge (defects #2 and #21: three Accepts in a row were
        # refused because this side paired the rename and the broker did not).
        release_manifest = cto._changed_paths(self.repository, self.base_commit, committed["commit"])
        self.assertEqual(release_manifest, committed["manifest"])
        candidate = {
            "recorded_base": self.base_commit, "commit": committed["commit"], "tree": committed["tree"],
            "manifest": release_manifest, "mirror_ref": pinned["ref"],
        }
        try:
            accepted = self.broker.accept_merge("TASK", candidate, board_mutation=lambda value: None)
        except BrokerError as error:
            self.fail(f"accept-merge refused a rename-shaped certified manifest: {error}")
        self.assertEqual(accepted["commit"], committed["commit"])
        self.assertEqual(self._git(self.repository, "rev-parse", "main").strip(), committed["commit"])


_own_tests_only(RenameShapedAcceptMergeTests, test_git_broker.GitBrokerTests)


class ManifestSiteTests(test_git_broker.GitBrokerTests):
    """Every other manifest site lists both halves of a rename."""

    def rename_shaped_history(self):
        self._git(self.repository, "config", "diff.renames", "true")
        (self.repository / "index-OLD.js").write_text(BUNDLE, encoding="utf-8")
        self._git(self.repository, "add", "index-OLD.js")
        self._git(self.repository, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "bundle")
        base = self._git(self.repository, "rev-parse", "HEAD").strip()
        (self.repository / "index-OLD.js").unlink()
        (self.repository / "index-NEW.js").write_text(BUNDLE + "export const rebuilt = true;\n", encoding="utf-8")
        self._git(self.repository, "add", "-A", ".")
        self._git(self.repository, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "rebuild")
        return base, self._git(self.repository, "rev-parse", "HEAD").strip()

    def test_review_artifact_cto_and_accepted_bytes_agree(self):
        base, head = self.rename_shaped_history()
        both = ["index-NEW.js", "index-OLD.js"]
        self.assertEqual(board._git_review_artifact(self.repository, base)["files"], both)
        self.assertEqual(board._git_review_artifact(self.repository)["files"], both)
        self.assertEqual(cto._changed_paths(self.repository, base, head), both)
        self.assertEqual(accepted_bytes.tree_delta(self.repository, base, head)["paths"], both)
        # git itself would have paired them: the rule, not the host, decides.
        self.assertEqual(
            self._git(self.repository, "diff", "--name-only", base, head).split(), ["index-NEW.js"],
        )


_own_tests_only(ManifestSiteTests, test_git_broker.GitBrokerTests)


class OneRenameRuleInvariantTests(unittest.TestCase):
    """No harness module may build a path manifest or patch on its own."""

    PATTERN = re.compile(r'"(diff|diff-tree)".*"(--name-only|--binary|--no-renames)"')

    def test_every_manifest_and_patch_command_uses_the_shared_helper(self):
        offenders = []
        for module in sorted(HARNESS.glob("*.py")):
            if module.name == "accepted_bytes.py":
                continue
            for number, line in enumerate(module.read_text(encoding="utf-8").splitlines(), 1):
                if self.PATTERN.search(line):
                    offenders.append(f"{module.name}:{number}: {line.strip()}")
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_helper_arguments_carry_the_rule(self):
        for arguments in (
            accepted_bytes.name_only_arguments("a..b"),
            accepted_bytes.name_only_arguments("a", "b", nul=True),
            accepted_bytes.name_only_arguments(cached=True),
            accepted_bytes.binary_patch_arguments("a", "b", paths=["x"]),
            accepted_bytes.commit_manifest_arguments("a"),
        ):
            self.assertIn("--no-renames", arguments)
            self.assertIn("--", arguments)


if __name__ == "__main__":
    unittest.main()
