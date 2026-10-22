# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""A real, unprivileged sshd on a high port plus a password-prompting ssh stand-in.

The key-only half of a Connect (host-key scan, key login, wrong key) runs against the REAL sshd and the REAL ssh.
The password half cannot: an unprivileged sshd cannot check a password. So the test uses a stand-in `ssh` program
(HARNESS_TEST_MACHINE_SSH) that behaves like ssh at the password prompt (it asks on the controlling terminal with echo off,
compares with a secret kept OUTSIDE the scanned folders, then runs the remote command locally against a fake home that the
real sshd also reads authorized_keys from) and hands every key-only (BatchMode=yes) call to the real ssh.
Machine inputs the tests remove: the owner's HOME, ~/.ssh, any ssh agent, any ssh config (-F /dev/null is in the arguments).
"""
from __future__ import annotations

import getpass
import os
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

SFTP_SERVER = next((path for path in ("/usr/libexec/sftp-server", "/usr/lib/openssh/sftp-server", "/usr/libexec/openssh/sftp-server", "/usr/lib/ssh/sftp-server") if Path(path).exists()), "")
SSHD = shutil.which("sshd") or ("/usr/sbin/sshd" if Path("/usr/sbin/sshd").exists() else "")

SHIM = r'''#!/usr/bin/env python3
import os, sys, termios, json, subprocess, base64, re
here = os.path.dirname(os.path.abspath(__file__))
args = sys.argv[1:]
if "BatchMode=yes" in args:
    os.execvp("ssh", ["ssh"] + args)
log = open(os.path.join(here, "shim.log"), "a")
log.write(json.dumps({"argv": args, "env_keys": sorted(os.environ), "env": dict(os.environ)}) + "\n"); log.flush()
mode = open(os.path.join(here, "shim.mode")).read().strip() if os.path.exists(os.path.join(here, "shim.mode")) else "posix"
secret_file = open(os.path.join(here, "shim.secretpath")).read().strip()
if mode == "no-password-login":
    sys.stderr.write("user@host: Permission denied (publickey).\n"); sys.exit(255)
if mode == "code-prompt":
    tty = open("/dev/tty", "r+b", buffering=0); tty.write(b"Verification code: "); os.read(tty.fileno(), 10); sys.exit(255)
tty = open("/dev/tty", "r+b", buffering=0)
tty.write(b"user@host's password: ")
fd = tty.fileno(); old = termios.tcgetattr(fd); new = old[:]; new[3] &= ~termios.ECHO; termios.tcsetattr(fd, termios.TCSADRAIN, new)
pw = b""
while not pw.endswith(b"\n"): pw += os.read(fd, 1)
termios.tcsetattr(fd, termios.TCSADRAIN, old); tty.write(b"\r\n")
if pw.strip().decode() != open(secret_file).read().strip():
    sys.stdout.write("Permission denied, please try again.\n"); sys.stdout.flush()
    sys.stderr.write("user@host: Permission denied (publickey,password).\n"); sys.exit(255)
command = args[-1]
home = open(os.path.join(here, "shim.home")).read().strip()
if mode == "windows":
    m = re.search(r"-EncodedCommand (\S+)", command)
    if m:
        script = base64.b64decode(m.group(1)).decode("utf-16-le")
        open(os.path.join(here, "windows_script.ps1"), "w").write(script)
        if "NMHPOS" in script:
            print("NMHPOS:Windows_NT"); sys.exit(0)
        key = re.search(r"\$key = '([^']+)'", script).group(1)
        ak = os.path.join(home, ".ssh", "authorized_keys")
        existing = open(ak).read() if os.path.exists(ak) else ""
        if key not in existing.splitlines():
            open(ak, "a").write(key + "\n")
        print("NMHP_INSTALL_OK admin=True"); sys.exit(0)
    print("NMHPOS:Windows_NT"); sys.exit(0)
env = dict(os.environ, HOME=home)
sys.exit(subprocess.call(["/bin/sh", "-c", command], env=env))
'''


def free_port() -> int:
    """An unused loopback port; NMHP_TEST_PORT_BASE (the shared Linux machine's 187xx range) limits where it looks."""
    base = int(os.environ.get("NMHP_TEST_PORT_BASE", "0") or 0)
    candidates = range(base, base + 100) if base else [0]
    for number in candidates:
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", number))
            except OSError:
                continue
            return sock.getsockname()[1]
    raise RuntimeError("no free test port in the allowed range")


class FakeMachine:
    """Start with `with FakeMachine(test) as machine:`; the real sshd serves `machine.port` as the current user."""

    def __init__(self, test: unittest.TestCase, password: str = "Pw-correct-horse-7391", offer_passwords: bool = False):
        self.test, self.password, self.offer_passwords = test, password, offer_passwords
        self.base = Path(tempfile.mkdtemp(prefix="machine-"))
        test.addCleanup(shutil.rmtree, self.base, True)
        self.secret_dir = Path(tempfile.mkdtemp(prefix="machine-secret-"))   # outside self.base so folder scans never see the secret
        test.addCleanup(shutil.rmtree, self.secret_dir, True)
        self.port = free_port()
        self.user = getpass.getuser()
        self.home = self.base / "home"
        (self.home / ".ssh").mkdir(parents=True)
        os.chmod(self.home / ".ssh", 0o700)
        (self.home / ".ssh" / "authorized_keys").write_text("")
        self.process: subprocess.Popen | None = None
        self.shim = self.base / "ssh"
        self.shim.write_text(SHIM)
        self.shim.chmod(0o755)
        (self.base / "shim.home").write_text(str(self.home))
        (self.secret_dir / "pw").write_text(password)
        (self.base / "shim.secretpath").write_text(str(self.secret_dir / "pw"))
        self.hostkey = self.base / "hostkey"

    @property
    def authorized_keys(self) -> Path:
        return self.home / ".ssh" / "authorized_keys"

    def set_mode(self, mode: str) -> None:
        (self.base / "shim.mode").write_text(mode)

    def shim_log(self) -> list[dict]:
        import json
        path = self.base / "shim.log"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def new_host_key(self) -> None:
        self.stop()
        self.hostkey.unlink(missing_ok=True)
        self.start()

    def start(self) -> None:
        if not self.hostkey.exists():
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.hostkey)], check=True)
        config = self.base / "sshd_config"
        config.write_text(
            f"Port {self.port}\nListenAddress 127.0.0.1\nHostKey {self.hostkey}\nPidFile {self.base / 'sshd.pid'}\n"
            f"AuthorizedKeysFile {self.authorized_keys}\nStrictModes no\nUsePAM no\nPasswordAuthentication {'yes' if self.offer_passwords else 'no'}\n"
            "PubkeyAuthentication yes\nLogLevel ERROR\n" + (f"Subsystem sftp {SFTP_SERVER}\n" if SFTP_SERVER else ""))
        self.process = subprocess.Popen([SSHD, "-D", "-f", str(config), "-E", str(self.base / "sshd.log")],
                                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=1):
                    return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("the test sshd did not start: " + (self.base / "sshd.log").read_text() if (self.base / "sshd.log").exists() else "no log")

    def stop(self) -> None:
        # the sshd this object started, by the pid it returned (never by pattern)
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.process = None

    def __enter__(self) -> "FakeMachine":
        if not SSHD:
            raise unittest.SkipTest("no sshd on this machine")
        self.test.addCleanup(self.stop)
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()
