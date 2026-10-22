# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""A program built on the owner's test machine reaches the owner ("Get the build").

Delivery declares the build (plain file names, who it is for and how to try it) before the final review. Its certified
test command copies each output into the machine's retained folder `harness-builds/<task>/` and prints one line per
output into its own certified output: `build: <name> sha256=<64 hex> size=<bytes>`. After the final PASS the harness
fetches exactly those files over the project's key, checks every byte against the certified lines, and keeps the set
in a manager-owned store that no managed agent can write. Mission Control serves it from there, checking the digest
on the descriptor it streams.

This module is the mechanical part: names, the remote commands, the copy, the store, the download. The board decides
when (board.py `collect_build`).
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import time
from pathlib import Path
from typing import Any, Iterator

from harness import project_registry, test_machine

REMOTE_ROOT = "harness-builds"          # relative to the ssh account's home
MAX_OUTPUTS = 10
MAX_BYTES = 200 * 1024 * 1024
MAX_STEPS = 10
TEXT_LIMIT = 2000
REMOTE_SECONDS = 60
COPY_SECONDS = 300
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,99}$")
WINDOWS_RESERVED = {"con", "prn", "aux", "nul", *(f"com{n}" for n in range(1, 10)), *(f"lpt{n}" for n in range(1, 10))}
BUILD_LINE = re.compile(r"(?mi)^[ \t]*build:[ \t]+(\S+)[ \t]+sha256=([0-9a-f]{64})[ \t]+size=(\d+)[ \t]*$")
SMOKE_LINE = re.compile(r"(?mi)^[ \t]*smoke:[ \t]+(\S+)[ \t]+(ok|unverified)\b[: \t]*(.*?)[ \t]*$")
MANIFEST_NAME = "manifest.json"
MARKER = "NMHBUILD:"
# Failure classes: machine = temporary (the machine or the connection), output = the build itself is missing or wrong
# (needs a new certified run), storage = this computer cannot keep it, declaration = the declaration is unusable,
# stale = the machine changed since the review ran (Delivery resubmits the review).
CLASSES = ("machine", "output", "storage", "declaration", "stale")
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
OPERATION_SECONDS = 300
RETRY_SECONDS = 300


class BuildError(ValueError):
    def __init__(self, message: str, klass: str = "output"):
        super().__init__(message)
        self.klass = klass if klass in CLASSES else "output"


# -- declaration -------------------------------------------------------------------------------------
def valid_name(name: Any) -> str:
    value = str(name or "")
    stem = value.split(".", 1)[0].lower()
    if not NAME_PATTERN.fullmatch(value) or value.endswith(".") or stem in WINDOWS_RESERVED or value.lower() == MANIFEST_NAME:
        raise BuildError(
            f"`{value[:60]}` is not a usable output name: one plain file name of letters, digits, dot, dash, plus or underscore "
            "(at most 100 characters, no folders, not starting with a dot, no Windows device names, not `manifest.json`, which the store keeps for itself).", "declaration")
    return value


def _text(label: str, value: Any) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        raise BuildError(f"The build declaration needs {label}.", "declaration")
    if len(text) > TEXT_LIMIT:
        raise BuildError(f"The build declaration's {label} is longer than {TEXT_LIMIT} characters; shorten it.", "declaration")
    return text


def validate_declaration(outputs: list[Any], platform: Any, requirements: Any, steps: list[Any], expect: Any, with_web_app: bool = False) -> dict[str, Any]:
    names = [valid_name(item) for item in outputs or []]
    if not names:
        raise BuildError("The build declaration needs at least one --output <file name>.", "declaration")
    if len(names) > MAX_OUTPUTS:
        raise BuildError(f"A build has at most {MAX_OUTPUTS} outputs; a program that needs more declares one archive it builds itself.", "declaration")
    if len({name.lower() for name in names}) != len(names):
        raise BuildError("Two outputs have the same name (names are compared without regard to upper and lower case).", "declaration")
    cleaned = [_text("a step", step) for step in steps or []]
    if not cleaned or len(cleaned) > MAX_STEPS:
        raise BuildError(f"The build declaration needs 1 to {MAX_STEPS} --step lines: what the owner does, in order, with the exact command.", "declaration")
    return {
        "outputs": [{"name": name} for name in names],
        "platform": _text("--platform (the operating system and processor the program is for)", platform),
        "requirements": _text("--requirements (a privilege or prerequisite the owner needs, or the word none)", requirements),
        "steps": cleaned,
        "expect": _text("--expect (what the owner should see when it works)", expect),
        "with_web_app": bool(with_web_app),
    }


def declaration_digest(declaration: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(declaration, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def declared_names(declaration: dict[str, Any]) -> list[str]:
    return [str(item.get("name") or "") for item in declaration.get("outputs") or []]


def certified_lines(evidence_text: str, declaration: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The `build:` lines of the certified output, one per declared output and nothing else."""
    found: dict[str, dict[str, Any]] = {}
    for name, digest, size in BUILD_LINE.findall(evidence_text or ""):
        entry = {"sha256": digest.lower(), "size": int(size)}
        if name in found and found[name] != entry:
            raise BuildError(f"The certified output names `{name}` twice with different digests; the test command must print one `build:` line per output.")
        found[name] = entry
    declared = declared_names(declaration)
    missing = [name for name in declared if name not in found]
    extra = sorted(set(found) - set(declared))
    if missing or extra:
        raise BuildError(
            "The certified test output does not carry exactly the declared build lines"
            + (f" (missing: {', '.join(missing)})" if missing else "") + (f" (not declared: {', '.join(extra)})" if extra else "")
            + ". The certified command must print `build: <name> sha256=<64 hex> size=<bytes>` for each declared output after it copies them into "
            f"`{REMOTE_ROOT}/<task>/<sha256>/` on the machine; fix the command, declare again if the names changed, and run a new certified review.")
    for name, entry in found.items():
        if entry["size"] > MAX_BYTES:
            raise BuildError(f"`{name}` is {entry['size']} bytes; a build output may be at most {MAX_BYTES // (1024 * 1024)} MiB.")
    return found


def launch_checks(evidence_text: str, declaration: dict[str, Any]) -> dict[str, dict[str, str]]:
    """What the certified run said about launching each output from its retained copy (`smoke: <name> ok` or `smoke: <name> unverified: <why>`).

    Never gates the release; the card tells the owner plainly which outputs were launched on the test machine and which were not.
    """
    found: dict[str, dict[str, str]] = {}
    for name, status, note in SMOKE_LINE.findall(evidence_text or ""):
        found[name] = {"status": status.lower(), "note": " ".join(note.split())[:200]}
    return {name: found.get(name, {"status": "unverified", "note": "the certified run reported no launch check"}) for name in declared_names(declaration)}


# -- the machine ------------------------------------------------------------------------------------
def task_component(task: str) -> str:
    """The task as one folder name that is also safe on Windows (no reserved device names, no trailing dot)."""
    try:
        return valid_name(project_registry.plain_task_id(task))
    except (ValueError, BuildError) as error:
        raise BuildError("This task's id cannot be used as a folder name on a test machine (letters, digits, dot, dash, plus and underscore, not a Windows device name, no trailing dot).", "declaration") from error


def _remote_dir(task: str, sha256: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", str(sha256)):
        raise BuildError("The certified digest is not a SHA-256.", "output")
    return f"{REMOTE_ROOT}/{task_component(task)}/{sha256}"


def _posix_script(task: str, name: str, sha256: str) -> str:
    directory = _remote_dir(task, sha256)
    task_dir = f"{REMOTE_ROOT}/{task_component(task)}"
    root, file = shlex.quote(REMOTE_ROOT), shlex.quote(f"{directory}/{name}")
    folder, parent = shlex.quote(directory), shlex.quote(task_dir)
    return (
        f"if [ -d {root} ] && [ ! -L {root} ] && [ -d {parent} ] && [ ! -L {parent} ] && [ -d {folder} ] && [ ! -L {folder} ] && [ -f {file} ] && [ ! -L {file} ]; then "
        f"size=$(wc -c < {file} | tr -d ' '); "
        f"if command -v sha256sum >/dev/null 2>&1; then sum=$(sha256sum {file}); "
        f"elif command -v shasum >/dev/null 2>&1; then sum=$(shasum -a 256 {file}); "
        f"else echo {MARKER}NOHASH; exit 4; fi; echo \"{MARKER}OK ${{sum%% *}} $size\"; "
        f"else echo {MARKER}MISSING; exit 3; fi")


def _windows_script(task: str, name: str, sha256: str) -> str:
    root = REMOTE_ROOT
    directory = _remote_dir(task, sha256).replace("/", "\\")
    parent = f"{REMOTE_ROOT}\\{task_component(task)}"
    return (
        "$ErrorActionPreference='Stop'\n"
        f"$home1=$env:USERPROFILE; $r=Join-Path $home1 '{root}'; $q=Join-Path $home1 '{parent}'; $d=Join-Path $home1 '{directory}'; $f=Join-Path $d '{name}'\n"
        "foreach ($p in @($r,$q,$d,$f)) {\n"
        f"  if (-not (Test-Path -LiteralPath $p)) {{ Write-Output '{MARKER}MISSING'; exit 3 }}\n"
        f"  if ((Get-Item -LiteralPath $p -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint) {{ Write-Output '{MARKER}LINK'; exit 5 }}\n"
        "}\n"
        f"if ((Get-Item -LiteralPath $f -Force).PSIsContainer) {{ Write-Output '{MARKER}MISSING'; exit 3 }}\n"
        "$h=(Get-FileHash -LiteralPath $f -Algorithm SHA256).Hash.ToLower(); $n=(Get-Item -LiteralPath $f -Force).Length\n"
        f"Write-Output (\"{MARKER}OK \" + $h + \" \" + $n)\n")


def scp_program() -> str:
    return os.environ.get("HARNESS_TEST_MACHINE_SCP", "") or "scp"


def _paths(root) -> dict[str, Path]:
    directory = test_machine.machine_dir(root)
    return {"key": directory / "key", "known_hosts": directory / "known_hosts"}


def _key_options(known_hosts: Path, key: Path) -> list[str]:
    return [
        "-o", "IdentitiesOnly=yes", "-o", f"UserKnownHostsFile={known_hosts}", "-o", "GlobalKnownHostsFile=/dev/null",
        "-o", "StrictHostKeyChecking=yes", "-o", "ControlMaster=no", "-o", "ControlPath=none", "-o", "ForwardAgent=no",
        "-o", "ForwardX11=no", "-o", f"ConnectTimeout={int(max(3, test_machine.PROBE_SECONDS))}", "-o", "LogLevel=ERROR", "-o", "CheckHostIP=no",
        "-o", "BatchMode=yes", "-o", "PreferredAuthentications=publickey", "-o", "PasswordAuthentication=no",
        "-o", "KbdInteractiveAuthentication=no", "-i", str(key),
    ]


def _usable(root) -> dict[str, Any]:
    ok, code, message = test_machine.usability(root)
    if not ok:
        raise BuildError(message or "The test machine is not usable.", "machine")
    return test_machine.read(root) or {}


def _remaining(deadline: float | None, limit: float) -> float:
    if deadline is None:
        return limit
    left = deadline - time.monotonic()
    if left <= 0:
        raise BuildError(f"Collecting the build took longer than {OPERATION_SECONDS} seconds and was stopped; nothing was kept. Check the connection and collect again.", "machine")
    return min(limit, left)


def remote_facts(root, task: str, name: str, sha256: str, deadline: float | None = None) -> tuple[str, int]:
    """(sha256, size) of one retained output, computed on the machine, which must hold a regular file in regular folders."""
    value = _usable(root)
    windows = value.get("os") == "windows"
    command = test_machine.powershell_command(_windows_script(task, name, sha256)) if windows else f"sh -c {shlex.quote(_posix_script(task, name, sha256))}"
    paths = _paths(root)
    argv = [test_machine.ssh_program(), *test_machine.common_ssh_options(paths["known_hosts"], int(value["port"]), value["user"], value["address"]),
            "-o", "BatchMode=yes", "-o", "PreferredAuthentications=publickey", "-o", "PasswordAuthentication=no",
            "-o", "KbdInteractiveAuthentication=no", "-i", str(paths["key"]), value["address"], command]
    try:
        code, text = test_machine.run_key_only(argv, timeout=_remaining(deadline, REMOTE_SECONDS))
    except test_machine.MachineError as error:
        raise BuildError(str(error), "machine") from error
    line = next((row for row in text.splitlines() if MARKER in row), "")
    if f"{MARKER}OK " in line:
        parts = line.split(f"{MARKER}OK ", 1)[1].split()
        if len(parts) == 2 and re.fullmatch(r"[0-9a-f]{64}", parts[0]) and parts[1].isdigit():
            return parts[0], int(parts[1])
    if f"{MARKER}MISSING" in line:
        raise BuildError(
            f"`{name}` is not on the machine at `{_remote_dir(task, sha256)}/` as a regular file in regular folders (or one of them is a link). "
            "The certified test command must copy each declared output there; nothing else is substituted. If the file was lost after the test, Delivery "
            "submits a changed clean candidate (a real committed change, such as a build number) with a new final review.")
    if f"{MARKER}LINK" in line:
        raise BuildError(f"`{name}` or one of its folders on the machine is a link or reparse point; only a regular file in regular folders is collected.")
    if f"{MARKER}NOHASH" in line:
        raise BuildError("The machine has neither `sha256sum` nor `shasum`, so the build cannot be checked there. Install one (macOS has `shasum`) and run a new certified review.", "machine")
    detail = (text or "").strip().splitlines()[-1:] or ["no answer"]
    raise BuildError(f"The machine could not report `{name}` ({detail[0][:200]}). Check that it is running and connected, then collect again.", "machine")


def fetch(root, task: str, name: str, sha256: str, destination: Path, size_cap: int, deadline: float | None = None) -> None:
    """scp one output into `destination`, killing the copy if it grows past `size_cap`; the caller checks the bytes."""
    value = _usable(root)
    paths = _paths(root)
    remote = f"{value['user']}@{value['address']}:{_remote_dir(task, sha256)}/{name}"
    argv = [scp_program(), "-q", "-F", "/dev/null", "-P", str(int(value["port"])), *_key_options(paths["known_hosts"], paths["key"]), remote, str(destination)]
    # The folder, digest and name were validated; the account is validated at Connect: nothing here is shell text.
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, start_new_session=True,
                                   env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C", "LC_ALL": "C", "HOME": os.environ.get("HOME", "/")})
    except FileNotFoundError as error:
        raise BuildError("This computer has no `scp` program, so the build cannot be copied from the machine. Install the OpenSSH client (built into macOS, "
                         "Windows 10 and later; on Linux install the openssh-client package).", "machine") from error
    stop_at = time.monotonic() + _remaining(deadline, COPY_SECONDS)
    try:
        while process.poll() is None:
            if time.monotonic() > stop_at:
                raise BuildError(f"Copying `{name}` took too long and was stopped. Check the connection and collect again.", "machine")
            try:
                if destination.stat().st_size > size_cap:
                    raise BuildError(f"`{name}` grew past the {size_cap} bytes the certified run recorded while it was copied; nothing was kept.")
            except FileNotFoundError:
                pass
            time.sleep(0.05)
        error_text = (process.stderr.read() if process.stderr else "") or ""
    except BaseException:
        if process.poll() is None:
            _kill(process)
        raise
    finally:
        if process.stderr:
            process.stderr.close()
    if process.returncode != 0:
        raise BuildError(f"Copying `{name}` from the machine failed ({error_text.strip()[:240] or 'scp exit ' + str(process.returncode)}). "
                         "Check that the machine is running and allows file transfer (OpenSSH's sftp subsystem), then collect again.", "machine")


def _kill(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, 15)
    except OSError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, 9)
        except OSError:
            pass
        process.wait(timeout=5)


def _hash_file(path: Path) -> tuple[str, int]:
    digest, total = hashlib.sha256(), 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1 << 20)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
    return digest.hexdigest(), total


# -- the store ------------------------------------------------------------------------------------------
def store_root(root) -> Path:
    store = project_registry.trusted_project_store(root, namespace="builds")
    if store is None:
        raise BuildError("This project has no manager-owned store for builds (it is not registered with Mission Control, or the store would lie inside "
                         "an agent-writable folder), so the build cannot be kept safely here.", "storage")
    return store


def _task_folder(root, task: str) -> Path:
    return store_root(root) / project_registry.trusted_record_name(task)[:-5]


def manifest_digest(manifest: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


@contextlib.contextmanager
def _collection_lock(folder: Path) -> Iterator[None]:
    descriptor = os.open(folder / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise BuildError("Another collection of this task's build is running; try again in a minute.", "machine")
                time.sleep(0.2)
        yield
    finally:
        os.close(descriptor)


def _prune(folder: Path, known: set[str]) -> None:
    """Remove what a crash left: collection folders no board record names, and stale temporary folders."""
    for child in folder.iterdir():
        name = child.name
        orphan = re.fullmatch(r"b-[0-9a-f]{16}", name) and name not in known
        stale = name.startswith(".collect-") and time.time() - child.stat().st_mtime > 3600
        if (orphan or stale) and child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)


def collect(root, task: str, expected: dict[str, dict[str, Any]], metadata: dict[str, Any], commit, known_ids) -> dict[str, Any]:
    """Fetch every output, check each against the certified line, publish the whole set at once. Returns the manifest.

    `expected` maps name -> {sha256, size} from the certified output. Nothing is published unless every output matches
    and `commit(manifest)` (the board's recheck and record, run under the collection lock) accepts it; otherwise the
    published folder is removed again. `known_ids()` names the manifests the board already records.
    """
    task_component(task)
    total = sum(int(item["size"]) for item in expected.values())
    if total > MAX_TOTAL_BYTES:
        raise BuildError(f"The declared outputs add up to {total} bytes; a build may be at most {MAX_TOTAL_BYTES // (1024 * 1024)} MiB in all.")
    folder = _task_folder(root, task)
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise BuildError(f"The build store could not be created here ({error.strerror or error}); free space or fix its permissions and collect again.", "storage") from error
    if folder.is_symlink() or not folder.is_dir():
        raise BuildError("The build store for this task is not a real folder; it was left untouched.", "storage")
    deadline = time.monotonic() + OPERATION_SECONDS
    try:
        with _collection_lock(folder):
            _prune(folder, set(known_ids()))
            temporary = folder / f".collect-{os.getpid()}-{secrets.token_hex(4)}"
            final = None
            try:
                temporary.mkdir(mode=0o700)
                outputs = []
                for name, certified in expected.items():
                    valid_name(name)
                    sha, size = remote_facts(root, task, name, certified["sha256"], deadline)
                    if sha != certified["sha256"] or size != certified["size"]:
                        raise BuildError(
                            f"`{name}` on the machine is not the file the certified run printed (the run recorded {certified['sha256'][:12]}… of {certified['size']} bytes; "
                            f"the machine holds {sha[:12]}… of {size} bytes). It changed after the test, so it is not collected; a new certified run is needed.")
                    if size > MAX_BYTES:
                        raise BuildError(f"`{name}` is {size} bytes; a build output may be at most {MAX_BYTES // (1024 * 1024)} MiB.")
                    target = temporary / name
                    fetch(root, task, name, certified["sha256"], target, size, deadline)
                    try:
                        got_sha, got_size = _hash_file(target)
                    except OSError as error:
                        raise BuildError(f"`{name}` was not copied ({error.strerror or error}).", "machine") from error
                    if (got_sha, got_size) != (certified["sha256"], certified["size"]):
                        raise BuildError(f"`{name}` arrived changed (copied {got_sha[:12]}… of {got_size} bytes, expected {certified['sha256'][:12]}… of {certified['size']}); nothing was kept. Collect again.", "machine")
                    os.chmod(target, 0o600)
                    outputs.append({"name": name, "size": got_size, "sha256": got_sha})
                manifest_id = "b-" + secrets.token_hex(8)
                manifest = {"manifest_id": manifest_id, "task": task, "outputs": outputs, **metadata}
                (temporary / MANIFEST_NAME).write_text(json.dumps(manifest, indent=1, sort_keys=True), encoding="utf-8")
                final = folder / manifest_id
                os.replace(temporary, final)
                try:
                    commit(manifest)
                except BaseException:
                    shutil.rmtree(final, ignore_errors=True)
                    raise
                return manifest
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary, ignore_errors=True)
    except OSError as error:
        raise BuildError(f"The build could not be stored here ({error.strerror or error}); free space and collect again.", "storage") from error


def manifest_path(root, task: str, manifest_id: str) -> Path:
    if not re.fullmatch(r"b-[0-9a-f]{16}", str(manifest_id)):
        raise BuildError("That is not a build id.", "declaration")
    return _task_folder(root, task) / manifest_id / MANIFEST_NAME


def read_manifest(root, task: str, manifest_id: str, expected_digest: str) -> dict[str, Any]:
    path = manifest_path(root, task, manifest_id)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise BuildError("The collected build's record is missing or unreadable; it must be collected again after a new certified run.") from error
    if manifest_digest(manifest) != expected_digest:
        raise BuildError("The collected build's record does not match what the board recorded; it was changed after collection and is not served.")
    return manifest


def verified_open(root, task: str, manifest: dict[str, Any], name: str) -> tuple[int, int]:
    """Open one collected output (no symlink), check it is the recorded regular file with the recorded SHA-256, return (fd, size).

    The digest is computed on THIS descriptor, which the caller then streams: replacing the path afterwards changes nothing.
    """
    entry = next((item for item in manifest.get("outputs") or [] if item.get("name") == name), None)
    if entry is None:
        raise BuildError("That file is not part of this build.", "declaration")
    path = manifest_path(root, task, manifest["manifest_id"]).parent / valid_name(name)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    except OSError as error:
        raise BuildError(f"`{name}` is no longer in the build store ({error.strerror or error}).") from error
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_size != int(entry["size"]):
            raise BuildError(f"`{name}` in the build store is not the file that was collected.")
        digest = hashlib.sha256()
        remaining = status.st_size
        while remaining:
            chunk = os.read(descriptor, min(1 << 20, remaining))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
        if remaining or digest.hexdigest() != entry["sha256"]:
            raise BuildError(f"`{name}` in the build store no longer matches the digest recorded at collection, so it is not served.")
        os.lseek(descriptor, 0, os.SEEK_SET)
        return descriptor, status.st_size
    except BaseException:
        os.close(descriptor)
        raise


def intact(root, task: str, record: dict[str, Any]) -> tuple[bool, str]:
    """(True, '') when the board's record of a collected build still matches the store, every file included."""
    try:
        manifest = read_manifest(root, task, record.get("manifest_id", ""), str(record.get("manifest_sha256") or ""))
        for entry in manifest.get("outputs") or []:
            descriptor, _size = verified_open(root, task, manifest, entry["name"])
            os.close(descriptor)
    except BuildError as error:
        return False, str(error)
    return True, ""


def present(root, task: str, record: dict[str, Any]) -> tuple[bool, str]:
    """The cheap check the page makes on every refresh: the manifest is the recorded one and every file exists with its size.

    Digests are verified where it matters (the release gate and every download), not on each poll.
    """
    try:
        manifest = read_manifest(root, task, record.get("manifest_id", ""), str(record.get("manifest_sha256") or ""))
        folder = manifest_path(root, task, manifest["manifest_id"]).parent
        for entry in manifest.get("outputs") or []:
            status = os.lstat(folder / valid_name(entry["name"]))
            if not stat.S_ISREG(status.st_mode) or status.st_size != int(entry["size"]):
                return False, f"`{entry['name']}` in the build store is not the file that was collected."
    except OSError as error:
        return False, f"A collected file is no longer in the build store ({error.strerror or error})."
    except BuildError as error:
        return False, str(error)
    return True, ""
