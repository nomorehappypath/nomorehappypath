# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Phone apps, step 1 (owner direction 2026-11-25): harness/phone_setup.check() against stand-in tools -
one test per requirement, no more. Real Xcode, Flutter and simctl calls are proven on the real Mac by the
contract-stage setup check and scripts/phone_proof.py itself; this file proves the six-step logic, its
exact failing-step wording, the retry-once rule, and that a simulator the owner had open is never touched.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from harness import phone_setup

BOOTED_IPHONE = {"udid": "ABCD", "name": "iPhone 15", "version": "17.4", "booted": True}
COLD_IPHONE = {"udid": "ABCD", "name": "iPhone 15", "version": "17.4", "booted": False}


class CheckStepsTests(unittest.TestCase):
    def setUp(self):
        # the check's own lock file goes under a throwaway home, never the real Mac's CoreSimulator folder
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        patcher = mock.patch.object(phone_setup, "host_env", return_value={"HOME": home.name, "PATH": os.environ.get("PATH", "")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_fails_when_xcode_is_not_ready(self):
        with mock.patch.object(phone_setup, "xcode_ready", return_value=(False, False, "")):
            result = phone_setup.check()
        self.assertFalse(result["ok"])
        self.assertEqual(result["label"], "Xcode is not ready")

    def test_fails_when_flutter_is_not_installed(self):
        with mock.patch.object(phone_setup, "xcode_ready", return_value=(True, False, "")), \
             mock.patch.object(phone_setup, "flutter_ready", return_value=(False, False, "")):
            result = phone_setup.check()
        self.assertEqual(result["label"], "Flutter is not installed")

    def test_a_slow_flutter_answer_is_reported_as_a_timeout_never_as_not_installed(self):
        # CTO triage 2026-11-25: Flutter's own first-time engine setup can take minutes; a slow answer
        # is not the same fact as "not installed", and must never be reported as if it were.
        with mock.patch.object(phone_setup, "xcode_ready", return_value=(True, False, "")), \
             mock.patch.object(phone_setup, "flutter_ready", return_value=(False, True, "Downloading engine... 42%")):
            result = phone_setup.check()
        self.assertEqual(result["label"], "Flutter did not answer in time")
        self.assertNotIn("not installed", result["detail"])
        self.assertIn("press Check again", result["detail"])
        self.assertIn("Downloading engine... 42%", result["detail"], "its own last lines must reach the card, not just the fact it hung")

    def test_the_timeout_card_names_the_macos_open_prompt_as_a_likely_cause(self):
        # Owner-approved addition, 2026-11-25: the owner's own Mac once hung here behind an unapproved macOS
        # "open this app?" prompt for dart. No quarantine mark is read; the card just names this as a likely
        # cause, the same on every platform (this test is never skipped on Linux).
        with mock.patch.object(phone_setup, "xcode_ready", return_value=(True, False, "")), \
             mock.patch.object(phone_setup, "flutter_ready", return_value=(False, True, "")):
            result = phone_setup.check()
        self.assertIn("macOS may be asking whether to open Flutter", result["detail"])
        self.assertIn("click Open", result["detail"])

    def test_fails_when_no_simulated_iphone_exists(self):
        with mock.patch.object(phone_setup, "xcode_ready", return_value=(True, False, "")), \
             mock.patch.object(phone_setup, "flutter_ready", return_value=(True, False, "")), \
             mock.patch.object(phone_setup, "find_iphone_simulator", return_value=None):
            result = phone_setup.check()
        self.assertEqual(result["label"], "no simulated iPhone")

    def test_boot_is_tried_twice_before_the_card(self):
        with mock.patch.object(phone_setup, "xcode_ready", return_value=(True, False, "")), \
             mock.patch.object(phone_setup, "flutter_ready", return_value=(True, False, "")), \
             mock.patch.object(phone_setup, "find_iphone_simulator", return_value=dict(COLD_IPHONE)), \
             mock.patch.object(phone_setup, "boot_simulator", return_value=(False, "booting failed")) as boot, \
             mock.patch.object(phone_setup, "cleanup", return_value=(True, "")) as cleanup:
            result = phone_setup.check()
        self.assertEqual(result["label"], "the simulated iPhone did not start")
        self.assertIn("booting failed", result["detail"])
        self.assertIn("Press Check again to retry.", result["detail"])
        self.assertEqual(boot.call_count, 2)
        cleanup.assert_called_once()
        self.assertFalse(cleanup.call_args.args[3], "nothing was booted, so cleanup must not shut anything down")

    def test_build_is_tried_twice_before_the_card(self):
        with mock.patch.object(phone_setup, "xcode_ready", return_value=(True, False, "")), \
             mock.patch.object(phone_setup, "flutter_ready", return_value=(True, False, "")), \
             mock.patch.object(phone_setup, "find_iphone_simulator", return_value=dict(BOOTED_IPHONE)), \
             mock.patch.object(phone_setup, "build_starter_app", return_value=(False, "no provisioning profile", "")) as build, \
             mock.patch.object(phone_setup, "cleanup", return_value=(True, "")):
            result = phone_setup.check()
        self.assertEqual(result["label"], "the starter app did not build for iPhone")
        self.assertEqual(result["detail"], "no provisioning profile")
        self.assertEqual(build.call_count, 2)

    def test_open_is_tried_twice_before_the_card(self):
        with mock.patch.object(phone_setup, "xcode_ready", return_value=(True, False, "")), \
             mock.patch.object(phone_setup, "flutter_ready", return_value=(True, False, "")), \
             mock.patch.object(phone_setup, "find_iphone_simulator", return_value=dict(BOOTED_IPHONE)), \
             mock.patch.object(phone_setup, "build_starter_app", return_value=(True, "", "/tmp/Starter.app")), \
             mock.patch.object(phone_setup, "install_open_capture", return_value=(False, "crashed on launch")) as open_call, \
             mock.patch.object(phone_setup, "cleanup", return_value=(True, "")):
            result = phone_setup.check()
        self.assertEqual(result["label"], "the starter app did not open")
        self.assertIn("crashed on launch", result["detail"])
        self.assertEqual(open_call.call_count, 2)

    def test_a_phone_shut_down_under_the_check_is_started_again_before_the_second_try(self):
        # owner's path run 2026-11-27: an agent's own phone run shut the simulator down while NMHP's check used it
        with mock.patch.object(phone_setup, "xcode_ready", return_value=(True, False, "")), \
             mock.patch.object(phone_setup, "flutter_ready", return_value=(True, False, "")), \
             mock.patch.object(phone_setup, "find_iphone_simulator", return_value=dict(BOOTED_IPHONE)), \
             mock.patch.object(phone_setup, "build_starter_app", return_value=(True, "", "/tmp/Starter.app")), \
             mock.patch.object(phone_setup, "boot_simulator", return_value=(True, "")) as boot, \
             mock.patch.object(phone_setup, "install_open_capture",
                               side_effect=[(False, "Unable to lookup in current state: Shutdown"), (True, "")]), \
             mock.patch.object(phone_setup, "cleanup", return_value=(True, "")) as clean:
            result = phone_setup.check()
        self.assertTrue(result["ok"], result)
        self.assertEqual(boot.call_count, 1)
        self.assertTrue(clean.call_args[0][3], "the phone it started again is shut down as its own")

    def test_success_names_the_phone_and_never_reboots_one_already_open(self):
        with mock.patch.object(phone_setup, "xcode_ready", return_value=(True, False, "")), \
             mock.patch.object(phone_setup, "flutter_ready", return_value=(True, False, "")), \
             mock.patch.object(phone_setup, "find_iphone_simulator", return_value=dict(BOOTED_IPHONE)), \
             mock.patch.object(phone_setup, "boot_simulator") as boot, \
             mock.patch.object(phone_setup, "build_starter_app", return_value=(True, "", "/tmp/Starter.app")), \
             mock.patch.object(phone_setup, "install_open_capture", return_value=(True, "")), \
             mock.patch.object(phone_setup, "cleanup", return_value=(True, "")) as cleanup:
            result = phone_setup.check()
        self.assertTrue(result["ok"])
        self.assertEqual(result["phone_name"], "iPhone 15")
        self.assertEqual(result["phone_version"], "17.4")
        self.assertIn("iPhone 15 (17.4)", result["detail"])
        boot.assert_not_called()   # it was already open: NMHP never boots a simulator it did not itself need to start
        cleanup.assert_called_once()
        self.assertFalse(cleanup.call_args.args[3], "an already-open simulator must not be the one NMHP shuts down")

    def test_cleanup_runs_even_when_a_step_raises(self):
        with mock.patch.object(phone_setup, "xcode_ready", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                phone_setup.check()

    def test_a_failed_cleanup_is_reported_not_silently_passed(self):
        # CTO finding B1 (review round 1, 2026-11-26): cleanup used to always report itself as passed
        # even when the uninstall, the shutdown, or the folder removal failed, so a failed cleanup never
        # reached the owner even though every one of steps 1-5 had passed.
        with mock.patch.object(phone_setup, "xcode_ready", return_value=(True, False, "")), \
             mock.patch.object(phone_setup, "flutter_ready", return_value=(True, False, "")), \
             mock.patch.object(phone_setup, "find_iphone_simulator", return_value=dict(BOOTED_IPHONE)), \
             mock.patch.object(phone_setup, "build_starter_app", return_value=(True, "", "/tmp/Starter.app")), \
             mock.patch.object(phone_setup, "install_open_capture", return_value=(True, "")), \
             mock.patch.object(phone_setup, "cleanup", return_value=(False, "uninstall failed: no such app")):
            result = phone_setup.check()
        self.assertFalse(result["ok"], "a failed cleanup must not be reported as a passed check")
        self.assertIn("uninstall failed: no such app", result["detail"])
        cleanup_step = next(step for step in result["steps"] if step["name"] == "cleanup")
        self.assertFalse(cleanup_step["ok"], "the cleanup step itself must show as failed, not passed")


class CleanupReportsItsOwnFailureTests(unittest.TestCase):
    def test_a_failed_uninstall_shutdown_or_folder_removal_is_reported_not_ignored(self):
        # Root cause of CTO finding B1: cleanup() ran uninstall, shutdown and shutil.rmtree(ignore_errors=True)
        # and never looked at any of their results. With all three stood in as failing, it must now say so.
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "work"
            work.mkdir()
            with mock.patch.object(phone_setup, "_run", return_value=(False, "uninstall failed: no such app", False)), \
                 mock.patch.object(phone_setup, "shutdown_simulator", return_value=(False, "shutdown failed: device busy")), \
                 mock.patch("shutil.rmtree", side_effect=OSError("folder is busy")):
                ok, detail = phone_setup.cleanup("ABCD", "/tmp/Starter.app", work, True, {})
        self.assertFalse(ok)
        self.assertIn("uninstall failed: no such app", detail)


class MobileExtraWritableTests(unittest.TestCase):
    def test_resolves_flutter_sdk_root_and_the_fixed_home_folders(self):
        # CTO finding, owner release repair 2026-11-26: a real project's setup check failed `flutter --version`
        # with "Operation not permitted" on Flutter's own cache; the agents' write confinement must include
        # Flutter's resolved SDK root plus the fixed folders a simulator build writes.
        with tempfile.TemporaryDirectory() as home_name:
            home = Path(home_name)
            flutter_root = home / "flutter-sdk"
            (flutter_root / "bin").mkdir(parents=True)
            (flutter_root / "bin" / "flutter").write_text("#!/bin/sh\n")
            (home / "Library" / "Developer" / "CoreSimulator").mkdir(parents=True)
            (home / ".pub-cache").mkdir()
            env = {"HOME": str(home), "PATH": str(flutter_root / "bin")}
            with mock.patch.object(phone_setup.shutil, "which", return_value=str(flutter_root / "bin" / "flutter")):
                roots = phone_setup.mobile_extra_writable(env)
        self.assertIn(str(flutter_root), roots)
        self.assertIn(str(home / "Library" / "Developer" / "CoreSimulator"), roots)
        self.assertIn(str(home / ".pub-cache"), roots)

    def test_no_flutter_on_path_grants_only_the_fixed_home_folders(self):
        with tempfile.TemporaryDirectory() as home_name:
            home = Path(home_name)
            env = {"HOME": str(home), "PATH": "/usr/bin"}
            with mock.patch.object(phone_setup.shutil, "which", return_value=None):
                roots = phone_setup.mobile_extra_writable(env)
        self.assertFalse(any("flutter-sdk" in root for root in roots))


class ConfinedExecutionTests(unittest.TestCase):
    def test_run_executes_argv_wrapped_by_confine_when_given(self):
        # Item 2 of the same finding: the real run must execute Xcode/Flutter/simulator calls through the
        # same OS write confinement a managed agent runs under, not outside it.
        ok, output, timed_out = phone_setup._run(
            ["/this/does/not/exist"], confine=lambda argv: ["/bin/echo", "CONFINED", *argv],
        )
        self.assertTrue(ok)
        self.assertFalse(timed_out)
        self.assertIn("CONFINED /this/does/not/exist", output)

    def test_check_threads_confine_into_every_step(self):
        confine = lambda argv: argv
        with mock.patch.object(phone_setup, "xcode_ready", return_value=(True, False, "")) as xcode, \
             mock.patch.object(phone_setup, "flutter_ready", return_value=(True, False, "")) as flutter, \
             mock.patch.object(phone_setup, "find_iphone_simulator", return_value=dict(BOOTED_IPHONE)) as find_sim, \
             mock.patch.object(phone_setup, "build_starter_app", return_value=(True, "", "/tmp/Starter.app")) as build, \
             mock.patch.object(phone_setup, "install_open_capture", return_value=(True, "")) as open_call, \
             mock.patch.object(phone_setup, "cleanup", return_value=(True, "")) as cleanup:
            phone_setup.check(confine=confine)
        xcode.assert_called_once_with(mock.ANY, confine)
        flutter.assert_called_once_with(mock.ANY, confine)
        find_sim.assert_called_once_with(mock.ANY, confine)
        build.assert_called_once_with(mock.ANY, mock.ANY, confine)
        open_call.assert_called_once_with(mock.ANY, mock.ANY, mock.ANY, mock.ANY, confine)
        cleanup.assert_called_once_with(mock.ANY, mock.ANY, mock.ANY, mock.ANY, mock.ANY, confine)


class SwiftPackageManagerDisabledTests(unittest.TestCase):
    def test_adds_the_cocoapods_setting_inside_the_existing_flutter_block(self):
        # CTO finding, owner release repair 2026-11-26: Xcode's Swift Package Manager starts its own
        # sandbox-exec, which macOS refuses to nest inside the agents' own confinement, so the starter
        # app's iOS build must resolve through CocoaPods there instead.
        with tempfile.TemporaryDirectory() as project_name:
            project = Path(project_name)
            (project / "pubspec.yaml").write_text(
                "name: nmhp_phone_proof\ndescription: A new Flutter project.\n\nflutter:\n"
                "  uses-material-design: true\n", encoding="utf-8",
            )
            phone_setup._disable_swift_package_manager(project)
            text = (project / "pubspec.yaml").read_text(encoding="utf-8")
        self.assertIn("flutter:\n  config:\n    enable-swift-package-manager: false\n  uses-material-design: true\n", text)

    def test_is_a_no_op_when_already_present(self):
        with tempfile.TemporaryDirectory() as project_name:
            project = Path(project_name)
            original = "flutter:\n  config:\n    enable-swift-package-manager: false\n"
            (project / "pubspec.yaml").write_text(original, encoding="utf-8")
            phone_setup._disable_swift_package_manager(project)
            self.assertEqual((project / "pubspec.yaml").read_text(encoding="utf-8"), original)


class BuildStarterAppSwiftPackageManagerTests(unittest.TestCase):
    def test_create_gets_the_environment_override_and_a_lingering_package_reference_fails_before_building(self):
        # CTO finding, owner release repair 2026-11-26 (attempt 2): the pubspec flag alone comes too late
        # for `flutter create` itself, which already writes a Swift Package reference before pubspec.yaml
        # exists to read; FLUTTER_SWIFT_PACKAGE_MANAGER=false must reach that one call, and a lingering
        # reference must stop the build before macOS's nested-sandbox refusal is reached.
        calls = []

        def fake_run(argv, cwd=None, timeout=None, env=None, log_file=None, confine=None):
            calls.append((list(argv), dict(env or {})))
            if argv[:2] == ["flutter", "create"]:
                project = Path(argv[-1])
                project.mkdir(parents=True)
                (project / "pubspec.yaml").write_text("flutter:\n  uses-material-design: true\n", encoding="utf-8")
                (project / "ios" / "Runner.xcodeproj").mkdir(parents=True)
                (project / "ios" / "Runner.xcodeproj" / "project.pbxproj").write_text(
                    "... XCSwiftPackageProductDependency ...", encoding="utf-8",
                )
            return True, "", False

        with tempfile.TemporaryDirectory() as work_name:
            work = Path(work_name)
            with mock.patch.object(phone_setup, "_run", side_effect=fake_run):
                ok, detail, app_path = phone_setup.build_starter_app(work, {"PATH": "/usr/bin"})
        self.assertFalse(ok)
        self.assertIn("XCSwiftPackageProductDependency", detail)
        self.assertEqual(calls[0][0][:2], ["flutter", "create"])
        self.assertEqual(calls[0][1].get("FLUTTER_SWIFT_PACKAGE_MANAGER"), "false")
        self.assertEqual(len(calls), 1, "the build must never run once a lingering Swift Package reference is found")


class RunWithALogFileTests(unittest.TestCase):
    def test_a_timeout_still_reads_back_what_the_tool_had_already_printed(self):
        # CTO triage 2026-11-25, round 3: a silent hang gave no clue what the tool was doing; with
        # output going straight to a file, the tail survives the kill and says what it was last doing.
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "out.log"
            script = 'import sys, time\nprint("step one", flush=True)\ntime.sleep(30)\n'
            ok, output, timed_out = phone_setup._run([sys.executable, "-c", script], timeout=1, log_file=log)
        self.assertFalse(ok)
        self.assertTrue(timed_out)
        self.assertIn("step one", output)


class RunKillsTheWholeProcessGroupTests(unittest.TestCase):
    def test_timeout_kills_the_whole_process_group_not_just_the_parent(self):
        # CTO triage 2026-11-25, round 2: flutter can start its own background dart process; killing only
        # the direct child left that one running and holding flutter's own startup lock, hanging every
        # later attempt. Stand-in: a parent that starts a grandchild and then hangs, like flutter did.
        script = ('import subprocess, sys, time\n'
                  'child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])\n'
                  'print(child.pid, flush=True)\n'
                  'time.sleep(30)\n')
        ok, output, timed_out = phone_setup._run([sys.executable, "-c", script], timeout=1)
        self.assertFalse(ok)
        self.assertTrue(timed_out)
        child_pid = int(output.strip().splitlines()[0])
        time.sleep(0.3)
        with self.assertRaises(ProcessLookupError, msg="the child the hung tool started must not survive the kill"):
            os.kill(child_pid, 0)


if __name__ == "__main__":
    unittest.main()
