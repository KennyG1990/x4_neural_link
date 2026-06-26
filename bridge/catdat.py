"""X4 cat/dat reader (Neural Link, Layer-3 execution).

Reads a single file out of X4: Foundations' packed archives without any third
party deps. X4's format is dead simple:

  * `NN.cat`  — a UTF-8 text index, one entry per line: `relpath size timestamp md5`
  * `NN.dat`  — every listed file concatenated, in cat order, uncompressed.

To pull one entry we sum the sizes of all preceding entries in its cat to get
the byte offset, then read `size` bytes from the matching `.dat`. Later-loaded
archives override earlier ones for the same relative path (vanilla 01..09 then
`ext_*` then `subst_*`), so we process in load order and let the last writer win.

Pure stdlib. Deterministic. The bridge runs on the host, so it can read the
game install directly — no Forge coupling, no network.
"""

from __future__ import annotations

import glob
import os
from typing import Optional

# Common Steam install location; overridable via X4_GAME_PATH or an explicit arg.
_DEFAULT_GAME_PATHS = [
    os.environ.get("X4_GAME_PATH", "").strip(),
    r"G:\SteamLibrary\steamapps\common\X4 Foundations",
    r"C:\Program Files (x86)\Steam\steamapps\common\X4 Foundations",
    r"D:\SteamLibrary\steamapps\common\X4 Foundations",
]


def _derived_game_path() -> str:
    """Derive the X4 root from THIS file's own location — the bridge ships inside the game at
    <X4>/extensions/x4_neural_link/bridge/catdat.py, so the install root is 3 parents up. This is the
    zero-friction path for a PUBLISHED mod: it works on any machine regardless of Steam library location,
    with no env var or hardcoded path. (Returns '' if the derived dir has no .cat archives.)"""
    try:
        from pathlib import Path
        root = Path(__file__).resolve().parents[3]
        if glob.glob(os.path.join(str(root), "*.cat")):
            return str(root)
    except Exception:
        pass
    return ""


def resolve_game_path(explicit: Optional[str] = None) -> Optional[str]:
    """First existing X4 install dir (one that actually has .cat archives). Prefers an explicit path, then the
    install we're running INSIDE (derived from our own location — robust for a shipped mod), then env/common."""
    for cand in [explicit, _derived_game_path(), *_DEFAULT_GAME_PATHS]:
        if cand and os.path.isdir(cand) and glob.glob(os.path.join(cand, "*.cat")):
            return cand
    return None


def _ordered_cats(game_path: str) -> list[str]:
    """Archives in X4 load order: 01.cat..NN.cat, then ext_*, then subst_* (last wins)."""
    cats = glob.glob(os.path.join(game_path, "*.cat"))

    def rank(p: str) -> tuple:
        name = os.path.basename(p).lower()
        if name.startswith("subst"):
            grp = 2
        elif name.startswith("ext"):
            grp = 1
        else:
            grp = 0
        return (grp, name)

    return sorted(cats, key=rank)


# Cache the per-install index — the cats are static during a session.
_INDEX_CACHE: dict[str, dict[str, tuple]] = {}


def build_index(game_path: str, refresh: bool = False) -> dict[str, tuple]:
    """{ relpath_lower: (dat_path, offset, size) } across all archives, last writer wins."""
    if not refresh and game_path in _INDEX_CACHE:
        return _INDEX_CACHE[game_path]
    index: dict[str, tuple] = {}
    for cat in _ordered_cats(game_path):
        dat = cat[:-4] + ".dat"
        if not os.path.exists(dat):
            continue
        offset = 0
        try:
            with open(cat, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.rstrip("\r\n")
                    if not line:
                        continue
                    # path may not contain spaces in vanilla; split the trailing
                    # size/timestamp/md5 off the right to be safe.
                    parts = line.rsplit(" ", 3)
                    if len(parts) != 4:
                        continue
                    rel, size_s, _ts, _md5 = parts
                    try:
                        size = int(size_s)
                    except ValueError:
                        continue
                    index[rel.replace("\\", "/").lower()] = (dat, offset, size)
                    offset += size
        except OSError:
            continue
    _INDEX_CACHE[game_path] = index
    return index


def extract_bytes(rel: str, game_path: Optional[str] = None) -> Optional[bytes]:
    gp = resolve_game_path(game_path)
    if not gp:
        return None
    hit = build_index(gp).get(rel.replace("\\", "/").lower())
    if not hit:
        return None
    dat, offset, size = hit
    try:
        with open(dat, "rb") as fh:
            fh.seek(offset)
            return fh.read(size)
    except OSError:
        return None


def extract_text(rel: str, game_path: Optional[str] = None) -> Optional[str]:
    raw = extract_bytes(rel, game_path)
    return raw.decode("utf-8", errors="replace") if raw is not None else None


def available(game_path: Optional[str] = None) -> dict:
    """Cheap probe for the endpoint: is the game readable, and are the two
    lore sources present?"""
    gp = resolve_game_path(game_path)
    if not gp:
        return {"game_path": None, "ok": False}
    idx = build_index(gp)
    return {
        "game_path": gp,
        "ok": True,
        "entries": len(idx),
        "has_factions": "libraries/factions.xml" in idx,
        "has_textdb": "t/0001-l044.xml" in idx,
    }
