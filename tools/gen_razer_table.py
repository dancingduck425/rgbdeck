#!/usr/bin/env python3
"""
Generate rgbdeck/hw/razer_devices.py from OpenRGB's Razer sources.

Reads (GPL-2.0, read only, never used at runtime):
  Controllers/RazerController/RazerDevices.h          PID macros
  Controllers/RazerController/RazerDevices.cpp        zones, devices, keyboard layout overlays, master list
  Controllers/RazerController/RazerControllerDetect.cpp  which USB interface / HID usage each PID is opened on
  Controllers/RazerController/RazerController/RazerController.cpp  per-PID quirk lists (switch/case blocks)
  KeyboardLayoutManager/KeyboardLayoutManager.cpp     base key blocks + regional overlays
  RGBController/RGBControllerKeyNames.cpp             KEY_EN_* string constants

Usage:  python3 tools/gen_razer_table.py [path/to/OpenRGB]   (default ~/src/OpenRGB)
Entries that cannot be parsed are skipped and counted; the output file is never hand-edited.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "rgbdeck", "hw", "razer_devices.py")

# OpenRGB enum values (RGBController/RGBControllerInterface.h, RazerController.h, KeyboardLayoutManager.h)
DEVICE_TYPES = [
    "MOTHERBOARD",
    "DRAM",
    "GPU",
    "COOLER",
    "LEDSTRIP",
    "KEYBOARD",
    "MOUSE",
    "MOUSEMAT",
    "HEADSET",
    "HEADSET_STAND",
    "GAMEPAD",
    "LIGHT",
    "SPEAKER",
    "VIRTUAL",
    "STORAGE",
    "CASE",
    "MICROPHONE",
    "ACCESSORY",
    "KEYPAD",
    "LAPTOP",
    "MONITOR",
    "UNKNOWN",
]
ZONE_TYPES = {"ZONE_TYPE_SINGLE": 0, "ZONE_TYPE_LINEAR": 1, "ZONE_TYPE_MATRIX": 2}
MATRIX_TYPES = {
    "RAZER_MATRIX_TYPE_NONE": 0,
    "RAZER_MATRIX_TYPE_STANDARD": 1,
    "RAZER_MATRIX_TYPE_EXTENDED": 2,
    "RAZER_MATRIX_TYPE_LINEAR": 3,
    "RAZER_MATRIX_TYPE_EXTENDED_ARGB": 4,
    "RAZER_MATRIX_TYPE_CUSTOM": 5,
}
KEYBOARD_SIZES = {
    "KEYBOARD_SIZE_EMPTY": 0,
    "KEYBOARD_SIZE_FULL": 15,
    "KEYBOARD_SIZE_TKL": 7,
    "KEYBOARD_SIZE_SEVENTY_FIVE": 3,
    "KEYBOARD_SIZE_SIXTY": 1,
}
KEYBOARD_LAYOUTS = {
    "KEYBOARD_LAYOUT_DEFAULT": 0,
    "KEYBOARD_LAYOUT_ANSI_QWERTY": 1,
    "KEYBOARD_LAYOUT_ISO_QWERTY": 2,
    "KEYBOARD_LAYOUT_ISO_QWERTZ": 3,
    "KEYBOARD_LAYOUT_ISO_AZERTY": 4,
    "KEYBOARD_LAYOUT_JIS": 5,
    "KEYBOARD_LAYOUT_ABNT2": 6,
}
OPCODES = {
    "KEYBOARD_OPCODE_INSERT_SHIFT_RIGHT": 0,
    "KEYBOARD_OPCODE_SWAP_ONLY": 1,
    "KEYBOARD_OPCODE_REMOVE_SHIFT_LEFT": 2,
    "KEYBOARD_OPCODE_INS_SHFT_ADJACENT": 3,
    "KEYBOARD_OPCODE_INSERT_ROW": 4,
    "KEYBOARD_OPCODE_REMOVE_ROW": 5,
    "KEYBOARD_OPCODE_ADD_ALT_NAME": 6,
}
# OpenRGB device_type -> RGB Deck type_name
TYPE_NAMES = {
    "KEYBOARD": "Keyboard",
    "LAPTOP": "Keyboard",
    "MOUSE": "Mouse",
    "MOUSEMAT": "Mousemat",
    "HEADSET": "Headset",
    "KEYPAD": "Keypad",
    "LEDSTRIP": "LED Strip",
}

skipped: List[str] = []


def skip(msg: str):
    skipped.append(msg)
    print("skip:", msg)


# ---------------------------------------------------------------- C tokenizer / brace tree
TOKEN_RE = re.compile(r'"(?:[^"\\]|\\.)*"|&?[A-Za-z_][A-Za-z0-9_:]*|0[xX][0-9A-Fa-f]+|\d+|[{}(),;=*\[\]]|\S')


def strip_comments(src: str) -> str:
    out, i, n = [], 0, len(src)
    while i < n:
        c = src[i]
        if c == '"':
            j = i + 1
            while j < n and src[j] != '"':
                j += 2 if src[j] == "\\" else 1
            out.append(src[i : j + 1])
            i = j + 1
        elif src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
            out.append(" ")
        else:
            out.append(c)
            i += 1
    return "".join(out)


def unquote(tok: str) -> str:
    return tok[1:-1].replace('\\"', '"').replace("\\\\", "\\")


def tree(tokens: List[str], pos: int) -> Tuple[list, int]:
    """tokens[pos] == '{' -> nested python list of tokens/lists, position after the closing brace."""
    assert tokens[pos] == "{", tokens[pos : pos + 5]
    out: list = []
    pos += 1
    while tokens[pos] != "}":
        if tokens[pos] == "{":
            sub, pos = tree(tokens, pos)
            out.append(sub)
        elif tokens[pos] == ",":
            pos += 1
        else:
            out.append(tokens[pos])
            pos += 1
    return out, pos + 1


def find_init(tokens: List[str], name: str) -> Optional[list]:
    """Brace initializer that follows `name` (either `name = {` or `name {`)."""
    for i, t in enumerate(tokens):
        if t == name and i + 1 < len(tokens):
            j = i + 1
            if tokens[j] == "=":
                j += 1
            if tokens[j] == "{":
                return tree(tokens, j)[0]
    return None


def toks(src: str) -> List[str]:
    return TOKEN_RE.findall(strip_comments(src))


# ---------------------------------------------------------------- key names, PIDs
def parse_key_names(path: str) -> Dict[str, str]:
    names: Dict[str, str] = {}
    for m in re.finditer(r'const char\*\s+(\w+)\s*=\s*("(?:[^"\\]|\\.)*")\s*;', open(path, encoding="utf-8").read()):
        names[m.group(1)] = unquote(m.group(2))
    return names


def parse_pids(path: str) -> Dict[str, int]:
    pids: Dict[str, int] = {}
    for m in re.finditer(r"#define\s+(RAZER_\w+_PID)\s+(0[xX][0-9A-Fa-f]+)", open(path).read()):
        pids[m.group(1)] = int(m.group(2), 16)
    return pids


def key_row(row: list, names: Dict[str, str]) -> Optional[Tuple[int, int, str, str, int]]:
    """{ zone, row, col, value, name, alt_name, opcode } -> (row, col, name, alt_name, opcode)."""
    if len(row) != 7:
        return None

    def name_of(t: str) -> str:
        if t.startswith('"'):
            return unquote(t)
        if t in names:
            return names[t]
        raise KeyError(t)

    try:
        if int(row[3], 0) != 0:
            raise ValueError("non-zero key value not supported")
        return int(row[1], 0), int(row[2], 0), name_of(row[4]), name_of(row[5]), OPCODES[row[6]]
    except (KeyError, ValueError) as e:
        skip(f"key row {row}: {e}")
        return None


def key_rows(rows: list, names: Dict[str, str]) -> List[Tuple[int, int, str, str, int]]:
    out = []
    for r in rows:
        if isinstance(r, list):
            k = key_row(r, names)
            if k:
                out.append(k)
    return out


# ---------------------------------------------------------------- KeyboardLayoutManager.cpp
def parse_klm(path: str, names: Dict[str, str]):
    tokens = toks(open(path, encoding="utf-8").read())
    base = {}
    for block in ("keyboard_zone_main", "keyboard_zone_fn_row", "keyboard_zone_extras", "keyboard_zone_numpad"):
        init = find_init(tokens, block)
        if init is None:
            skip(f"KLM block {block} not found")
            continue
        base[block.replace("keyboard_zone_", "")] = key_rows(init, names)
    regional = {}
    for var, layout in (
        ("ansi_qwerty", 1),
        ("iso_qwerty", 2),
        ("iso_qwertz", 3),
        ("iso_azerty", 4),
        ("jis", 5),
        ("abnt2", 6),
    ):
        init = find_init(tokens, var)
        if init is None or len(init) < 2:
            skip(f"KLM regional overlay {var} not found")
            continue
        regional[layout] = key_rows(init[1], names)
    return base, regional


# ---------------------------------------------------------------- RazerDevices.cpp
def parse_layouts(tokens: List[str], names: Dict[str, str]) -> Dict[str, dict]:
    layouts: Dict[str, dict] = {}
    for i, t in enumerate(tokens):
        if t == "keyboard_keymap_overlay_values" and tokens[i + 2] == "{":
            lname = tokens[i + 1]
            init = tree(tokens, i + 2)[0]
            try:
                size = KEYBOARD_SIZES[init[0].split("::")[-1]]
                values = init[1]  # { {defaults}, { {LAYOUT, {rows}}, ... } }
                edits = init[2]
            except (KeyError, IndexError) as e:
                skip(f"layout {lname}: {e}")
                continue
            regional = {}
            overlay_map = values[1] if len(values) > 1 else []
            for entry in overlay_map:
                if isinstance(entry, list) and len(entry) == 2 and entry[0] in KEYBOARD_LAYOUTS:
                    regional[KEYBOARD_LAYOUTS[entry[0]]] = key_rows(entry[1], names)
                else:
                    skip(f"layout {lname}: unparsed regional entry {entry!r:.60}")
            layouts[lname] = {"size": size, "regional": regional, "edits": key_rows(edits, names)}
    return layouts


def parse_zones(tokens: List[str], names: Dict[str, str]) -> Dict[str, dict]:
    zones: Dict[str, dict] = {}
    for i, t in enumerate(tokens):
        if t == "razer_zone" and tokens[i + 2] == "=" and tokens[i + 3] == "{":
            zname = tokens[i + 1]
            init = tree(tokens, i + 3)[0]
            try:
                title = unquote(init[0]) if init[0].startswith('"') else names[init[0]]
                zones[zname] = {
                    "name": title,
                    "type": ZONE_TYPES[init[1]],
                    "rows": int(init[2], 0),
                    "cols": int(init[3], 0),
                }
            except (KeyError, IndexError, ValueError) as e:
                skip(f"zone {zname}: {e}")
    return zones


def parse_devices(tokens: List[str], pids: Dict[str, int], zones: Dict[str, dict], layouts: Dict[str, dict]):
    devices: Dict[str, dict] = {}
    for i, t in enumerate(tokens):
        if t == "razer_device" and tokens[i + 2] == "=" and tokens[i + 3] == "{":
            dname = tokens[i + 1]
            init = tree(tokens, i + 3)[0]
            try:
                name = unquote(init[0])
                pid_macro = init[1]
                dtype = init[2].replace("DEVICE_TYPE_", "")
                if dtype not in DEVICE_TYPES:
                    raise KeyError(init[2])
                zone_list = []
                for z in init[7]:
                    if z == "NULL":
                        continue
                    zone_list.append(zones[z.lstrip("&")])
                layout_tok = init[8] if len(init) > 8 else "NULL"
                layout = None if layout_tok == "NULL" else layout_tok.lstrip("&")
                if layout is not None and layout not in layouts:
                    raise KeyError(layout)
                devices[dname] = {
                    "name": name,
                    "pid": pids[pid_macro],
                    "pid_macro": pid_macro,
                    "device_type": dtype,
                    "type": TYPE_NAMES.get(dtype, "Accessory"),
                    "matrix_type": MATRIX_TYPES[init[3]],
                    "transaction_id": int(init[4], 0),
                    "rows": int(init[5], 0),
                    "cols": int(init[6], 0),
                    "zones": zone_list,
                    "layout": layout,
                }
            except (KeyError, IndexError, ValueError) as e:
                skip(f"device {dname}: {e}")
    return devices


def parse_master_list(tokens: List[str]) -> List[str]:
    init = find_init(tokens, "razer_device_list[]") or find_init(tokens, "razer_device_list")
    if init is None:
        # tokens: razer_device_list [ ] = {
        for i, t in enumerate(tokens):
            if t == "razer_device_list" and tokens[i + 3] == "=" and tokens[i + 4] == "{":
                init = tree(tokens, i + 4)[0]
                break
    if init is None:
        skip("device master list not found")
        return []
    return [t.lstrip("&") for t in init if isinstance(t, str)]


# ---------------------------------------------------------------- RazerControllerDetect.cpp
def parse_detect(
    path: str, pids: Dict[str, int]
) -> Dict[int, List[Tuple[Optional[int], Optional[int], Optional[int]]]]:
    """pid -> [(interface or None, usage_page or None, usage or None)], for DetectRazerControllers only."""
    rules: Dict[int, list] = {}
    in_win32 = False
    src = strip_comments(open(path).read())
    for line in src.splitlines():
        s = line.strip()
        if s.startswith("#ifdef _WIN32"):
            in_win32 = True
            continue
        if s.startswith("#else"):
            in_win32 = False
            continue
        if s.startswith("#endif"):
            in_win32 = False
            continue
        if in_win32:
            continue
        m = re.match(
            r'REGISTER_HID_DETECTOR_(IPU|PU|I)\s*\(\s*"[^"]*"\s*,\s*(\w+)\s*,\s*(\w+)\s*,\s*(\w+)\s*,([^)]*)\)', s
        )
        if not m:
            continue
        kind, detector, vid, pid_macro, rest = m.groups()
        if detector != "DetectRazerControllers" or vid != "RAZER_VID":
            continue
        if pid_macro not in pids:
            skip(f"detector for unknown PID macro {pid_macro}")
            continue
        nums = [int(x.strip(), 0) for x in rest.split(",") if x.strip()]
        if kind == "IPU" and len(nums) == 3:
            rule = (nums[0], nums[1], nums[2])
        elif kind == "PU" and len(nums) == 2:
            rule = (None, nums[0], nums[1])
        elif kind == "I" and len(nums) == 1:
            rule = (nums[0], None, None)
        else:
            skip(f"detector line {s[:80]}")
            continue
        if rule not in rules.setdefault(pids[pid_macro], []):
            rules[pids[pid_macro]].append(rule)
    return rules


# ---------------------------------------------------------------- RazerController.cpp quirk lists
def cases_before(src: str, marker: str, pids: Dict[str, int]) -> List[int]:
    """PIDs of the `case RAZER_*_PID:` labels that directly precede the first line matching `marker`."""
    lines = strip_comments(src).splitlines()
    idx = next((i for i, line in enumerate(lines) if re.search(marker, line)), None)
    if idx is None:
        skip(f"quirk marker not found: {marker}")
        return []
    out: List[int] = []
    i = idx - 1
    while i >= 0:
        s = lines[i].strip()
        m = re.match(r"case\s+(RAZER_\w+_PID)\s*:", s)
        if m:
            if m.group(1) in pids:
                out.append(pids[m.group(1)])
            else:
                skip(f"quirk case for unknown PID macro {m.group(1)}")
        elif s not in ("", "{", "default:"):
            break
        i -= 1
    return sorted(set(out))


# ---------------------------------------------------------------- output
def py_str(s: str) -> str:
    return repr(s)


def fmt_rows(rows, indent: str) -> str:
    return "".join(f"{indent}({r}, {c}, {py_str(n)}, {py_str(a)}, {op}),\n" for r, c, n, a, op in rows)


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/src/OpenRGB")
    rz = os.path.join(root, "Controllers", "RazerController")
    names = parse_key_names(os.path.join(root, "RGBController", "RGBControllerKeyNames.cpp"))
    pids = parse_pids(os.path.join(rz, "RazerDevices.h"))
    base, regional = parse_klm(os.path.join(root, "KeyboardLayoutManager", "KeyboardLayoutManager.cpp"), names)

    dev_src = open(os.path.join(rz, "RazerDevices.cpp"), encoding="utf-8").read()
    tokens = toks(dev_src)
    layouts = parse_layouts(tokens, names)
    zones = parse_zones(tokens, names)
    devices = parse_devices(tokens, pids, zones, layouts)
    order = parse_master_list(tokens)
    detect = parse_detect(os.path.join(rz, "RazerControllerDetect.cpp"), pids)
    ctl_src = open(os.path.join(rz, "RazerController", "RazerController.cpp")).read()

    quirks = {
        "LED_ID_ZERO_PIDS": cases_before(ctl_src, r"dev_led_id\s*=\s*RAZER_LED_ID_ZERO;", pids),
        "WAVE_PIDS": cases_before(ctl_src, r"supports_wave\s*=\s*true;", pids),
        "NO_BREATHING_PIDS": cases_before(ctl_src, r"supports_breathing\s*=\s*false;", pids),
        "SOFTWARE_MODE_PIDS": cases_before(ctl_src, r"razer_set_device_mode\(RAZER_DEVICE_MODE_SOFTWARE\);", pids),
        "KEEPALIVE_PIDS": cases_before(ctl_src, r"keepalive_thread_run\s*=\s*true;", pids),
        "REPORT_INDEX_07_PIDS": cases_before(ctl_src, r"report_index\s*=\s*0x07;", pids),
        "BRIGHTNESS_100_PIDS": cases_before(ctl_src, r"max_brightness\s*=\s*100;", pids),
    }

    # master list order, last entry wins on duplicate PIDs (RazerController's lookup loop keeps the last match)
    table: Dict[int, dict] = {}
    not_detected = []
    for dname in order:
        d = devices.get(dname)
        if d is None:
            skip(f"master list entry {dname} has no parsed device")
            continue
        if d["pid"] not in detect:
            not_detected.append(f"{d['name']} (0x{d['pid']:04X}, {d['device_type']})")
            continue
        if d["pid"] in table:
            print(f"note: duplicate PID 0x{d['pid']:04X}: {table[d['pid']]['name']} replaced by {d['name']}")
        d["detect"] = detect[d["pid"]]
        table[d["pid"]] = d
    used_layouts = sorted({d["layout"] for d in table.values() if d["layout"]})

    lines = []
    w = lines.append
    w('"""')
    w("Razer device table for rgbdeck.hw.razer -- GENERATED by tools/gen_razer_table.py, do not edit.")
    w("")
    w("Source: OpenRGB (GPL-2.0-or-later) Controllers/RazerController/RazerDevices.{h,cpp},")
    w("RazerControllerDetect.cpp, RazerController/RazerController.cpp, KeyboardLayoutManager/KeyboardLayoutManager.cpp")
    w("and RGBController/RGBControllerKeyNames.cpp.  Only PIDs that OpenRGB registers for DetectRazerControllers")
    w("are included (Kraken headsets, the Hanbo cooler and the Chroma ARGB controller use other protocols).")
    w("")
    w("Zone dict: name, type (0 single, 1 linear, 2 matrix), rows, cols.")
    w("Layout edit tuple: (row, col, name, alt_name, opcode) with opcode 0 insert+shift right, 1 swap only,")
    w("2 remove+shift left, 4 insert row, 5 remove row, 6 add alternate name (see KeyboardLayoutManager.h).")
    w("KEYBOARD_SIZE bits: 1 main block, 2 function row, 4 extras (nav cluster), 8 numpad.")
    w("KEYBOARD_LAYOUT: 1 ANSI QWERTY, 2 ISO QWERTY, 3 ISO QWERTZ, 4 ISO AZERTY, 5 JIS, 6 ABNT2.")
    w('"""')
    w("from __future__ import annotations")
    w("")
    w("VID = 0x1532")
    w("")
    w("# RAZER_<name>_PID macros from RazerDevices.h (name without the RAZER_ prefix and _PID suffix)")
    w("PIDS = {")
    for macro, pid in sorted(pids.items(), key=lambda kv: kv[0]):
        w(f"    {py_str(macro[len('RAZER_') : -len('_PID')])}: 0x{pid:04X},")
    w("}")
    w("")
    w("# Per-PID quirks ported from RazerController.cpp switch blocks")
    for k, v in quirks.items():
        w(f"{k} = frozenset([{', '.join(f'0x{p:04X}' for p in v)}])")
    w("")
    w("# KeyboardLayoutManager.cpp base key blocks: (row, col, name, alt_name, opcode)")
    w("KLM_BASE = {")
    for k, rows in base.items():
        w(f"    {py_str(k)}: [")
        w(fmt_rows(rows, "        ").rstrip("\n"))
        w("    ],")
    w("}")
    w("")
    w("# KeyboardLayoutManager.cpp regional overlays keyed by KEYBOARD_LAYOUT")
    w("KLM_REGIONAL = {")
    for k, rows in regional.items():
        w(f"    {k}: [")
        w(fmt_rows(rows, "        ").rstrip("\n"))
        w("    ],")
    w("}")
    w("")
    w("# RazerDevices.cpp keyboard_keymap_overlay_values tables")
    w("LAYOUTS = {")
    for lname in used_layouts:
        lay = layouts[lname]
        w(f"    {py_str(lname)}: {{")
        w(f"        'size': {lay['size']},")
        w("        'regional': {")
        for k, rows in lay["regional"].items():
            w(f"            {k}: [")
            w(fmt_rows(rows, "                ").rstrip("\n"))
            w("            ],")
        w("        },")
        w("        'edits': [")
        w(fmt_rows(lay["edits"], "            ").rstrip("\n"))
        w("        ],")
        w("    },")
    w("}")
    w("")
    w("# pid -> device entry.  detect: (usb interface or None, hid usage page or None, hid usage or None) rules,")
    w("# any of which may match the hidraw node (RazerControllerDetect.cpp REGISTER_HID_DETECTOR_*).")
    w("DEVICES = {")
    for pid, d in table.items():
        w(f"    0x{pid:04X}: {{")
        w(
            f"        'name': {py_str(d['name'])}, 'type': {py_str(d['type'])}, 'device_type': {py_str(d['device_type'])},"
        )
        w(
            f"        'matrix_type': {d['matrix_type']}, 'transaction_id': 0x{d['transaction_id']:02X}, "
            f"'rows': {d['rows']}, 'cols': {d['cols']},"
        )
        w("        'zones': [")
        for z in d["zones"]:
            w(
                f"            {{'name': {py_str(z['name'])}, 'type': {z['type']}, 'rows': {z['rows']}, 'cols': {z['cols']}}},"
            )
        w("        ],")
        w(f"        'layout': {py_str(d['layout']) if d['layout'] else None},")
        w(f"        'detect': {d['detect']!r},")
        w("    },")
    w("}")
    w("")
    w("USB_IDS = [(VID, pid) for pid in DEVICES]")
    w("")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    by_type: Dict[str, int] = {}
    for d in table.values():
        by_type[d["device_type"]] = by_type.get(d["device_type"], 0) + 1
    print(f"wrote {os.path.relpath(OUT)}: {len(table)} devices, {len(used_layouts)} keyboard layouts")
    print("by OpenRGB type:", ", ".join(f"{k} {v}" for k, v in sorted(by_type.items())))
    print(f"in table but not handled by DetectRazerControllers ({len(not_detected)}):")
    for n in not_detected:
        print("   ", n)
    print(f"skipped/unparsed entries: {len(skipped)}")


if __name__ == "__main__":
    main()
