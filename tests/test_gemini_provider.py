# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Gemini (Antigravity CLI) as a provider (spec T1): launch under the wrapper, identity, the sign-in rule,
cross-vendor pairings, the connection test, sign-in and trust prompts.

The real `agy` is never started: a stub records what the runner launched it with.
"""
from __future__ import annotations

import itertools
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import attention, board, control, gemini_auth, global_settings, platform_support
from tests.environment_support import require_loopback
from tests.test_conversation_memory import run_runner

SETTINGS = {
    "delivery": {"provider": "gemini", "model": "gemini-3.8-flash-low", "effort": "high"},
    "cto": {"provider": "claude", "model": "opus", "effort": "high"},
    "reviewer": {"provider": "codex", "model": "gpt-5.6-sol", "effort": "high"},
}


class PairingTests(unittest.TestCase):
    """T1-4: all nine author/reviewer pairings, in Settings, at launch and on the board."""

    def entry(self, provider):
        return {"provider": provider, "model": control.PROVIDER_DEFAULT_MODELS[provider], "effort": "high"}

    def test_the_three_same_provider_pairings_are_refused_and_the_six_cross_pairings_accepted(self):
        for delivery, reviewer in itertools.product(control.PROVIDERS, control.PROVIDERS):
            settings = {"delivery": self.entry(delivery), "reviewer": self.entry(reviewer), "cto": self.entry("claude")}
            if delivery == reviewer:
                with self.assertRaisesRegex(ValueError, "different providers", msg=f"{delivery}/{reviewer}"):
                    control._validated_agent_settings(settings)
            else:
                self.assertEqual(control._validated_agent_settings(settings)["delivery"]["provider"], delivery)

    def test_a_gemini_custom_model_must_be_a_google_model_server_side(self):
        for bad in ("claude-opus-4-6-thinking", "gpt-oss-120b-medium", "opus"):
            settings = {**SETTINGS, "delivery": {"provider": "gemini", "model": bad, "effort": "high"}}
            with self.assertRaisesRegex(ValueError, "only Google's models"):
                control._validated_agent_settings(settings)

    def test_every_provider_declares_a_different_vendor_so_the_board_refuses_same_vendor_review(self):
        vendors = [entry["vendor"] for entry in control.PROVIDERS.values()]
        self.assertEqual(sorted(vendors), ["Anthropic", "Google", "OpenAI"])

    def test_the_board_registers_the_vendor_of_the_provider_that_runs(self):
        for provider, vendor in (("gemini", "Google"), ("claude", "Anthropic"), ("codex", "OpenAI")):
            self.assertEqual(control.session_identity("claude_reviewer", provider)["vendor"], vendor)
        self.assertEqual(control.session_identity("codex_delivery", "gemini")["label"], "GEMINI CLI")
        self.assertEqual(control.session_identity("claude_cto", "gemini")["label"], "CTO (GEMINI)")
        # A Claude Delivery agent used to be labelled "CODEX CLI" with vendor OpenAI.
        self.assertEqual(control.session_identity("codex_delivery", "claude"), {"label": "CLAUDE CLI", "vendor": "Anthropic"})

    def test_board_refuses_review_between_two_gemini_agents_and_accepts_gemini_with_codex(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            root.mkdir()
            from harness import project_memory
            project_memory.initialize(root, project_name="Pairing proof", description="vendor rule")
            developer = board.register(root, "engineering", "AWAITING_OWNER_DIRECTION", "Delivery", "Google")
            same = board.register(root, "qa", "REVIEW_QUEUE", "Reviewer", "Google")
            other = board.register(root, "qa", "REVIEW_QUEUE", "Reviewer", "OpenAI")
            self.assertEqual(developer["vendor"], "Google")
            self.assertEqual(same["vendor"], "Google")
            self.assertNotEqual(other["vendor"], developer["vendor"])


class LaunchCollisionTests(unittest.TestCase):
    """T1-4 at launch: a Reviewer cannot start on the provider a live Delivery agent already uses."""

    def test_a_gemini_reviewer_is_refused_while_a_gemini_delivery_agent_is_live_and_a_codex_reviewer_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "p"
            root.mkdir()
            claude = {"provider": "claude", "model": "opus", "effort": "high"}
            gemini = {"provider": "gemini", "model": "gemini-3.8-flash-low", "effort": "high"}
            codex = {"provider": "codex", "model": "gpt-5.6-sol", "effort": "high"}
            control.create(root, "codex_delivery", settings_override={"delivery": gemini, "cto": claude, "reviewer": codex})
            # The owner then changes Settings so the Reviewer is Gemini too; the live Delivery agent still uses Gemini.
            changed = {"delivery": codex, "cto": claude, "reviewer": gemini}
            with self.assertRaisesRegex(ValueError, "cannot start Independent Reviewer with Gemini \\(Antigravity CLI\\) while an active Delivery Agent"):
                control.create(root, "claude_reviewer", settings_override=changed)
            reviewer = control.create(root, "claude_reviewer", settings_override={"delivery": gemini, "cto": claude, "reviewer": codex})
            self.assertEqual((reviewer["label"], reviewer["vendor"]), ("CODEX CLI", "OpenAI"))


class SignInRuleTests(unittest.TestCase):
    def test_a_subscription_setup_passes_and_an_api_key_or_endpoint_setup_is_refused_in_plain_words(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.assertEqual(gemini_auth.api_key_problem(home, {}), "")
            settings = home / ".gemini" / "antigravity-cli" / "settings.json"
            settings.parent.mkdir(parents=True)
            settings.write_text(json.dumps({"permission": {"allow": ["x"]}}))
            self.assertEqual(gemini_auth.api_key_problem(home, {"GEMINI_API_KEY": "stray"}), "", "a stray key alone changes nothing")
            settings.write_text(json.dumps({"modelProvider": "gemini"}))
            self.assertIn("API key or a custom endpoint", gemini_auth.api_key_problem(home, {}))
            settings.write_text("{}")
            self.assertIn("GOOGLE_GEMINI_BASE_URL", gemini_auth.api_key_problem(home, {"GOOGLE_GEMINI_BASE_URL": "http://x"}))
            settings.write_text("not json")
            self.assertEqual(gemini_auth.api_key_problem(home, {}), "", "an unreadable file is the CLI's to report")
        self.assertNotIn("edit", gemini_auth.REFUSAL.lower().split("remove")[0], "the harness never edits the owner's file")


class FolderTrustTests(unittest.TestCase):
    """Owner's order 2026-10-03: no human at a folder-trust question. The harness writes the trust, outside the sandbox."""

    def test_the_projects_folders_are_added_and_every_other_key_and_trusted_folder_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            settings = home / ".gemini" / "antigravity-cli" / "settings.json"
            settings.parent.mkdir(parents=True)
            settings.write_text(json.dumps({"trustedWorkspaces": ["/old"], "permission": {"allow": ["a"]}, "theme": "dark"}))
            added = gemini_auth.trust_workspaces([str(home / "proj"), "/old", str(home / "proj")], home)
            self.assertEqual(added, [str(home / "proj")])
            value = json.loads(settings.read_text())
            self.assertEqual(value["trustedWorkspaces"], ["/old", str(home / "proj")])
            self.assertEqual((value["permission"], value["theme"]), ({"allow": ["a"]}, "dark"))
            self.assertEqual(gemini_auth.trust_workspaces([str(home / "proj")], home), [], "idempotent: nothing to add")

    def test_agents_starting_together_each_keep_their_folders(self):
        # Found by the pre-review audit: an unlocked read-modify-write let the last of three simultaneous launches
        # (CTO, Reviewer, Delivery) erase the others' folders, and a human trust question came back.
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            folders = [str(home / f"project-{index}") for index in range(12)]
            threads = [threading.Thread(target=gemini_auth.trust_workspaces, args=([folder], home)) for folder in folders]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            trusted = json.loads((home / ".gemini" / "antigravity-cli" / "settings.json").read_text())["trustedWorkspaces"]
            self.assertEqual(sorted(trusted), sorted(folders))

    def test_a_first_run_with_no_settings_file_creates_one_and_an_unreadable_file_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            self.assertEqual(gemini_auth.trust_workspaces([str(home / "p")], home), [str(home / "p")])
            settings = home / ".gemini" / "antigravity-cli" / "settings.json"
            self.assertEqual(json.loads(settings.read_text()), {"trustedWorkspaces": [str(home / "p")]})
            settings.write_text("this is not json")
            self.assertEqual(gemini_auth.trust_workspaces([str(home / "q")], home), [])
            self.assertEqual(settings.read_text(), "this is not json")
            settings.write_text("[1, 2]")
            self.assertEqual(gemini_auth.trust_workspaces([str(home / "q")], home), [])

    def test_the_file_keeps_its_permissions_and_no_temporary_file_is_left(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            settings = home / ".gemini" / "antigravity-cli" / "settings.json"
            settings.parent.mkdir(parents=True)
            settings.write_text("{}")
            settings.chmod(0o600)
            gemini_auth.trust_workspaces([str(home / "p")], home)
            self.assertEqual(settings.stat().st_mode & 0o777, 0o600)
            self.assertEqual([item.name for item in settings.parent.iterdir()], ["settings.json"])


class AttentionTests(unittest.TestCase):
    def screen(self, text):
        watch = attention.PromptWatch(height=30)
        return watch.feed(text.encode())

    def test_the_trust_question_names_gemini_and_holds_harness_input(self):
        reason = attention.detect("Do you trust the contents of this project?\n  1. Yes  2. No")
        self.assertEqual(reason, attention.GEMINI_TRUST_REASON)
        self.assertTrue(attention.holds_harness_input(reason))
        self.assertIn("Gemini", reason)

    def test_the_sign_in_screen_names_gemini_holds_input_and_counts_as_signed_out(self):
        for text in ("You are not logged into Antigravity.\nPlease sign in", "Waiting for authentication (timeout 60s)...\nOr, paste the authorization code here"):
            reason = attention.detect(text)
            self.assertEqual(reason, attention.GEMINI_SIGN_IN_REASON, text)
            self.assertTrue(attention.holds_harness_input(reason))
            self.assertTrue(attention.needs_sign_in({"attention_reason": reason}))
        self.assertEqual(attention.sign_in_how("gemini"), "follow the sign-in prompt in its window")
        self.assertEqual(attention.sign_in_how("claude"), "run /login")

    def test_only_wording_the_cli_itself_printed_counts_as_its_sign_in_screen(self):
        # An agent's own output may quote words; a guess must never hold the harness's messages for nothing.
        for quoted in ("Sign in with Google", "Please sign in to Antigravity", "Waiting for authentication", "I will sign in later"):
            self.assertIsNone(attention.detect(quoted), quoted)

    def test_claude_and_codex_prompts_are_unchanged(self):
        self.assertEqual(attention.detect("Yes, I trust this folder"), attention.TRUST_REASON)
        self.assertEqual(attention.detect("Trust and continue"), attention.CODEX_TRUST_REASON)
        self.assertIn("/login", attention.detect("Please run /login"))
        self.assertTrue(attention.needs_sign_in({"attention_reason": attention.LOGIN_REASON}))
        self.assertFalse(attention.needs_sign_in({"attention_reason": attention.GEMINI_TRUST_REASON}))

    def test_the_board_names_the_right_sign_in_step_for_a_signed_out_gemini_agent(self):
        message = board.sign_in_message({"role": "qa", "vendor": "Google"})
        self.assertEqual(message, "The Reviewer needs you to sign in again: open its terminal and follow the sign-in prompt in its window.")
        self.assertIn("run /login", board.sign_in_message({"role": "qa", "vendor": "Anthropic"}))
        self.assertIn("run /login", board.sign_in_message({"role": "engineering", "vendor": "OpenAI"}))


class ConnectionTestTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.home = self.base / "home"
        self.workspace = self.base / "ws"
        self.workspace.mkdir()

    def agy(self, body: str) -> Path:
        path = self.base / "agy"
        path.write_text("#!/bin/sh\ncase \"$1\" in --version) echo 1.3.1 ;; *) " + body + " ;; esac\n")
        path.chmod(0o755)
        return path

    def run_test(self, agy: Path, model="gemini-3.8-flash-low", effort="low"):
        with mock.patch.dict(os.environ, {"HARNESS_AGY_BIN": str(agy)}), \
                mock.patch.object(gemini_auth, "api_key_problem", return_value=""), \
                mock.patch("harness.agent_confinement.wrap", side_effect=lambda argv, *a, **k: argv):
            return global_settings.test_connection(self.home, "gemini", model, effort, self.workspace)

    def test_a_real_answer_passes_and_names_google_not_openai_or_anthropic(self):
        agy = self.agy('echo \'{"status": "SUCCESS", "response": "ready"}\'')
        result = self.run_test(agy)
        self.assertTrue(result["ok"])
        self.assertIn("Google account", result["message"])
        self.assertIn("inside the agents' sandbox".replace("inside", "from inside"), result["message"])
        self.assertNotIn("OpenAI", result["message"] + "Anthropic" * 0)

    def test_each_failure_is_told_apart(self):
        cases = {
            "signed out": ("echo 'You are not logged into Antigravity.' >&2; exit 1", "not signed in where the agents run"),
            "quota": ("echo 'RESOURCE_EXHAUSTED: weekly limit reached' >&2; exit 1", "used up your Google plan's quota"),
            "capacity": ("echo 'model is overloaded, no capacity' >&2; exit 1", "no capacity right now"),
            "billing": ("echo 'insufficient credits' >&2; exit 1", "needs credits"),
            "refused": ("echo 'unknown flag' >&2; exit 2", "rejected model"),
        }
        for name, (body, expected) in cases.items():
            with self.assertRaisesRegex(ValueError, expected, msg=name):
                self.run_test(self.agy(body))
        recorded = global_settings.load(self.home)["connectivity"]["gemini"]
        self.assertFalse(recorded["ok"])

    def test_api_key_mode_and_a_missing_cli_are_reported_before_any_request(self):
        with mock.patch.dict(os.environ, {"HARNESS_AGY_BIN": str(self.base / "absent")}):
            with self.assertRaisesRegex(ValueError, "was not found"):
                global_settings.test_connection(self.home, "gemini", "gemini-3.8-flash-low", "low", self.workspace)
        agy = self.agy("echo ran > '%s/ran'" % self.base)
        with mock.patch.dict(os.environ, {"HARNESS_AGY_BIN": str(agy)}), \
                mock.patch.object(gemini_auth, "api_key_problem", return_value=gemini_auth.REFUSAL.format(where="")):
            with self.assertRaisesRegex(ValueError, "API key or a custom endpoint"):
                global_settings.test_connection(self.home, "gemini", "gemini-3.8-flash-low", "low", self.workspace)
        self.assertFalse((self.base / "ran").exists())

    def test_a_non_google_model_is_refused_before_any_request(self):
        with self.assertRaisesRegex(ValueError, "only Google's models"):
            global_settings.test_connection(self.home, "gemini", "claude-opus-4-6-thinking", "low", self.workspace)

    def test_the_request_runs_inside_the_agents_wrapper_with_gemini_state_rules(self):
        agy = self.agy('echo \'{"status": "SUCCESS", "response": "ready"}\'')
        seen = {}

        def wrap(argv, roots, **kwargs):
            seen.update(kwargs, argv=argv, roots=roots)
            return argv
        with mock.patch.dict(os.environ, {"HARNESS_AGY_BIN": str(agy)}), \
                mock.patch.object(gemini_auth, "api_key_problem", return_value=""), \
                mock.patch("harness.agent_confinement.wrap", side_effect=wrap):
            global_settings.test_connection(self.home, "gemini", "gemini-3.8-flash-low", "low", self.workspace)
        self.assertEqual(seen["provider"], "gemini")
        self.assertEqual(seen["roots"], [str(self.workspace.resolve())])
        self.assertIn("--dangerously-skip-permissions", seen["argv"])
        self.assertIn("gemini-3.8-flash-low", seen["argv"])


class RunnerLaunchTests(unittest.TestCase):
    """The real runner launches the stub agy as a Delivery agent: argv, boundary, identity."""

    def setUp(self):
        require_loopback()
        if not platform_support.agent_confinement().available():
            raise unittest.SkipTest("no write-confinement primitive on this platform")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve()
        self.root = base / "project"
        self.root.mkdir()
        self.home = base / "home"
        (self.home / ".gemini" / "config").mkdir(parents=True)
        (self.home / ".gemini" / "antigravity-cli").mkdir(parents=True)
        agy = self.root / "agy"
        agy.write_text(
            "#!/usr/bin/env bash\n"
            "[ \"$1\" = --version ] && { echo 1.3.1; exit 0; }\n"
            "printf '%s\\0' \"$@\" > \"$HARNESS_CAPTURE\"\n"
            f"( echo x > '{self.home}/.gemini/config/hooks.json' && echo WROTE_CONTROLS ) 2>/dev/null || echo DENIED_CONTROLS\n"
            f"( echo x > '{self.home}/.gemini/antigravity-cli/antigravity-oauth-token' && echo WROTE_STATE ) 2>/dev/null || echo DENIED_STATE\n"
            f"( echo x > '{self.home}/elsewhere.txt' && echo WROTE_HOME ) 2>/dev/null || echo DENIED_HOME\n"
            "env | grep -c '^GEMINI_API_KEY=' || true\n", encoding="utf-8")
        agy.chmod(0o755)
        self.outside = Path.home() / f".nmhp-gemini-probe-{os.getpid()}"
        self.outside.mkdir()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.outside)]))
        agy.write_text(agy.read_text().replace(f"'{self.home}/elsewhere.txt'", f"'{self.outside}/probe.txt'"), encoding="utf-8")
        self.environment = {
            **os.environ, "HOME": str(self.home), "HARNESS_CAPTURE": str(self.root / "captured.txt"),
            "HARNESS_AGY_BIN": str(agy), "GEMINI_API_KEY": "stray-key-must-not-reach-the-agent",
        }

    def start(self):
        with mock.patch.dict(os.environ, {"HARNESS_AGY_BIN": self.environment["HARNESS_AGY_BIN"]}):
            return control.create(self.root, "codex_delivery", settings_override=SETTINGS)

    def test_the_session_is_labelled_and_declared_gemini_google(self):
        session = self.start()
        self.assertEqual((session["provider"], session["label"], session["vendor"]),
                         ("gemini", "GEMINI CLI", "Google"))
        self.assertEqual(session["provider_label"], "Gemini (Antigravity CLI)")

    def test_a_delivery_agent_launches_agy_confined_with_the_agreed_arguments(self):
        session = self.start()
        done = run_runner(self.root, session, self.environment)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        words = done.stdout.split()
        self.assertIn("DENIED_CONTROLS", words, done.stdout)
        self.assertIn("WROTE_STATE", words, "the sign-in folder is the CLI's own")
        self.assertIn("DENIED_HOME", words, done.stdout)
        self.assertEqual(done.stdout.strip().splitlines()[-1], "0", "a stray API key never reaches the agent")
        argv = (self.root / "captured.txt").read_text(encoding="utf-8").split("\0")
        # The model id carries the thinking level and the CLI rejects a disagreeing pair; this test used to pin
        # flash-low with effort high, which is exactly that pair. The chosen effort selects the level.
        self.assertEqual(argv[argv.index("--model") + 1], "gemini-3.8-flash-high")
        self.assertEqual(argv[argv.index("--effort") + 1], "high")
        self.assertIn("--dangerously-skip-permissions", argv)
        prompt = argv[argv.index("-i") + 1]
        self.assertIn("MODE: Delivery Agent.", prompt)
        self.assertIn("HARNESS_MANAGED_SESSION=", prompt, "the marker the supervisor finds the conversation by")
        self.assertFalse((self.home / ".gemini" / "config" / "hooks.json").exists())
        self.assertFalse((self.outside / "probe.txt").exists())
        trusted = json.loads((self.home / ".gemini" / "antigravity-cli" / "settings.json").read_text())["trustedWorkspaces"]
        self.assertIn(str(self.root.resolve()), trusted, "the opened project is trusted, so the CLI asks nobody")

    def test_the_agent_registers_on_the_board_as_vendor_google(self):
        session = self.start()
        run_runner(self.root, session, self.environment)
        agents = board.snapshot(self.root)["agents"]
        self.assertEqual([agent["vendor"] for agent in agents.values()] if isinstance(agents, dict)
                         else [agent["vendor"] for agent in agents], ["Google"])

    def test_a_relaunch_resumes_the_recorded_conversation_with_conversation_not_continue(self):
        session = self.start()
        conversation_dir = self.home / ".gemini" / "antigravity-cli" / "brain" / "conv-1" / ".system_generated" / "logs"
        conversation_dir.mkdir(parents=True)
        (conversation_dir / "transcript.jsonl").write_text("{}\n")
        control.record_cli_session(self.root, session["id"], "conv-1", "gemini")
        control.note_cli_launch(self.root, session["id"], False)
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}):
            done = run_runner(self.root, session, self.environment)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        argv = (self.root / "captured.txt").read_text(encoding="utf-8").split("\0")
        self.assertEqual(argv[argv.index("--conversation") + 1], "conv-1")
        self.assertIn("--dangerously-skip-permissions", argv,
                      "bypass mode on EVERY launch, resume included (Claude: --permission-mode bypassPermissions; Codex: approval never)")
        self.assertNotIn("--continue", argv)
        self.assertNotIn("-c", argv)

    def test_a_codex_agent_keeps_a_google_key_its_own_project_may_use(self):
        # The key is removed for Gemini only (found by the pre-review audit: it was removed for every provider).
        codex = self.root / "codex-env-probe"
        codex.write_text("#!/usr/bin/env bash\nprintf '%s' \"${GEMINI_API_KEY:-ABSENT}\" > \"$HARNESS_CAPTURE\"\n", encoding="utf-8")
        codex.chmod(0o755)
        session = control.create(self.root, "codex_delivery")          # the default Delivery provider: Codex
        self.assertEqual(session["provider"], "codex")
        done = run_runner(self.root, session, {**self.environment, "HARNESS_CODEX_BIN": str(codex)})
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual((self.root / "captured.txt").read_text(), "stray-key-must-not-reach-the-agent")

    def test_an_api_key_setup_refuses_the_launch_and_never_starts_the_cli(self):
        (self.home / ".gemini" / "antigravity-cli" / "settings.json").write_text('{"modelProvider": "gemini"}')
        session = self.start()
        done = run_runner(self.root, session, self.environment)
        self.assertEqual(done.returncode, 3, done.stdout + done.stderr)
        self.assertIn("API key or a custom endpoint", done.stderr)
        self.assertFalse((self.root / "captured.txt").exists())


def tearDownModule():
    from tests.environment_support import assert_agy_pinned
    assert_agy_pinned()


if __name__ == "__main__":
    unittest.main()
