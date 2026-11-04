# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 dancingduck425
"""What every HID backend hands the drivers: one HidInfo per USB interface, plus a tiny report-descriptor parser."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Set, Tuple


@dataclass
class HidInfo:
    path: str  # /dev/hidrawN on Linux; on Windows the interface's device path (collection index stripped)
    vid: int
    pid: int
    name: str
    interface: int  # USB interface number, -1 if unknown
    descriptor: bytes  # HID report descriptor (Windows: reconstructed per collection, concatenated)


def parse_collections(desc: bytes) -> List[Tuple[int, int, Set[int]]]:
    """(usage page, usage, report ids) of every top-level collection in a report descriptor."""
    out: List[Tuple[int, int, Set[int]]] = []
    i = 0
    depth = 0
    page = usage = 0
    rid = None
    cur: Dict[str, object] = {}
    while i < len(desc):
        b = desc[i]
        i += 1
        if b == 0xFE:  # long item
            if i + 1 >= len(desc):
                break
            i += 2 + desc[i]
            continue
        size = b & 3
        size = 4 if size == 3 else size
        typ, tag = (b >> 2) & 3, b >> 4
        val = int.from_bytes(desc[i : i + size], "little")
        i += size
        if typ == 1 and tag == 0:  # Usage Page
            page = val
        elif typ == 1 and tag == 8:  # Report ID
            rid = val
            if cur:
                cur["rids"].add(val)  # type: ignore[union-attr]
        elif typ == 2 and tag == 0:  # Usage
            if size == 4:
                page, usage = val >> 16, val & 0xFFFF
            else:
                usage = val
        elif typ == 0 and tag == 0xA:  # Collection
            if depth == 0:
                cur = {"page": page, "usage": usage, "rids": set()}
            depth += 1
        elif typ == 0 and tag == 0xC:  # End Collection
            depth -= 1
            if depth == 0 and cur:
                out.append((int(cur["page"]), int(cur["usage"]), cur["rids"]))  # type: ignore[arg-type]
                cur = {}
                rid = None
        elif typ == 0 and tag in (8, 9, 0xB) and cur and rid is not None:  # Input / Output / Feature
            cur["rids"].add(rid)  # type: ignore[union-attr]
    return out
