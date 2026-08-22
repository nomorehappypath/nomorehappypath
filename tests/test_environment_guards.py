# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Executable invariant: every socket-binding test class runs the loopback guard.

A text scanner once claimed this and was wrong - a duplicated setUp() let a
later definition silently overwrite the guarded one. This test asks Python
itself: for every TestCase whose source binds a loopback server, the RESOLVED
setUp (after any overwrites, through the MRO) must invoke require_loopback().
"""
from __future__ import annotations

import importlib
import inspect
import re
import unittest
from pathlib import Path

BINDING = re.compile(r"ThreadingHTTPServer|HTTPServer\(|serve_forever|\.bind\(")


class EnvironmentGuardInvariantTests(unittest.TestCase):
    def test_every_socket_binding_test_class_runs_the_loopback_guard(self):
        offenders = []
        for path in sorted(Path(__file__).parent.glob("test_*.py")):
            if path.name == Path(__file__).name:
                continue
            module = importlib.import_module(f"tests.{path.stem}")
            for name, cls in vars(module).items():
                if not (isinstance(cls, type) and issubclass(cls, unittest.TestCase)):
                    continue
                if cls.__module__ != module.__name__:
                    continue  # imported, judged in its own module
                try:
                    source = inspect.getsource(cls)
                except (OSError, TypeError):
                    continue
                if not BINDING.search(source):
                    continue
                setup = getattr(cls, "setUp", None)
                guarded = False
                if setup is not None:
                    try:
                        guarded = "require_loopback" in inspect.getsource(setup)
                    except (OSError, TypeError):
                        guarded = False
                if not guarded:
                    # method-level guards count when every binding method has one
                    methods = [m for n, m in vars(cls).items() if n.startswith("test") and callable(m)]
                    binding_methods = [m for m in methods if BINDING.search(inspect.getsource(m))]
                    if binding_methods and all(
                        "require_loopback" in inspect.getsource(m) for m in binding_methods
                    ):
                        guarded = True
                if not guarded:
                    offenders.append(f"{path.name}:{name}")
        self.assertEqual(offenders, [], f"unguarded socket-binding classes: {offenders}")


if __name__ == "__main__":
    unittest.main()
