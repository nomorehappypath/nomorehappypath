# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""What an agent may WRITE, judged by IDENTITY rather than by shape.

Three versions of this failed review, and the third failed in BOTH directions at
once — over-granting any sibling directory while under-granting the harness's own
adopted-project storage. That is what showed the APPROACH was wrong: "beside the
project" describes the harness's task workspace and someone else's project
equally well, so position cannot separate them.

The harness assigns and records each project's storage. The question is not
"does this path look like storage" but "IS this the storage this project was
given".
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from harness import project_context, project_registry
from harness.agent_grant import GrantTooBroad, agent_writable_roots


def default_layout(tmp: Path):
    project = tmp / "parent" / "proj"
    project.mkdir(parents=True, exist_ok=True)
    context = project_context.project_context(project)
    Path(context.data_root).mkdir(parents=True, exist_ok=True)
    Path(context.workspace_root).mkdir(parents=True, exist_ok=True)
    return project, Path(context.data_root), Path(context.workspace_root)


def adopted_layout(tmp: Path):
    """Storage under the MANAGER HOME, deliberately outside the repository."""
    home = tmp / "manager-home"; home.mkdir(exist_ok=True)
    code = tmp / "someones" / "repo"; code.mkdir(parents=True, exist_ok=True)
    data = home / "projects" / "repo" / "data"; data.mkdir(parents=True, exist_ok=True)
    workspace = home / "projects" / "repo" / "workspaces"; workspace.mkdir(parents=True, exist_ok=True)
    project_registry.save(home, {
        "version": project_registry.REGISTRY_VERSION,
        "projects": [{"id": "p1", "name": "repo", "code_root": str(code),
                      "data_root": str(data), "workspace_root": str(workspace)}],
    })
    return home, code, data, workspace


class LegitimateLayoutsTests(unittest.TestCase):
    """Under-granting breaks real launches, which is how the last version failed."""

    def test_the_default_layout_is_granted(self):
        with tempfile.TemporaryDirectory() as tmp:
            project, data, workspace = default_layout(Path(tmp))
            granted = agent_writable_roots(project, data, workspace, project)
        self.assertEqual(len(granted), 3)

    def test_the_ADOPTED_layout_under_the_manager_home_is_granted(self):
        """The reviewer's second blocking finding: this was refused."""
        with tempfile.TemporaryDirectory() as tmp:
            home, code, data, workspace = adopted_layout(Path(tmp))
            granted = agent_writable_roots(code, data, workspace, code, home)
        self.assertIn(str(data.resolve()), granted)
        self.assertIn(str(workspace.resolve()), granted)


class OverGrantTests(unittest.TestCase):
    """Refused wherever a REGISTRY names the project — the real deployment.

    Every case here is exercised against a registered project, because that is
    what an installed harness has. The declared limit for the unregistered case
    is asserted separately below, and stated rather than hidden.
    """

    def test_an_unrelated_SIBLING_project_is_refused(self):
        """The reviewer's first blocking finding: proved writable through the sandbox."""
        with tempfile.TemporaryDirectory() as tmp:
            home, code, data, workspace = adopted_layout(Path(tmp))
            other = Path(tmp) / "someones" / "other-project"; other.mkdir(parents=True)
            with self.assertRaises(GrantTooBroad):
                agent_writable_roots(code, other, workspace, code, home)

    def test_an_unrelated_path_under_the_SAME_manager_home_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, code, data, workspace = adopted_layout(Path(tmp))
            intruder = home / "projects" / "someone-else"; intruder.mkdir(parents=True)
            with self.assertRaises(GrantTooBroad):
                agent_writable_roots(code, intruder, workspace, code, home)

    def test_an_ANCESTOR_of_the_project_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, code, data, workspace = adopted_layout(Path(tmp))
            with self.assertRaises(GrantTooBroad):
                agent_writable_roots(code, code.parent, workspace, code, home)

    def test_a_SYMLINK_resolving_to_an_ancestor_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, code, data, workspace = adopted_layout(Path(tmp))
            innocent = Path(tmp) / "looks-innocent"
            os.symlink(code.parent, innocent)
            with self.assertRaises(GrantTooBroad):
                agent_writable_roots(code, innocent, workspace, code, home)

    def test_every_broad_root_the_reviewer_PROVED_is_refused(self):
        candidates = ["/", "/Volumes", "/private", "/System", "/Library", "/mnt",
                      "/media", "/srv", "/run", "/tmp", "/usr", "/etc",
                      str(Path.home()), str(Path.home() / ".ssh")]
        with tempfile.TemporaryDirectory() as tmp:
            home, code, data, workspace = adopted_layout(Path(tmp))
            checked = 0
            for candidate in candidates:
                if not Path(candidate).exists():
                    continue
                checked += 1
                with self.assertRaises(GrantTooBroad, msg=f"{candidate} was granted"):
                    agent_writable_roots(code, candidate, workspace, code, home)
        self.assertGreater(checked, 5, "too few candidates existed to be meaningful")


class NoUnregisteredLatitudeTests(unittest.TestCase):
    """This class previously asserted the OPPOSITE, and that was the defect.

    It held that an explicitly supplied adopted context is accepted when no
    registry names the project, and called that a declared limit of the design.
    A reviewer showed the limit was the hole: the caller that supplies the roots
    also decides whether a trusted home arrives, so omitting one flag turned the
    documented edge case into the ordinary path.

    The latitude is not needed. `project_registry.register` records data_root
    and workspace_root for every project the product creates OR adopts, so a
    genuine launch always has an assignment to check against. Reaching here with
    explicit roots and no assignment means the launch did not come from the
    harness, which is precisely when it must not be trusted.

    A test that encodes what the code happens to do, rather than what it must
    guarantee, protects the defect. This one now states the guarantee.
    """

    def test_an_explicit_adopted_context_is_REFUSED_when_nothing_registers_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = Path(tmp) / "repo"; code.mkdir()
            data = Path(tmp) / "elsewhere" / "data"; data.mkdir(parents=True)
            workspace = Path(tmp) / "elsewhere" / "ws"; workspace.mkdir(parents=True)
            with self.assertRaises(GrantTooBroad,
                                   msg="unregistered explicit roots were granted"):
                agent_writable_roots(code, data, workspace, code)

    def test_the_SAME_roots_are_granted_once_the_registry_assigns_them(self):
        """The refusal must be about the assignment, not about the shape.

        Without this, the rule above could be satisfied by refusing every
        adopted layout -- which is how an earlier version broke real launches.
        """
        with tempfile.TemporaryDirectory() as tmp:
            home, code, data, workspace = adopted_layout(Path(tmp))
            granted = agent_writable_roots(code, data, workspace, code, home)
        self.assertIn(str(Path(data).resolve()), granted)

    def test_but_a_REGISTERED_project_still_refuses_a_path_it_was_not_given(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, code, data, workspace = adopted_layout(Path(tmp))
            elsewhere = Path(tmp) / "elsewhere"; elsewhere.mkdir()
            with self.assertRaises(GrantTooBroad):
                agent_writable_roots(code, elsewhere, workspace, code, home)


class LauncherTests(unittest.TestCase):
    def test_the_launcher_validates_and_REFUSES_rather_than_narrowing(self):
        script = (Path(__file__).resolve().parent.parent
                  / "scripts" / "run_managed_agent.sh").read_text(encoding="utf-8")
        self.assertIn("agent_writable_roots", script)
        self.assertIn("writable_roots=${writable_roots_json}", script)
        self.assertIn("REFUSED:", script)


if __name__ == "__main__":
    unittest.main()


class PlantedRegistryTests(unittest.TestCase):
    """A registry is authoritative only where the harness said it lives.

    A previous version LOCATED the registry by walking up from the requested
    roots, with a comment claiming that finding one was not trusting it, because
    the entry still had to name this project and this exact path.

    That claim was false, and a reviewer proved it: whoever supplies the bad
    root also supplies the registry beside it, so they write the entry that
    blesses their own path. The same root was refused without the planted
    registry and accepted with it, and the bad root then reached Codex in
    sandbox_workspace_write.writable_roots.

    A false claim in a SECURITY comment is worse than no comment: it stops the
    next reader looking.
    """

    def _planted(self, tmp: Path):
        """A bad root with a registry beside it that blesses the bad root."""
        code = tmp / "repo"; code.mkdir()
        bad = tmp / "somewhere-else"; bad.mkdir()
        workspace = tmp / "ws"; workspace.mkdir()
        project_registry.save(bad, {
            "version": project_registry.REGISTRY_VERSION,
            "projects": [{"id": "x", "name": "repo", "code_root": str(code),
                          "data_root": str(bad), "workspace_root": str(workspace)}],
        })
        return code, bad, workspace

    def test_a_planted_registry_beside_the_bad_root_is_NOT_consulted(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, bad, workspace = self._planted(Path(tmp))
            # A real registry says this project's storage is elsewhere entirely.
            home = Path(tmp) / "real-home"; home.mkdir()
            real_data = home / "data"; real_data.mkdir()
            project_registry.save(home, {
                "version": project_registry.REGISTRY_VERSION,
                "projects": [{"id": "x", "name": "repo", "code_root": str(code),
                              "data_root": str(real_data), "workspace_root": str(workspace)}],
            })
            with self.assertRaises(GrantTooBroad,
                                   msg="a planted registry outranked the real assignment"):
                agent_writable_roots(code, bad, workspace, code, home)

    def test_the_launcher_only_consults_a_home_it_was_TOLD(self):
        """Assert the CALL forwards the home, not that the file mentions it.

        The previous version of this test checked only that `--manager-home`
        and `"$manager_home"` appeared somewhere in the script. Both did, while
        the option parser had no case for the flag and the validator was invoked
        without the home -- so a real launch exited 2 and the security boundary
        existed nowhere but in the argv of its callers. The reviewer caught that
        by running it. A static test that can pass over a broken runner is worth
        less than nothing, because it reads as coverage.
        """
        script = (Path(__file__).resolve().parent.parent
                  / "scripts" / "run_managed_agent.sh").read_text(encoding="utf-8")
        self.assertIn("--manager-home)", script,
                      "the option parser must have a case for the flag")

        invocation = script[script.index("agent_writable_roots(sys.argv"):]
        invocation = invocation[:invocation.index(")\"; then")]
        self.assertIn('"$manager_home"', invocation,
                      "the trusted home must be forwarded to the validator call "
                      "itself, not merely appear elsewhere in the file")
        self.assertIn("sys.argv[6]", invocation,
                      "and the helper must read it as the home argument")

    def test_no_registry_is_discovered_from_the_input(self):
        """The mechanism itself is gone, not merely unused."""
        source = (Path(__file__).resolve().parent.parent
                  / "harness" / "agent_grant.py").read_text(encoding="utf-8")
        self.assertNotIn(".parents]", source,
                         "walking up from a requested root is how the bypass worked")


class RunnerRefusesBeforeInvokingTheAgent(unittest.TestCase):
    """Execute the real runner. Source inspection cannot show what it DOES.

    A reviewer asked for this specifically: the grant helper refusing in
    isolation does not prove the launcher never reaches the CLI. These tests
    run `scripts/run_managed_agent.sh` with a fake agent binary that leaves a
    sentinel behind, so "the agent was not invoked" is an observation rather
    than an assumption.
    """

    RUNNER = Path(__file__).resolve().parent.parent / "scripts" / "run_managed_agent.sh"
    SESSION_ID = "codex_delivery-s1"

    def _world(self, tmp):
        """A registered project whose assigned storage sits OUTSIDE the repo."""
        root = Path(tmp)
        code = root / "project"; code.mkdir()
        data = root / "assigned-data"; data.mkdir()
        workspace = root / "assigned-workspace"; workspace.mkdir()
        home = root / "manager-home"; home.mkdir()
        project_registry.save(home, {
            "version": project_registry.REGISTRY_VERSION,
            "projects": [{"id": "p1", "name": "project", "code_root": str(code),
                          "data_root": str(data), "workspace_root": str(workspace)}],
        })
        # The runner attaches to an EXISTING session before it validates
        # anything, so the session has to be real or the refusal we are trying
        # to observe is never reached. The positive control below is what
        # revealed that; a source-reading test would have missed it.
        from harness import control as control_module
        context = project_context.context_from_roots(
            code_root=code, data_root=data, workspace_root=workspace)
        control_module.restore_missing_resume_session(
            context, self.SESSION_ID, "codex_delivery")

        sentinel = root / "AGENT-WAS-INVOKED"
        fake = root / "fake-codex"
        fake.write_text(f'#!/bin/sh\ntouch "{sentinel}"\n', encoding="utf-8")
        fake.chmod(0o755)
        return code, data, workspace, home, fake, sentinel

    def _run(self, *, code, data, workspace, home, fake, extra=()):
        argv = [
            "bash", str(self.RUNNER),
            "--root", str(code), "--session-id", self.SESSION_ID,
            "--kind", "codex_delivery",
            "--data-root", str(data), "--workspace-root", str(workspace),
            "--python", sys.executable, *extra,
        ]
        if home is not None:
            argv += ["--manager-home", str(home)]
        env = dict(os.environ, HARNESS_CODEX_BIN=str(fake))
        env.pop("PYTHONPATH", None)
        return subprocess.run(argv, capture_output=True, text=True, env=env,
                              stdin=subprocess.DEVNULL, timeout=120)

    def test_the_fake_agent_IS_invoked_for_the_assigned_storage(self):
        """The positive control. Without it, every refusal below proves nothing."""
        with TemporaryDirectory() as tmp:
            code, data, workspace, home, fake, sentinel = self._world(tmp)
            self._run(code=code, data=data, workspace=workspace, home=home, fake=fake)
            self.assertTrue(sentinel.exists(),
                            "the runner never reached the agent even for its OWN assigned "
                            "storage, so this suite cannot detect an invocation at all")

    def test_an_unassigned_root_is_refused_and_the_agent_never_runs(self):
        """The workspace root names another project's folder.

        The DATA root is left assigned on purpose. The runner attaches to
        control state that lives under the data root, so corrupting that one
        stops the launch earlier, at attach, and would prove nothing about the
        grant. This arrangement reaches the grant check and fails there.
        """
        with TemporaryDirectory() as tmp:
            code, data, workspace, home, fake, sentinel = self._world(tmp)
            victim = Path(tmp) / "someone-elses-project"; victim.mkdir()
            done = self._run(code=code, data=data, workspace=victim,
                             home=home, fake=fake)
            self.assertFalse(sentinel.exists(),
                             "the agent was launched with a root outside its grant")
            self.assertEqual(3, done.returncode, done.stderr)
            self.assertIn("REFUSED", done.stderr)

    def test_an_unassigned_data_root_also_never_reaches_the_agent(self):
        """Refused earlier, by a different mechanism -- recorded, not assumed."""
        with TemporaryDirectory() as tmp:
            code, data, workspace, home, fake, sentinel = self._world(tmp)
            victim = Path(tmp) / "elsewhere"; victim.mkdir()
            done = self._run(code=code, data=victim, workspace=workspace,
                             home=home, fake=fake)
            self.assertFalse(sentinel.exists())
            self.assertNotEqual(0, done.returncode)

    def test_suppressing_the_manager_home_fails_CLOSED(self):
        """The reviewer's second finding: omit the flag, reach the looser path.

        There is no looser path now. Whoever supplies the roots also controls
        whether the trusted home arrives, so a missing home must refuse rather
        than relax -- even for roots that WOULD be granted with the home present.
        """
        with TemporaryDirectory() as tmp:
            code, data, workspace, home, fake, sentinel = self._world(tmp)
            done = self._run(code=code, data=data, workspace=workspace,
                             home=None, fake=fake)
            self.assertFalse(sentinel.exists(),
                             "dropping --manager-home let the agent launch unvalidated")
            self.assertEqual(3, done.returncode, done.stderr)


class PlantedRegistryAtRunnerLevel(RunnerRefusesBeforeInvokingTheAgent):
    """The reviewer's exact probe: a registry planted beside the bad root.

    The helper-level test for this already existed. The reviewer asked for it at
    runner level, because the helper refusing in isolation says nothing about
    what the executable actually passes to Codex.
    """

    def test_a_registry_planted_beside_the_bad_root_grants_nothing(self):
        with TemporaryDirectory() as tmp:
            code, data, workspace, home, fake, sentinel = self._world(tmp)
            loot = Path(tmp) / "planted-author" / "loot"
            loot.mkdir(parents=True)
            # The attacker writes the entry that would bless their own path,
            # in a registry beside it. It must never be consulted: the harness
            # did not say this home was trustworthy.
            project_registry.save(loot.parent, {
                "version": project_registry.REGISTRY_VERSION,
                "projects": [{"id": "p1", "name": "project", "code_root": str(code),
                              "data_root": str(data), "workspace_root": str(loot)}],
            })
            done = self._run(code=code, data=data, workspace=loot,
                             home=home, fake=fake)
            self.assertFalse(sentinel.exists(),
                             "a planted registry got the agent launched")
            self.assertEqual(3, done.returncode, done.stderr)
            self.assertIn("REFUSED", done.stderr)
