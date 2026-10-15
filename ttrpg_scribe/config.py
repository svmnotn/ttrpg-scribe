"""Single-file store for ttrpg-scribe: %USERPROFILE%\\.ttrpg-scribe.json.

Holds everything: capture defaults, bot token, folder → guild/channel maps.
A bare ``ttrpg-scribe <folder>`` just works (Stream Mix default); change any
default once with ``ttrpg-scribe --set-default KEY VALUE``. CLI flags always
win for one-off overrides. Secrets are only ever written by hand-editing —
never via CLI args (which land in shell history).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

CONFIG_PATH = Path.home() / ".ttrpg-scribe.json"

DEFAULTS: dict[str, str] = {
    "mode": "sidecar",
    "input": "Broadcast Stream Mix",
    "device": "",
    "mic": "",
    "model": "large-v3-turbo",
    "guild": "",
    "channel": "",
}

# Valid --set-default keys (CLI spelling == stored key).
KEYS: set[str] = set(DEFAULTS)


def _blank() -> dict[str, Any]:
    return {"defaults": dict(DEFAULTS), "discord_token": "", "opus_dll_path": "", "maps": {}}


def _read() -> dict[str, Any]:
    if CONFIG_PATH.exists():
        try:
            doc = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(doc, dict):
                doc.setdefault("defaults", {})
                doc.setdefault("maps", {})
                return doc
        except (OSError, json.JSONDecodeError):
            pass
    doc = _blank()
    _write(doc)
    return doc


def _write(doc: dict[str, Any]) -> None:
    CONFIG_PATH.write_text(json.dumps(doc, indent=2), encoding="utf-8")


def load() -> dict[str, str]:
    """Effective defaults (built-ins + stored)."""
    doc = _read()
    cfg: dict[str, str] = dict(DEFAULTS)
    stored = doc.get("defaults", {})
    for key in DEFAULTS:
        if key in stored:
            cfg[key] = stored[key]
    return cfg


def secrets() -> dict[str, str]:
    doc = _read()
    return {"discord_token": doc.get("discord_token", ""),
            "opus_dll_path": doc.get("opus_dll_path", "")}


def save(patch: dict[str, str]) -> Path:
    doc = _read()
    doc.setdefault("defaults", {}).update(patch)
    _write(doc)
    return CONFIG_PATH


def describe() -> str:
    cfg = load()
    lines = [f"[defaults] ({CONFIG_PATH})"]
    for key in DEFAULTS:
        marker = "" if cfg[key] == DEFAULTS[key] else "  (changed)"
        lines.append(f"  {key} = {cfg[key]!r}{marker}")
    return "\n".join(lines)


# ---- folder → guild/channel maps (several folders may share one target) ----

def _map_key(game_dir: str) -> str:
    return os.path.normcase(os.path.abspath(os.path.expanduser(game_dir)))


def lookup_map(game_dir: str) -> dict[str, str] | None:
    maps = _read().get("maps", {})
    entry = maps.get(_map_key(game_dir))
    if isinstance(entry, dict):
        return entry
    # Fallback: match stored path (handles keys written with a typo or
    # different spelling — e.g. deal-of-blood vs deals-of-blood).
    want = _map_key(game_dir)
    for key, cand in maps.items():
        if not isinstance(cand, dict):
            continue
        if _map_key(str(cand.get("path", ""))) == want or os.path.normcase(key) == want:
            return cand
    return None


def save_map(game_dir: str, guild: str, channel: str) -> Path:
    doc = _read()
    doc.setdefault("maps", {})[_map_key(game_dir)] = {
        "path": os.path.abspath(os.path.expanduser(game_dir)),
        "guild": guild,
        "channel": channel,
    }
    _write(doc)
    return CONFIG_PATH


def remove_map(game_dir: str) -> bool:
    doc = _read()
    maps = doc.get("maps", {})
    if _map_key(game_dir) not in maps:
        return False
    del maps[_map_key(game_dir)]
    _write(doc)
    return True


def describe_maps() -> str:
    maps = _read().get("maps", {})
    if not maps:
        return f"(no folder mappings in {CONFIG_PATH})"
    lines = [f"[folder mappings] ({CONFIG_PATH})"]
    for entry in maps.values():
        lines.append(f"  {entry.get('path')}\n    guild={entry.get('guild')!r} channel={entry.get('channel')!r}")
    return "\n".join(lines)


# ---- interactive prompts ----

def prompt_choice(title: str, names: list[str]) -> str:
    """Let the user pick a device by number or substring. Aborts cleanly on EOF."""
    print(f"--- {title} ---")
    for i, name in enumerate(names, 1):
        print(f"  {i}. {name}")
    while True:
        try:
            raw = input("Pick a number or type part of a name (Enter to abort): ").strip()
        except EOFError:
            raise SystemExit("Aborted.")
        if not raw:
            raise SystemExit("Aborted.")
        if raw.isdigit() and 1 <= int(raw) <= len(names):
            return names[int(raw) - 1]
        hits = [n for n in names if raw.lower() in n.lower()]
        if len(hits) == 1:
            return hits[0]
        if hits:
            print("Multiple matches:")
            for n in hits:
                print(f"  {n}")
        else:
            print("No match, try again.")


def prompt_yes(question: str) -> bool:
    try:
        return input(f"{question} [y/N]: ").strip().lower().startswith("y")
    except EOFError:
        return False
