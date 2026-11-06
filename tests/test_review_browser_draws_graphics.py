# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The review browser draws a page's WebGL graphics, and outside map tiles are not a reason for NOT TESTED (2026-11-05).

Second stranger run: the delivered weather app drew its area map with WebGL over map tiles from another site. The harness
browser was launched with graphics off and refuses every outside host, so the Reviewer's captures showed a blank rectangle;
three of six final review rounds ended "not tested" for that one cause and the round limit started a fresh builder attempt.
Tested here: the real browser launch gives a loopback page a working WebGL context when the browser is not confined (it
keeps graphics off inside the harness's confinement, where software WebGL cannot start), and the rule texts the agents read
say a blank outside background is the harness's limit.

Run:  PYTHONPATH=. python3 -m unittest tests.test_review_browser_draws_graphics -v
"""
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from harness import browser_acceptance, platform_support
from tests.environment_support import require_loopback

ROOT = Path(__file__).resolve().parents[1]
PAGE = b"""<html><body><div id="r"></div><script>
const gl = document.createElement('canvas').getContext('webgl');
document.getElementById('r').textContent = 'WEBGL_' + (gl ? 'WORKS' : 'MISSING');
</script></body></html>"""


class _Page(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers(); self.wfile.write(PAGE)

    def log_message(self, *args):
        pass


class TheReviewBrowserDrawsGraphics(unittest.TestCase):
    def setUp(self):
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        require_loopback()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Page)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.runtime = tempfile.TemporaryDirectory()
        self.addCleanup(self.runtime.cleanup)

    def dump(self) -> tuple[str, list[str]]:
        """Launch the acceptance browser once on the page; return what it printed and the arguments it was started with."""
        process = browser_acceptance.launch(f"http://127.0.0.1:{self.server.server_address[1]}/", Path(self.runtime.name),
                                            extra_args=["--dump-dom", "--virtual-time-budget=4000"], capture_output=True, one_shot=True)
        try:
            out, _ = process.process.communicate(timeout=60)
        finally:
            process.close()
        return (out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out)), list(process.process.args)

    def test_a_page_s_webgl_drawing_works_in_the_real_launch(self):
        if platform_support.browser_host().confined_by_harness():
            raise unittest.SkipTest("inside the harness's confinement graphics stay off (software WebGL needs Chrome's own sandbox)")
        printed, args = self.dump()
        self.assertIn("--enable-unsafe-swiftshader", args)
        self.assertNotIn("--disable-gpu", args)
        self.assertIn("WEBGL_WORKS", printed, printed[-400:])

    def test_inside_the_harness_s_confinement_graphics_stay_off(self):
        host = platform_support.browser_host()
        real = browser_acceptance.subprocess.Popen
        seen: list[list[str]] = []

        def refuse_the_browser(command, *args, **kwargs):
            """Let the launch's own helper commands run; stop only the browser itself, keeping its arguments."""
            if isinstance(command, list) and "--headless=new" in command:
                seen.append(command)
                raise RuntimeError("not started in this test")
            return real(command, *args, **kwargs)

        with patch.object(type(host), "confined_by_harness", return_value=True), \
                patch.object(browser_acceptance.subprocess, "Popen", side_effect=refuse_the_browser):
            with self.assertRaises(RuntimeError):
                browser_acceptance.launch(f"http://127.0.0.1:{self.server.server_address[1]}/", Path(self.runtime.name), one_shot=True)
        self.assertEqual(len(seen), 1)
        args = seen[0]
        self.assertIn("--disable-gpu", args)
        self.assertNotIn("--enable-unsafe-swiftshader", args)


MAP_PAGE = b"""<html><body><h1>Area map</h1><div id="gl"></div><div id="tile"></div><button id="go">Show labels</button><div id="labels"></div>
<script>
const gl = document.createElement('canvas').getContext('webgl');
document.getElementById('gl').textContent = 'WEBGL_' + (gl ? 'WORKS' : 'MISSING');
const tile = new Image();                       // the map background comes from another site, like a real tile layer
tile.onerror = () => { document.getElementById('tile').textContent = 'TILE_MISSING'; };
tile.src = 'http://203.0.113.9/tiles/5/16/11.png';
fetch('http://203.0.113.9/style.json').catch(() => {});
document.getElementById('go').onclick = () => { document.getElementById('labels').textContent = 'LABELS 8 C 6 km/h rain 0.0 mm'; };
</script></body></html>"""


class _MapPage(_Page):
    def do_GET(self):
        self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers(); self.wfile.write(MAP_PAGE)


class AMapWithOutsideTilesCanBeCheckedToTheEnd(unittest.TestCase):
    """Codex R1-1: working graphics were not enough, because the script runner failed the step as soon as the page asked
    another site for a tile. The whole walk is run here: page load, the tile and style requests refused, a click, the labels."""

    def setUp(self):
        try:
            browser_acceptance.resolve_binary()
        except (FileNotFoundError, ValueError) as error:
            raise unittest.SkipTest(str(error)) from error
        if platform_support.browser_host().confined_by_harness():
            raise unittest.SkipTest("inside the harness's confinement graphics stay off")
        require_loopback()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _MapPage)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.runtime = tempfile.TemporaryDirectory()
        self.addCleanup(self.runtime.cleanup)

    def test_the_walk_passes_the_map_is_drawn_and_the_missing_tiles_are_noted_once(self):
        from harness import screen_script
        url = f"http://127.0.0.1:{self.server.server_address[1]}/"
        steps = screen_script.parse_steps(json.dumps([
            {"open": url}, {"expect": "WEBGL_WORKS"}, {"expect": "TILE_MISSING"},     # the refused requests have settled here
            {"click": {"text": "Show labels"}}, {"expect": "LABELS 8 C 6 km/h rain 0.0 mm"}]))
        result = screen_script.run_script(url, steps, Path(self.runtime.name))
        self.assertTrue(result["all_passed"], [(s.get("ok"), s.get("reason")) for s in result["steps"]])
        self.assertEqual(result["violations"], [])
        self.assertEqual(result["notes"], ["not shown in this capture: the page asked 203.0.113.9 for a file (the harness browser loads nothing from other servers)"])
        self.assertTrue(result["pictures"][-1], "the final picture was captured")


class TheAgentsAreToldOutsideTilesAreNotAGap(unittest.TestCase):
    def test_the_rendered_proof_chapter_and_the_reviewer_card_say_so(self):
        chapter = " ".join((ROOT / "directives" / "AGENT.md").read_text(encoding="utf-8").split())
        self.assertIn("The harness browser loads nothing from outside this computer", chapter)
        self.assertIn("not the app's defect and not a reason for NOT TESTED", chapter)
        card = (ROOT / "directives" / "cards" / "REVIEWER.md").read_text(encoding="utf-8")
        self.assertIn("Outside tiles or pictures the harness browser never loads are not such a step", card)


if __name__ == "__main__":
    unittest.main()
