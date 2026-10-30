# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""A project's test environment for governed commands (task 2026-10-28_project-test-environment): the module itself.

Run:  PYTHONPATH=. python3 -m unittest tests.test_project_test_env -v
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from harness import board, certified_execution, child_process, test_env
import tests.test_execute_once as _execute_once
from tests.test_requirements_contract import context

PASSWORD = "s3cretPassw0rd"
URL = f"postgresql+psycopg://tester:{PASSWORD}@localhost:5432/test_db"


def make_env(base: Path, packages: dict[str, dict[str, str]], name: str = "venv") -> Path:
    """A prepared environment: the real interpreter linked into a venv layout, with third-party stand-in packages installed."""
    env = base / name
    (env / "bin").mkdir(parents=True)
    (env / "bin" / "python3").symlink_to(sys.executable)
    (env / "pyvenv.cfg").write_text(f"home = {Path(sys.executable).parent}\nversion = {sys.version_info[0]}.{sys.version_info[1]}.{sys.version_info[2]}\n")
    site = env / "lib" / f"python{sys.version_info[0]}.{sys.version_info[1]}" / "site-packages"
    site.mkdir(parents=True)
    for package, files in packages.items():
        info = site / f"{package}-1.0.dist-info"
        info.mkdir()
        (info / "METADATA").write_text(f"Name: {package}\nVersion: 1.0\n")
        for relative, content in files.items():
            (site / relative).parent.mkdir(parents=True, exist_ok=True)
            (site / relative).write_text(content)
    return env


class Fixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name).resolve()
        self.root = context(self.base)
        self.candidate = self.base / "candidate"
        (self.candidate / "backend" / "tests").mkdir(parents=True)
        (self.candidate / "backend" / "myapp.py").write_text("VALUE = 'one'\n")
        self.env = make_env(self.base, {"thirdparty_demo": {"thirdparty_demo/__init__.py": "NAME = 'third party'\n"}})
        self.secret_file = self.base / "secrets.env"
        self.secret_file.write_text(f"# comment\nTEST_DB={URL}\nOTHER=plain\n")

    def declare(self, **changes):
        declaration = {"python": str(self.env / "bin" / "python3"), "workdir": "backend",
                       "env": {"TEST_DB": {"file": str(self.secret_file), "key": "TEST_DB"}, "MODE": "test"}}
        declaration.update(changes)
        return test_env.save(self.root, declaration)

    def resolve(self):
        return test_env.resolve(self.root, self.candidate)

    def execute(self, resolved, code):
        env = test_env.apply(child_process.execution_environment(), resolved)
        return subprocess.run(["python3", "-c", code], cwd=self.candidate / test_env.workdir_of(resolved), env=env, capture_output=True, text=True, timeout=60)


class DeclarationTests(Fixture):
    def test_a_project_that_declares_nothing_is_untouched(self):
        self.assertIsNone(self.resolve())
        base = child_process.execution_environment()
        self.assertEqual(test_env.apply(base, None), base)
        self.assertEqual(test_env.identity_fields(None), {})
        self.assertEqual(test_env.scrub("anything", None), "anything")
        self.assertEqual(test_env.workdir_of(None), ".")

    def test_a_saved_declaration_never_shows_a_secret(self):
        shown = self.declare()
        text = repr(shown)
        self.assertNotIn(PASSWORD, text)
        self.assertIn("read from the file at run time", text)

    def test_reserved_and_invalid_variable_names_are_refused(self):
        for name in ("PATH", "PYTHONPATH", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES", "BASH_ENV", "HARNESS_X", "HOME", "9bad", "a b"):
            with self.subTest(name=name), self.assertRaises(test_env.TestEnvError):
                self.declare(env={name: "x"})

    def test_relative_folders_must_stay_inside_the_code(self):
        for bad in ("../outside", "/abs/path", "a/../../b"):
            with self.subTest(bad=bad), self.assertRaises(test_env.TestEnvError):
                self.declare(workdir=bad)
        with self.assertRaises(test_env.TestEnvError):
            self.declare(python="relative/python")


class ResolutionTests(Fixture):
    def test_a_governed_command_runs_in_the_environment_from_the_declared_folder_with_the_variables(self):
        self.declare()
        resolved = self.resolve()
        result = self.execute(resolved, "import os, thirdparty_demo, myapp; print(thirdparty_demo.NAME, myapp.VALUE, os.environ['MODE'], os.getcwd().endswith('backend'), bool(os.environ['TEST_DB']))")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split()[:2], ["third", "party"])
        self.assertIn("one test True True", result.stdout)

    def test_changed_candidate_code_runs_with_an_unchanged_environment_and_identity(self):
        self.declare()
        before = self.resolve()
        (self.candidate / "backend" / "myapp.py").write_text("VALUE = 'two'\n")
        after = self.resolve()
        self.assertEqual(before.identity, after.identity, "the environment did not change")
        self.assertIn("two", self.execute(after, "import myapp; print(myapp.VALUE)").stdout)

    def test_traversal_and_symlink_escapes_are_refused(self):
        self.declare()
        outside = self.base / "outside"
        outside.mkdir()
        (self.candidate / "backend" / "link").symlink_to(outside)
        self.declare(workdir="backend/link")
        with self.assertRaisesRegex(test_env.TestEnvError, "leaves the code folder"):
            self.resolve()

    def test_a_missing_interpreter_is_one_plain_sentence_naming_what_to_do(self):
        self.declare(python=str(self.base / "nowhere" / "bin" / "python3"))
        with self.assertRaisesRegex(test_env.TestEnvError, "not ready.*ask the CTO to prepare it"):
            self.resolve()


class LibraryTests(Fixture):
    def test_a_changed_installed_library_changes_the_identity(self):
        self.declare()
        before = self.resolve().identity["libraries_sha256"]
        site = next((self.env / "lib").glob("python*/site-packages"))
        (site / "another_lib-2.0.dist-info").mkdir()
        self.assertNotEqual(self.resolve().identity["libraries_sha256"], before)

    def test_an_environment_with_no_libraries_is_refused_plainly(self):
        empty = self.base / "empty"
        (empty / "bin").mkdir(parents=True)
        (empty / "bin" / "python3").symlink_to(sys.executable)
        self.declare(python=str(empty / "bin" / "python3"))
        with self.assertRaisesRegex(test_env.TestEnvError, "not ready.*ask the CTO to prepare it"):
            self.resolve()


class SecretTests(Fixture):
    def test_the_value_and_the_url_password_are_scrubbed_from_output(self):
        self.declare()
        resolved = self.resolve()
        result = self.execute(resolved, "import os; print(os.environ['TEST_DB']); print('pw=' + os.environ['TEST_DB'].split(':')[2].split('@')[0])")
        raw = result.stdout
        self.assertIn(PASSWORD, raw, "the child really received it")
        cleaned = test_env.scrub(raw, resolved)
        self.assertNotIn(PASSWORD, cleaned)
        self.assertIn(test_env.SCRUBBED, cleaned)

    def test_the_secret_never_enters_the_identity_or_the_shown_declaration(self):
        self.declare()
        resolved = self.resolve()
        self.assertNotIn(PASSWORD, repr(resolved.identity))
        self.assertNotIn(PASSWORD, repr(test_env.public(test_env.load(self.root))))

    def test_the_configuration_digest_changes_when_the_value_behind_the_file_changes(self):
        self.declare()
        before = self.resolve().identity["config_sha256"]
        self.secret_file.write_text(f"TEST_DB={URL.replace('test_db', 'other_db')}\n")
        self.assertNotEqual(self.resolve().identity["config_sha256"], before)

    def test_a_missing_duplicate_or_empty_key_is_a_plain_refusal_without_the_value(self):
        self.declare()
        for body in ("OTHER=1\n", f"TEST_DB={URL}\nTEST_DB={URL}\n", "TEST_DB=\n"):
            with self.subTest(body=body[:12]):
                self.secret_file.write_text(body)
                with self.assertRaises(test_env.TestEnvError) as caught:
                    self.resolve()
                self.assertNotIn(PASSWORD, str(caught.exception))
        self.secret_file.unlink()
        with self.assertRaisesRegex(test_env.TestEnvError, "cannot be read"):
            self.resolve()

    def test_identity_fields_change_with_the_declaration(self):
        self.declare()
        one = test_env.identity_fields(self.resolve())
        self.declare(env={"MODE": "other"})
        two = test_env.identity_fields(self.resolve())
        self.assertNotEqual(one["declaration_sha256"], two["declaration_sha256"])
        self.assertEqual(one["workdir"], "backend")



class BoardIntegrationTests(Fixture):
    """The board's own runners: declaration, identity, a governed run, the secret scrub, and a changed environment."""

    def setUp(self):
        super().setUp()
        # the board root's code folder is the candidate in these tests
        from harness.project_context import project_context
        self.code = Path(project_context(self.root).code_root)
        (self.code / "backend" / "tests").mkdir(parents=True, exist_ok=True)
        (self.code / "backend" / "tests" / "__init__.py").write_text("")
        (self.code / "backend" / "tests" / "test_probe.py").write_text(
            "import os, unittest, thirdparty_demo\n\n\nclass Probe(unittest.TestCase):\n"
            "    def test_runs_in_the_environment(self):\n"
            "        print('NAME', thirdparty_demo.NAME, 'CWD', os.path.basename(os.getcwd()))\n"
            "        print('LEAK', os.environ['TEST_DB'])\n"
            "        self.assertEqual(os.environ['MODE'], 'test')\n")

    def declare_here(self, **changes):
        declaration = {"python": str(self.env / "bin" / "python3"), "workdir": "backend",
                       "env": {"TEST_DB": {"file": str(self.secret_file), "key": "TEST_DB"}, "MODE": "test"}}
        declaration.update(changes)
        from harness import board
        return board.test_env_set(self.root, __import__("json").dumps(declaration), who="the CTO")

    def test_declaring_and_the_status_command_never_show_the_value(self):
        from harness import board
        self.assertFalse(board.test_env_status(self.root)["declared"])
        result = self.declare_here()
        self.assertNotIn(PASSWORD, repr(result))
        status = board.test_env_status(self.root)
        self.assertTrue(status["ready"], status)
        self.assertNotIn(PASSWORD, repr(status))
        self.assertIn("libraries_sha256", status["identity"])

    def test_the_identity_is_unchanged_for_an_undeclared_project_and_changes_with_the_environment(self):
        from harness import board
        plain = board._environment_identity(self.root, self.code)["sha256"]
        self.assertEqual(plain, board._environment_identity()["sha256"])
        self.declare_here()
        declared = board._environment_identity(self.root, self.code)["sha256"]
        self.assertNotEqual(plain, declared)
        self.declare_here(env={"MODE": "test", "EXTRA": "1", "TEST_DB": {"file": str(self.secret_file), "key": "TEST_DB"}})
        self.assertNotEqual(declared, board._environment_identity(self.root, self.code)["sha256"])

    def test_a_governed_run_uses_the_environment_the_folder_and_the_variables_and_scrubs_the_secret(self):
        from harness import board
        self.declare_here()
        output = board._execute_internal_qa("python3 -m unittest tests.test_probe -v", self.root)
        self.assertIn("NAME third party CWD backend", output)
        self.assertNotIn(PASSWORD, output)
        self.assertIn(test_env.SCRUBBED, output)

    def test_an_unready_environment_is_refused_in_plain_words_before_anything_runs(self):
        from harness import board
        self.declare_here(python=str(self.base / "nowhere" / "bin" / "python3"))
        with self.assertRaisesRegex(ValueError, "not ready.*ask the CTO to prepare it"):
            board._execute_internal_qa("python3 -m unittest tests.test_probe", self.root)

    def test_a_declaration_changed_after_the_request_is_not_run_against_the_old_identity(self):
        from harness import board, execution_identity
        self.declare_here()
        recorded = board._environment_identity(self.root, self.code)["sha256"]
        self.declare_here(env={"MODE": "other", "TEST_DB": {"file": str(self.secret_file), "key": "TEST_DB"}})
        candidate = execution_identity.run_candidate_identity("abc", "def")
        certification = {"board_root": self.root, "candidate": candidate, "environment_sha256": recorded, "lockfile_digests": {},
                         "role": "delivery", "gate": "final:::", "retry_reason": "", "task": "T"}
        with self.assertRaisesRegex(ValueError, "changed after this was requested"):
            board._execute_internal_qa("python3 -m unittest tests.test_probe", self.root, certification=certification)

    def test_the_stored_process_audit_of_a_real_certified_run_never_holds_the_secret(self):
        from harness import execution_identity
        (self.code / "backend" / "tests" / "test_spawn.py").write_text(
            "import os, subprocess, sys, unittest\n\n\nclass Spawn(unittest.TestCase):\n"
            "    def test_child_gets_the_value_as_an_argument(self):\n"
            "        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(2)', os.environ['TEST_DB']])\n"
            "        child.wait()\n"
            "        self.assertEqual(os.environ['MODE'], os.environ.get('EXPECT', 'test'))\n")
        self.declare_here()
        recorded = board._environment_identity(self.root, self.code)["sha256"]
        for expect, label in (("test", "success"), ("nope", "failure")):
            candidate = execution_identity.run_candidate_identity("abc" + label, "def" + label)
            certification = {"board_root": self.root, "candidate": candidate, "environment_sha256": recorded, "lockfile_digests": {},
                             "role": "delivery", "gate": "final:::", "retry_reason": "", "task": "T"}
            import os as _os
            _os.environ["EXPECT"] = expect
            self.addCleanup(_os.environ.pop, "EXPECT", None)
            try:
                board._execute_internal_qa("python3 -m unittest tests.test_spawn", self.root, certification=certification)
            except ValueError:
                pass   # the failing run is refused; its audit is stored all the same
        audits = list((board.board_dir(self.root) / "execution-audits").glob("*.json"))
        self.assertTrue(audits, "a certified run stores its process audit")
        stored = b"".join(path.read_bytes() for path in audits)
        self.assertIn(b"time.sleep(2)", stored, "the child process really was observed")
        self.assertNotIn(PASSWORD.encode(), stored)

    def test_the_reviewer_capture_command_runs_in_the_same_environment_and_scrubs(self):
        from harness import board, control
        self.declare_here()
        session = control.create(self.root, "claude_reviewer")
        reviewer = board.register(self.root, "qa", "REVIEW_QUEUE", vendor="Anthropic", session_id=session["id"])
        result = board.test_env_run(self.root, reviewer["id"], "", "python3 -m unittest tests.test_probe -v")
        self.assertIn("NAME third party CWD backend", result["output"])
        self.assertNotIn(PASSWORD, result["output"])
        with self.assertRaisesRegex(ValueError, "shell control"):
            board.test_env_run(self.root, reviewer["id"], "", "python3 -c 1; echo x")


class PublicPathTests(_execute_once.ExecuteOnceTests):
    """Codex round 1: the public execute-challenge path and the stored process audit."""

    def setUp(self):
        super().setUp()
        self._env_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._env_tmp.cleanup)
        self.env_dir = make_env(Path(self._env_tmp.name), {"thirdparty_demo": {"thirdparty_demo/__init__.py": "NAME = 'x'\n"}})

    def declare(self, mode):
        import json
        board.test_env_set(self.root, json.dumps({"python": str(self.env_dir / "bin" / "python3"), "workdir": ".", "env": {"MODE": mode}}), who="the CTO")

    def test_a_changed_configuration_never_reuses_the_old_certified_pass(self):
        self.declare("test")
        qa, request, _, _ = self._claimed_review()
        first = board.execute_challenge(self.root, qa["id"], request["id"])
        self.assertNotEqual(first.get("status"), "already_certified")
        self.assertEqual(board.execute_challenge(self.root, qa["id"], request["id"])["status"], "already_certified", "nothing changed: the shortcut still works")
        self.declare("must-fail-now")
        with self.assertRaisesRegex(ValueError, "changed after this was requested"):
            board.execute_challenge(self.root, qa["id"], request["id"])

    def test_the_stored_process_audit_never_holds_the_secret(self):
        resolved = test_env.Resolved(python="p", python_dir="d", env_root="e", workdir=".", variables={}, secrets=[PASSWORD, URL])
        scrub = lambda text: test_env.scrub(text, resolved)
        audit = {"processes": [{"pid": 7, "argv": ["python3", "-c", f"connect('{URL}')", PASSWORD]}], "problems": [f"saw {PASSWORD}"], "timed_out": False}
        cleaned = certified_execution._scrubbed_tree(audit, scrub)
        self.assertNotIn(PASSWORD, repr(cleaned))
        self.assertEqual(cleaned["processes"][0]["pid"], 7)
        self.assertEqual(cleaned["timed_out"], False)
        for failing in (False, True):
            stored = certified_execution._write_process_audit(self.root, cleaned | {"timed_out": failing})
            self.assertNotIn(PASSWORD.encode(), Path(stored["path"]).read_bytes())


for _name in [n for n in dir(_execute_once.ExecuteOnceTests) if n.startswith("test_")]:
    setattr(PublicPathTests, _name, None)



if __name__ == "__main__":
    unittest.main()
