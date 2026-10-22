# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""What a managed Codex agent must never be given: Codex's bundled browser and computer-use tools.

Owner 2026-10-21 ("what am i to do with this?????"): a Codex agent in a review walk used the bundled browser plugin, which started the
owner's real Google Chrome, and macOS asked him about a keychain. The harness renders pages itself (screen-check, screen-script) in its
own headless browser; an agent never needs a browser of the owner's. A launch therefore carries `-c` overrides that remove the plugins
(and the plugin-provided tool servers) and switch off any browser-, computer- or REPL-style tool server found in the owner's own Codex
configuration. The owner's configuration file is only READ, never written.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# Policy (owner 2026-10-21): a managed Codex agent gets NO Codex plugins at all (they bundle the browser, computer-use and
# code-review tools and their servers) and none of the owner's own tool servers that can drive a browser or the computer. The
# supported route for rendering a page is the board's screen-check / screen-script (loopback pages, the harness's own headless
# browser). This is tool suppression for the Codex path that raised the dialog, not a claim that no agent can ever open a window:
# an agent can still run a shell command, and Claude and Gemini agents have their own paths.
RISKY = re.compile(r"browser|chrome|repl|computer|cua|sky|playwright|puppeteer|node_repl|SkyComputerUse|cua_node", re.I)
HEADER = re.compile(r"""^\s*\[\s*mcp_servers\s*\.\s*(?:"([^"]+)"|'([^']+)'|([A-Za-z0-9_-]+))\s*(?:\..*)?\]\s*(?:#.*)?$""")
ANY_HEADER = re.compile(r"^\s*\[")
DOTTED = re.compile(r"""^\s*mcp_servers\s*\.\s*(?:"([^"]+)"|'([^']+)'|([A-Za-z0-9_-]+))\s*\.\s*[A-Za-z0-9_-]+\s*=""")
INLINE_TABLE_ENTRY = re.compile(r"""^\s*(?:"([^"]+)"|'([^']+)'|([A-Za-z0-9_-]+))\s*=\s*\{""")


class IsolationError(Exception):
    """The owner's Codex configuration exists but cannot be read: the launch is refused rather than made without isolation."""


def config_path(environment: dict[str, str] | None = None) -> Path:
    env = os.environ if environment is None else environment
    home = env.get("CODEX_HOME") or str(Path(env.get("HOME", "~")).expanduser() / ".codex")
    return Path(home).expanduser() / "config.toml"


def _name(match: re.Match) -> str:
    return next(group for group in match.groups() if group)


def config_files(environment: dict[str, str] | None = None, cwd: str | None = None) -> list[Path]:
    """Every Codex configuration that can define a tool server for a launch from `cwd`: the owner's global file and the
    `.codex/config.toml` of the folder and each of its parents (a trusted project's own file is merged in by Codex)."""
    files = [config_path(environment)]
    folder = Path(cwd or os.getcwd()).resolve()
    for directory in (folder, *folder.parents):
        candidate = directory / ".codex" / "config.toml"
        if candidate not in files:
            files.append(candidate)
    return files


def _scan(config: Path) -> dict[str, list[str]]:
    if not config.exists():
        return {}
    try:
        text = config.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        raise IsolationError(f"cannot read {config}: {error.strerror or error}") from error
    blocks: dict[str, list[str]] = {}
    current = ""
    in_servers_table = False
    for line in text.splitlines():
        header = HEADER.match(line)
        if header:
            current, in_servers_table = _name(header), False
            blocks.setdefault(current, []).append(line)
            continue
        if ANY_HEADER.match(line):
            in_servers_table = bool(re.match(r"^\s*\[\s*mcp_servers\s*\]\s*(?:#.*)?$", line))
            # the env sub-table of a server belongs to it: [mcp_servers.NAME.env]
            sub = re.match(r"""^\s*\[\s*mcp_servers\s*\.\s*(?:"([^"]+)"|'([^']+)'|([A-Za-z0-9_-]+))\s*\.""", line)
            current = _name(sub) if sub else ""
            if sub:
                blocks.setdefault(current, []).append(line)
            continue
        dotted = DOTTED.match(line)
        if dotted:
            blocks.setdefault(_name(dotted), []).append(line)
            continue
        if in_servers_table:
            inline = INLINE_TABLE_ENTRY.match(line)
            if inline:
                blocks.setdefault(_name(inline), []).append(line)
                continue
        if current:
            blocks.setdefault(current, []).append(line)
    return blocks


def risky_servers(config: Path | list[Path]) -> list[str]:
    """Names of the browser-, computer- or REPL-style tool servers the given Codex configuration file(s) define (read only).

    A missing file means none; a file that exists but cannot be read raises IsolationError. A server is risky when its name OR any
    of its own lines (command, args, url, env) matches: `[mcp_servers.NAME]`, quoted names, trailing comments, dotted keys
    (`mcp_servers.NAME.command = ...`) and inline entries inside a bare `[mcp_servers]` table are all read.
    """
    files = config if isinstance(config, list) else [config]
    found: set[str] = set()
    for path in files:
        for name, lines in _scan(path).items():
            if RISKY.search(name) or RISKY.search("\n".join(lines)):
                found.add(name)
    return sorted(found)


def disable_arguments(environment: dict[str, str] | None = None, cwd: str | None = None) -> list[str]:
    """`-c key=value` pairs for a managed Codex launch (every role, interactive or app-server) started from `cwd`."""
    arguments = ["-c", "features.plugins=false"]
    for name in risky_servers(config_files(environment, cwd)):
        key = name if re.fullmatch(r"[A-Za-z0-9_-]+", name) else '"' + name.replace('"', '') + '"'
        # A COMPLETE disabled table, never just `.enabled=false`: for a server this launch's Codex does not load (another CODEX_HOME,
        # a config outside the project's scope) a bare `.enabled=false` makes Codex reject the config ("invalid transport"); for one it
        # does load, the stub replaces it and it never starts. So the scan need not be exact about Codex's own configuration scope.
        arguments += ["-c", f'mcp_servers.{key}={{command="/usr/bin/false", enabled=false}}']
    return arguments


def main() -> int:
    """Run BY PATH from the launch script (python -E ignores PYTHONPATH and the script runs inside the project folder): one argument per line."""
    try:
        sys.stdout.write("\n".join(disable_arguments()) + "\n")
    except IsolationError as error:
        sys.stderr.write(f"Codex tool isolation could not be prepared: {error}. The agent was not started.\n")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
