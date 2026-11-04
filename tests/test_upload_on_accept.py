# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Upload on accept (task 2026-11-03_github-upload-on-accept), criteria A1-A9, on local bare repositories.

Every test goes through the real board path: a governed task is certified, the owner's Accept is recorded with
board.record_release_decision (which saves the pending upload), and harness/upload_on_accept.py runs it against a bare repository
on disk standing in for GitHub. A10 (a real GitHub and a real Linux host) is the developer's, not covered here.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from harness import board, board_viewer, control, project_registry, upload_on_accept
from tests import test_git_model_board
from tests.environment_support import require_loopback

_fixture = vars(test_git_model_board.GitModelBoardIntegrationTests)  # helpers only
TOKEN = "ghp_" + "abcdefghijklmnopqrstuvwxyz0123456789"   # split so the release scan never sees a token-shaped literal


class CrashForTest(BaseException):
    """Stands in for the app dying: not an Exception, so nothing in the upload code can swallow it."""


class UploadBase(unittest.TestCase):
    setUp_fixture = _fixture["setUp"]
    git = _fixture["git"]
    certified_candidate = _fixture["certified_candidate"]

    def setUp(self):
        self.setUp_fixture()
        self.extra = tempfile.TemporaryDirectory()
        self.remote = Path(self.extra.name) / "remote.git"
        subprocess.run(["/usr/bin/git", "init", "--bare", "-q", str(self.remote)], check=True)

    def tearDown(self):
        self.extra.cleanup()
        self.temporary.cleanup()

    def accept(self, branch="release", url=None, destination=...):
        """Certify the task and record the owner's Accept; returns the saved decision."""
        self.certified_candidate()
        if destination is ...:
            destination = {"url": url or str(self.remote), "branch": branch}
        return board.record_release_decision(self.root, "GIT-MODEL", "accepted", upload_destination=destination)

    def saved(self, task="GIT-MODEL"):
        with board.locked_state(self.root) as state:
            return json.loads(json.dumps(state["release_decisions"][task]))

    def upload(self, task="GIT-MODEL"):
        return self.saved(task)["upload"]

    def main(self):
        return self.git("rev-parse", "refs/heads/main").strip()

    def remote_tip(self, branch="release"):
        result = subprocess.run(["/usr/bin/git", "--git-dir", str(self.remote), "rev-parse", f"refs/heads/{branch}"], capture_output=True, text=True)
        return result.stdout.strip() if result.returncode == 0 else ""

    def remote_commit(self, branch, name, *, unrelated=False):
        """Put one more commit on the remote branch from a separate clone (a descendant, or an unrelated history)."""
        work = Path(tempfile.mkdtemp(dir=self.extra.name))
        if unrelated:
            self.git("init", "-q", "-b", "other", cwd=work)
        else:
            self.git("clone", "-q", str(self.remote), str(work / "c"))
            work = work / "c"
            self.git("checkout", "-q", branch, cwd=work)
        self.git("config", "user.name", "Other", cwd=work)
        self.git("config", "user.email", "other@example.invalid", cwd=work)
        (work / name).write_text(name, encoding="utf-8")
        self.git("add", name, cwd=work)
        self.git("commit", "-q", "-m", name, cwd=work)
        self.git("push", "-q", str(self.remote), f"HEAD:refs/heads/{branch}", cwd=work)
        return self.git("rev-parse", "HEAD", cwd=work).strip()

    def run_uploads(self, **options):
        upload_on_accept.wake(self.root, **options).join(30)
        return self.upload()

    def failing_network(self, code, output):
        """A _run_git that fails every push and network fetch with `output` but runs local steps (the staging copy of main, is-ancestor, rev-list) for real."""
        real = upload_on_accept._run_git
        def network(arguments):
            return arguments[0] == "push" or (arguments[0] == "fetch" and "+refs/heads/main:refs/nmhp/main" not in arguments)
        return lambda repository, arguments, timeout=None: (code, output) if network(arguments) else real(repository, arguments, timeout)

    def reset_to_pending(self):
        with board.locked_state(self.root) as state:
            state["release_decisions"]["GIT-MODEL"]["upload"]["status"] = "pending"


class RegistryDestinationTests(unittest.TestCase):
    """A2: what the edit dialog may save."""

    def test_accepted_forms_and_refusals(self):
        good = [
            "https://github.com/example/app.git", "http://git.example.com:8080/team/repo", "ssh://git@github.com/example/x.git",
            "ssh://github.com:2222/a/b", "git@github.com:example/app.git", "/srv/git/project.git",
        ]
        for url in good:
            self.assertEqual(project_registry.normalize_upload_destination({"url": f"  {url} ", "branch": " main "}), {"url": url, "branch": "main"}, url)
        bad_urls = [
            "https://user@github.com/a/b.git", "https://user:secret@github.com/a/b.git", f"https://{TOKEN}@github.com/a/b.git",
            f"https://x-access-token:{TOKEN}@github.com/a/b", "ftp://host/a", "github.com/a/b", "relative/path", "git@host", "file:///tmp/x",
        ]
        for url in bad_urls:
            with self.assertRaises(ValueError, msg=url) as caught:
                project_registry.normalize_upload_destination({"url": url, "branch": "main"})
            self.assertNotIn(TOKEN, str(caught.exception))
        for branch in ("bad branch", "a..b", "-x", "x.lock", "a~1", "", "@"):
            with self.assertRaises(ValueError, msg=branch):
                project_registry.normalize_upload_destination({"url": "git@github.com:a/b.git", "branch": branch})
        for empty in (None, {}, {"url": "", "branch": ""}):
            self.assertIsNone(project_registry.normalize_upload_destination(empty))

    def test_saved_on_the_project_as_a_structure_and_cleared(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as code:
            entry = project_registry.register(Path(home), "Proj", Path(code))
            saved = project_registry.update_entry(Path(home), entry["id"], upload_destination={"url": "git@github.com:a/b.git", "branch": "main"})
            self.assertEqual(saved["upload_destination"], {"url": "git@github.com:a/b.git", "branch": "main"})
            self.assertEqual(upload_on_accept.destination_for(Path(home), entry["id"]), {"url": "git@github.com:a/b.git", "branch": "main"})
            with self.assertRaises(ValueError):
                project_registry.update_entry(Path(home), entry["id"], upload_destination={"url": "https://u:p@h/x", "branch": "main"})
            self.assertEqual(project_registry.load(Path(home))["projects"][0]["upload_destination"]["branch"], "main", "a refused save changes nothing")
            cleared = project_registry.update_entry(Path(home), entry["id"], upload_destination=None)
            self.assertNotIn("upload_destination", cleared)
            self.assertIsNone(upload_on_accept.destination_for(Path(home), entry["id"]))
            audit = (Path(home) / project_registry.AUDIT_FILENAME).read_text(encoding="utf-8")
            self.assertNotIn("git@github.com", audit)

    def test_the_manager_edit_endpoint_and_dialog_carry_the_field(self):
        from harness import project_manager, project_manager_page
        source = Path(project_manager.__file__).read_text(encoding="utf-8")
        self.assertIn('"workspace_root", "upload_destination")', source)
        self.assertIn('id="repair-upload-url"', project_manager_page.PAGE)
        self.assertIn('id="repair-upload-branch"', project_manager_page.PAGE)
        self.assertIn("turn it off", project_manager_page.PAGE)


class UploadOnAcceptTests(UploadBase):
    def test_a1_no_destination_keeps_todays_flow(self):
        response = self.accept(destination=None)
        self.assertIsNone(response["upload"])
        self.assertEqual(upload_on_accept.pending_tasks(self.root, include_stopped=True), [])
        self.assertEqual(self.remote_tip(), "")

    def test_a3_accept_saves_the_destination_and_the_commit_reaches_an_empty_branch(self):
        response = self.accept()
        saved = response["upload"]
        self.assertEqual((saved["url"], saved["branch"], saved["status"]), (str(self.remote), "release", "pending"))
        self.assertEqual(saved["release_head"], self.main(), "the reviewed head is what was accepted")
        self.assertTrue(saved["id"] and saved["created_at"])
        self.assertEqual(self.remote_tip(), "", "nothing is uploaded until the worker runs")
        upload = self.run_uploads()
        self.assertEqual(upload["status"], "uploaded")
        self.assertEqual(self.remote_tip(), self.main())
        self.assertEqual(upload["commit"], self.main())
        self.assertEqual([a["outcome"] for a in upload["attempts"]], ["uploaded"])
        self.assertEqual(self.git("for-each-ref", "refs/nmhp-upload").strip(), "", "the private ref is gone")

    def test_settings_planted_in_the_task_repository_are_never_used_for_the_upload(self):
        # A Delivery agent can write the task repository's config: a redirect of the saved address and a credential helper that
        # runs a program must both be ignored while the owner's sign-in is in use.
        decoy = Path(self.extra.name) / "decoy.git"
        subprocess.run(["/usr/bin/git", "init", "--bare", "-q", str(decoy)], check=True)
        marker = Path(self.extra.name) / "helper-ran"
        self.git("config", f"url.{decoy}.insteadOf", str(self.remote))
        self.git("config", f"url.{decoy}.pushInsteadOf", str(self.remote))
        self.git("config", "credential.helper", f"!touch {marker}")
        self.accept()
        upload = self.run_uploads()
        self.assertEqual(upload["status"], "uploaded")
        self.assertEqual(self.remote_tip(), self.main(), "the upload reached the saved address")
        decoy_heads = subprocess.run(["/usr/bin/git", "--git-dir", str(decoy), "for-each-ref"], capture_output=True, text=True).stdout
        self.assertEqual(decoy_heads.strip(), "", "the planted redirect received nothing")
        self.assertFalse(marker.exists(), "the planted credential helper never ran")

    def test_a3_running_again_changes_nothing(self):
        self.accept()
        self.run_uploads()
        tip = self.remote_tip()
        self.reset_to_pending()
        self.assertEqual(self.run_uploads()["status"], "uploaded")
        self.assertEqual(self.remote_tip(), tip)

    def test_a4_a_branch_that_already_contains_the_commit_is_uploaded_without_pushing_an_older_one(self):
        self.accept()
        self.run_uploads()
        later = self.remote_commit("release", "later.txt")
        self.reset_to_pending()
        upload = self.run_uploads()
        self.assertEqual(upload["status"], "uploaded")
        self.assertEqual(self.remote_tip(), later, "the newer remote tip is left alone")

    def test_a5_a_populated_branch_without_a_tracking_ref_is_never_overwritten(self):
        other = self.remote_commit("release", "theirs.txt", unrelated=True)
        self.accept()
        self.assertEqual(self.git("for-each-ref", "refs/remotes").strip(), "", "there is no tracking ref here")
        upload = self.run_uploads()
        self.assertEqual(upload["status"], "diverged")
        self.assertEqual(self.remote_tip(), other)
        self.assertIn("work this project does not have", upload["attempts"][-1]["reason"])
        self.assertEqual(self.git("for-each-ref", "refs/nmhp-upload").strip(), "")

    def test_a5_the_branch_moves_while_the_upload_is_under_way(self):
        self.accept()
        real = upload_on_accept._run_git
        moved = {}

        def racing(repository, arguments, timeout=None):
            if arguments[:1] == ["push"] and not moved:
                self.run_push_first = real(repository, arguments, timeout)
                moved["tip"] = self.remote_commit("release", "raced.txt")    # someone else adds a descendant right after our push
                return 1, f"! refs/heads/release:refs/heads/release [rejected] (fetch first)\nerror: failed to push some refs"
            return real(repository, arguments, timeout)

        with patch.object(upload_on_accept, "_run_git", racing):
            upload = self.run_uploads()
        self.assertEqual(upload["status"], "uploaded")
        self.assertEqual(self.remote_tip(), moved["tip"])

    def test_a5_the_branch_moves_to_other_work_while_the_upload_is_under_way(self):
        self.accept()
        real = upload_on_accept._run_git
        moved = {}

        def racing(repository, arguments, timeout=None):
            if arguments[:1] == ["push"] and not moved:
                moved["tip"] = self.remote_commit("release", "theirs.txt", unrelated=True)
            return real(repository, arguments, timeout)

        with patch.object(upload_on_accept, "_run_git", racing):
            upload = self.run_uploads()
        self.assertEqual(upload["status"], "diverged")
        self.assertEqual(self.remote_tip(), moved["tip"])

    def test_a6_failures_leave_accept_done_main_unchanged_and_give_a_plain_reason(self):
        missing = Path(self.extra.name) / "nowhere.git"
        self.accept(url=str(missing))
        main = self.main()
        upload = self.run_uploads()
        self.assertEqual(upload["status"], "failed")
        self.assertEqual(upload["attempts"][-1]["reason"], upload_on_accept.REASON_NOT_FOUND)
        self.assertEqual(self.main(), main)
        with board.locked_state(self.root) as state:
            self.assertIn("GIT-MODEL", state["git_acceptances"])
            self.assertNotIn("git_acceptance", state["release_decisions"]["GIT-MODEL"])

    def test_a6_the_time_limit_stops_a_hanging_push(self):
        hook = self.remote / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
        hook.chmod(0o755)
        self.accept()
        with patch.object(upload_on_accept, "GIT_TIMEOUT_SECONDS", 1):
            upload = self.run_uploads()
        self.assertEqual(upload["status"], "failed")
        self.assertEqual(upload["attempts"][-1]["reason"], upload_on_accept.REASON_TIME)
        self.assertEqual(self.remote_tip(), "")

    def test_a6_plain_reasons_for_sign_in_network_and_other(self):
        cases = {
            "fatal: could not read Username for 'https://github.com': terminal prompts disabled": upload_on_accept.REASON_SIGN_IN,
            "git@github.com: Permission denied (publickey).": upload_on_accept.REASON_SIGN_IN,
            "remote: Repository not found.\nfatal: repository 'https://github.com/a/b/' not found": upload_on_accept.REASON_NOT_FOUND,
            "fatal: unable to access 'https://github.com/a/b/': Could not resolve host: github.com": upload_on_accept.REASON_NETWORK,
            "something nobody expected": upload_on_accept.REASON_OTHER,
        }
        for output, reason in cases.items():
            self.assertEqual(upload_on_accept._plain_reason(output), reason, output)

    def test_a6_a_token_in_git_output_never_reaches_the_board(self):
        self.accept()
        leaked = f"fatal: unable to access 'https://x-access-token:{TOKEN}@github.com/o/r.git/': The requested URL returned error: 403\nAuthorization: token {TOKEN}"
        with patch.object(upload_on_accept, "_run_git", self.failing_network(128, leaked)):
            upload = self.run_uploads()
        self.assertEqual(upload["status"], "failed")
        self.assertEqual(upload["attempts"][-1]["reason"], upload_on_accept.REASON_SIGN_IN)
        with board.locked_state(self.root) as state:
            everything = json.dumps(state)
        self.assertNotIn(TOKEN, everything)
        self.assertLessEqual(len(upload["attempts"][-1]["output"]), upload_on_accept.OUTPUT_KEEP_CHARS)
        self.assertNotIn(TOKEN, json.dumps(board.snapshot(self.root)))

    def test_a6_only_the_last_2000_redacted_characters_are_kept(self):
        self.accept()
        noisy = "line of noise\n" * 400
        with patch.object(upload_on_accept, "_run_git", self.failing_network(1, noisy)):
            upload = self.run_uploads()
        self.assertEqual(len(upload["attempts"][-1]["output"]), upload_on_accept.OUTPUT_KEEP_CHARS)

    def test_a7_the_app_stops_before_the_upload_starts(self):
        self.accept()
        self.assertEqual(self.upload()["status"], "pending")   # no worker ran: the app was gone right after Accept
        upload_on_accept.wake(self.root, include_stopped=True).join(30)    # start / open / Resume
        self.assertEqual(self.upload()["status"], "uploaded")
        self.assertEqual(self.remote_tip(), self.main())

    def test_a7_the_app_stops_during_the_upload(self):
        self.accept()
        with patch.object(upload_on_accept, "_attempt_push", side_effect=CrashForTest()):
            with self.assertRaises(CrashForTest):
                upload_on_accept.run_pending(self.root)
        self.assertEqual(self.upload()["status"], "running")
        upload = self.run_uploads()
        self.assertEqual(upload["status"], "uploaded")
        self.assertEqual([a["outcome"] for a in upload["attempts"]], ["interrupted", "uploaded"], "every attempt is kept")
        self.assertEqual(self.remote_tip(), self.main())

    def test_a7_the_remote_took_it_but_the_outcome_was_never_saved(self):
        self.accept()
        with patch.object(upload_on_accept, "_save_outcome", side_effect=CrashForTest()):
            with self.assertRaises(CrashForTest):
                upload_on_accept.run_pending(self.root)
        self.assertEqual(self.remote_tip(), self.main())
        self.assertEqual(self.upload()["status"], "running")
        tip = self.remote_tip()
        upload = self.run_uploads()
        self.assertEqual(upload["status"], "uploaded")
        self.assertEqual(self.remote_tip(), tip)

    def test_r2_a_push_cut_off_by_a_restart_that_landed_anyway_is_uploaded_without_upload_again(self):
        # Review R2-1: the first push finished on the server after the app stopped; the attempt after the restart is refused
        # with "failed to update ref" / "reference already exists", which must still end uploaded.
        self.accept()
        real = upload_on_accept._run_git
        with patch.object(upload_on_accept, "_attempt_push", side_effect=CrashForTest()):
            with self.assertRaises(CrashForTest):
                upload_on_accept.run_pending(self.root)
        self.assertEqual(self.upload()["status"], "running")
        landed = {}

        def late_landing(repository, arguments, timeout=None):
            if arguments[:1] == ["push"] and not landed:
                landed["result"] = real(repository, arguments, timeout)     # the interrupted push completes on the server
                return 1, ("! refs/heads/main:refs/heads/release [remote rejected] (failed to update ref)\n"
                           "error: reference already exists\nerror: failed to push some refs")
            return real(repository, arguments, timeout)

        with patch.object(upload_on_accept, "_run_git", late_landing):
            upload_on_accept.wake(self.root, include_stopped=True).join(30)     # the restart / Resume
        upload = self.upload()
        self.assertEqual(landed["result"][0], 0)
        self.assertEqual(upload["status"], "uploaded")
        self.assertEqual([a["outcome"] for a in upload["attempts"]], ["interrupted", "uploaded"])
        self.assertEqual(self.remote_tip(), self.main())

    def test_r2_a_refusal_that_left_the_commit_off_the_branch_stays_failed_with_its_own_reason(self):
        self.accept()
        refusal = "! refs/heads/main:refs/heads/release [remote rejected] (pre-receive hook declined)\nerror: failed to push some refs"
        real = upload_on_accept._run_git
        with patch.object(upload_on_accept, "_run_git",
                          lambda repository, arguments, timeout=None: (1, refusal) if arguments[:1] == ["push"] else real(repository, arguments, timeout)):
            upload = self.run_uploads()
        self.assertEqual(upload["status"], "failed")
        self.assertEqual(upload["attempts"][-1]["reason"], upload_on_accept.REASON_OTHER)
        self.assertEqual(self.remote_tip(), "")
        other = self.remote_commit("release", "theirs.txt", unrelated=True)
        self.reset_to_pending()
        with patch.object(upload_on_accept, "_run_git",
                          lambda repository, arguments, timeout=None: (1, refusal) if arguments[:1] == ["push"] else real(repository, arguments, timeout)):
            upload = self.run_uploads()
        self.assertEqual(upload["status"], "failed", "a refusal that is not 'behind' is never reported as diverged")
        self.assertEqual(self.remote_tip(), other)

    def test_a7_main_has_the_commit_but_the_board_never_saved_the_acceptance(self):
        """The broker journal said done, then the app died before the board was written: no git_acceptances record survives."""
        self.accept()
        accepted = self.main()
        # a later accepted commit on main, so "the newest commit on main" would be the wrong thing to upload
        tree = self.git("rev-parse", "main^{tree}").strip()
        later = self.git("commit-tree", tree, "-p", accepted, "-m", "later").strip()
        self.git("update-ref", "refs/heads/main", later)
        with board.locked_state(self.root) as state:
            state["git_acceptances"].pop("GIT-MODEL")
        upload = self.run_uploads()
        self.assertEqual(upload["status"], "uploaded")
        self.assertEqual(self.remote_tip(), accepted, "the accepted commit, not the later one")

    def test_a7_the_upload_waits_while_main_does_not_hold_the_reviewed_head(self):
        self.accept()
        with board.locked_state(self.root) as state:
            state["release_decisions"]["GIT-MODEL"]["upload"]["release_head"] = "1" * 40
            state["git_acceptances"].pop("GIT-MODEL")
        self.assertEqual(upload_on_accept.run_one(self.root, "GIT-MODEL"), "pending")
        self.assertEqual(self.upload()["attempts"], [])
        self.assertEqual(self.upload()["status"], "pending")

    def test_a7_a_paused_project_runs_nothing(self):
        self.accept()
        with patch.object(board, "locked_state", side_effect=board.ProjectPausedError("paused")):
            self.assertEqual(upload_on_accept.pending_tasks(self.root, include_stopped=True), [])
        self.assertEqual(self.remote_tip(), "")

    def test_r1_an_upload_finished_while_paused_is_saved_after_resume(self):
        # Review 2026-11-03 R1-1: the push went through, but the outcome could not be saved because the project was paused.
        self.accept()
        real = upload_on_accept._save_outcome
        with patch.object(upload_on_accept, "_save_outcome", side_effect=board.ProjectPausedError("paused")):
            upload = self.run_uploads()
        self.assertEqual(upload["status"], "running")
        self.assertEqual(self.remote_tip(), self.main(), "the remote already took it")
        upload = self.run_uploads(include_stopped=True)   # what Resume does
        self.assertEqual(upload["status"], "uploaded")
        self.assertEqual([a["outcome"] for a in upload["attempts"]], ["interrupted", "uploaded"])
        self.assertIs(upload_on_accept._save_outcome, real)

    def test_r1_an_upload_refused_while_paused_runs_again_after_resume(self):
        self.accept()
        with patch.object(upload_on_accept, "_run_git", self.failing_network(128, "fatal: unable to access: Could not resolve host")), \
                patch.object(upload_on_accept, "_save_outcome", side_effect=board.ProjectPausedError("paused")):
            upload = self.run_uploads()
        self.assertEqual((upload["status"], self.remote_tip()), ("running", ""))
        upload = self.run_uploads(include_stopped=True)   # the destination works again by Resume
        self.assertEqual(upload["status"], "uploaded")
        self.assertEqual(self.remote_tip(), self.main())


    def test_a8_the_destination_saved_with_the_accept_is_used_even_after_the_setting_changes(self):
        with tempfile.TemporaryDirectory() as home:
            entry = project_registry.register(Path(home), "Proj", self.root, description="")
            project_registry.update_entry(Path(home), entry["id"], upload_destination={"url": str(self.remote), "branch": "release"})
            self.certified_candidate()
            destination = upload_on_accept.destination_for(Path(home), entry["id"])
            board.record_release_decision(self.root, "GIT-MODEL", "accepted", upload_destination=destination)
            other = Path(self.extra.name) / "other.git"
            subprocess.run(["/usr/bin/git", "init", "--bare", "-q", str(other)], check=True)
            project_registry.update_entry(Path(home), entry["id"], upload_destination={"url": str(other), "branch": "elsewhere"})
            project_registry.update_entry(Path(home), entry["id"], upload_destination=None)
            self.assertIsNone(upload_on_accept.destination_for(Path(home), entry["id"]))
            self.assertEqual(self.run_uploads()["status"], "uploaded")
            self.assertEqual(self.remote_tip(), self.main())
            self.assertEqual(self.git("--git-dir", str(other), "for-each-ref").strip(), "")

    def two_accepts(self):
        """T1 accepted first (commit c1), T2 second (commit c2 on top of c1), both pending."""
        self.accept()
        c1 = self.main()
        tree = self.git("rev-parse", "main^{tree}").strip()
        c2 = self.git("commit-tree", tree, "-p", c1, "-m", "second").strip()
        self.git("update-ref", "refs/heads/main", c2)
        with board.locked_state(self.root) as state:
            first = state["release_decisions"]["GIT-MODEL"]
            second = json.loads(json.dumps(first))
            second["task"] = "SECOND"
            second["upload"].update({"id": "second-upload", "release_head": c2, "created_at": "2999-01-01T00:00:00+00:00"})
            first["upload"]["created_at"] = "2000-01-01T00:00:00+00:00"
            state["release_decisions"]["SECOND"] = second
            state["task_repositories"]["SECOND"] = state["task_repositories"]["GIT-MODEL"]
        return c1, c2

    def test_a8_two_accepts_upload_one_at_a_time_in_accept_order(self):
        c1, c2 = self.two_accepts()
        order = []
        real = upload_on_accept._attempt_push

        def spy(repository, upload, commit):
            order.append((upload["id"] == "second-upload", commit))
            return real(repository, upload, commit)

        with patch.object(upload_on_accept, "_attempt_push", spy):
            self.assertEqual(upload_on_accept.run_pending(self.root), ["GIT-MODEL", "SECOND"])
        self.assertEqual(order, [(False, c1), (True, c2)])
        self.assertEqual(self.remote_tip(), c2)

    def test_a8_an_older_commit_never_moves_the_branch_back(self):
        c1, c2 = self.two_accepts()
        self.assertEqual(upload_on_accept.run_one(self.root, "SECOND"), "uploaded")
        self.assertEqual(self.remote_tip(), c2)
        self.assertEqual(upload_on_accept.run_one(self.root, "GIT-MODEL"), "uploaded")
        self.assertEqual(self.remote_tip(), c2)

    def test_a8_repeated_upload_again_presses_make_one_more_attempt_on_the_same_upload(self):
        self.accept(url=str(Path(self.extra.name) / "nowhere.git"))
        self.assertEqual(self.run_uploads()["status"], "failed")
        before = self.upload()
        subprocess.run(["/usr/bin/git", "init", "--bare", "-q", str(Path(self.extra.name) / "nowhere.git")], check=True)
        for _ in range(3):
            upload_on_accept.retry(self.root, "GIT-MODEL")
        for thread in list(threading.enumerate()):
            if thread.name == "upload-on-accept":
                thread.join(30)
        upload = self.upload()
        self.assertEqual(upload["id"], before["id"], "never a second upload")
        self.assertEqual(upload["status"], "uploaded")
        self.assertEqual([a["outcome"] for a in upload["attempts"]], ["failed", "uploaded"])
        with board.locked_state(self.root) as state:
            self.assertEqual(len(state["release_decisions"]), 1)

    def test_a8_upload_again_on_a_finished_upload_does_nothing(self):
        self.accept()
        self.run_uploads()
        result = upload_on_accept.retry(self.root, "GIT-MODEL")
        self.assertEqual(result["status"], "uploaded")
        with self.assertRaises(ValueError):
            upload_on_accept.retry(self.root, "NO-SUCH-TASK")

    def test_stopped_uploads_wait_for_the_owner_unless_the_app_is_starting(self):
        self.accept(url=str(Path(self.extra.name) / "nowhere.git"))
        self.run_uploads()
        self.assertEqual(upload_on_accept.pending_tasks(self.root, include_stopped=False), [])
        self.assertEqual(upload_on_accept.pending_tasks(self.root, include_stopped=True), ["GIT-MODEL"])

    def test_a9_administrative_closure_creates_no_upload(self):
        workspace = Path(self.begun["task_workspace"])
        (workspace / "product.txt").write_text("shipped\n", encoding="utf-8")
        committed = board.broker_stage_commit(self.root, self.delivery["id"], ["product.txt"], "task work")
        self.git("merge", "--ff-only", committed["commit"])
        session = control.create(self.root, "claude_cto")
        cto = board.register(self.root, "cto", "GLOBAL_MONITOR", vendor="Anthropic", session_id=session["id"])["id"]
        board.close_released_administratively(self.root, cto, "GIT-MODEL", committed["commit"], "Owner shipped it by hand, long enough reason.")
        self.assertNotIn("upload", self.saved())
        self.assertEqual(upload_on_accept.pending_tasks(self.root, include_stopped=True), [])
        self.assertEqual(upload_on_accept.run_pending(self.root, include_stopped=True), [])

    def test_a9_no_agent_reachable_surface_mentions_uploads(self):
        harness = Path(board.__file__).parent
        for name in ("board.py", "board_client.py", "board_mcp.py", "board_surface.py", "git_broker.py"):
            self.assertNotRegex((harness / name).read_text(encoding="utf-8"), r"(?m)^\s*(from|import) .*upload_on_accept", name)
        self.assertNotIn("upload_destination", (harness / "board_surface.py").read_text(encoding="utf-8"))

    def test_a9_a_decision_written_any_other_way_carries_no_upload(self):
        self.certified_candidate()
        response = board.record_release_decision(self.root, "GIT-MODEL", "accepted")
        self.assertIsNone(response["upload"], "no destination handed in by the owner's route, no upload")


class UploadRouteTests(UploadBase):
    """The owner's routes: Accept saves the destination from the manager registry; Upload again is same-origin only."""

    def setUp(self):
        require_loopback()
        super().setUp()

    def serve(self, home=None, project_id=""):
        server = board_viewer.ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(
            self.root, settings_home=Path(home) if home else None, project_id=project_id))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def post(self, base, path, body=None, headers=None):
        request = Request(base + path, data=json.dumps(body or {}).encode(), headers={"Content-Type": "application/json", **(headers or {})}, method="POST")
        try:
            with urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            return error.code, json.loads(error.read())

    def test_accept_over_http_takes_the_destination_from_the_manager_registry_and_uploads(self):
        with tempfile.TemporaryDirectory() as home:
            entry = project_registry.register(Path(home), "Proj", self.root)
            project_registry.update_entry(Path(home), entry["id"], upload_destination={"url": str(self.remote), "branch": "release"})
            self.certified_candidate()
            base = self.serve(home, entry["id"])
            status, body = self.post(base, "/api/releases/GIT-MODEL/decision", {"decision": "accepted", "upload": {"url": "/tmp/evil", "branch": "x"}})
            self.assertEqual(status, 201)
            self.assertEqual(body["decision"]["upload"]["url"], str(self.remote), "a destination in the request body is ignored")
            for thread in list(threading.enumerate()):
                if thread.name == "upload-on-accept":
                    thread.join(30)
            self.assertEqual(self.upload()["status"], "uploaded")
            self.assertEqual(self.remote_tip(), self.main())

    def test_upload_again_route_is_owner_checked_and_never_makes_a_second_upload(self):
        self.accept(url=str(Path(self.extra.name) / "nowhere.git"))
        self.assertEqual(self.run_uploads()["status"], "failed")
        base = self.serve()
        status, _ = self.post(base, "/api/uploads/GIT-MODEL/retry", headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        status, _ = self.post(base, "/api/uploads/GIT-MODEL/retry", headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 403)
        self.assertEqual(len(self.upload()["attempts"]), 1, "refused requests change nothing")
        subprocess.run(["/usr/bin/git", "init", "--bare", "-q", str(Path(self.extra.name) / "nowhere.git")], check=True)
        status, body = self.post(base, "/api/uploads/GIT-MODEL/retry")
        self.assertEqual(status, 200)
        for thread in list(threading.enumerate()):
            if thread.name == "upload-on-accept":
                thread.join(30)
        self.assertEqual(self.upload()["status"], "uploaded")
        status, body = self.post(base, "/api/uploads/NO-SUCH/retry")
        self.assertEqual(status, 400)


class ResumeWakesUploadsTests(unittest.TestCase):
    def test_every_resume_wakes_unfinished_uploads_even_with_a_kept_worker(self):
        from harness import project_manager
        from tests.test_evidence_reuse import EvidenceReuseTests, _Worker
        fixture = EvidenceReuseTests("test_valid_pass_is_reused_once_through_real_resume")
        fixture.setUp() if hasattr(fixture, "setUp") else None
        try:
            case = fixture._case()
            manager = project_manager.ProjectManager(case["home"], board_port=0, terminal_launcher=lambda *_: None)
            manager.pause_project(case["entry"]["id"], drain_seconds=0, stop_timeout=0)
            manager.worker = _Worker()
            manager.worker_project = case["entry"]["id"]
            with patch.object(upload_on_accept, "wake") as wake:
                manager.resume_project(case["entry"]["id"])
                manager.resume_project(case["entry"]["id"])   # a repeated press wakes again too
            self.assertEqual(wake.call_count, 2)
            self.assertTrue(all(call.kwargs == {"include_stopped": True} for call in wake.call_args_list))
        finally:
            fixture.doCleanups()


if __name__ == "__main__":
    unittest.main()
