#!/usr/bin/env python3
"""Deathwatch (4.6) save probe / editor (read-only parsing + targeted rewrites).

Two save files:
  * <APPDATA>/DeathwatchGame/Saved/SaveGames/dw_metagame.save  -- meta-game (progress / inventory / unit abilities / wargear)
  * <APPDATA>/DeathwatchGame/Saved/SaveGames/<slot>.sav        -- single campaign / mission save (no local sample)

Serialization: UE4 FObjectAndNameAsStringProxyArchive (ASCII length-prefixed strings + property triples).
Actual byte layout of each property (verified byte by byte in dw_metagame.save):
  name(lps: int32 byte count incl NUL + ASCII) →
  type(lps: e.g. StrProperty/ObjectProperty/IntProperty/BoolProperty/...) →
  Size(int32) →
  ArrayIndex(int32) →
  value (by type: Str/Name/Object = lps; Int = int32; Float = f32; Bool = 1 byte right after ArrayIndex)

Important: value is preceded by a 4-byte ArrayIndex (a standard FPropertyTag field); early parsers
missed it and read misaligned empty strings. Also Str/Object values are themselves lps, so the
value occupies 4 + datalen bytes.

Observed property model (from deserializing dw_metagame.save):
  GameInst_Metagame
    UnitMetagameInstance_0..4           per-unit progress (5 marines)
      RangedWargearTemplateName  Str   e.g. WT_T1_Bolter / WT_T1_PlasmaGun
      MeleeWargearTemplateName   Str   e.g. WT_T1_Chainsword / WT_T1_Poweraxe
      AbilityMetagameInstance_0..N → AbilityTemplateClass(Object) /Game/Abilities/.../ABA_xxx
    WargearMetagameInstance_0..9 + WargearInventoryID(Int) + WargearList   owned wargear
    ProgressTrackerSaveData / CompletedMissionNames / CompletedMissionDifficulties  progress

Note: in the meta-game save, the **same unit's weapon / ability properties are serialized twice**
(e.g. RangedWargearTemplateName appears 10 times = 5 units x 2), likely a "current equipment" copy
and a "template copy". When changing weapons / abilities, **all occurrences must be updated**,
or the game will load the unchanged one. set-wargear locates and updates all occurrences by unit.

Usage:
  python save_game_probe.py parse <path.save>            # print a readable summary (unit → weapons / abilities)
  python save_game_probe.py ls-wargear <path.save>      # list all WT_ template names present
  python save_game_probe.py set-wargear <path.save> <unit> <ranged> <melee> [--out NEW.save]
        # rewrite a unit's ranged / melee template names (reproduce specific weapons)
  python save_game_probe.py --check                     # parse only the bundled sample to confirm it works
"""
from __future__ import annotations
import argparse
import json
import os
import struct
import re
import sys

# bundled sample (a real meta-game save)
SAMPLE = os.path.expandvars(
    r"C:\Users\9600ES\AppData\Local\DeathwatchGame\Saved\SaveGames\dw_metagame.save")

OBJ_RE = (b"UnitMetagameInstance_", b"AbilityMetagameInstance_",
          b"WargearMetagameInstance_", b"GameInst_Metagame")


def lps(b: bytes, i: int):
    """length-prefixed string (int32 byte-len incl NUL). returns (text, new_i)."""
    n = struct.unpack_from("<i", b, i)[0]
    i += 4
    if n < 0:
        raw = b[i:i - n * 2]
        i += -n * 2
        return raw.decode("utf-16-le", "replace").rstrip("\x00"), i
    raw = b[i:i + n]
    i += n
    return raw.decode("ascii", "replace").rstrip("\x00"), i


TARGETS = {
    "RangedWargearTemplateName": "Str",
    "MeleeWargearTemplateName": "Str",
    "AbilityTemplateClass": "Object",
    "WargearInventoryID": "Int",
    "NextWargearInventoryID": "Int",
}


def _read_value(b: bytes, pos_after_name: int):
    """UE4 FPropertyTag layout (FObjectAndNameAsStringProxyArchive):
        name(lps) → type(lps) → Size(int32) → ArrayIndex(int32) → value
      * Str/Object/Name: value = lps (length-prefixed string)
      * Int: value = int32; Float: value = float32
      * Bool: a 1-byte bool value right after ArrayIndex (Size is usually 0)
    Returns (type, value_str, p_end)."""
    try:
        typ, p = lps(b, pos_after_name)
    except Exception:
        return None, None, pos_after_name
    if typ not in ("StrProperty", "ObjectProperty", "IntProperty", "FloatProperty",
                   "BoolProperty", "NameProperty", "ByteProperty", "EnumProperty"):
        return typ, None, p
    if p + 8 > len(b):
        return typ, None, p
    _size = struct.unpack_from("<i", b, p)[0]; p += 4      # Size
    _array_index = struct.unpack_from("<i", b, p)[0]; p += 4  # ArrayIndex
    val = None
    if typ in ("StrProperty", "ObjectProperty", "NameProperty"):
        if p + 4 <= len(b):
            val, p = lps(b, p)
    elif typ == "IntProperty":
        if p + 4 <= len(b):
            val = str(struct.unpack_from("<i", b, p)[0]); p += 4
    elif typ == "FloatProperty":
        if p + 4 <= len(b):
            val = str(struct.unpack_from("<f", b, p)[0]); p += 4
    elif typ == "BoolProperty":
        if p + 1 <= len(b):
            val = "True" if b[p] else "False"; p += 1
    elif typ in ("ByteProperty", "EnumProperty"):
        # enum/byte: usually followed by the enum name as lps
        if p + 4 <= len(b):
            val, p = lps(b, p)
        else:
            val = "<%s size=%d>" % (typ, _size); p += max(0, _size)
    else:
        val = "<%s size=%d>" % (typ, _size)
        p += max(0, _size)
    return typ, val, p


def _locate_prop(b: bytes, name: str):
    """Locate all occurrences of the property named `name`; returns:
       [{pos_name, namelen, type_pos, size_pos, arrayindex_pos, value_pos, ...}].
       value_pos points to the start of the value's lps (Str/Object/Name) or i32 (Int)."""
    nb = name.encode()
    occ = []
    idx = 0
    while True:
        j = b.find(nb, idx)
        if j < 0:
            break
        idx = j + 1
        if j < 4:
            continue
        namelen = struct.unpack_from("<i", b, j - 4)[0]
        if namelen <= 0 or namelen > 200:
            continue
        after = j + namelen
        try:
            typ, p = lps(b, after)
        except Exception:
            continue
        if p + 8 > len(b):
            continue
        size_pos = p
        arrayindex_pos = p + 4
        value_pos = arrayindex_pos + 4
        occ.append(dict(pos_name=j, namelen=namelen, type=typ,
                        size_pos=size_pos, arrayindex_pos=arrayindex_pos,
                        value_pos=value_pos))
    return occ


def parse_props(b: bytes):
    """Robust parsing: locate each target property name with find, then parse type/value directly;
    object context = the nearest preceding object instance whose prefix matches the property's owner.
    Returns [(obj, name, type, value)]."""
    # object name positions per prefix (for context attribution)
    span_by_prefix = {
        "Unit":   [m.start() for m in re.finditer(rb"UnitMetagameInstance_\d+", b)],
        "Ability":[m.start() for m in re.finditer(rb"AbilityMetagameInstance(?:_\d+)?", b)],
        "Wargear":[m.start() for m in re.finditer(rb"WargearMetagameInstance_\d+", b)],
        "Root":   [m.start() for m in re.finditer(rb"GameInst_Metagame", b)],
    }

    def ctx_of(pos, prefix):
        spans = span_by_prefix.get(prefix, [])
        c = "(root)"
        for s in spans:
            if s <= pos:
                c = b[s:s + 40].split(b"\x00")[0].decode("ascii", "replace")
            else:
                break
        return c

    prefix_for = {
        "RangedWargearTemplateName": "Unit",
        "MeleeWargearTemplateName": "Unit",
        # abilities are nested in units: attribute to the nearest UnitMetagameInstance
        "AbilityTemplateClass": "Unit",
        "WargearInventoryID": "Wargear",
        "NextWargearInventoryID": "Root",
    }

    out = []
    for name in TARGETS:
        nb = name.encode()
        idx = 0
        while True:
            j = b.find(nb, idx)
            if j < 0:
                break
            idx = j + 1
            namelen = struct.unpack_from("<i", b, max(0, j - 4))[0] if j >= 4 else -1
            if namelen <= 0 or namelen > 200:
                continue
            after = j + namelen
            typ, val, _ = _read_value(b, after)
            if typ is None or val is None:
                continue
            out.append((ctx_of(j, prefix_for[name]), name, typ, val))
    return out


def summarize(props):
    units = {}
    for obj, name, typ, val in props:
        if not obj.startswith("UnitMetagameInstance_"):
            continue
        u = units.setdefault(obj, {"ranged": None, "melee": None, "abilities": []})
        if name == "RangedWargearTemplateName":
            u["ranged"] = val
        elif name == "MeleeWargearTemplateName":
            u["melee"] = val
        elif name == "AbilityTemplateClass":
            if val not in u["abilities"]:
                u["abilities"].append(val)
    return units


def cmd_parse(a):
    p = a.path or SAMPLE
    props = parse_props(open(p, "rb").read())
    print("=== parse %s ===" % p)
    print("  property records: %d" % len(props))
    units = summarize(props)
    for u in sorted(units):
        d = units[u]
        print("  %-26s 主=%s 副=%s 技能=%d" % (u, d["ranged"], d["melee"], len(d["abilities"])))
        for ab in d["abilities"]:
            print("       - %s" % ab)
    return 0


def cmd_ls_wargear(a):
    p = a.path or SAMPLE
    b = open(p, "rb").read()
    import re
    names = sorted(set(re.findall(rb"WT_T\d+_[A-Za-z0-9_]+", b)))
    print("=== wargear template names in %s ===" % p)
    for nm in names:
        print("  ", nm.decode())
    return 0


def _owning_unit(b: bytes, pos: int):
    """Return N of the nearest UnitMetagameInstance_N before pos (for attribution)."""
    best = None
    for m in re.finditer(rb"UnitMetagameInstance_(\d+)", b):
        if m.start() <= pos:
            best = int(m.group(1))
        else:
            break
    return best


def cmd_set_wargear(a):
    p = a.path or SAMPLE
    b = open(p, "rb").read()
    out = bytearray(b)
    edits = 0
    # For each occurrence: patch the value range [value_pos:v_end] and update Size,
    # leaving ArrayIndex untouched (otherwise the lps length prefix would be consumed and parsing breaks).
    ops = []  # (value_pos, v_end, new_val_lps, size_pos, new_size, name, old, new)
    for name, newval in (("RangedWargearTemplateName", a.ranged),
                         ("MeleeWargearTemplateName", a.melee)):
        if newval is None:
            continue
        # collect all occurrences under this unit (the same property is serialized multiple times; all must be rewritten)
        occ = _locate_prop(bytes(out), name)
        for o in occ:
            if _owning_unit(bytes(out), o["pos_name"]) != a.unit:
                continue
            old_val, v_end = lps(bytes(out), o["value_pos"])
            new_val_lps = struct.pack("<i", len(newval) + 1) + newval.encode() + b"\x00"
            new_size = len(new_val_lps)  # Size = total bytes of value data
            ops.append((o["value_pos"], v_end, new_val_lps, o["size_pos"], new_size, name, old_val, newval))
    if not ops:
        print("  [warn] no matching property for unit %d" % a.unit)
    # apply in reverse (descending value_pos) so offsets stay valid
    ops.sort(key=lambda r: r[0], reverse=True)
    for value_pos, v_end, new_val_lps, size_pos, new_size, name, old, new in ops:
        out[value_pos:v_end] = new_val_lps          # replace value data only
        out[size_pos:size_pos + 4] = struct.pack("<i", new_size)  # update Size
        edits += 1
        print("  SET unit%d.%s: %r -> %r" % (a.unit, name, old, new))
    dst = a.out or (p + ".edited.save")
    open(dst, "wb").write(bytes(out))
    print("  wrote %s  (edits=%d)" % (dst, edits))
    return 0


# ---- save injection functions (directly importable; no CLI side effects) ----


def _find_int_prop(b: bytes, name: str) -> list[dict]:
    """Locate all IntProperty/UInt32Property occurrences; returns [{pos_name, size_pos, value_pos}]."""
    out = []
    for o in _locate_prop(b, name):
        if o["type"] in ("IntProperty", "UInt32Property"):
            out.append(o)
    return out


def _read_int(b: bytes, value_pos: int, unsigned: bool = False) -> int:
    if unsigned:
        return struct.unpack_from("<I", b, value_pos)[0]
    return struct.unpack_from("<i", b, value_pos)[0]


def _write_int(b: bytes, value_pos: int, val: int, unsigned: bool = False) -> None:
    if unsigned:
        struct.pack_into("<I", b, value_pos, val)
    else:
        struct.pack_into("<i", b, value_pos, val)


def _find_str_prop(b: bytes, name: str) -> list[dict]:
    """Locate all StrProperty occurrences."""
    out = []
    for o in _locate_prop(b, name):
        if o["type"] == "StrProperty":
            out.append(o)
    return out


def _read_str_val(b: bytes, value_pos: int) -> str:
    v, _ = lps(b, value_pos)
    return v


def _write_str_val(b: bytes, value_pos: int, newval: str) -> int:
    """Write a new StrProperty value; returns the new Size."""
    new_lps = struct.pack("<i", len(newval) + 1) + newval.encode() + b"\x00"
    b[value_pos:value_pos + len(new_lps)] = new_lps
    return len(new_lps)


def set_cardpacks(b: bytes, count: int) -> bytes:
    """Set Requisition high enough to buy `count` card packs.

    Strategy: Requisition = max(100 * count, current) + 100 * count
    (ensures enough points to buy `count` packs in the CARD SHOP).
    BonusPacks stays True and CardPackSeed is unchanged (fixed seed = reproducible draws).
    """
    out = bytearray(b)
    # find the Requisition IntProperty
    occ = _find_int_prop(bytes(out), "Requisition")
    if not occ:
        print("  [set_cardpacks] 未找到 Requisition 属性")
        return bytes(out)
    o = occ[0]
    old_val = _read_int(bytes(out), o["value_pos"])
    new_val = max(100 * count, old_val) + 100 * count
    _write_int(out, o["value_pos"], new_val)
    print(f"  [set_cardpacks] Requisition: {old_val} -> {new_val} (够买 {count} 包 @ ¤100)")
    return bytes(out)


def set_cardpack_seed(b: bytes, seed: int) -> bytes:
    """Set CardPackSeed (a fixed seed makes draws reproducible)."""
    out = bytearray(b)
    occ = _locate_prop(bytes(out), "CardPackSeed")
    if not occ:
        print("  [set_cardpack_seed] 未找到 CardPackSeed 属性")
        return bytes(out)
    o = occ[0]
    old_val = _read_int(bytes(out), o["value_pos"], unsigned=True)
    _write_int(out, o["value_pos"], seed, unsigned=True)
    print(f"  [set_cardpack_seed] CardPackSeed: {old_val} (0x{old_val:08X}) -> {seed} (0x{seed:08X})")
    return bytes(out)


def set_unit_template(b: bytes, unit_idx: int, template_name: str) -> bytes:
    """Set the ranged weapon template name of a given unit.

    Same logic as set-wargear: find all RangedWargearTemplateName occurrences for the unit,
    rewrite them all and update Size, applying in descending value_pos order to avoid offset shifts.
    """
    out = bytearray(b)
    occ = _locate_prop(bytes(out), "RangedWargearTemplateName")
    ops = []
    for o in occ:
        if _owning_unit(bytes(out), o["pos_name"]) != unit_idx:
            continue
        old_val, v_end = lps(bytes(out), o["value_pos"])
        new_val_lps = struct.pack("<i", len(template_name) + 1) + template_name.encode() + b"\x00"
        new_size = len(new_val_lps)
        ops.append((o["value_pos"], v_end, new_val_lps, o["size_pos"], new_size, old_val))
    if not ops:
        print(f"  [set_unit_template] 未找到 unit{unit_idx} 的 RangedWargearTemplateName")
        return bytes(out)
    ops.sort(key=lambda r: r[0], reverse=True)
    for value_pos, v_end, new_val_lps, size_pos, new_size, old in ops:
        out[value_pos:v_end] = new_val_lps
        out[size_pos:size_pos + 4] = struct.pack("<i", new_size)
        print(f"  [set_unit_template] unit{unit_idx}: {old!r} -> {template_name!r}")
    return bytes(out)


def add_wargear(b: bytes, template_name: str) -> bytes:
    """Append a new wargear instance to WargearList.

    Steps:
    1. append a new WargearMetagameInstance_N at the end of the WargearList array
    2. update the array element count
    3. update the CollectedWargearNames array
    4. update NextWargearInventoryID
    """
    out = bytearray(b)
    # find the WargearList ArrayProperty
    wl_occ = _locate_prop(bytes(out), "WargearList")
    if not wl_occ:
        print("  [add_wargear] 未找到 WargearList 数组")
        return bytes(out)
    wl = wl_occ[0]
    # find the element-count position (ArrayProperty: name → type → Size → ArrayIndex → count → data)
    # array data starts at after_type + 8; the first 4 bytes are the element count
    name, after_name = lps(bytes(out), wl["pos_name"] - 4)
    typ, after_type = lps(bytes(out), after_name)
    size_val = struct.unpack_from("<i", bytes(out), after_type)[0]
    arr_data_start = after_type + 8
    elem_count = struct.unpack_from("<i", bytes(out), arr_data_start)[0]
    print(f"  [add_wargear] 当前 WargearList: {elem_count} 个元素, Size={size_val}")

    # find the current maximum WargearInventoryID
    next_id_occ = _locate_prop(bytes(out), "NextWargearInventoryID")
    if next_id_occ:
        next_id = _read_int(bytes(out), next_id_occ[0]["value_pos"])
        new_id = next_id
    else:
        # infer from existing WargearInventoryID values
        all_ids = [_read_int(bytes(out), o["value_pos"]) for o in _find_int_prop(bytes(out), "WargearInventoryID")]
        new_id = max(all_ids) + 1 if all_ids else 0
        print(f"  [add_wargear] NextWargearInventoryID 未找到，推断新 ID={new_id}")

    # build the new WargearMetagameInstance element
    # layout: ObjectProperty → /Engine/Transient → WargearMetagameInstance_N →
    #       RangedWargearTemplateName → StrProperty → Size → ArrayIndex → value
    inst_name = f"WargearMetagameInstance_{new_id}"
    inst_obj = (
        struct.pack("<i", len("ObjectProperty") + 1) + b"ObjectProperty\x00"
        + struct.pack("<i", len("/Engine/Transient") + 1) + b"/Engine/Transient\x00"
        + struct.pack("<i", len(inst_name) + 1) + inst_name.encode() + b"\x00"
        + struct.pack("<i", len("RangedWargearTemplateName") + 1) + b"RangedWargearTemplateName\x00"
        + struct.pack("<i", len("StrProperty") + 1) + b"StrProperty\x00"
    )
    # Size and ArrayIndex are patched later (placeholders for now)
    template_lps = struct.pack("<i", len(template_name) + 1) + template_name.encode() + b"\x00"
    # WargearInventoryID IntProperty
    wid_name = "WargearInventoryID"
    wid_lps = (
        struct.pack("<i", len(wid_name) + 1) + wid_name.encode() + b"\x00"
        + struct.pack("<i", len("IntProperty") + 1) + b"IntProperty\x00"
    )
    # compute the Size field (total bytes of value data)
    ranged_size = len(template_lps)
    wid_size = 4

    # assemble the full element
    elem_data = inst_obj
    # Size for RangedWargearTemplateName
    elem_data += struct.pack("<i", ranged_size)
    elem_data += struct.pack("<i", 0)  # ArrayIndex
    elem_data += template_lps
    # WargearInventoryID
    elem_data += struct.pack("<i", len(wid_name) + 1) + wid_name.encode() + b"\x00"
    elem_data += struct.pack("<i", len("IntProperty") + 1) + b"IntProperty\x00"
    elem_data += struct.pack("<i", wid_size)
    elem_data += struct.pack("<i", 0)  # ArrayIndex
    elem_data += struct.pack("<i", new_id)

    print(f"  [add_wargear] 新元素: {inst_name}  主武器={template_name}  ID={new_id}")

    # insert at the end of the array (before Size; increment elem_count first)
    # insertion point = arr_data_start + 4 + size_val (end of array data)
    insert_pos = arr_data_start + 4 + size_val
    # increment the element count
    struct.pack_into("<i", out, arr_data_start, elem_count + 1)
    # insert the new element
    out[insert_pos:insert_pos] = elem_data
    # update Size (old Size + new element size)
    new_size = size_val + len(elem_data)
    struct.pack_into("<i", out, after_type, new_size)
    print(f"  [add_wargear] WargearList: {elem_count} -> {elem_count + 1}  Size: {size_val} -> {new_size}")

    # update CollectedWargearNames
    cn_occ = _locate_prop(bytes(out), "CollectedWargearNames")
    if cn_occ:
        cn = cn_occ[0]
        cn_name, cn_after_name = lps(bytes(out), cn["pos_name"] - 4)
        cn_typ, cn_after_type = lps(bytes(out), cn_after_name)
        cn_size = struct.unpack_from("<i", bytes(out), cn_after_type)[0]
        cn_arr_start = cn_after_type + 8
        cn_elem_count = struct.unpack_from("<i", bytes(out), cn_arr_start)[0]
        # insert the new name
        cn_template_lps = struct.pack("<i", len(template_name) + 1) + template_name.encode() + b"\x00"
        cn_insert_pos = cn_arr_start + 4 + cn_size
        out[cn_insert_pos:cn_insert_pos] = cn_template_lps
        struct.pack_into("<i", out, cn_arr_start, cn_elem_count + 1)
        new_cn_size = cn_size + len(cn_template_lps)
        struct.pack_into("<i", out, cn_after_type, new_cn_size)
        print(f"  [add_wargear] CollectedWargearNames: {cn_elem_count} -> {cn_elem_count + 1}")

    # update NextWargearInventoryID (re-locate after the insertion shifted offsets)
    nid_occ_new = _locate_prop(bytes(out), "NextWargearInventoryID")
    if nid_occ_new:
        _write_int(out, nid_occ_new[0]["value_pos"], new_id + 1)
        print(f"  [add_wargear] NextWargearInventoryID: {next_id} -> {new_id + 1}")
    else:
        print(f"  [add_wargear] 未找到 NextWargearInventoryID，跳过更新")

    return bytes(out)


# ---- CLI commands ----

def cmd_set_cardpacks(a):
    p = a.path or SAMPLE
    b = open(p, "rb").read()
    out = set_cardpacks(b, a.count)
    dst = a.out or (p + ".cardpacks.save")
    open(dst, "wb").write(out)
    print("  wrote %s" % dst)
    return 0


def cmd_set_unit(a):
    p = a.path or SAMPLE
    b = open(p, "rb").read()
    out = set_unit_template(b, a.unit, a.template)
    dst = a.out or (p + ".unit.save")
    open(dst, "wb").write(out)
    print("  wrote %s" % dst)
    return 0


def cmd_add_wargear(a):
    p = a.path or SAMPLE
    b = open(p, "rb").read()
    out = add_wargear(b, a.template)
    dst = a.out or (p + ".wargear.save")
    open(dst, "wb").write(out)
    print("  wrote %s" % dst)
    return 0


def cmd_set_seed(a):
    p = a.path or SAMPLE
    b = open(p, "rb").read()
    out = set_cardpack_seed(b, a.seed)
    dst = a.out or (p + ".seed.save")
    open(dst, "wb").write(out)
    print("  wrote %s" % dst)
    return 0


def main():
    ap = argparse.ArgumentParser(description="Deathwatch save probe/editor")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("parse"); p.add_argument("path", nargs="?", default=None); p.set_defaults(func=cmd_parse)
    p = sub.add_parser("ls-wargear"); p.add_argument("path", nargs="?", default=None); p.set_defaults(func=cmd_ls_wargear)
    p = sub.add_parser("set-wargear")
    p.add_argument("path", nargs="?"); p.add_argument("unit", type=int); p.add_argument("ranged", nargs="?"); p.add_argument("melee", nargs="?")
    p.add_argument("--out", default=None); p.set_defaults(func=cmd_set_wargear)
    p = sub.add_parser("set-cardpacks")
    p.add_argument("path", nargs="?"); p.add_argument("count", type=int); p.add_argument("--out", default=None)
    p.set_defaults(func=cmd_set_cardpacks)
    p = sub.add_parser("set-unit")
    p.add_argument("path", nargs="?"); p.add_argument("unit", type=int); p.add_argument("template")
    p.add_argument("--out", default=None); p.set_defaults(func=cmd_set_unit)
    p = sub.add_parser("add-wargear")
    p.add_argument("path", nargs="?"); p.add_argument("template"); p.add_argument("--out", default=None)
    p.set_defaults(func=cmd_add_wargear)
    p = sub.add_parser("set-seed")
    p.add_argument("path", nargs="?"); p.add_argument("seed", type=int); p.add_argument("--out", default=None)
    p.set_defaults(func=cmd_set_seed)
    p = sub.add_parser("check"); p.set_defaults(func=lambda a: cmd_parse(argparse.Namespace(path=None)))
    a = ap.parse_args()
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
