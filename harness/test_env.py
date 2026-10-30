# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""A project's test environment for governed commands (owner's order 2026-10-28, task 2026-10-28_project-test-environment).

Delivery's ledger, the Reviewer's ledger and the release check ran the project's tests with plain system Python, so a project with its own
libraries (Acremuse: sqlmodel, fastapi, psycopg) could not be tested by the Reviewer at all, and the builder pasted the test-database password
into its files to make a test run.

The owner or the CTO prepares an environment with ordinary tools (a virtual environment holding the project's third-party libraries) and declares
it once with `test-env-set`: the interpreter, the working folder inside the code, and the variables the tests need (a value may come from a file
outside the code, so it is never written anywhere). For each governed run the harness puts that interpreter first on PATH, sets the variables,
runs from the working folder, adds the environment to the run's identity (so a stored pass is not reused after the environment, the
declaration or a value changes) and hides the secret values from the output it shows. A project that declares nothing is untouched.

Honest limit: the harness never gives a secret to an agent and never writes one; it cannot stop test code from printing a value it was given.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

DECLARATION_NAME = "declaration.json"
# Names a declaration can never set: they redirect interpreters, loaders or shells, or are the harness's own markers.
RESERVED_NAMES = {"PATH", "HOME", "ENV", "SHELL", "IFS", "CDPATH", "VIRTUAL_ENV", "TMPDIR", "LANG", "TZ", "CI", "OLDPWD", "PWD"}
RESERVED_PREFIXES = ("PYTHON", "LD_", "DYLD_", "BASH_", "ZDOTDIR", "GIT_", "HARNESS_", "NMHP_")
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
SCRUBBED = "[hidden]"
HOW_TO_PREPARE = ("ask the CTO to prepare it: create a virtual environment outside the project's code folders with the project's libraries "
                  "(for example `uv sync --frozen --no-install-project`), then record it with `test-env-set`")


MISSING_LIBRARY = re.compile(r"No module named|ModuleNotFoundError")
MISSING_LIBRARY_ADVICE = (" The tests ran with the computer's plain Python and a library they need is not installed there (a project's own libraries live in a prepared "
                          "test environment). The CTO prepares it with test-env-set, then Delivery asks for the review again; this is not a failure of the tests themselves.")


def missing_library_advice(output: Any) -> str:
    """The plain next step when a stored test command died for want of a Python library, else ''."""
    return MISSING_LIBRARY_ADVICE if MISSING_LIBRARY.search(str(output or "")) else ""


class TestEnvError(ValueError):
    """A refusal in plain words: what is wrong and what to do. Never contains a declared secret value."""
    __test__ = False


@dataclass
class Resolved:
    python: str
    python_dir: str
    env_root: str
    workdir: str                      # relative to the candidate ("." when none)
    variables: dict[str, str]         # every declared variable, secrets included (child process only)
    secrets: list[str] = field(default_factory=list)
    identity: dict[str, str] = field(default_factory=dict)


def declaration_path(root: Any) -> Path:
    from harness.project_context import project_context
    return project_context(root).storage_path("test-env") / DECLARATION_NAME


def load(root: Any) -> dict[str, Any] | None:
    try:
        value = json.loads(declaration_path(root).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise TestEnvError("the project's test environment declaration cannot be read; record it again with `test-env-set`") from error
    return value if isinstance(value, dict) else None


def _relative(value: Any, label: str) -> str:
    text = str(value or "").strip().replace("\\", "/")
    if not text or text.startswith("/") or re.match(r"^[A-Za-z]:", text) or ".." in Path(text).parts:
        raise TestEnvError(f"{label} must be a folder path relative to the code, without \"..\" (got {text[:80]!r})")
    return text.strip("/") or "."


def validate(raw: Any) -> dict[str, Any]:
    """The normalised declaration, or a plain refusal. Checks shape only; the environment itself is checked at run time."""
    if not isinstance(raw, dict):
        raise TestEnvError("the declaration must be a JSON object with python and optionally workdir and env")
    python = str(raw.get("python") or "").strip()
    if not python or not os.path.isabs(python):
        raise TestEnvError("`python` must be the absolute path of the prepared environment's interpreter")
    declared: dict[str, Any] = {"python": python, "workdir": _relative(raw.get("workdir") or ".", "`workdir`")}
    env: dict[str, Any] = {}
    for name, spec in (raw.get("env") or {}).items():
        name = str(name)
        if not _NAME.match(name) or name.upper() in RESERVED_NAMES or name.upper().startswith(RESERVED_PREFIXES):
            raise TestEnvError(f"the variable name {name!r} cannot be set by a declaration (it is reserved or not a valid name)")
        if isinstance(spec, str):
            env[name] = spec
        elif isinstance(spec, dict) and os.path.isabs(str(spec.get("file") or "")) and str(spec.get("key") or "").strip():
            env[name] = {"file": str(spec["file"]), "key": str(spec["key"]).strip()}
        else:
            raise TestEnvError(f"variable {name}: give a literal text value, or {{\"file\": <absolute path>, \"key\": <name in that file>}}")
    declared["env"] = env
    return declared


def save(root: Any, raw: Any) -> dict[str, Any]:
    declared = validate(raw)
    path = declaration_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(declared, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return public(declared)


def public(declared: Mapping[str, Any]) -> dict[str, Any]:
    """The declaration as it may be shown: file-sourced values appear as their source, never their value."""
    shown = dict(declared)
    shown["env"] = {name: (value if isinstance(value, str) else {"file": value["file"], "key": value["key"], "value": "(read from the file at run time)"})
                    for name, value in (declared.get("env") or {}).items()}
    return shown


def _file_value(path: str, key: str, name: str) -> str:
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise TestEnvError(f"variable {name}: the file it comes from cannot be read; check the path with `test-env-set`") from error
    found = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        left, _, right = line.partition("=")
        if left.strip().removeprefix("export ").strip() == key:
            found.append(right.strip().strip("'\"") if right.strip()[:1] in "'\"" else right.strip())
    if len(found) != 1 or not found[0]:
        raise TestEnvError(f"variable {name}: the key {key!r} must appear exactly once, with a value, in the file it comes from")
    return found[0]


def _secret_parts(value: str) -> list[str]:
    parts = [value]
    if "://" in value:
        try:
            password = urlsplit(value).password
        except ValueError:
            password = None
        if password:
            parts.append(password)
    return [part for part in parts if len(part) >= 4]


def _installed(env_root: Path) -> str:
    """The installed libraries (name and version, from the environment's own metadata folders) as one text; changes when a library does."""
    sites = [path for path in sorted(env_root.glob("lib*/python*/site-packages")) + sorted(env_root.glob("Lib/site-packages")) if path.is_dir()]
    if not sites:
        raise TestEnvError(f"the test environment for this project is not ready: no libraries found under {str(env_root)!r}; {HOW_TO_PREPARE}")
    return "\n".join(sorted(info.name for site in sites for info in site.glob("*.dist-info")))


def resolve(root: Any, candidate_root: Any) -> Resolved | None:
    """The environment for one governed run of `candidate_root`, or None when the project declares nothing."""
    declared = load(root)
    if not declared:
        return None
    try:
        checked = validate(declared)
    except TestEnvError as error:
        raise TestEnvError(f"the project's test environment declaration is not valid ({error}); record it again with `test-env-set`") from error
    candidate = Path(candidate_root).resolve()
    workdir = (candidate / checked["workdir"]).resolve()
    if workdir != candidate and candidate not in workdir.parents:
        raise TestEnvError(f"the working folder {checked['workdir']!r} leaves the code folder being tested; fix it with `test-env-set`")
    if not workdir.is_dir():
        raise TestEnvError(f"the working folder {checked['workdir']!r} named in the declaration does not exist in this code; fix it with `test-env-set`")
    python = Path(checked["python"])
    if not python.is_file() or not os.access(python, os.X_OK):
        raise TestEnvError(f"the test environment for this project is not ready: the interpreter {str(python)!r} is missing or not runnable; {HOW_TO_PREPARE}")
    installed = _installed(python.parent.parent)
    variables: dict[str, str] = {}
    hidden: list[str] = []
    for name, spec in sorted(checked["env"].items()):
        if isinstance(spec, str):
            variables[name] = spec
        else:
            variables[name] = _file_value(spec["file"], spec["key"], name)
            hidden.extend(_secret_parts(variables[name]))
    digest = lambda text: hashlib.sha256(text.encode()).hexdigest()
    return Resolved(
        python=str(python), python_dir=str(python.parent), env_root=str(python.parent.parent), workdir=checked["workdir"], variables=variables,
        secrets=sorted(set(hidden), key=len, reverse=True),
        identity={"python": str(python), "libraries_sha256": digest(installed), "declaration_sha256": digest(json.dumps(checked, sort_keys=True)),
                  "config_sha256": digest(json.dumps(sorted(variables.items())))},
    )


def apply(environment: Mapping[str, str], resolved: Resolved | None) -> dict[str, str]:
    """The governed environment with the project's interpreter first on PATH and its declared variables set."""
    values = dict(environment)
    if resolved is None:
        return values
    values["PATH"] = resolved.python_dir + os.pathsep + values.get("PATH", "")
    values["VIRTUAL_ENV"] = resolved.env_root
    values.update(resolved.variables)
    return values


def workdir_of(resolved: Resolved | None) -> str:
    return resolved.workdir if resolved is not None else "."


def identity_fields(resolved: Resolved | None) -> dict[str, str]:
    """Non-secret fields that join the execution identity: any change defeats every reuse shortcut. Empty when nothing is declared."""
    return dict(resolved.identity) | {"workdir": resolved.workdir} if resolved is not None else {}


def scrub(text: Any, resolved: Resolved | None) -> str:
    """The text with every declared secret value, and a URL's password, replaced; unchanged when nothing is declared."""
    value = str(text or "")
    if resolved is None:
        return value
    for secret in resolved.secrets:
        value = value.replace(secret, SCRUBBED)
    return value
