# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Delivery Scenario Ledger for Tasks 2/3 of the Dedicated Support NMHP (requirements R3-R9):
exact-version checkout, replication, the real repair agent and other-vendor review gate, the
pull request and the owner-key Deploy, the spending-ceiling gate, and the "never retried" one-email-per-milestone rule. Everything here runs
against a local fake git repository and a monkeypatched GitHub API — never the real GitHub, never
a real mail server.
"""
from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

from harness import claude_auth, support_fix, support_github, support_mode as sm, support_repair, support_replicate
from tests.test_support_mode import TEST_OWNER_KEY, owner_opener
from tests.test_warning_email import RecordingSmtp
from tests.environment_support import require_loopback


def _run(*args, cwd):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


def _make_fake_app_repo(root: Path, *, reproduces: bool) -> Path:
    """Build a local bare "GitHub" repository with one tagged commit carrying SUPPORT_PLAN.md.

    Args:
        root: an existing empty directory to build the repo in.
        reproduces: whether SUPPORT_PLAN.md's reproduce command exits non-zero (the bug happens).

    Return: the path to the repository (usable as a local "app_repo" setting).
    """
    repo = root / "app.git"
    work = root / "work"
    _run("git", "init", "--quiet", "--bare", str(repo), cwd=root)
    _run("git", "clone", "--quiet", str(repo), str(work), cwd=root)
    _run("git", "config", "user.email", "fixture@example.com", cwd=work)
    _run("git", "config", "user.name", "Fixture", cwd=work)
    _run("git", "checkout", "--quiet", "-B", "main", cwd=work)
    reproduce = (
        "python3 -c \"import os, sys; sys.exit(0 if os.path.exists('fixed.txt') else 1)\"" if reproduces
        else "python3 -c \"import sys; sys.exit(0)\""
    )
    (work / "SUPPORT_PLAN.md").write_text("# Support plan\n\n## Reproduce\n```\n" + reproduce + "\n```\n")
    _run("git", "add", "SUPPORT_PLAN.md", cwd=work)
    _run("git", "commit", "--quiet", "-m", "fixture", cwd=work)
    _run("git", "tag", "v1.0.0", cwd=work)
    _run("git", "push", "--quiet", "origin", "main", "--tags", cwd=work)
    return repo


class ReplicationFromSupportPlan(unittest.TestCase):
    """R4: Support follows SUPPORT_PLAN.md and shows what it tried, on both outcomes."""

    def test_reproduce_command_that_fails_is_replicated(self):
        with tempfile.TemporaryDirectory() as root:
            repo = _make_fake_app_repo(Path(root), reproduces=True)
            checkout = support_github.checkout_version(str(repo), "v1.0.0", Path(root) / "scratch")
            result = support_replicate.replicate(checkout)
            self.assertTrue(result.replicated)
            self.assertIn("exit 1", result.attempts[0])

    def test_reproduce_command_that_passes_is_not_replicated(self):
        with tempfile.TemporaryDirectory() as root:
            repo = _make_fake_app_repo(Path(root), reproduces=False)
            checkout = support_github.checkout_version(str(repo), "v1.0.0", Path(root) / "scratch")
            result = support_replicate.replicate(checkout)
            self.assertFalse(result.replicated)
            self.assertIn("exit 0", result.attempts[0])

    def test_no_support_plan_is_not_replicated_and_says_what_was_tried(self):
        with tempfile.TemporaryDirectory() as root:
            empty = Path(root) / "empty"
            empty.mkdir()
            result = support_replicate.replicate(empty)
            self.assertFalse(result.replicated)
            self.assertIn("no SUPPORT_PLAN.md", result.attempts[0])


class ExactVersionCheckout(unittest.TestCase):
    """R3: Support checks out exactly the reported tag/commit, never a guess."""

    def test_checkout_lands_on_the_exact_tag(self):
        with tempfile.TemporaryDirectory() as root:
            repo = _make_fake_app_repo(Path(root), reproduces=True)
            checkout = support_github.checkout_version(str(repo), "v1.0.0", Path(root) / "scratch")
            self.assertTrue((checkout / "SUPPORT_PLAN.md").is_file())

    def test_an_unknown_tag_fails_rather_than_guessing(self):
        with tempfile.TemporaryDirectory() as root:
            repo = _make_fake_app_repo(Path(root), reproduces=True)
            with self.assertRaises(support_github.GitCheckoutFailed):
                support_github.checkout_version(str(repo), "v9.9.9-does-not-exist", Path(root) / "scratch")



FAKE_CLAUDE_EDITS = {
    "fixes": "import pathlib; pathlib.Path('fixed.txt').write_text('fixed by the fake repair agent')",
    "nothing": "pass",
    "rewrites_plan": "import pathlib; pathlib.Path('SUPPORT_PLAN.md').write_text('# plan\\n\\n## Reproduce\\n```\\ntrue\\n```\\n')",
    "half": "import pathlib; pathlib.Path('notes.txt').write_text('half done')",
}


def _fake_vendors(root: Path, *, claude_does: str = "fixes", codex_says: str = "REVIEW: PASS — the fake reviewer agrees") -> dict[str, str]:
    """Stand in for the two vendor command-line tools themselves — NOT for any Support check.

    The fake `claude` edits the checkout the way a repair agent would (per claude_does, a key of
    FAKE_CLAUDE_EDITS) and records the arguments it was called with; the fake `codex` writes
    codex_says into the --output-last-message file exactly like the real `codex exec`. Both are
    found through the harness's own HARNESS_CLAUDE_BIN / HARNESS_CODEX_BIN overrides, and the
    Claude sign-in comes from a dummy token in the environment (Keychain access switched off, so
    nothing of the owner's is read).

    Args:
        root: an existing scratch directory for the two scripts.
        claude_does: what the fake repair agent does to the checkout.
        codex_says: the fake reviewer's whole answer.

    Return: the environment overrides to run Support under (use with mock.patch.dict(os.environ)).
    """
    bin_dir = root / "fake-bin"
    bin_dir.mkdir(exist_ok=True)
    claude = bin_dir / "claude"
    claude.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "pathlib.Path(sys.argv[0] + '.argv').write_text(repr(sys.argv[1:]))\n"
        + FAKE_CLAUDE_EDITS[claude_does] + "\n"
        "print('done')\n"
    )
    codex = bin_dir / "codex"
    codex.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "pathlib.Path(sys.argv[0] + '.argv').write_text(repr(args))\n"
        "out = args[args.index('--output-last-message') + 1]\n"
        f"pathlib.Path(out).write_text({codex_says!r})\n"
        f"print({codex_says!r})\n"
    )
    for script in (claude, codex):
        script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return {
        "HARNESS_CLAUDE_BIN": str(claude), "HARNESS_CODEX_BIN": str(codex),
        claude_auth.TOKEN_ENV: "fake-oauth-token-for-this-test-only", "HARNESS_CLAUDE_TOKEN_SOURCE": "",
        "HARNESS_CLAUDE_KEYCHAIN_SERVICE": "",
    }


def _case(fingerprint: str = "case-1") -> dict:
    return {"fingerprint": fingerprint, "description": "A ValueError happened", "version": "v1.0.0", "trace_excerpt": ""}


class RepairIsARealAgentRunAndAnOtherVendorReview(unittest.TestCase):
    """R5 (review r3 B1): the repair is Support's own Delivery agent changing the app, proven by the
    owner's reproduce command failing before and passing after, then judged by a reviewer from the
    other vendor. A plan command is never a repair and never a review."""

    def test_fake_passing_review_becomes_fixed(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            repo = _make_fake_app_repo(root, reproduces=True)
            checkout = support_github.checkout_version(str(repo), "v1.0.0", root / "scratch")
            env = _fake_vendors(root)
            with mock.patch.dict(os.environ, env):
                result = support_fix.attempt_fix(checkout, _case(), "v1.0.0", budget_usd=10)
            self.assertTrue(result.fixed, result.notes)
            self.assertIn("failing before", result.notes)
            self.assertIn("exit 0", result.notes.split("after the repair:")[1])
            self.assertIn("Codex (OpenAI): PASS", result.notes)
            self.assertTrue(support_github.branch_has_changes(checkout, "v1.0.0"))
            claude_argv = (root / "fake-bin" / "claude.argv").read_text()
            self.assertIn("--max-budget-usd", claude_argv)
            self.assertIn("'10.00'", claude_argv, "the owner's spending ceiling is the repair agent's budget")
            codex_argv = (root / "fake-bin" / "codex.argv").read_text()
            self.assertIn("'read-only'", codex_argv, "the reviewer reads, it never edits")
            self.assertIn("REVIEW: PASS", codex_argv.replace("\\n", " ") or "", "the brief asks for a verdict line")

    def test_fake_failing_review_stays_not_fixed_and_nothing_is_pushed(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            repo = _make_fake_app_repo(root, reproduces=True)
            store = sm.CaseStore(root / "home")
            store.save_settings(app_repo=str(repo), spending_ceiling="10", github_token="fake-token")
            store.pair("sample-app", "production", frozenset())
            store.record_case_event(
                fingerprint="fp-fail", app="sample-app", env="production", version="v1.0.0",
                trace_excerpt="", description="A ValueError happened", level="Level 3", report_id="case-fail",
            )
            env = _fake_vendors(root, codex_says="I looked.\nREVIEW: FAIL — the regression test is missing")
            with mock.patch.dict(os.environ, env), \
                 mock.patch("harness.support_github.push_branch") as push, \
                 mock.patch("harness.support_github.open_pull_request") as open_pr:
                sm.process_case(store, "fp-fail")
            case = store.get_case("fp-fail")
            self.assertEqual(case["fix_state"], "not_fixed")
            self.assertIn("FAIL — the regression test is missing", case["fix_notes"])
            push.assert_not_called()
            open_pr.assert_not_called()
            store.close()

    def test_a_repair_agent_that_changes_nothing_is_not_fixed_whatever_the_reviewer_says(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            repo = _make_fake_app_repo(root, reproduces=True)
            checkout = support_github.checkout_version(str(repo), "v1.0.0", root / "scratch")
            with mock.patch.dict(os.environ, _fake_vendors(root, claude_does="nothing")):
                result = support_fix.attempt_fix(checkout, _case(), "v1.0.0", budget_usd=10)
            self.assertFalse(result.fixed)
            self.assertIn("changed nothing", result.notes)

    def test_a_repair_that_rewrites_the_owners_plan_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            repo = _make_fake_app_repo(root, reproduces=True)
            checkout = support_github.checkout_version(str(repo), "v1.0.0", root / "scratch")
            with mock.patch.dict(os.environ, _fake_vendors(root, claude_does="rewrites_plan")):
                result = support_fix.attempt_fix(checkout, _case(), "v1.0.0", budget_usd=10)
            self.assertFalse(result.fixed)
            self.assertIn("SUPPORT_PLAN.md", result.notes)
            self.assertIn("refused", result.notes)

    def test_a_change_that_leaves_the_problem_happening_is_not_fixed_and_never_reviewed(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            repo = _make_fake_app_repo(root, reproduces=True)
            checkout = support_github.checkout_version(str(repo), "v1.0.0", root / "scratch")
            with mock.patch.dict(os.environ, _fake_vendors(root, claude_does="half")):
                result = support_fix.attempt_fix(checkout, _case(), "v1.0.0", budget_usd=10)
            self.assertFalse(result.fixed)
            self.assertIn("still happens after the repair", result.notes)
            self.assertFalse((root / "fake-bin" / "codex.argv").exists(), "the reviewer is only asked about a fix that passes the owner's check")

    def test_plan_commands_never_count_as_a_repair_or_a_review(self):
        """Review r3 S-006, watched failing on the unrepaired code: a plan naming "## Apply fix"
        and "## Fix check" commands made a commit and was called Fixed with no reviewer at all."""
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            _run("git", "init", "--quiet", str(root), cwd=root)
            (root / "bug.txt").write_text("broken")
            (root / "SUPPORT_PLAN.md").write_text(
                "# Plan\n\n## Reproduce\n```\npython3 -c \"import pathlib,sys; sys.exit(0 if pathlib.Path('bug.txt').read_text()=='fixed' else 1)\"\n```\n"
                "\n## Apply fix\n```\npython3 -c \"import pathlib; pathlib.Path('bug.txt').write_text('fixed')\"\n```\n"
                "\n## Fix check\n```\npython3 -c \"import sys; sys.exit(0)\"\n```\n",
            )
            _run("git", "add", "-A", cwd=root)
            _run("git", "-c", "user.email=qa@example.test", "-c", "user.name=QA", "commit", "--quiet", "-m", "baseline", cwd=root)
            base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root).decode().strip()
            no_agents = {"HARNESS_CLAUDE_BIN": str(root / "missing-claude"), "HARNESS_CODEX_BIN": str(root / "missing-codex")}
            with mock.patch.dict(os.environ, no_agents):
                result = support_fix.attempt_fix(root, _case("case-one"), base, budget_usd=10)
            after = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root).decode().strip()
            self.assertFalse(result.fixed)
            self.assertEqual(base, after, "no plan command may make a commit")
            self.assertEqual((root / "bug.txt").read_text(), "broken")
            self.assertIn("no repair was attempted", result.notes)
            self.assertIn("not installed", result.notes)

    def test_without_a_claude_sign_in_the_card_says_so_instead_of_pretending(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            repo = _make_fake_app_repo(root, reproduces=True)
            checkout = support_github.checkout_version(str(repo), "v1.0.0", root / "scratch")
            env = _fake_vendors(root)
            with mock.patch.dict(os.environ, env), \
                 mock.patch.object(support_repair.claude_auth, "resolve_token", side_effect=claude_auth.ClaudeAuthRequired()):
                result = support_fix.attempt_fix(checkout, _case(), "v1.0.0", budget_usd=10)
            self.assertFalse(result.fixed)
            self.assertIn("no repair was attempted", result.notes)
            self.assertIn("has no sign-in on this Support machine", result.notes)
            self.assertFalse(support_github.branch_has_changes(checkout, "v1.0.0"))

    def test_the_repair_pair_is_two_vendors(self):
        repair_vendor, review_vendor = support_repair.vendor_pair()
        self.assertNotEqual(repair_vendor, review_vendor)
        self.assertEqual((repair_vendor, review_vendor), ("Anthropic", "OpenAI"))

    def test_the_agents_never_see_this_process_own_secrets(self):
        with mock.patch.dict(os.environ, {"PLANTED_SUPPORT_SECRET": "leak-me-not"}):
            with tempfile.TemporaryDirectory() as root:
                root = Path(root)
                repo = _make_fake_app_repo(root, reproduces=True)
                checkout = support_github.checkout_version(str(repo), "v1.0.0", root / "scratch")
                env = _fake_vendors(root)
                bin_dir = root / "fake-bin"
                (bin_dir / "claude").write_text(
                    "#!/usr/bin/env python3\nimport os, pathlib\n"
                    "pathlib.Path('fixed.txt').write_text('x')\n"
                    "pathlib.Path('seen.txt').write_text(os.environ.get('PLANTED_SUPPORT_SECRET', '') + '|' + os.environ.get('GIT_CONFIG_VALUE_0', ''))\n"
                )
                with mock.patch.dict(os.environ, env):
                    support_fix.attempt_fix(checkout, _case(), "v1.0.0", budget_usd=10)
                self.assertEqual((checkout / "seen.txt").read_text(), "|", "the repair agent saw a secret from this process's environment")

class EndToEndCaseThroughPullRequestAndDeploy(unittest.TestCase):
    """R6/R7: with settings saved (a real app repository, a positive ceiling, and a GitHub
    credential) and a passing fake review injected, a replicated case really does reach Ready to
    deploy with a pull request, and pressing Deploy really does merge it — proving the mechanism
    the given c6/c7 contract-check fixtures ask for from a zero-config fresh install cannot show
    (see the filed check-deviation: that fixture never saves a repository, credential or ceiling,
    so a correct, honest implementation cannot reach Ready to deploy there without faking it)."""

    def test_replicated_and_reviewed_case_reaches_ready_to_deploy_then_deployed(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            repo = _make_fake_app_repo(root, reproduces=True)
            store = sm.CaseStore(root / "home")
            store.save_settings(app_repo=str(repo), spending_ceiling="10", github_token="fake-token")
            store.pair("sample-app", "production", frozenset())
            fingerprint = "fp-e2e"
            store.record_case_event(
                fingerprint=fingerprint, app="sample-app", env="production", version="v1.0.0",
                trace_excerpt="", description="A ValueError happened", level="Level 3", report_id="case-e2e",
            )
            with mock.patch.dict(os.environ, _fake_vendors(root)), \
                 mock.patch("harness.support_github.push_branch"), \
                 mock.patch("harness.support_github.open_pull_request", return_value=f"https://github.com/owner/app/pull/7"), \
                 mock.patch("harness.support_github.merge_pull_request", return_value={"merged": True, "sha": "abc"}):
                sm.process_case(store, fingerprint)
                case = store.get_case(fingerprint)
                self.assertEqual(case["replication"], "replicated")
                self.assertEqual(case["fix_state"], "fixed")
                self.assertEqual(case["status"], "ready_to_deploy")
                self.assertEqual(case["pull_request_url"], "https://github.com/owner/app/pull/7")
                result = sm.deploy_case(store, fingerprint)
                self.assertTrue(result["deployed"])
                self.assertTrue(store.get_case(fingerprint)["deployed"])
            store.close()


class SpendingCeilingGate(unittest.TestCase):
    """R9: without a positive spending ceiling, replication and repair never start."""

    def test_zero_or_unset_ceiling_blocks_replication(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            repo = _make_fake_app_repo(root, reproduces=True)
            store = sm.CaseStore(root / "home")
            store.save_settings(app_repo=str(repo))  # no spending_ceiling saved
            store.pair("sample-app", "production", frozenset())
            fingerprint = "fp-blocked"
            store.record_case_event(
                fingerprint=fingerprint, app="sample-app", env="production", version="v1.0.0",
                trace_excerpt="", description="A ValueError happened", level="Level 3", report_id="case-blocked",
            )
            with mock.patch.object(sm, "_checkout") as checkout:
                sm.process_case(store, fingerprint)
                checkout.assert_not_called()
            self.assertEqual(store.get_case(fingerprint)["status"], "receive_and_show_only")
            store.close()

    def test_positive_ceiling_allows_replication(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            repo = _make_fake_app_repo(root, reproduces=True)
            store = sm.CaseStore(root / "home")
            store.save_settings(app_repo=str(repo), spending_ceiling="5")
            store.pair("sample-app", "production", frozenset())
            fingerprint = "fp-allowed"
            store.record_case_event(
                fingerprint=fingerprint, app="sample-app", env="production", version="v1.0.0",
                trace_excerpt="", description="A ValueError happened", level="Level 3", report_id="case-allowed",
            )
            sm.process_case(store, fingerprint)
            self.assertEqual(store.get_case(fingerprint)["replication"], "replicated")
            store.close()


class MilestoneEmailNeverRetried(unittest.TestCase):
    """R8: exactly one email per milestone; a repeat is never sent twice."""

    def test_each_milestone_is_recorded_once_and_a_repeat_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            store = sm.CaseStore(Path(root))
            store.pair("app", "env", frozenset())
            store.record_case_event(
                fingerprint="fp-email", app="app", env="env", version="v1", trace_excerpt="",
                description="d", level="Level 3", report_id="r1",
            )
            self.assertTrue(store.record_email_once("fp-email", "received"))
            self.assertFalse(store.record_email_once("fp-email", "received"))
            self.assertTrue(store.record_email_once("fp-email", "replicated"))
            store.close()


class SettingsSaveAndTestEmail(unittest.TestCase):
    """R2: saving Support settings stores them, and Send a test email reaches the saved mail
    server using only what was saved (the "Save first" rule every other NMHP mail setting follows)."""

    def setUp(self):
        require_loopback()

    def test_save_then_test_email_round_trip(self):
        with tempfile.TemporaryDirectory() as root:
            store = sm.CaseStore(Path(root))
            admin = ThreadingHTTPServer(("127.0.0.1", 0), sm.make_admin_handler(store))
            threading.Thread(target=admin.serve_forever, daemon=True).start()
            try:
                base = f"http://127.0.0.1:{admin.server_address[1]}"
                body = urlencode({
                    "app_repo": "https://github.com/owner/app", "notice_email": "owner@example.com",
                    "mail_host": "smtp.example.com", "mail_port": "587", "mail_user": "owner@example.com",
                    "spending_ceiling": "5", "github_token": "tok-123", "mail_password": "s3cret",
                }).encode()
                request = Request(f"{base}/support/settings", data=body, method="POST",
                                   headers={"Content-Type": "application/x-www-form-urlencoded"})
                owner = owner_opener(base, store)  # review r3 B2: settings are an owner control
                with owner.open(request, timeout=3) as response:
                    self.assertEqual(response.status, 200)
                saved = store.settings()
                self.assertEqual(saved["app_repo"], "https://github.com/owner/app")
                self.assertEqual(saved["notice_email"], "owner@example.com")
                self.assertTrue(saved["has_github_token"])
                self.assertTrue(saved["has_mail_password"])
                self.assertEqual(store.github_token(), "tok-123")
                with mock.patch.object(sm.warning_email.smtplib, "SMTP", RecordingSmtp):
                    with owner.open(Request(f"{base}/support/settings/test-email", data=b"", method="POST",
                                             headers={"Content-Type": "application/x-www-form-urlencoded"}), timeout=3) as response:
                        self.assertIn(b"Test email sent", response.read())
                self.assertEqual(RecordingSmtp.instances[-1].sent["To"], "owner@example.com")
            finally:
                admin.shutdown(); admin.server_close(); store.close()


class ReplicaNeverInheritsThisProcessOwnEnvironment(unittest.TestCase):
    """Repair cycle 1, finding B3: a planted secret in this Support process's own environment
    (its mail password, GitHub token, or anything else) must never reach the replica's command."""

    def test_a_planted_secret_in_this_process_env_does_not_reach_the_reproduce_command(self):
        with mock.patch.dict("os.environ", {"PLANTED_SUPPORT_SECRET": "leak-me-not"}):
            with tempfile.TemporaryDirectory() as root:
                checkout = Path(root)
                (checkout / "SUPPORT_PLAN.md").write_text(
                    "# Support plan\n\n## Reproduce\n```\n"
                    "python3 -c \"import os,sys; sys.exit(1 if 'PLANTED_SUPPORT_SECRET' in os.environ else 0)\"\n"
                    "```\n",
                )
                result = support_replicate.replicate(checkout)
                self.assertFalse(result.replicated, "the planted secret reached the replica: " + result.attempts[0])

class PushBranchNeverPutsTheCredentialOnTheCommandLine(unittest.TestCase):
    """Repair cycle 1, finding B3: a `-c http.extraHeader=...` argument is readable by any other
    local process through ps/proc; the token must travel only through the environment."""

    def test_the_token_never_appears_in_the_subprocess_argv(self):
        captured = {}
        real_run = subprocess.run

        def spy(args, **kwargs):
            if args and args[0] == "git" and "push" in args:
                captured["args"] = args
                captured["env"] = kwargs.get("env") or {}
                raise subprocess.CalledProcessError(1, args, stderr=b"no real remote in this test")
            return real_run(args, **kwargs)

        with mock.patch("harness.support_github.subprocess.run", side_effect=spy):
            with self.assertRaises(support_github.GitCheckoutFailed):
                support_github.push_branch(Path("."), "support/case-x", "SECRET-TOKEN-VALUE")
        self.assertNotIn("SECRET-TOKEN-VALUE", " ".join(captured["args"]), "the credential appeared in argv")
        self.assertIn("SECRET-TOKEN-VALUE", captured["env"].get("GIT_CONFIG_VALUE_0", ""), "the credential must still reach git, via its env")


class CheckoutAuthenticatesAPrivateRepository(unittest.TestCase):
    """Repair cycle 1, finding B5: checkout_version cloned with no credential at all, so a
    private HTTPS app repository could never be cloned even with a correct saved token — the
    case was then wrongly called "Version unknown" instead of what really happened."""

    def test_clone_carries_the_saved_token_when_one_is_given(self):
        captured = {}
        real_run = subprocess.run

        def spy(args, **kwargs):
            if args and args[0] == "git" and "clone" in args:
                captured["env"] = kwargs.get("env") or {}
                raise subprocess.CalledProcessError(1, args, stderr=b"no real remote in this test")
            return real_run(args, **kwargs)

        with mock.patch("harness.support_github.subprocess.run", side_effect=spy):
            with self.assertRaises(support_github.GitCheckoutFailed):
                support_github.checkout_version("https://example.invalid/app.git", "v1", Path("/tmp"), token="PRIVATE-REPO-TOKEN")
        self.assertIn("PRIVATE-REPO-TOKEN", captured["env"].get("GIT_CONFIG_VALUE_0", ""), "the clone never received the saved credential")

    def test_clone_carries_no_credential_when_none_is_saved(self):
        captured = {}
        real_run = subprocess.run

        def spy(args, **kwargs):
            if args and args[0] == "git" and "clone" in args:
                captured["env"] = kwargs.get("env")
                raise subprocess.CalledProcessError(1, args, stderr=b"no real remote in this test")
            return real_run(args, **kwargs)

        with mock.patch("harness.support_github.subprocess.run", side_effect=spy):
            with self.assertRaises(support_github.GitCheckoutFailed):
                support_github.checkout_version("https://example.invalid/app.git", "v1", Path("/tmp"))
        self.assertIsNone(captured["env"], "an unauthenticated clone (no saved token) must not pass a credential env")

    def test_process_case_passes_the_saved_credential_to_checkout(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            repo = _make_fake_app_repo(root, reproduces=True)
            store = sm.CaseStore(root / "home")
            store.save_settings(app_repo=str(repo), spending_ceiling="10", github_token="PRIVATE-REPO-TOKEN")
            store.pair("sample-app", "production", frozenset())
            store.record_case_event(
                fingerprint="fp-private", app="sample-app", env="production", version="v1.0.0",
                trace_excerpt="", description="d", level="Level 3", report_id="case-private",
            )
            with mock.patch("harness.support_mode._checkout", wraps=sm._checkout) as checkout:
                sm.process_case(store, "fp-private")
                checkout.assert_called_once_with(str(repo), "v1.0.0", mock.ANY, "PRIVATE-REPO-TOKEN")
            store.close()


class DeployIsOwnerOnlyThroughTheOwnerKey(unittest.TestCase):
    """R7 (review r3 B2): a page token and a browser header are both things a local agent's own
    HTTP client can supply, so neither made Deploy owner-only. Now every owner control needs an
    owner session, opened only with the owner key shown once where Support was started."""

    def setUp(self):
        require_loopback()

    def _ready_to_deploy_store(self, root: Path):
        store = sm.CaseStore(root)
        store.pair("sample-app", "production", frozenset())
        store.record_case_event(
            fingerprint="fp-deploy", app="sample-app", env="production", version="v1",
            trace_excerpt="", description="d", level="Level 3", report_id="case-deploy",
        )
        store.set_pull_request("fp-deploy", "https://github.com/owner/app/pull/9")
        return store

    def _serve(self, store):
        admin = ThreadingHTTPServer(("127.0.0.1", 0), sm.make_admin_handler(store))
        threading.Thread(target=admin.serve_forever, daemon=True).start()
        self.addCleanup(admin.server_close)
        self.addCleanup(admin.shutdown)
        return f"http://127.0.0.1:{admin.server_address[1]}"

    def test_an_agent_like_request_that_copies_the_token_and_forges_the_browser_header_is_refused(self):
        """The Reviewer's own round-3 shape: GET the page as a local agent, copy the token, POST it back
        with Sec-Fetch-Site: same-origin. Watched passing (merge called) on the unrepaired code."""
        with tempfile.TemporaryDirectory() as root:
            store = self._ready_to_deploy_store(Path(root))
            base = self._serve(store)
            owner = owner_opener(base, store)
            with urlopen(Request(f"{base}/support", headers={"User-Agent": "managed-agent-test", "X-Harness-Agent-Id": "agent-fixture"}), timeout=3) as response:
                agent_page = response.read().decode()
            self.assertNotIn('name="action_token"', agent_page, "an unsigned viewer is not even shown the Deploy form")
            self.assertIn("Only the owner can deploy", agent_page)
            with owner.open(f"{base}/support", timeout=3) as response:  # the token, as if leaked from the owner's page
                token = response.read().decode().split('name="action_token" value="', 1)[1].split('"', 1)[0]
            body = urlencode({"fingerprint": "fp-deploy", "action_token": token}).encode()
            request = Request(f"{base}/support/deploy", data=body, method="POST", headers={
                "Content-Type": "application/x-www-form-urlencoded", "User-Agent": "managed-agent-test",
                "X-Harness-Agent-Id": "agent-fixture", "Sec-Fetch-Site": "same-origin",
            })
            with mock.patch("harness.support_github.merge_pull_request", return_value={"merged": True, "sha": "fixture"}) as merge:
                with self.assertRaises(HTTPError) as caught:
                    urlopen(request, timeout=3)
            self.assertEqual(caught.exception.code, 403)
            merge.assert_not_called()
            self.assertFalse(store.get_case("fp-deploy")["deployed"])
            store.close()

    def test_the_owner_signs_in_with_the_key_and_deploys(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._ready_to_deploy_store(Path(root))
            base = self._serve(store)
            owner = owner_opener(base, store)
            with owner.open(f"{base}/support", timeout=3) as response:
                page = response.read().decode()
            self.assertIn("Signed in as owner", page)
            token = page.split('name="action_token" value="', 1)[1].split('"', 1)[0]
            body = urlencode({"fingerprint": "fp-deploy", "action_token": token}).encode()
            request = Request(f"{base}/support/deploy", data=body, method="POST", headers={"Content-Type": "application/x-www-form-urlencoded"})
            with mock.patch("harness.support_mode.deploy_case", return_value={"deployed": True}) as deploy:
                with owner.open(request, timeout=3) as response:
                    self.assertIn(b"Deployed", response.read())
                deploy.assert_called_once_with(store, "fp-deploy")
            store.close()

    def test_a_signed_in_owner_still_needs_the_pages_own_token_against_a_cross_site_form(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._ready_to_deploy_store(Path(root))
            base = self._serve(store)
            owner = owner_opener(base, store)
            body = urlencode({"fingerprint": "fp-deploy", "action_token": "forged"}).encode()
            request = Request(f"{base}/support/deploy", data=body, method="POST", headers={"Content-Type": "application/x-www-form-urlencoded"})
            with self.assertRaises(HTTPError) as caught:
                owner.open(request, timeout=3)
            self.assertEqual(caught.exception.code, 403)
            store.close()

    def test_wrong_keys_are_refused_and_guessing_is_paused_after_five(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._ready_to_deploy_store(Path(root))
            store.set_owner_key(TEST_OWNER_KEY)
            base = self._serve(store)
            opener = build_opener(HTTPCookieProcessor())
            codes = []
            for attempt in range(6):
                request = Request(f"{base}/support/sign-in", data=f"owner_key=wrong-{attempt}".encode(), method="POST",
                                  headers={"Content-Type": "application/x-www-form-urlencoded"})
                try:
                    opener.open(request, timeout=3)
                    codes.append(200)
                except HTTPError as error:
                    codes.append(error.code)
            self.assertEqual(codes, [403, 403, 403, 403, 403, 429])
            body = urlencode({"app_repo": "x"}).encode()
            with self.assertRaises(HTTPError) as caught:  # still no session: settings stay owner-only too
                opener.open(Request(f"{base}/support/settings", data=body, method="POST", headers={"Content-Type": "application/x-www-form-urlencoded"}), timeout=3)
            self.assertEqual(caught.exception.code, 403)
            store.close()

    def test_choosing_the_owner_key_on_first_visit_stores_only_its_hash(self):
        with tempfile.TemporaryDirectory() as root:
            store = sm.CaseStore(Path(root))
            self.assertFalse(store.owner_key_exists())
            with self.assertRaises(ValueError):  # review r5 B7: a short key is guessable, refused
                store.set_owner_key("short9chr")
            self.assertFalse(store.owner_key_exists(), "a rejected short key must not be stored")
            key = "owner-key-chosen-on-first-visit"
            store.set_owner_key(key)
            self.assertTrue(store.owner_key_exists())
            with self.assertRaises(ValueError):  # the first visitor's choice is final
                store.set_owner_key("a-different-key")
            self.assertTrue(store.verify_owner_key(key))
            self.assertFalse(store.verify_owner_key(key + "x"))
            self.assertFalse(store.verify_owner_key(""))
            kept = (Path(root) / sm.OWNER_KEY_HASH_FILENAME).read_text()
            self.assertNotIn(key, kept)
            store.close()


if __name__ == "__main__":
    unittest.main()
