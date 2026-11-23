# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""A test's local web server always shuts down (2026-11-22 Linux full-suite hang).

The Linux full suite on 7dba7c1 stopped for 7 hours inside a rendered test's cleanup: `shutdown()` waited for the
`serve_forever` thread, which sat in a blocking `accept()` on a listening socket no client would connect to again.
The tests package now gives every listening socket a short accept timeout, so `serve_forever` gets back to its loop
and sees the shutdown. This reproduces the stuck state on purpose: the selector reports the socket ready when no
connection is waiting, so `accept()` is reached with nothing to accept.
"""
from __future__ import annotations

import selectors
import socketserver
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import tests  # noqa: F401 - the guard under test is installed by the tests package
from tests.environment_support import require_loopback


class _AlwaysReady(selectors.DefaultSelector):
    """Says the listening socket is ready even when nobody connected (the state the Linux run was stuck in)."""

    def select(self, timeout=None):
        return [(key, selectors.EVENT_READ) for key in self.get_map().values()]


class ServerShutdownNeverHangsTests(unittest.TestCase):
    def setUp(self):
        require_loopback()

    def test_shutdown_returns_when_serve_forever_is_inside_accept_with_no_client(self):
        with mock.patch.object(socketserver, "_ServerSelector", _AlwaysReady):
            server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
            serving = threading.Thread(target=server.serve_forever, daemon=True)
            serving.start()
            done = threading.Event()

            def stop():
                server.shutdown()
                done.set()

            threading.Thread(target=stop, daemon=True).start()
            finished = done.wait(10)
            server.server_close()
        self.assertTrue(finished, "shutdown() still waiting: serve_forever is stuck in accept()")

    def test_an_accepted_connection_is_still_a_normal_blocking_socket(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        self.addCleanup(server.server_close)
        import socket
        client = socket.create_connection(server.server_address, timeout=5)
        self.addCleanup(client.close)
        accepted, _ = server.get_request()
        self.addCleanup(accepted.close)
        self.assertIsNone(accepted.gettimeout())


if __name__ == "__main__":
    unittest.main()
