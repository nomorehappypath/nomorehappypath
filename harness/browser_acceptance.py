# Copyright (c) 2026 KpiMinds LLC. Licensed under the Business Source License 1.1; see LICENSE.
"""Process-isolated browser acceptance without touching the owner's browser."""
from __future__ import annotations

import hashlib
import fcntl
import json
import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


LIVE_HARNESS_PORTS = {8740, 8742}
_IDENTITIES: dict[tuple[str, int, int], dict[str, str]] = {}
_PROCESS_LOCK = threading.Lock()
_UNSAFE_EXTRA_ARG_PREFIXES = (
    "--headless", "--user-data-dir", "--disk-cache-dir",
    "--crash-dumps-dir", "--remote-debugging-port", "--password-store",
    "--use-mock-keychain",
)


def _safe_binary(candidate: str | os.PathLike[str] | None) -> str | None:
    if not candidate:
        return None
    path = Path(candidate).expanduser()
    if not path.is_file() or not os.access(path, os.X_OK):
        return None
    resolved = path.resolve()
    if any(part.lower().endswith(".app") for part in resolved.parts):
        return None
    return str(resolved)


def resolve_binary() -> str:
    configured = os.environ.get("HARNESS_BROWSER_BIN", "").strip() or os.environ.get("CHROME_BIN", "").strip()
    if configured:
        selected = _safe_binary(configured)
        if selected:
            return selected
        raise ValueError("the configured acceptance browser must be executable and outside every macOS .app bundle")
    cache = Path.home() / "Library" / "Caches" / "ms-playwright"
    candidates = sorted(
        cache.glob("chromium_headless_shell-*/chrome-headless-shell-mac-*/chrome-headless-shell"),
        reverse=True,
    )
    candidates.extend(shutil.which(name) for name in ("chrome-headless-shell", "chromium", "chromium-browser"))
    for candidate in candidates:
        selected = _safe_binary(candidate)
        if selected:
            return selected
    raise FileNotFoundError("no process-isolated Chromium headless binary is available")


def _release_launch_lock(lock_handle) -> None:
    """Release both launch locks, tolerating a half-built state.

    Called only from the launch failure path, where the file handle may never
    have been opened. Releasing the in-process lock is the part that must
    always happen: without it every later launch blocks forever.
    """
    if lock_handle is not None:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        except (OSError, ValueError):
            pass
        try:
            lock_handle.close()
        except OSError:
            pass
    _PROCESS_LOCK.release()


def _binary_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def browser_identity(binary: str) -> dict[str, str]:
    path = Path(_safe_binary(binary) or "")
    if not path.is_file():
        raise ValueError("acceptance browser identity requires a safe executable")
    stat = path.stat()
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    if key in _IDENTITIES:
        return dict(_IDENTITIES[key])
    version = subprocess.run(
        [str(path), "--version"], capture_output=True, text=True, timeout=10,
    )
    identity = {
        "path": str(path),
        "sha256": _binary_digest(path),
        "version": (version.stdout or version.stderr).strip(),
        "application_id": "chromium-headless-shell",
        "mode": "headless",
    }
    _IDENTITIES[key] = identity
    return dict(identity)


class ProcessTableUnavailable(OSError):
    """The OS process table could not be read at all.

    Deliberately an OSError. Callers that already tolerate an OSError from these
    routines - the ownership observer at :211 keeps polling, release_preview's
    liveness check treats it as "not our process" - must keep behaving exactly
    as they did, or this refactor would silently kill a background thread while
    claiming to change nothing.

    Process identity is evidence, not decoration: without it, ownership of a
    launched process cannot be proven and no execution certificate is honest.
    A restricted environment that forbids running ``ps`` therefore produces
    this named refusal rather than a raw OSError from the middle of an
    evidence routine - and never an empty table, which would read as "nothing
    was running".
    """


def _run_ps(arguments: list[str], *, check: bool) -> subprocess.CompletedProcess:
    """Run ps, translating an unrunnable ps into one named condition."""
    try:
        return subprocess.run(
            ["ps", *arguments], capture_output=True, text=True, check=check,
        )
    except OSError as error:
        raise ProcessTableUnavailable(
            f"cannot read the process table: 'ps' could not be executed ({error})"
        ) from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or "").strip() or f"exit status {error.returncode}"
        raise ProcessTableUnavailable(
            f"cannot read the process table: 'ps' failed ({detail})"
        ) from error


def _start_token(pid: int) -> str:
    # A non-zero exit means that pid is gone, which is an ANSWER, not a
    # failure - it stays an empty token exactly as before.
    result = _run_ps(["-p", str(pid), "-o", "lstart="], check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def _process_table() -> dict[int, dict[str, Any]]:
    result = _run_ps(["-axo", "pid=,ppid=,pgid=,lstart=,command="], check=True)
    table: dict[int, dict[str, Any]] = {}
    for raw in result.stdout.splitlines():
        parts = raw.strip().split(None, 8)
        if len(parts) != 9:
            continue
        try:
            pid, ppid, pgid = (int(parts[index]) for index in range(3))
        except ValueError:
            continue
        table[pid] = {
            "pid": pid, "ppid": ppid, "pgid": pgid,
            "start_token": " ".join(parts[3:8]), "command": parts[8],
        }
    return table


def _is_browser_app_command(command: str) -> bool:
    """Identify a top-level browser app, excluding its normal helper apps."""
    value = str(command or "").lower()
    marker = ".app/contents/macos/"
    marker_at = value.find(marker)
    if marker_at < 0:
        return False
    bundle_prefix = value[:marker_at]
    # Chrome/Safari helpers are nested app bundles below the owner app's
    # Contents directory. Tabs may create and retire these at any time; they
    # are not evidence that the owner browser itself restarted.
    if ".app/contents/" in bundle_prefix:
        return False
    bundle_name = bundle_prefix.rsplit("/", 1)[-1]
    if "remote desktop" in bundle_name or "chromeremotedesktop" in bundle_name:
        return False
    return any(name in bundle_name for name in (
        "chrome", "chromium", "safari", "firefox", "arc", "brave", "edge", "opera",
    ))


def _app_processes(table: dict[int, dict[str, Any]]) -> dict[int, dict[str, str]]:
    return {
        pid: {
            "command": str(value["command"]),
            "start_token": str(value.get("start_token") or ""),
        }
        for pid, value in table.items()
        if _is_browser_app_command(str(value.get("command", "")))
    }


def _default_handlers_digest() -> str:
    if sys_platform() != "darwin":
        return "not-macos"
    result = subprocess.run(
        ["defaults", "read", "com.apple.LaunchServices/com.apple.launchservices.secure", "LSHandlers"],
        capture_output=True,
    )
    return hashlib.sha256(result.stdout + result.stderr).hexdigest()


def _record_owned(
    identities: dict[int, dict[str, Any]], table: dict[int, dict[str, Any]], pgid: int,
) -> None:
    """Retain process identities even after helpers re-parent or change group."""
    changed = True
    while changed:
        changed = False
        known = set(identities)
        for pid, value in table.items():
            if pid in identities:
                continue
            if value.get("pgid") != pgid and value.get("ppid") not in known:
                continue
            identities[pid] = dict(value)
            changed = True


def _observe_owned(
    identities: dict[int, dict[str, Any]], stopped: threading.Event, pgid: int,
) -> None:
    while not stopped.wait(0.02):
        try:
            _record_owned(identities, _process_table(), pgid)
        except (OSError, subprocess.SubprocessError):
            # The final synchronous scan remains authoritative. Observation
            # failure is surfaced by absent ownership evidence at close.
            continue


def sys_platform() -> str:
    import sys
    return sys.platform


def _validate_url(url: str) -> int:
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("browser acceptance requires a loopback HTTP URL")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("browser acceptance URL has an invalid port") from error
    if not port:
        raise ValueError("browser acceptance requires an explicit private server port")
    if port in LIVE_HARNESS_PORTS:
        raise ValueError("browser acceptance cannot target a live owner harness port")
    return port


@dataclass
class BrowserProcess:
    process: subprocess.Popen
    runtime: Path
    identity: dict[str, str]
    pid: int
    start_token: str
    pgid: int
    baseline_apps: dict[int, dict[str, str]]
    baseline_handlers: str
    url: str
    owned_identities: dict[int, dict[str, Any]]
    observer_stop: threading.Event
    observer: threading.Thread
    lock_handle: Any
    process_lock_held: bool = True
    _closed: bool = False

    def poll(self):
        return self.process.poll()

    def wait(self, timeout: float | None = None):
        return self.process.wait(timeout=timeout)

    def communicate(self, timeout: float | None = None):
        return self.process.communicate(timeout=timeout)

    def _release_serialization(self) -> None:
        if not getattr(self.lock_handle, "closed", True):
            fcntl.flock(self.lock_handle.fileno(), fcntl.LOCK_UN)
            self.lock_handle.close()
        if self.process_lock_held:
            _PROCESS_LOCK.release()
            self.process_lock_held = False

    def close(self, *, validate: bool = True) -> dict[str, Any]:
        if self._closed:
            return json.loads((self.runtime / "acceptance-audit.json").read_text(encoding="utf-8"))
        try:
            before_cleanup = _process_table()
            _record_owned(self.owned_identities, before_cleanup, self.pgid)
            current_token = str(before_cleanup.get(self.pid, {}).get("start_token") or _start_token(self.pid))
            signals: list[dict[str, Any]] = []
            if self.process.poll() is None:
                if not current_token or current_token != self.start_token:
                    raise RuntimeError("browser PID identity changed before cleanup; refusing to signal it")
                try:
                    os.killpg(self.pgid, signal.SIGTERM)
                    signals.append({"signal": "SIGTERM", "pgid": self.pgid,
                                    "owned_pids": sorted(self.owned_identities)})
                except ProcessLookupError:
                    pass
                try:
                    self.process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(self.pgid, signal.SIGKILL)
                        signals.append({"signal": "SIGKILL", "pgid": self.pgid,
                                        "owned_pids": sorted(self.owned_identities)})
                    except ProcessLookupError:
                        pass
                    self.process.wait(timeout=5)
            self.observer_stop.set()
            self.observer.join(timeout=2)
            _record_owned(self.owned_identities, _process_table(), self.pgid)
            # Helpers can re-parent or create a new process group. Only terminate a
            # PID whose immutable start token is the one observed while it was
            # descended from the acceptance launch.
            for pid, identity in sorted(self.owned_identities.items()):
                if pid == self.pid:
                    continue
                current = _process_table().get(pid)
                if not current or current.get("start_token") != identity.get("start_token"):
                    continue
                try:
                    os.kill(pid, signal.SIGTERM)
                    signals.append({"signal": "SIGTERM", "pid": pid,
                                    "start_token": identity.get("start_token")})
                except ProcessLookupError:
                    pass
            after = _process_table()
            new_apps = {
                pid: identity for pid, identity in _app_processes(after).items()
                if pid not in self.baseline_apps
            }
            owner_changes = []
            for pid, identity in self.baseline_apps.items():
                current = after.get(pid)
                if not current:
                    owner_changes.append({"pid": pid, "change": "process exited",
                                          "start_token": identity.get("start_token", "")})
                elif current.get("start_token") != identity.get("start_token"):
                    owner_changes.append({"pid": pid, "change": "PID was restarted",
                                          "start_token": identity.get("start_token", "")})
            audit = {
                "version": 1, "browser": self.identity, "url": self.url,
                "spawn": {"pid": self.pid, "start_token": self.start_token, "pgid": self.pgid},
                "owned_processes_observed": list(self.owned_identities.values()), "signals": signals,
                "new_app_bundle_processes": new_apps,
                "owner_browser_baseline_changes": owner_changes,
                "default_handlers_unchanged": _default_handlers_digest() == self.baseline_handlers,
                "mock_keychain": True,
            }
            (self.runtime / "acceptance-audit.json").write_text(
                json.dumps(audit, indent=2, sort_keys=True) + "\n", encoding="utf-8",
            )
            self._closed = True
            if validate:
                if new_apps:
                    detail = "; ".join(
                        f"{pid}: {identity['command']}" for pid, identity in sorted(new_apps.items())
                    )
                    raise RuntimeError(
                        "an independently observed macOS app-bundle process was launched during browser acceptance: "
                        + detail
                    )
                if owner_changes:
                    raise RuntimeError(
                        "owner browser baseline changed during acceptance; attribution is inconclusive"
                    )
                if not audit["default_handlers_unchanged"]:
                    raise RuntimeError("the default-browser handler changed during browser acceptance")
            return audit
        finally:
            self.observer_stop.set()
            self.observer.join(timeout=2)
            self._release_serialization()

    def __enter__(self) -> "BrowserProcess":
        return self

    def __exit__(self, exc_type, exc, _traceback) -> None:
        try:
            self.close(validate=exc is None)
        except Exception:
            if exc is None:
                raise


def launch(
    url: str, runtime: Path, *, width: int = 1400, height: int = 1000,
    extra_args: list[str] | None = None, capture_output: bool = False,
) -> BrowserProcess:
    _validate_url(url)
    extra_args = list(extra_args or [])
    unsafe = [
        value for value in extra_args
        if any(value == prefix or value.startswith(prefix + "=") for prefix in _UNSAFE_EXTRA_ARG_PREFIXES)
    ]
    if unsafe:
        raise ValueError("browser acceptance security arguments cannot be overridden: " + ", ".join(unsafe))
    binary = resolve_binary()
    identity = browser_identity(binary)
    if _binary_digest(Path(binary)) != identity.get("sha256"):
        raise RuntimeError("acceptance browser binary changed after identity certification")
    runtime = Path(runtime).resolve()
    for name in ("home", "profile", "cache", "crash", "tmp", "output"):
        (runtime / name).mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ)
    environment.update({
        "HOME": str(runtime / "home"), "TMPDIR": str(runtime / "tmp"),
        "XDG_CACHE_HOME": str(runtime / "cache"), "XDG_CONFIG_HOME": str(runtime / "home" / ".config"),
    })
    args = [
        binary, "--headless=new", "--disable-gpu", "--no-first-run",
        "--no-default-browser-check", "--disable-extensions", "--disable-sync",
        "--disable-background-networking", "--password-store=basic", "--use-mock-keychain",
        f"--user-data-dir={runtime / 'profile'}", f"--disk-cache-dir={runtime / 'cache'}",
        f"--crash-dumps-dir={runtime / 'crash'}", "--remote-debugging-port=0",
        f"--window-size={int(width)},{int(height)}", *extra_args, url,
    ]
    # Everything from here to the successful return holds two locks. Any escape
    # that does not release BOTH deadlocks every later launch in this process:
    # a failure between acquiring and returning used to leak them, so the next
    # caller blocked forever on _PROCESS_LOCK.acquire(). One handler now covers
    # every exit, including the ones that were never anticipated - an
    # unreadable process table among them.
    _PROCESS_LOCK.acquire()
    lock_path = Path(os.environ.get("TMPDIR") or "/tmp") / "harness-browser-acceptance.lock"
    lock_handle = None
    process = None
    observer_stop: threading.Event | None = None
    observer: threading.Thread | None = None
    try:
        lock_handle = lock_path.open("a+")
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        baseline = _process_table()
        process = subprocess.Popen(
            args, env=environment, start_new_session=True,
            stdout=subprocess.PIPE if capture_output else subprocess.DEVNULL,
            stderr=subprocess.PIPE if capture_output else subprocess.DEVNULL,
            text=capture_output,
        )
        start_token = ""
        for _ in range(20):
            start_token = _start_token(process.pid)
            if start_token:
                break
            time.sleep(0.01)
        if not start_token:
            raise RuntimeError("could not record browser process start identity")
        owned_identities: dict[int, dict[str, Any]] = {
            process.pid: {
                "pid": process.pid, "ppid": os.getpid(), "pgid": os.getpgid(process.pid),
                "start_token": start_token, "command": binary,
            },
        }
        observer_stop = threading.Event()
        observer = threading.Thread(
            target=_observe_owned,
            args=(owned_identities, observer_stop, os.getpgid(process.pid)),
            name=f"harness-browser-observer-{process.pid}", daemon=True,
        )
        observer.start()
        return BrowserProcess(
            process=process, runtime=runtime, identity=identity, pid=process.pid,
            start_token=start_token, pgid=os.getpgid(process.pid),
            baseline_apps=_app_processes(baseline), baseline_handlers=_default_handlers_digest(), url=url,
            owned_identities=owned_identities, observer_stop=observer_stop,
            observer=observer, lock_handle=lock_handle,
        )
    except BaseException:
        if observer_stop is not None:
            observer_stop.set()
        if observer is not None:
            observer.join(timeout=1)
        if process is not None:
            try:
                process.kill()
            except OSError:
                pass
        _release_launch_lock(lock_handle)
        raise


def dump_dom(url: str, runtime: Path, *, timeout: float = 30) -> str:
    session = launch(url, runtime, extra_args=["--dump-dom"], capture_output=True)
    try:
        stdout, stderr = session.communicate(timeout=timeout)
        if session.process.returncode != 0:
            raise RuntimeError(f"browser DOM capture failed: {stderr[-500:]}")
        return stdout
    finally:
        session.close()
