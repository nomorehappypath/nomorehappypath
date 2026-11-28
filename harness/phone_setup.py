# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""Phone apps, step 1 of 4 (owner direction 2026-11-25): build a small Flutter starter app and run it on a
simulated iPhone, proving this Mac's build environment is ready before a mobile task's contract is shown.

Six steps, each able to stop the whole check with its own named card (the owner's own table): Xcode and
Flutter both answer; a simulated iPhone exists; it starts; a starter app builds for it; it installs, opens
and is still running a moment later with one picture kept; then cleanup. NMHP never creates or deletes a
simulated phone - it only ever boots one that already exists, shuts it down again only when it is the one
it itself booted, and removes only its own temporary folder and the app it installed. One phone operation
runs at a time (`_LOCK`), and this module is the single place both the contract-stage setup check
(`harness/environment_check.py`) and the real proof script (`scripts/phone_proof.py`) call `check()` from -
never a copy, never a stand-in. NMHP runs it itself (never a private agent of its own), but every
Xcode/Flutter/simulator call goes through the SAME OS write confinement a managed agent runs under
(CTO finding, owner release repair 2026-11-26: a real project's setup check failed `flutter --version`
with "Operation not permitted" on Flutter's own cache inside that confinement; `check()`'s `confine`
parameter, built from `mobile_extra_writable()`, is how both callers prove the confined path actually
works, instead of only running it open and missing that exact failure).
"""
from __future__ import annotations

import base64
import contextlib
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

from harness import owner_env

STEP_SECONDS = 600       # one attempt's budget for a boot, build or open step
_LOCK = threading.Lock()  # one phone operation at a time in this process


@contextlib.contextmanager
def _phone_lock(env: dict[str, str]):
    """One phone operation at a time on this Mac, across processes: the board's setup checks and an agent's own
    `phone_proof.py` run in different processes, and one's cleanup shut the simulator down under the other ("Unable to
    lookup in current state: Shutdown", owner's path run 2026-11-27). The lock file sits in CoreSimulator's folder,
    which the board and every agent may write."""
    folder = Path(env.get("HOME") or Path.home()) / "Library" / "Developer" / "CoreSimulator"
    with _LOCK:
        try:
            folder.mkdir(parents=True, exist_ok=True)
            handle = open(folder / ".nmhp-phone.lock", "a+")
        except OSError:
            yield           # no shared folder: the in-process lock still holds
            return
        with handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def mobile_extra_writable(env: dict[str, str]) -> list[str]:
    """The extra roots an agent's write confinement must grant for Flutter/Xcode to work at all (CTO finding,
    owner release repair 2026-11-26: a real project's setup check failed `flutter --version` with "Operation
    not permitted" on the Flutter SDK's own cache, because the agents' protected area only grants the project
    and the CLI's own state). Returns Flutter's resolved SDK root (found from `flutter` on PATH; empty if not
    installed) plus the fixed home folders a simulator build writes: CoreSimulator state, Xcode's DerivedData,
    and Dart/Flutter's own pub cache and tool-state folders. Only paths that already exist, or whose parent
    exists, are returned, so this never grants a guess at a path nothing on this Mac actually uses."""
    home = Path(env.get("HOME") or Path.home())
    roots: list[str] = []
    flutter_bin = shutil.which("flutter", path=env.get("PATH"))
    if flutter_bin:
        flutter_root = Path(flutter_bin).resolve().parent.parent
        if flutter_root.is_dir():
            roots.append(str(flutter_root))
    for candidate in (
        home / "Library" / "Developer" / "CoreSimulator",
        home / "Library" / "Developer" / "Xcode" / "DerivedData",
        home / ".pub-cache",
        home / ".dart-tool",
        home / ".dartServer",  # `flutter analyze` (Dart's analysis server); test567 2026-11-27: "Operation not permitted"
        home / ".cocoapods",
        home / "Library" / "Caches" / "CocoaPods",
    ):
        if candidate.is_dir() or candidate.parent.is_dir():
            roots.append(str(candidate))
    try:
        roots.extend(str(item) for item in home.glob(".flutter*"))
    except OSError:
        pass
    return roots


def host_env() -> dict[str, str]:
    """The owner's real environment (his login shell's PATH, Homebrew included) - the same source
    harness/environment_check.py reads - so xcodebuild/flutter/xcrun found on the owner's own PATH are
    found here too, even though this check runs from a background process whose own inherited PATH is
    the bare minimum (CTO triage 2026-11-25: `shutil.which` on the raw process PATH missed Flutter at
    /opt/homebrew/bin, reporting "not installed" when it plainly was)."""
    try:
        return owner_env.read()
    except owner_env.OwnerEnvError:
        return dict(os.environ)


def _tail(path: Path, limit: int = 4000) -> str:
    """The end of a log file, read fresh (never raises)."""
    try:
        return path.read_text(errors="replace")[-limit:]
    except OSError:
        return ""


def _run(argv: list[str], cwd: str | None = None, timeout: int = STEP_SECONDS, env: dict[str, str] | None = None,
         log_file: Path | None = None, confine: Callable[[list[str]], list[str]] | None = None) -> tuple[bool, str, bool]:
    """One subprocess call, in `env` (the owner's own environment by default); never raises. Returns (ok, combined
    stdout+stderr, timed_out) - a timeout is never reported as the tool missing (CTO triage 2026-11-25: Flutter's
    own first-time engine setup can run well past a short timeout; that is not "not installed").

    `confine`, when given, wraps `argv` in the same OS write confinement a managed agent runs under (CTO finding,
    owner release repair 2026-11-26: the proof must run Flutter the way agents do, not outside their protected
    area, so a confinement problem reaches the owner through this same check rather than only in real agent use).

    Run in its own process group (`start_new_session`) and, on a timeout, the WHOLE group is killed, not just the
    direct child (CTO triage 2026-11-25, round 2: `flutter` can start its own background `dart` process; killing
    only the parent left that child running and holding flutter's own startup lock, hanging every later attempt
    at the full timeout instead of answering). With `log_file` (CTO triage 2026-11-25, round 3: a silent 600 s
    hang gave no clue what it was doing), output goes straight to that file instead of a pipe, so whatever the
    tool had printed - even verbose, in-progress output - can still be read after a kill, not just what a pipe
    happened to buffer."""
    real_argv = confine(argv) if confine else argv
    out = log_file.open("w") if log_file else subprocess.PIPE
    try:
        process = subprocess.Popen(real_argv, cwd=cwd, stdout=out, stderr=subprocess.STDOUT, text=True,
                                   env=env or host_env(), start_new_session=True)
    except OSError as error:
        return False, str(error), False
    finally:
        if log_file:
            out.close()   # the child already has its own duplicated handle open on the file
    try:
        if log_file:
            process.wait(timeout=timeout)
            return process.returncode == 0, _tail(log_file), False
        output, _ = process.communicate(timeout=timeout)
        return process.returncode == 0, output or "", False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            process.kill()
        if log_file:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            partial = _tail(log_file)
        else:
            try:
                partial, _ = process.communicate(timeout=5)
            except Exception:  # noqa: BLE001 - best effort to recover whatever was already printed
                partial = ""
        return False, (partial or "") + f"\n(did not finish within {timeout} seconds; it and anything it started were ended)", True


def _last_line(text: str) -> str:
    """The last non-blank line of `text`, trimmed to 300 characters; "" when `text` has no such line."""
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    return (lines[-1] if lines else "")[:300]


def _last_lines(text: str, count: int = 15) -> str:
    """The last `count` non-blank lines of `text`, joined with newlines; "" when `text` has none."""
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    return "\n".join(lines[-count:])


TOOL_ANSWER_SECONDS = 600   # flutter's own first-time engine setup can run well past a short timeout; still
                            # inside the owner's own "up to 30 minutes in all" budget for the whole six-step check


def xcode_ready(env: dict[str, str], confine: Callable[[list[str]], list[str]] | None = None) -> tuple[bool, bool, str]:
    """`xcodebuild -version` answers (step 1, first half), confined the way an agent would run it when `confine`
    is given. Returns (ready, timed_out, output)."""
    if shutil.which("xcodebuild", path=env.get("PATH")) is None:
        return False, False, ""
    ok, output, timed_out = _run(["xcodebuild", "-version"], timeout=TOOL_ANSWER_SECONDS, env=env, confine=confine)
    return ok, timed_out, output


def flutter_ready(env: dict[str, str], confine: Callable[[list[str]], list[str]] | None = None) -> tuple[bool, bool, str]:
    """`flutter --version` answers (step 1, second half), confined the way an agent would run it when `confine`
    is given. Returns (ready, timed_out, output). Verbose, and logged straight to a file (CTO triage 2026-11-25,
    round 3), so a silent hang still leaves a trail of what it was last doing, read back here even after it is
    killed."""
    if shutil.which("flutter", path=env.get("PATH")) is None:
        return False, False, ""
    with tempfile.TemporaryDirectory(prefix="nmhp-flutter-check-") as tmp:
        ok, output, timed_out = _run(["flutter", "--verbose", "--version"], timeout=TOOL_ANSWER_SECONDS, env=env,
                                     log_file=Path(tmp) / "flutter-version.log", confine=confine)
    return ok, timed_out, output


def find_iphone_simulator(env: dict[str, str], confine: Callable[[list[str]], list[str]] | None = None) -> dict[str, Any] | None:
    """One of the owner's own simulated iPhones (step 2); an already-booted one is preferred. None when there is none yet."""
    ok, output, _timed_out = _run(["xcrun", "simctl", "list", "devices", "available", "--json"], timeout=60, env=env, confine=confine)
    if not ok:
        return None
    try:
        data = json.loads(output)
    except ValueError:
        return None
    best = None
    for runtime, devices in (data.get("devices") or {}).items():
        match = re.search(r"iOS[- ]([\d-]+)", runtime)
        version = match.group(1).replace("-", ".") if match else runtime
        for device in devices or []:
            if "iPhone" not in str(device.get("name") or "") or not device.get("isAvailable", True):
                continue
            candidate = {"udid": device["udid"], "name": device["name"], "version": version,
                         "booted": device.get("state") == "Booted"}
            if candidate["booted"]:
                return candidate
            best = best or candidate
    return best


def boot_simulator(udid: str, env: dict[str, str], confine: Callable[[list[str]], list[str]] | None = None) -> tuple[bool, str]:
    """Start a simulated iPhone that already exists, with no window (step 3)."""
    ok, output, _timed_out = _run(["xcrun", "simctl", "boot", udid], timeout=STEP_SECONDS, env=env, confine=confine)
    if not ok and "already booted" in output.lower():
        ok = True
    return ok, _last_line(output)


def shutdown_simulator(udid: str, env: dict[str, str], confine: Callable[[list[str]], list[str]] | None = None) -> tuple[bool, str]:
    """Shut down a simulated iPhone NMHP itself booted (step 6, second half). Returns (ok, last line)
    instead of ignoring a failed shutdown (CTO finding B1, review round 1, 2026-11-26)."""
    ok, output, _timed_out = _run(["xcrun", "simctl", "shutdown", udid], timeout=60, env=env, confine=confine)
    return ok, _last_line(output)


def _disable_swift_package_manager(project: Path) -> None:
    """Add `flutter: config: enable-swift-package-manager: false` to the starter app's own pubspec.yaml
    (CTO finding, owner release repair 2026-11-26: Xcode's Swift Package Manager starts its own
    sandbox-exec, and macOS refuses a sandbox inside a sandbox, so SPM can never resolve while the build
    runs inside the agents' confinement; CocoaPods has no such conflict). Per-project, never a global
    `flutter config` change. No-op if the key is already present."""
    pubspec = project / "pubspec.yaml"
    text = pubspec.read_text(encoding="utf-8")
    if "enable-swift-package-manager" in text:
        return
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.rstrip("\n") == "flutter:":
            lines.insert(index + 1, "  config:\n    enable-swift-package-manager: false\n")
            pubspec.write_text("".join(lines), encoding="utf-8")
            return
    pubspec.write_text(text + "\nflutter:\n  config:\n    enable-swift-package-manager: false\n", encoding="utf-8")


def build_starter_app(work: Path, env: dict[str, str], confine: Callable[[list[str]], list[str]] | None = None) -> tuple[bool, str, str]:
    """`flutter create` then `flutter build ios --simulator` (step 4). Returns (ok, detail, .app path).

    CTO finding, owner release repair 2026-11-26 (attempt 2): the pubspec flag alone came too late -
    `flutter create` already writes a Swift Package reference into the generated Xcode project before
    anything in pubspec.yaml exists to read. `FLUTTER_SWIFT_PACKAGE_MANAGER=false` is flutter_tools' own
    documented environment override for this feature, read whenever pubspec.yaml has no `config` entry
    yet (true for `create`); it is scoped to this one subprocess call, never the owner's global Flutter
    settings. The pubspec edit still runs too, so the build step reads the same answer from the project
    itself once pubspec.yaml exists."""
    project = work / "nmhp_phone_proof"
    flutter_env = dict(env, FLUTTER_SWIFT_PACKAGE_MANAGER="false")
    ok, output, timed_out = _run(["flutter", "create", "--org", "com.nomorehappypath", "--project-name", "nmhp_phone_proof", str(project)], env=flutter_env, confine=confine)
    if not ok:
        return False, (f"`flutter create` did not answer within {STEP_SECONDS} seconds" if timed_out else _last_line(output) or "`flutter create` did not finish"), ""
    try:
        _disable_swift_package_manager(project)
    except OSError as error:
        return False, f"pubspec.yaml could not be updated to use CocoaPods instead of Swift Package Manager: {error}", ""
    pbxproj = project / "ios" / "Runner.xcodeproj" / "project.pbxproj"
    try:
        has_swift_package = "XCSwiftPackageProductDependency" in pbxproj.read_text(encoding="utf-8")
    except OSError:
        has_swift_package = False
    if has_swift_package:
        return False, ("the starter app's Xcode project still references a Swift Package "
                       "(XCSwiftPackageProductDependency); Swift Package Manager must be off before the build, "
                       "since macOS refuses to run its own sandbox-exec inside the agents' confinement"), ""
    ok, output, timed_out = _run(["flutter", "build", "ios", "--simulator", "--debug"], cwd=str(project), env=flutter_env, confine=confine)
    if not ok:
        return False, (f"`flutter build ios --simulator` did not answer within {STEP_SECONDS} seconds" if timed_out
                       else "\n".join(output.strip().splitlines()[-10:]) or "`flutter build ios --simulator` did not finish"), ""
    apps = list((project / "build" / "ios" / "iphonesimulator").glob("*.app"))
    if not apps:
        return False, "the build produced no .app bundle for the simulator", ""
    return True, "", str(apps[0])


def _bundle_id(app_path: str, env: dict[str, str], confine: Callable[[list[str]], list[str]] | None = None) -> str:
    """The built app's bundle identifier, read from its Info.plist; a fixed fallback id when that fails."""
    ok, output, _timed_out = _run(["/usr/libexec/PlistBuddy", "-c", "Print :CFBundleIdentifier", str(Path(app_path) / "Info.plist")], timeout=30, env=env, confine=confine)
    return output.strip() if ok and output.strip() else "com.nomorehappypath.nmhpPhoneProof"


def install_open_capture(udid: str, app_path: str, picture: Path, env: dict[str, str],
                         confine: Callable[[list[str]], list[str]] | None = None) -> tuple[bool, str]:
    """Install the starter app, open it, wait, and keep one picture of it running (step 5)."""
    ok, output, _timed_out = _run(["xcrun", "simctl", "install", udid, app_path], env=env, confine=confine)
    if not ok:
        return False, _last_line(output) or "the starter app could not be installed"
    bundle_id = _bundle_id(app_path, env, confine)
    ok, output, _timed_out = _run(["xcrun", "simctl", "launch", udid, bundle_id], env=env, confine=confine)
    if not ok:
        return False, _last_line(output) or "the starter app could not be opened"
    pid_match = re.search(r":\s*(\d+)", output)
    time.sleep(3)
    if pid_match and not _run(["kill", "-0", pid_match.group(1)], env=env, confine=confine)[0]:
        return False, "the starter app was no longer running a few seconds after it opened"
    ok, output, _timed_out = _run(["xcrun", "simctl", "io", udid, "screenshot", str(picture)], timeout=60, env=env, confine=confine)
    if not ok or not picture.exists():
        return False, _last_line(output) or "the picture of the starter app could not be taken"
    return True, ""


def cleanup(udid: str, app_path: str, work: Path, started_by_us: bool, env: dict[str, str],
           confine: Callable[[list[str]], list[str]] | None = None) -> tuple[bool, str]:
    """Step 6: always run, success or failure. Removes only what NMHP itself installed or booted. Returns
    (ok, detail): a failed uninstall, a failed shutdown, or a folder that could not be removed used to be
    silently swallowed, so the proof said cleanup passed even though it had not (CTO finding B1, review
    round 1, 2026-11-26); `detail` names the first of those that failed."""
    ok = True
    detail = ""
    if udid and app_path:
        uninstall_ok, output, _timed_out = _run(["xcrun", "simctl", "uninstall", udid, _bundle_id(app_path, env, confine)], timeout=30, env=env, confine=confine)
        if not uninstall_ok:
            ok = False
            detail = _last_line(output) or "the starter app could not be uninstalled"
    if udid and started_by_us:
        shutdown_ok, shutdown_last_line = shutdown_simulator(udid, env, confine)
        if not shutdown_ok:
            ok = False
            detail = detail or shutdown_last_line or "the simulator could not be shut down"
    try:
        shutil.rmtree(work)
    except FileNotFoundError:
        pass
    except OSError as error:
        ok = False
        detail = detail or f"the temporary folder could not be removed: {error}"
    return ok, detail


def check(progress: Callable[[str], None] | None = None,
         confine: Callable[[list[str]], list[str]] | None = None) -> dict[str, Any]:
    """Run steps 1-6 for real (each subprocess call's own errors are caught; cleanup always runs, in `finally`, even
    if something above it raises). Returns {"ok", "label", "detail", "steps": [{"name","ok","seconds"}, ...],
    and, only when ok: "phone_name", "phone_version", "picture_png_base64"}. `label`/`detail` match the owner's own
    card table exactly for the first step that fails. One phone operation at a time across the whole process.

    `confine`, when given, wraps every Xcode/Flutter/simulator call in the same OS write confinement a managed
    agent runs under (CTO finding, owner release repair 2026-11-26: the proof must run Flutter the way agents
    do, not outside their protected area); the caller builds it from `mobile_extra_writable()`'s roots."""
    note = progress or (lambda _text: None)
    steps: list[dict[str, Any]] = []
    # Mutated in place (never reassigned) so `finally`, below, can still fold a failed cleanup into
    # whatever result a `return result` already handed back (CTO finding B1, review round 1, 2026-11-26).
    result: dict[str, Any] = {"ok": False, "label": "", "detail": "", "steps": steps}

    def record(name: str, ok: bool, started: float) -> None:
        """Append one step's pass/fail and elapsed seconds to `steps`, timed from `started`."""
        steps.append({"name": name, "ok": ok, "seconds": round(time.time() - started, 1)})

    def fail(label: str, detail: str) -> dict[str, Any]:
        """Set `result` to a failing outcome with this `label`/`detail` and return it."""
        result.update({"ok": False, "label": label, "detail": detail})
        return result

    env = host_env()
    with _phone_lock(env):
        work = Path(tempfile.mkdtemp(prefix="nmhp-phone-proof-"))
        udid, app_path, started_by_us = "", "", False
        try:
            note("Checking the phone setup: building a starter app for iPhone. A first build can take several minutes.")
            started = time.time()
            xcode_ok, xcode_timed_out, xcode_output = xcode_ready(env, confine)
            if not xcode_ok:
                record("xcode_and_flutter", False, started)
                if xcode_timed_out:
                    return fail("Xcode did not answer in time",
                                f"`xcodebuild -version` did not answer within {TOOL_ANSWER_SECONDS} seconds. Last lines:\n"
                                f"{_last_lines(xcode_output)}\nPress Check again to retry.")
                return fail("Xcode is not ready",
                            "Install Xcode from the App Store, open it once and accept its terms, then press Check again.")
            flutter_ok, flutter_timed_out, flutter_output = flutter_ready(env, confine)
            if not flutter_ok:
                record("xcode_and_flutter", False, started)
                if flutter_timed_out:
                    # Honest, not "not installed" (CTO triage 2026-11-25): Flutter's own first-time engine setup can
                    # genuinely take several minutes; a slow answer is not the same thing as a missing program. Its
                    # own verbose last lines (round 3) say what it was doing when it was ended, not just that it was.
                    # The owner's own Mac once hung here behind an unapproved macOS "open this app?" prompt for
                    # dart; the same sentence names that as the likely cause, without reading any quarantine mark.
                    return fail("Flutter did not answer in time",
                                f"`flutter --verbose --version` did not answer within {TOOL_ANSWER_SECONDS} seconds "
                                f"(it may still be doing its own first-time setup). Last lines:\n{_last_lines(flutter_output)}\n"
                                "macOS may be asking whether to open Flutter. Look for that window and click Open, "
                                "or run flutter --version once in Terminal, then press Check again.")
                return fail("Flutter is not installed",
                            "Open Terminal, run `brew install --cask flutter`, then press Check again.")
            record("xcode_and_flutter", True, started)

            started = time.time()
            device = find_iphone_simulator(env, confine)
            record("simulator_exists", bool(device), started)
            if not device:
                return fail("no simulated iPhone",
                            "Open Xcode, Settings, Components, add an iOS simulator, then press Check again.")
            udid = device["udid"]

            started = time.time()
            if device["booted"]:
                record("simulator_start", True, started)
            else:
                ok, last_line = boot_simulator(udid, env, confine)
                if not ok:
                    ok, last_line = boot_simulator(udid, env, confine)   # tried a second time before any card
                record("simulator_start", ok, started)
                if not ok:
                    return fail("the simulated iPhone did not start", f"{last_line} Press Check again to retry.")
                started_by_us = True

            started = time.time()
            ok, detail, app_path = build_starter_app(work, env, confine)
            if not ok:
                ok, detail, app_path = build_starter_app(work, env, confine)   # tried a second time before any card
            record("build", ok, started)
            if not ok:
                return fail("the starter app did not build for iPhone", detail)

            started = time.time()
            picture = work / "starter-app.png"
            ok, detail = install_open_capture(udid, app_path, picture, env, confine)
            if not ok:
                if "Shutdown" in detail:
                    # another program (an agent's own phone run) shut the phone down under this check: start it again
                    # (owner's path run 2026-11-27), and shut it down afterwards as this check's own
                    started_by_us = boot_simulator(udid, env, confine)[0] or started_by_us
                ok, detail = install_open_capture(udid, app_path, picture, env, confine)   # tried a second time before any card
            record("open", ok, started)
            if not ok:
                return fail("the starter app did not open", f"{detail} Press Check again to retry.")

            picture_base64 = base64.b64encode(picture.read_bytes()).decode("ascii") if picture.exists() else ""
            result.update({"ok": True, "label": "iPhone",
                            "detail": f"a Flutter starter app was built, installed and opened on the simulated phone "
                                      f"{device['name']} ({device['version']})",
                            "phone_name": device["name"], "phone_version": device["version"],
                            "picture_png_base64": picture_base64})
            return result
        finally:
            cleanup_started = time.time()
            cleanup_ok, cleanup_detail = cleanup(udid, app_path, work, started_by_us, env, confine)
            steps.append({"name": "cleanup", "ok": cleanup_ok, "seconds": round(time.time() - cleanup_started, 1)})
            if not cleanup_ok and result.get("ok"):
                # Steps 1-5 passed but cleanup itself failed: the proof must say so, not report a pass
                # it did not earn (CTO finding B1, review round 1, 2026-11-26).
                result.update({"ok": False, "label": "the phone setup did not clean up",
                               "detail": (f"{cleanup_detail} " if cleanup_detail else "") + "Press Check again to retry."})
