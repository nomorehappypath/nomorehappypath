# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Owner 2026-10-15 (Studio, 15:46-15:56Z): the CTO pinned "Sign in to Claude inside Studio" for a private
copy Delivery had started under /private/tmp on the owner's own port; the owner could not sign in and lost
ten minutes. A card that sends the owner somewhere must name the place, and the board checks the place
before the card exists: it answers, and it is not an agent's private copy (temp folder, task workspace,
or a child of a running agent session). These tests fail on 2a7e4d6 (the card is accepted).
"""
from __future__ import annotations

import io
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from harness import board, board_viewer, claude_inbox, control, gemini_runtime, owner_location
from tests.environment_support import require_loopback
from tests.test_requirements_contract import context

ROOT = Path(__file__).resolve().parents[1]
STUDIO_TITLE = "Sign in to Claude inside Studio"
STUDIO_WHY = "Delivery's task got 'Claude needs sign-in'. Open Studio and sign in to Claude, then tell Delivery to retry."


def flat(path: str) -> str:
    return " ".join((ROOT / path).read_text(encoding="utf-8").split())


class _Board(unittest.TestCase):
    def setUp(self):
        require_loopback()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = context(Path(self.temporary.name))
        session = control.create(self.root, "claude_cto")
        self.cto = board.register(self.root, "cto", "GLOBAL_MONITOR", vendor="Anthropic", session_id=session["id"])

    def serve(self, cwd: Path) -> int:
        """A real HTTP listener whose working directory is ``cwd``; its port."""
        process = subprocess.Popen(
            [sys.executable, "-u", "-m", "http.server", "0", "--bind", "127.0.0.1"], cwd=str(cwd),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        self.addCleanup(process.wait)
        self.addCleanup(process.terminate)
        line = process.stdout.readline()
        match = re.search(r"port (\d+)", line)
        self.assertIsNotNone(match, line)
        return int(match.group(1))

    def pin(self, title: str, **kw) -> dict:
        return board.record_owner_action(self.root, self.cto["id"], title, **kw)

    def refused(self, title: str, **kw) -> str:
        with self.assertRaises(ValueError) as refused:
            self.pin(title, **kw)
        self.assertFalse(board.snapshot(self.root).get("owner_actions"), "nothing is recorded when a card is refused")
        return str(refused.exception)


class LocationGuardTests(_Board):
    def test_the_studio_card_names_no_place_and_is_refused(self):
        for title, why in (
            (STUDIO_TITLE, STUDIO_WHY), ("Log into Studio", "then retry"), ("Approve access inside Studio", "the app asks"),
            # review round 4: the app's own Settings is not Mission Control's Settings
            (STUDIO_TITLE, "Open Studio's Settings and connect Claude so work can continue"),
            ("Sign in to Claude inside Studio using its Settings page", ""),
            ("Connect Claude in the app's Settings, then tell Delivery", ""),
            # review round 5: a control word beside the unknown app does not resolve it
            ("Sign in to Claude inside Studio using Settings", ""),
            ("Open Studio and use Settings to connect Claude", ""),
        ):
            text = self.refused(title, why=why)
            self.assertTrue(text.startswith("name where the owner goes"), text)
            self.assertIn("a copy an agent started for its own test is never the owner's", text)
        for note in ("OWNER ACTION: sign in to Claude inside Studio, then tell Delivery",
                     "OWNER ACTION: Sign in to Claude inside Studio using its Settings page",
                     "OWNER ACTION: Sign in to Claude inside Studio using Settings"):
            with self.assertRaises(ValueError) as refused:
                board.status(self.root, self.cto["id"], note, "waiting")
            self.assertIn("name where the owner goes", str(refused.exception), note)

    def test_ordinary_act_inside_wording_and_a_bare_url_are_checked_too(self):
        """Review 2026-10-15 round 3: "Open http://… and approve", "Complete the setup in Studio"."""
        port = self.serve(Path(self.temporary.name))
        text = self.refused(f"Open http://127.0.0.1:{port}/ and approve the connection request", why="")
        self.assertIn("private copy", text)
        with self.assertRaises(ValueError) as refused:
            board.status(self.root, self.cto["id"], f"OWNER ACTION: open http://127.0.0.1:{port}/ and approve the connection", "waiting")
        self.assertIn("private copy", str(refused.exception))
        for title, why in (
            ("Complete the setup in Studio", "Approve the connection so work can continue"),
            ("Approve the connection request in the app", ""),
            ("Go to Studio and accept the pairing", ""),
            ("Finish the wizard at the portal", "then tell Delivery"),
        ):
            self.assertIn("name where the owner goes", self.refused(title, why=why), title)
        with self.assertRaises(ValueError):
            board.status(self.root, self.cto["id"], "OWNER ACTION: complete the setup in Studio, then tell Delivery", "waiting")
        owner_port = self.serve(self.root)
        card = self.pin(f"Open http://127.0.0.1:{owner_port}/ and approve the connection request", why="")
        self.assertEqual(card["location"]["urls"][0]["verdict"], "owner")

    def test_the_existing_harness_sign_in_flows_are_untouched(self):
        for title, kw in (
            ("Open the Delivery terminal and run /login", {"why": "Delivery is signed out."}),
            ("Follow the Claude authentication card in Mission Control", {"why": "the token expired"}),
            ("Sign the Reviewer back in: open the Reviewer terminal and type /login", {"why": ""}),
            ("Reviewer is not answering: open its terminal, press Enter once; if blank, type /login", {"why": ""}),
            ("Run the film GPU proof", {"command": "bash /tmp/release_film.sh", "why": "The runtime only runs outside the sandbox."}),
            ("In Terminal run: bash /x/fix.sh then restart the agent once", {"why": ""}),
            ("Clear the saved site permission in Chrome for the local page", {"why": "Delivery's browser was denied once; open Chrome's Site Settings"}),
            ("Open Settings and choose another provider for the Reviewer", {"why": "the quota is out"}),
            ("Sign the Reviewer in again from Mission Control's Settings page", {"why": "the provider changed"}),
        ):
            card = self.pin(title, **kw)
            self.assertNotIn("location", card, title)

    def test_a_private_copy_in_a_temp_folder_is_refused_and_the_owners_instance_is_accepted(self):
        port = self.serve(Path(self.temporary.name))            # a temp folder, outside the project's folder
        text = self.refused(f"Sign in to Claude at http://127.0.0.1:{port}/", why="the app needs you")
        self.assertIn("runs from a temp folder", text); self.assertIn("never send the owner to an agent's copy", text)
        owner_port = self.serve(self.root)                       # the project's own folder
        card = self.pin(f"Sign in to Claude at http://127.0.0.1:{owner_port}/", why="the app needs you")
        self.assertEqual(card["location"]["urls"][0]["verdict"], "owner")
        shown = board_viewer.dashboard_payload(self.root)["state"]["owner_actions"][card["id"]]
        self.assertEqual(shown["location"]["urls"][0]["verdict"], "owner")
        self.assertIn("data-card-location", board_viewer.rendered_page())

    def test_a_listener_started_by_a_running_agent_session_is_refused(self):
        port = self.serve(self.root)
        pid = next(p for p in owner_location.listeners(port)[0])
        parent = owner_location.ancestors(pid)[0]  # this test process
        with control.locked_state(self.root) as state:
            delivery = control.create(self.root, "codex_delivery") if False else None
        session = control.create(self.root, "codex_delivery")
        with control.locked_state(self.root) as state:
            state["sessions"][session["id"]].update({"pid": parent, "status": "running"})
        text = self.refused(f"Sign in at http://127.0.0.1:{port}/", why="x")
        self.assertIn("started by a running agent session", text)
        with control.locked_state(self.root) as state:
            state["sessions"][session["id"]].update({"status": "stopped"})
        card = self.pin(f"Sign in at http://127.0.0.1:{port}/", why="x")
        self.assertEqual(card["location"]["urls"][0]["verdict"], "owner", "an ended session never counts (pid reuse)")

    def test_a_task_workspace_copy_is_refused(self):
        workspace = Path(self.temporary.name) / "candidate"
        workspace.mkdir()
        with board.locked_state(self.root) as state:
            state["task_workspaces"]["TASK-X"] = str(workspace)
        port = self.serve(workspace)
        self.assertIn("task workspace", self.refused(f"Open the app at http://127.0.0.1:{port}/ and sign in", why=""))

    def test_an_unanswering_a_bind_and_an_unverifiable_place_each_get_their_own_refusal(self):
        self.assertIn("name the one the owner opens", self.refused("Sign in at " + " or ".join(f"http://127.0.0.1:{p}/" for p in range(1, 7)), why=""))
        self.assertIn("does not answer", self.refused("Sign in at http://127.0.0.1:1/", why="x"))
        self.assertIn("bind address", self.refused("Sign in at http://0.0.0.0:8900/", why="x"))
        text = self.refused("Sign in at https://studio.example.invalid/login", why="x")
        self.assertIn("could not verify", text); self.assertIn("--location-verified", text)
        card = self.pin("Sign in at https://studio.example.invalid/login", why="x", location_verified="the owner's own hosted Studio, per the project README")
        self.assertEqual(card["location"]["urls"][0]["verdict"], "cannot_verify")
        self.assertEqual(card["location"]["verified_how"], "the owner's own hosted Studio, per the project README")
        with redirect_stdout(io.StringIO()):
            code = board.main(["--root", str(self.root), "owner-action", "--agent", self.cto["id"], "--title", "Sign in at https://studio.example.invalid/login",
                               "--location-verified", "the owner's hosted Studio"])
        self.assertEqual(code, 0)

    def test_a_corrected_destination_replaces_the_open_card_in_place(self):
        """Plan check Part 2, finding 2: the owner never keeps seeing the old URL after the CTO corrects it."""
        first = self.serve(self.root)
        card = self.pin(f"Sign in at http://127.0.0.1:{first}/", why="x", task="")
        second = self.serve(self.root)
        again = self.pin(f"Sign in at http://127.0.0.1:{second}/", why="x", task="")
        self.assertEqual(again["id"], card["id"])
        self.assertIn(str(second), again["location"]["urls"][0]["url"])
        self.assertEqual(again["title"], f"Sign in at http://127.0.0.1:{second}/")
        self.assertTrue(any(e["kind"] == "owner_action_refreshed" for e in board.snapshot(self.root)["events"]))


LOCATION_PROBE = r"""
<script>
(async () => {
  const visible = node => Boolean(node) && node.getBoundingClientRect().width > 0 && node.getBoundingClientRect().height > 0;
  for (let attempt = 0; attempt < 120; attempt++) {
    if (document.querySelector('#owner-actions [data-card-location]')) break;
    await new Promise(resolve => setTimeout(resolve, 150));
  }
  const line = document.querySelector('#owner-actions [data-card-location]');
  await fetch('/__probe__', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({
    visible: visible(line), text: line ? line.innerText : '', fontSize: line ? getComputedStyle(line).fontSize : '',
  })});
})();
</script>
"""


class RenderedLocationTests(_Board):
    """The owner sees what the board checked, on the card itself (real headless Chrome, both platforms)."""

    def setUp(self):
        from harness import browser_acceptance
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()
        super().setUp()

    def test_the_card_shows_the_checked_place_and_the_cto_statement(self):
        import threading, time
        from http.server import ThreadingHTTPServer
        from harness import browser_acceptance
        from tests.test_branding_rendered import probe_proxy
        port = self.serve(self.root)
        self.pin(f"Sign in to Claude at http://127.0.0.1:{port}/", why="the app needs you", location_verified="")
        server = ThreadingHTTPServer(("127.0.0.1", 0), board_viewer.make_handler(self.root, project_name="Location proof"))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        sink: dict = {}
        proxy = ThreadingHTTPServer(("127.0.0.1", 0), probe_proxy(f"http://127.0.0.1:{server.server_address[1]}", sink, LOCATION_PROBE))
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        self.addCleanup(proxy.server_close); self.addCleanup(proxy.shutdown)
        profile = tempfile.TemporaryDirectory(); self.addCleanup(profile.cleanup)
        process = browser_acceptance.launch(f"http://127.0.0.1:{proxy.server_address[1]}/", Path(profile.name), width=1300, height=1000)
        try:
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline and "value" not in sink:
                time.sleep(0.1)
        finally:
            process.close()
        self.assertIn("value", sink, "Chrome reported no probe; a blank render is not a pass")
        reading = sink["value"]
        self.assertTrue(reading["visible"], reading)
        self.assertIn("Checked by the board", reading["text"])
        self.assertIn(f"http://127.0.0.1:{port}/ — the owner's instance", reading["text"])
        self.assertGreaterEqual(float(reading["fontSize"].rstrip("px") or 0), 12.0, "readable at the size it ships")


class LocationDirectiveTests(unittest.TestCase):
    def test_delivery_and_cto_directives_carry_the_rules_under_budget(self):
        agent = flat("directives/AGENT.md")
        self.assertIn("The owner signs in only to the owner's own instance, and your test servers never take the project's port", agent)
        self.assertIn("runs in the owner's real instance", agent)
        self.assertIn("state the exact URL the owner opens and prove it answers", agent)
        self.assertIn("never ask the owner, directly or through the CTO, to sign in to a copy you started for your own test", agent)
        self.assertIn("never takes a port the project's own launcher uses", agent)
        self.assertIn("read that port from the project's start script or docs (Studio: 8900)", agent)
        self.assertIn("picks a free private port, is stopped when the test ends, and your status says which port and folder it used", agent)
        self.assertIn("The same holds for a Reviewer's test servers", agent)
        spawn = flat("harness/directives/00_SPAWN_DEVELOPMENT_DIRECTIVE.md")
        self.assertIn("The owner signs in only to the owner's own instance", spawn)
        self.assertIn("never takes a port the project's own launcher uses", spawn)
        cto = flat("directives/CTO.md")
        self.assertIn("for a sign-in or act-inside card open the URL yourself, never a copy an agent started for its own test", cto)
        mirror = flat("harness/directives/CTO_COMPLETION_DIRECTIVE.md")
        self.assertIn("the same for a sign-in or act-inside card: open the URL yourself, never a copy an agent started for its own test", mirror)
        # Owner 2026-10-15 (Studio 16:37Z): "restarted Studio. Confirmed live" relayed while the process predated the merge.
        # Owner 2026-10-15 (Studio 17:36Z): "in progress ... against fixed commit" relayed for a run that had died with the old process.
        self.assertIn("A status claiming restarted, deployed, live, running or in progress names the evidence you checked in that cycle", cto)
        self.assertIn("a process start later than the change, the served version, the task id with a live process or heartbeat", cto)
        self.assertIn("relay nothing the cycle did not check", cto)
        self.assertIn("A status claiming restarted, deployed, live, running or in progress names the evidence the CTO checked in that cycle", mirror)
        self.assertIn("relay nothing the cycle did not check", mirror)
        text = (ROOT / "directives/CTO.md").read_text(encoding="utf-8")
        size = len(text.encode("utf-8")) + len(claude_inbox.AUTHORITY_NOTE.encode("utf-8")) + 2
        self.assertLessEqual(size, gemini_runtime.RULES_LIMIT_BYTES, f"CTO.md payload {size} bytes exceeds the Gemini rules budget")


if __name__ == "__main__":
    unittest.main()
