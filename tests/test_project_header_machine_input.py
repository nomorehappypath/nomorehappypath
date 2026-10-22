# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The header's Test machine link driven the way a person drives it: a real mouse click, Tab then Enter, and a live poll failure
(adopted from the independent reviewer's checks of task 2026-10-21_project-header-test-machine, which passed on macOS and Linux).

Machine inputs removed: the owner's HOME, ~/.ssh and any ssh config (no ssh runs; the descriptor is written as Connect leaves it).

Run:  PYTHONPATH=. python3 -m unittest tests.test_project_header_machine_input -v
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from harness import browser_acceptance as ba, test_machine
from tests import test_project_header_machine_rendered as rendered
from tests.test_test_machine import connected_descriptor

LINK = "document.querySelector('#project-test-machine')"
OPEN_DIALOG = "Array.from(document.querySelectorAll('dialog')).filter(d => d.open).map(d => d.innerText).join(' ')"


class HeaderMachineInputTests(rendered.HeaderMachineButtonTests):
    def browser(self, url, width=390):
        profile = Path(tempfile.mkdtemp())
        process = ba.launch(url, profile, width=width, height=1000)
        self.addCleanup(process.close)
        port, path = ba._devtools(profile, time.monotonic() + 30, process.process)
        driver = ba._DevToolsSocket(port, path, 20)
        self.addCleanup(driver.close)
        return driver

    def js(self, driver, expression):
        result = driver.call("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True})
        self.assertNotIn("exceptionDetails", result, result)
        return result.get("result", {}).get("value")

    def wait(self, driver, expression):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.js(driver, expression):
                return
            time.sleep(0.1)
        self.fail("Timed out: " + expression + " | body=" + str(self.js(driver, "document.body.innerText"))[:500])

    def key(self, driver, key, code, vk):
        for kind in ("keyDown", "keyUp"):
            driver.call("Input.dispatchKeyEvent", {"type": kind, "key": key, "code": code, "windowsVirtualKeyCode": vk})

    def click_header_link_and_read_dialog(self, name):
        manager_url, _manager = self.serve_manager()
        driver = self.browser(self.serve_board(name, manager_url))
        self.wait(driver, f"{LINK}?.textContent === 'Test machine: not connected'")
        point = self.js(driver, f"(() => {{ const a = {LINK}; a.scrollIntoView({{block: 'center'}}); const b = a.getBoundingClientRect(); return {{x: b.x + b.width / 2, y: b.y + b.height / 2}}; }})()")
        for kind in ("mousePressed", "mouseReleased"):
            driver.call("Input.dispatchMouseEvent", dict(type=kind, button="left", clickCount=1, **point))
        self.wait(driver, f"{OPEN_DIALOG}.includes('Test machine — {name}')")
        body = self.js(driver, OPEN_DIALOG)
        self.assertIn("No machine connected yet.", body)
        other = "alpha" if name == "beta" else "beta"
        self.assertNotIn("Test machine — " + other, body)

    def test_a_real_click_on_the_header_link_opens_the_form_of_that_project_alpha(self):
        self.click_header_link_and_read_dialog("alpha")

    def test_a_real_click_on_the_header_link_opens_the_form_of_that_project_beta(self):
        self.click_header_link_and_read_dialog("beta")

    def test_tab_reaches_the_link_and_enter_opens_the_form(self):
        manager_url, _manager = self.serve_manager()
        driver = self.browser(self.serve_board("beta", manager_url), 1400)
        self.wait(driver, f"{LINK}?.textContent.includes('not connected')")
        self.js(driver, "document.querySelector('.project-nav a:last-child').focus()")
        for _ in range(15):
            self.key(driver, "Tab", "Tab", 9)
            if self.js(driver, f"document.activeElement?.id === 'project-test-machine'"):
                break
        self.assertTrue(self.js(driver, "document.activeElement?.id === 'project-test-machine'"), "Tab never reached the link")
        self.key(driver, "Enter", "Enter", 13)
        self.wait(driver, f"{OPEN_DIALOG}.includes('Test machine — beta')")

    def test_a_failed_poll_resets_the_label_and_recovery_restores_it(self):
        manager_url, manager = self.serve_manager()
        context = self.context("alpha")
        connected_descriptor(context, identity=manager.machine_identity, baseline_state="complete")
        driver = self.browser(self.serve_board("alpha", manager_url))
        self.wait(driver, f"{LINK}?.textContent === 'Test machine: connected u@127.0.0.1'")
        self.js(driver, "window.keptFetch = window.fetch; window.fetch = () => Promise.reject(new Error('temporary outage')); refresh()")
        self.wait(driver, f"{LINK}?.textContent === 'Test machine'")
        self.js(driver, "window.fetch = window.keptFetch; refresh()")
        self.wait(driver, f"{LINK}?.textContent === 'Test machine: connected u@127.0.0.1'")
        test_machine.mark_reauth(context, "project_stopped")
        self.wait(driver, f"{LINK}?.textContent === 'Test machine: needs you'")
        self.assertFalse(self.js(driver, "document.querySelector('#machine-card').hidden"), "the existing card still appears")


# the inherited rendered tests run in tests.test_project_header_machine_rendered; only this class's own tests run here
for _name in [n for n in dir(rendered.HeaderMachineButtonTests) if n.startswith("test_") and n not in HeaderMachineInputTests.__dict__]:
    setattr(HeaderMachineInputTests, _name, None)


if __name__ == "__main__":
    unittest.main()
