#!/usr/bin/env python3
"""Save injection / structure research -- the save layer of the patching self-test toolchain.

Design notes:
  * **Add unopened packs directly**: `set-unopened-packs N` (alias `set-cardpacks`) sets
    `Wallet.NumUnopenedPacks` (IntProperty). A save diff confirmed that UE4 omits this property
    at 0 packs, so a static scan of the reference save does not show it; the game writes it once
    packs are bought. The tool inserts a full 49-byte FPropertyTag and updates Wallet.Size, or
    patches the existing fixed-length value. Use `set-requisition` to control points separately.
  * **Change unit class / marine name**: the real fields are `TemplateName` (UTP_*) for the class
    and `UnitName` (e.g. Magnus...) for the marine's name -- not `RangedWargearTemplateName` (weapon).
  * **Two replicas**: each unit is serialized twice (UnitMetagameInstance_0..5 x 2); rewrites must
    hit every occurrence, applied in **descending** value_pos order with Size updated in sync.
  * **Safety**: callers must back up before any write; diff() verifies that only target fields changed.
  * **Template discovery**: list_templates() scans Content/Characters/UnitTemplates/UTP_*.uasset
    and WT_*.uasset instead of hard-coding a list.

CLI (standalone and importable):
  python save_inject.py scan [path.save]               # enumerate all properties
  python save_inject.py units [path.save]              # list units: index / name / class / weapons
  python save_inject.py templates unit|wargear [kw]    # discover template names from assets
  python save_inject.py inject <ops.json> --out X.save # apply operations from a JSON array
  python save_inject.py diff A.save B.save             # field-by-field diff
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import struct
import sys

if sys.stdout is not None:                     # no console when packaged with --windowed
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import save_game_probe as SGP  # noqa: E402  reuse low-level lps / _locate_prop without altering their CLI
import save_anatomy as SA     # noqa: E402  anatomy helpers: tag scanning / ancestor Size / ability arrays

ROOT = pathlib.Path(__file__).resolve().parents[1]          # steam patching/
CONTENT = ROOT / "sandbox" / "deathwatchgame" / "Content"   # default sandbox; the installer overrides this before injection
UNIT_TEMPLATES = CONTENT / "Characters" / "UnitTemplates"
ABILITIES = CONTENT / "Abilities"


def use_content(content_root) -> None:
    """Point asset discovery at a given game Content root (the installer must set this to the user's game directory)."""
    global CONTENT, UNIT_TEMPLATES, ABILITIES
    CONTENT = pathlib.Path(content_root)
    UNIT_TEMPLATES = CONTENT / "Characters" / "UnitTemplates"
    ABILITIES = CONTENT / "Abilities"

# unit fields we may rewrite (all StrProperty with lps values)
UNIT_STR_FIELDS = ("TemplateName", "UnitName", "RangedWargearTemplateName",
                   "MeleeWargearTemplateName")


def encode_lps(s: str) -> bytes:
    """UE4 FString length-prefixed encoding: ASCII uses a positive length; non-ASCII uses UTF-16LE with a negative length.

    Note: plain `newval.encode()` would write UTF-8 bytes with an ASCII length marker, producing
    garbled text or a corrupt save. Encoding here follows the FObjectAndNameAsStringProxyArchive
    rules (symmetric with the negative-length branch of save_game_probe.lps).
    """
    try:
        raw = s.encode("ascii") + b"\x00"
        return struct.pack("<i", len(raw)) + raw
    except UnicodeEncodeError:
        raw = s.encode("utf-16-le") + b"\x00\x00"
        return struct.pack("<i", -(len(s) + 1)) + raw


# ---- structure research ----

TYPES = {
    "StrProperty", "ObjectProperty", "IntProperty", "FloatProperty", "BoolProperty",
    "NameProperty", "ByteProperty", "EnumProperty", "ArrayProperty", "StructProperty",
    "MapProperty", "SetProperty", "TextProperty", "UInt32Property", "UInt64Property",
    "Int64Property", "UInt16Property", "Int16Property", "SoftObjectProperty",
    "LazyObjectProperty", "FieldPathProperty",
}


def scan_structure(b: bytes) -> dict:
    """Enumerate all (name, type) pairs plus key field values for structure research."""
    def lps(i):
        if i < 0 or i + 4 > len(b):
            return None, None
        n = struct.unpack_from("<i", b, i)[0]
        if n <= 0 or n > 300:
            return None, None
        end = i + 4 + n
        if end > len(b):
            return None, None
        try:
            return b[i + 4:end].rstrip(b"\x00").decode("ascii"), end
        except UnicodeDecodeError:
            return None, None

    pairs: dict[tuple[str, str], int] = {}
    for i in range(0, len(b) - 4):
        name, p1 = lps(i)
        if name is None or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            continue
        typ, _ = lps(p1)
        if typ in TYPES:
            pairs[(name, typ)] = pairs.get((name, typ), 0) + 1

    scalars = {}
    for nm, typ in (("Requisition", "IntProperty"), ("CardPackSeed", "UInt32Property"),
                    ("TwoLetterCultureSetting", "StrProperty"), ("BonusPacks", "BoolProperty"),
                    ("NumUnopenedPacks", "IntProperty")):
        occ = SGP._locate_prop(b, nm)
        vals = []
        for o in occ:
            if typ in ("IntProperty", "UInt32Property"):
                vals.append(SGP._read_int(b, o["value_pos"],
                                          unsigned=(typ == "UInt32Property")))
            elif typ == "StrProperty":
                vals.append(SGP._read_str_val(b, o["value_pos"]))
            elif typ == "BoolProperty":
                vals.append(bool(b[o["value_pos"]]))
        scalars[nm] = {"type": typ, "values": vals}

    return {"size": len(b), "unique_props": len(pairs),
            "props": [{"name": n, "type": t, "count": c}
                      for (n, t), c in sorted(pairs.items())],
            "scalars": scalars, "units": list_units(b)}


def list_units(b: bytes) -> list[dict]:
    """List units: index / marine name / class template / ranged and melee weapons (first replica per unit)."""
    out = {}
    for fld in UNIT_STR_FIELDS:
        for o in SGP._locate_prop(b, fld):
            if o["type"] != "StrProperty":
                continue
            u = SGP._owning_unit(b, o["pos_name"])
            if u is None:
                continue
            d = out.setdefault(u, {"unit": u, "UnitName": None, "TemplateName": None,
                                   "RangedWargearTemplateName": None,
                                   "MeleeWargearTemplateName": None,
                                   "occurrences": 0})
            v = SGP._read_str_val(b, o["value_pos"])
            if d[fld] is None:
                d[fld] = v
            d["occurrences"] += 1
    return [out[k] for k in sorted(out)]


def list_templates(kind: str = "unit", keyword: str = "") -> list[str]:
    """Discover template names from assets instead of a hard-coded list."""
    pat = "UTP_*.uasset" if kind == "unit" else "WT_*.uasset"
    if kind == "unit":
        files = UNIT_TEMPLATES.glob(pat)
    else:
        files = CONTENT.rglob(pat)
    names = sorted({p.stem for p in files})
    if keyword:
        names = [n for n in names if keyword.lower() in n.lower()]
    return names


# ---- targeted rewrites ----

def _set_str_all(b: bytes, field: str, unit_idx: int, newval: str) -> tuple[bytes, int]:
    """Set every occurrence (both replicas) of a StrProperty for a given unit to newval.

    Returns (new_bytes, edits). Applied in descending value_pos order with Size updated in sync.
    """
    out = bytearray(b)
    ops = []
    for o in SGP._locate_prop(bytes(out), field):
        if o["type"] != "StrProperty":
            continue
        if SGP._owning_unit(bytes(out), o["pos_name"]) != unit_idx:
            continue
        old, v_end = SGP.lps(bytes(out), o["value_pos"])
        new_lps = encode_lps(newval)
        ops.append((o["value_pos"], v_end, new_lps, o["size_pos"], len(new_lps), old))
    ops.sort(key=lambda r: r[0], reverse=True)
    for value_pos, v_end, new_lps, size_pos, new_size, old in ops:
        out[value_pos:v_end] = new_lps
        struct.pack_into("<i", out, size_pos, new_size)
        print(f"    [set] unit{unit_idx}.{field}: {old!r} -> {newval!r}")
    return bytes(out), len(ops)


def set_unit(b: bytes, unit_idx: int, template: str | None = None,
             name: str | None = None, ranged: str | None = None,
             melee: str | None = None) -> bytes:
    """Change one Space Marine: class template / marine name / ranged weapon / melee weapon.

    unit_idx is N in UnitMetagameInstance_N (0-based). Field meanings:
      template -> TemplateName                 class (UTP_*, discover with `templates unit`)
      name     -> UnitName                     marine display name
      ranged   -> RangedWargearTemplateName    ranged weapon (WT_*)
      melee    -> MeleeWargearTemplateName     melee weapon (WT_*)
    """
    out = b
    for fld, val in (("TemplateName", template), ("UnitName", name),
                     ("RangedWargearTemplateName", ranged),
                     ("MeleeWargearTemplateName", melee)):
        if val is None:
            continue
        out, n = _set_str_all(out, fld, unit_idx, val)
        if n == 0:
            print(f"    [warn] unit{unit_idx} 未找到 {fld} 的任何出现（跳过）")
    return out


def set_wargear_template(b: bytes, inventory_id: int, template: str,
                         equipped_to: int | None = None) -> tuple[bytes, dict]:
    """Rewrite the template name (bare lps, variable length) and/or owner
    (EquippedTeamRosterMemberID, fixed 4 bytes) of a wargear inventory entry WargearMetagameInstance_<id>.

    Why the inventory entry must be changed, not just the unit strings: on load the game rewrites a
    unit's Ranged/MeleeWargearTemplateName from the inventory entry (verified: changing only the
    strings with set-unit was reverted from the inventory's WT_T1_* on mission start, and the next
    save wrote T1 back). Inventory entry layout (verified byte by byte in the reference save):
      lps(/Script/DeathwatchGame) lps(WargearMetagameInstance) lps(entry name)
      lps(/Engine/Transient) IntProp(WargearInventoryID) IntProp(EquippedTeamRosterMemberID)
      lps(None) i32(0) bare lps(WT_ template name)
    The entry has no length field of its own; after a variable-length replacement, only the enclosing
    containers (WargearList / WargearInventory) need a Size update.

    Extended for consumables:
      * `template` may be a **WTP_/WDT_ consumable template** (stasis grenade WTP_StasisGrenade,
        stimpack WTP_T3_Stimpack, stasis field WTP_T4_StasisField); the reference save stores
        `WTP_FragGrenade` this way (verified in the bytes); older callers were limited to WT_/WTP_.
      * `equipped_to` = TeamRosterMemberID (1-based: unit0→1 ... unit5→6).
        Equipped entries: fixed-length 4-byte patch. **Unequipped entries have no
        `EquippedTeamRosterMemberID` tag at all** (UE4 skips default values, as with
        `NumUnopenedPacks`), so insert a full tag (50 B) right after the `WargearInventoryID`
        value and update ancestor Sizes.
    """
    out = bytearray(b)
    pat = b"WargearMetagameInstance_%d\x00" % inventory_id
    hits = [m.start() for m in re.finditer(re.escape(pat), bytes(out))]
    if not hits:
        raise RuntimeError(f"WargearMetagameInstance_{inventory_id} 不存在")
    term = re.compile(rb"\x05\x00\x00\x00None\x00")
    edits = []
    for pos0 in sorted(hits, reverse=True):          # descending: patch later positions first so earlier offsets stay valid
        m = term.search(bytes(out), pos0)
        if not m:
            raise RuntimeError(f"inv{inventory_id}@{pos0} 找不到属性终止符 None")
        eq_edit = None
        if equipped_to is not None:
            seg = bytes(out[pos0:m.end()])
            mm = re.search(rb"EquippedTeamRosterMemberID\x00", seg)
            if mm:
                # equipped entry: fixed-length 4-byte value patch
                q = pos0 + mm.end()
                _t, q2 = SGP.lps(bytes(out), q)          # IntProperty
                sz = struct.unpack_from("<i", out, q2)[0]
                ai = struct.unpack_from("<i", out, q2 + 4)[0]
                if sz != 4 or ai != 0:
                    raise RuntimeError(f"inv{inventory_id} EquippedTeamRosterMemberID 布局异常")
                vpos = q2 + 8
                old_eq = struct.unpack_from("<i", out, vpos)[0]
                struct.pack_into("<i", out, vpos, int(equipped_to))
                eq_edit = {"mode": "value_patch", "old": old_eq,
                           "new": int(equipped_to), "at": vpos}
            else:
                # unequipped entry: tag is absent, so insert a full FPropertyTag (50 B)
                mi = re.search(rb"WargearInventoryID\x00", seg)
                if not mi:
                    raise RuntimeError(f"inv{inventory_id}@{pos0} 两个 ID tag 都没有")
                q = pos0 + mi.end()
                _t, q2 = SGP.lps(bytes(out), q)
                sz = struct.unpack_from("<i", out, q2)[0]
                ai = struct.unpack_from("<i", out, q2 + 4)[0]
                if sz != 4 or ai != 0:
                    raise RuntimeError(f"inv{inventory_id} WargearInventoryID 布局异常")
                ins_at = q2 + 8 + 4                      # after value
                tag = (SA.L("EquippedTeamRosterMemberID") + SA.L("IntProperty")
                       + struct.pack("<i", 4) + struct.pack("<i", 0)
                       + struct.pack("<i", int(equipped_to)))
                ancestors = [t for t in SA.scan_tags(bytes(out))
                             if t["size"] >= 200
                             and t["data_start"] < ins_at <= t["data_end"]]
                out[ins_at:ins_at] = tag
                for a in ancestors:
                    cur = struct.unpack_from("<i", out, a["size_pos"])[0]
                    struct.pack_into("<i", out, a["size_pos"], cur + len(tag))
                eq_edit = {"mode": "tag_insert", "at": ins_at, "bytes": len(tag),
                           "ancestors_updated": [a["name"] for a in ancestors]}
                m = term.search(bytes(out), pos0)        # terminator shifted; re-locate it
                if not m:
                    raise RuntimeError(f"inv{inventory_id}@{pos0} 插入后找不到终止符")
        p = m.end()
        if struct.unpack_from("<i", out, p)[0] != 0:
            raise RuntimeError(f"inv{inventory_id}@{pos0} None 后不是 i32(0)")
        p += 4
        old, v_end = SGP.lps(bytes(out), p)          # old is a str
        if not old.startswith(("WT_", "WTP_", "WDT_")):
            raise RuntimeError(f"inv{inventory_id}@{pos0} 终止符后不是模板名: {old!r}")
        new_lps = encode_lps(template)
        delta = len(new_lps) - (v_end - p)
        tags = SA.scan_tags(bytes(out))
        ancestors = [t for t in tags if t["size"] >= 200
                     and t["data_start"] < pos0 <= t["data_end"]]
        out[p:v_end] = new_lps
        for a in ancestors:
            cur = struct.unpack_from("<i", out, a["size_pos"])[0]
            struct.pack_into("<i", out, a["size_pos"], cur + delta)
        edits.append({"pos": pos0, "old": old, "new": template,
                      "delta": delta, "ancestors_updated": [a["name"] for a in ancestors],
                      **({"equipped_to": eq_edit} if eq_edit else {})})
        print(f"    [set] wargear inv{inventory_id}: {old} -> {template} "
              f"(delta={delta:+d}B, 祖先={[a['name'] for a in ancestors]}"
              f"{', 装备者 -> ' + str(equipped_to) if eq_edit else ''})")
    return bytes(out), {"op": "set-wargear-template", "inventory_id": inventory_id,
                        "template": template, "equipped_to": equipped_to, "edits": edits}


def set_requisition(b: bytes, value: int) -> bytes:
    """Set Requisition directly (IntProperty, fixed 4 bytes, no offset risk)."""
    out = bytearray(b)
    occ = SGP._find_int_prop(bytes(out), "Requisition")
    if not occ:
        raise RuntimeError("未找到 Requisition")
    old = SGP._read_int(bytes(out), occ[0]["value_pos"])
    SGP._write_int(out, occ[0]["value_pos"], value)
    print(f"    [set] Requisition: {old} -> {value}")
    return bytes(out)


def set_cardpacks(b: bytes, count: int) -> dict:
    """Legacy CLI name; now delegates to direct pack insertion via NumUnopenedPacks.

    Earlier logic raised Requisition enough to buy N packs because UE4 skips NumUnopenedPacks
    at 0, which was mistaken for a missing field. To test point-based purchasing alone, use
    set-requisition.
    """
    out, note = set_unopened_packs(b, count)
    return {"bytes": out, **note}


def set_seed(b: bytes, seed: int) -> bytes:
    """Fix CardPackSeed (UInt32Property) for reproducible draws (not yet verified)."""
    out = bytearray(b)
    occ = SGP._locate_prop(bytes(out), "CardPackSeed")
    if not occ:
        print("    [warn] 未找到 CardPackSeed，跳过")
        return b
    old = SGP._read_int(bytes(out), occ[0]["value_pos"], unsigned=True)
    SGP._write_int(out, occ[0]["value_pos"], seed, unsigned=True)
    print(f"    [set] CardPackSeed: {old} (0x{old:08X}) -> {seed} (0x{seed:08X})")
    return bytes(out)


# ---- ability-array rebuild / inline stat edits ----

def list_ability_assets(keyword: str = "") -> list[str]:
    """Discover ability asset paths (/Game/Abilities/...) under Content/Abilities/ instead of a hard-coded list."""
    if not ABILITIES.exists():
        return []
    paths = sorted({"/Game/" + p.relative_to(CONTENT).with_suffix("").as_posix()
                    for p in ABILITIES.rglob("*.uasset")})
    if keyword:
        paths = [p for p in paths if keyword.lower() in p.lower()]
    return paths


def ability_element(idx: int, path: str) -> bytes:
    """Build one Active/PassiveAbilities array element (AbilityMetagameInstance).

    Byte-for-byte layout verified in the reference save:
      '/Script/DeathwatchGame' / 'AbilityMetagameInstance' / 'AbilityMetagameInstance_N'
      / '/Engine/Transient' / 'AbilityTemplateClass' / 'ObjectProperty'
      / Size(i32) / ArrayIndex(i32=0) / path(lps) / '<Name>_C'(lps) / 'None'(lps) / i32(0)
    Size = byte length of path(lps) + cls(lps); the element ends with one i32(0).
    """
    cls = path.rsplit("/", 1)[-1] + "_C"
    val = SA.L(path) + SA.L(cls)
    e = SA.L("/Script/DeathwatchGame")
    e += SA.L("AbilityMetagameInstance")
    e += SA.L(f"AbilityMetagameInstance_{idx}")
    e += SA.L("/Engine/Transient")
    e += SA.L("AbilityTemplateClass")
    e += SA.L("ObjectProperty")
    e += struct.pack("<i", len(val))
    e += struct.pack("<i", 0)
    e += val
    e += SA.L("None")
    e += struct.pack("<i", 0)
    return e


def _rewrite_one_array(b: bytes, tag: dict, new_data: bytes) -> tuple[bytes, int, list[str]]:
    """Replace one array's data range with new_data and update all ancestor container Sizes
    (MarineList / AlphaTeamSheet / TeamRosterMembers / TeamSheet).

    new_data starts with an i32 Num (the payload counted by the ArrayProperty's Size).
    """
    out = bytearray(b)
    delta = len(new_data) - (tag["data_end"] - tag["data_start"])
    tags = SA.scan_tags(bytes(out))
    ancestors = [t for t in tags
                 if t["size"] >= 200
                 and t["data_start"] < tag["data_start"] <= t["data_end"]]
    out[tag["data_start"]:tag["data_end"]] = new_data
    struct.pack_into("<i", out, tag["size_pos"], len(new_data))   # this array's Size
    for a in ancestors:                                            # ancestor Size += delta
        cur = struct.unpack_from("<i", out, a["size_pos"])[0]
        struct.pack_into("<i", out, a["size_pos"], cur + delta)
    return bytes(out), delta, [a["name"] for a in ancestors]


def _unit_array_tags(b: bytes, unit_idx: int, kind: str) -> list[dict]:
    """All ActiveAbilities / PassiveAbilities array tags for a unit (ascending by position)."""
    return [t for t in SA.scan_tags(b)
            if t["name"] == kind and t["type"] == "ArrayProperty"
            and SA._unit_of(b, t["pos"]) == unit_idx]


def set_abilities(b: bytes, unit_idx: int, active=None, passive=None) -> tuple[bytes, dict]:
    """Rebuild the ability arrays in **all serialized replicas** of a unit, with any element count (e.g. 9).

    Note: replica count is not always 2. During an active mission the save contains an extra copy
    (`ActiveDropgameMissionInProgressSaveData → TeamSheet → TeamRosterMembers`), and `MarineList`
    holds its own as well. The old approach, based on the A/B grouping from `parse_units`, would
    **silently miss the third copy**. This version scans all array tags for the unit and rewrites each.
    Empty shell replicas (no UnitName/Stats/arrays) have no tags and are skipped.

    Rewrites run in **descending** data_start order: later edits do not shift earlier tags, so the
    recorded offsets stay valid for the remaining targets. The N in `AbilityMetagameInstance_N` must
    be **unique across the whole save** (the reference save uses 0..21 globally; numbering each unit
    from 100 caused cross-unit name collisions, and the game then resolved all units to one ability
    set). N is allocated globally from "current maximum + 1", incrementing within each array; the same
    array and element reuses the same N across replicas (as the reference save's B replica does).
    """
    plan = []
    for kind, paths in (("ActiveAbilities", active), ("PassiveAbilities", passive)):
        if paths is None:
            continue
        tags = _unit_array_tags(b, unit_idx, kind)
        if not tags:
            raise RuntimeError(f"unit{unit_idx} 没有 {kind} 数组")
        for t in tags:
            plan.append({"kind": kind, "paths": paths,
                         "tag": {k: t[k] for k in ("size_pos", "data_start", "data_end")},
                         "old_num": t["num"], "at": t["data_start"]})
    if not plan:
        raise RuntimeError(f"unit{unit_idx} 未指定 active/passive")
    # globally unique base: current max N + 1 (includes earlier writes this session, avoiding collisions across calls).
    # each kind has one array per replica; the reference save's B replica reuses the same N values as A
    # (the same UObject serialized twice); do the same: allocate once per kind, share across replicas.
    used = [int(m.group(1)) for m in
            re.finditer(rb"AbilityMetagameInstance_(\d+)", b)]
    counter = max(used, default=-1) + 1
    kind_idx: dict[str, list[int]] = {}

    def _idx(kind: str, n: int) -> int:
        nonlocal counter
        lst = kind_idx.setdefault(kind, [])
        while len(lst) <= n:
            lst.append(counter)
            counter += 1
        return lst[n]

    notes = []
    for item in sorted(plan, key=lambda x: x["at"], reverse=True):
        paths = item["paths"]
        new_data = struct.pack("<i", len(paths)) + b"".join(
            ability_element(_idx(item["kind"], i), p) for i, p in enumerate(paths))
        b, delta, anc = _rewrite_one_array(b, item["tag"], new_data)
        notes.append({"array": item["kind"], "at": item["at"],
                      "num": f"{item['old_num']} -> {len(item['paths'])}",
                      "delta_bytes": delta, "ancestors_updated": anc})
        print(f"    [set] unit{unit_idx} {item['kind']}@{item['at']}: "
              f"{item['old_num']} -> {len(item['paths'])} 技能, delta={delta:+d}B, 祖先={anc}")
    return b, {"op": "set-abilities", "unit": unit_idx,
               "active": active, "passive": passive, "edits": notes}


def set_stats(b: bytes, unit_idx: int, values: dict) -> tuple[bytes, dict]:
    """Edit inline combat values Stats (IntProperty, indexed by ArrayIndex). Fixed 4 bytes, no offset risk.

    All replicas are updated (u5's B replica is an empty shell, so only A exists there).
    values is {index: new_value} (JSON keys are strings, converted to int internally).

    Stats[12] = experience XP (a save diff matched observed values 1050/960/1500/1050/960;
    a missing entry means 0). A missing index takes the **insert path**:
    insert a full FPropertyTag after the unit's last Stats tag
    (name + type + Size=4 + ArrayIndex + value) and update every ancestor container Size covering
    the insertion point (MarineList / StatSheet, etc.). UE4 does not serialize zero-valued fields,
    so "no entry" means "value 0" and inserting is a change from 0 to the target.
    """
    vals = {int(k): int(v) for k, v in values.items()}
    out = bytearray(b)
    inserted = []
    for idx, val in sorted(vals.items()):
        snap = bytes(out)
        hits = [t for t in SA.scan_tags(snap)
                if t["name"] == "Stats" and t["type"] == "IntProperty"
                and SGP._owning_unit(snap, t["pos"]) == unit_idx
                and t["array_index"] == idx]
        if hits:
            for t in hits:
                old = struct.unpack_from("<i", out, t["value_pos"])[0]
                struct.pack_into("<i", out, t["value_pos"], vals[t["array_index"]])
                print(f"    [set] unit{unit_idx}.Stats[{t['array_index']}]: "
                      f"{old} -> {vals[t['array_index']]}")
            continue
        # -- insert path: one entry per StatSheet of this unit --
        # Note: the old implementation inserted only after the last Stats tag,
        # silently missing the other replicas in multi-replica saves (active mission = 3), caught only by writeback verification
        # (unit2 replica A Stats[12] came back None); earlier cases hid it because B/5 was an empty shell.
        # Now insert once per StatSheet object owned by the unit, applying in descending insertion-point order.
        own = [t for t in SA.scan_tags(snap)
               if t["name"] == "Stats" and t["type"] == "IntProperty"
               and SGP._owning_unit(snap, t["pos"]) == unit_idx]
        if not own:
            raise RuntimeError(f"unit{unit_idx} 无任何 Stats 字段（空壳记录不可插）")
        sheets = [t for t in SA.scan_tags(snap)
                  if t["name"] == "StatSheet" and t["type"] == "ObjectProperty"
                  and SGP._owning_unit(snap, t["pos"]) == unit_idx]
        if sheets:
            anchors = []
            for sh in sheets:
                mine = [t for t in own
                        if sh["data_start"] <= t["pos"] < sh["data_end"]]
                if mine:
                    anchors.append(max(mine, key=lambda t: t["value_pos"])["value_pos"] + 4)
            if not anchors:
                anchors = [max(own, key=lambda t: t["value_pos"])["value_pos"] + 4]
        else:
            anchors = [max(own, key=lambda t: t["value_pos"])["value_pos"] + 4]
        new_tag = (SA.L("Stats") + SA.L("IntProperty")
                   + struct.pack("<i", 4)        # Size
                   + struct.pack("<i", idx)      # ArrayIndex
                   + struct.pack("<i", val))     # value
        for insert_at in sorted(set(anchors), reverse=True):   # descending: earlier offsets stay valid
            snap = bytes(out)
            # ancestor containers: sized tags whose data range strictly covers the insertion point (the anchor's data_end == insertion point, excluded by >)
            ancestors = [t for t in SA.scan_tags(snap)
                         if t.get("size_pos") is not None and t["name"] != "Stats"
                         and t["data_start"] < insert_at < t["data_end"]]
            out[insert_at:insert_at] = new_tag
            for a in ancestors:
                cur = struct.unpack_from("<i", out, a["size_pos"])[0]
                struct.pack_into("<i", out, a["size_pos"], cur + len(new_tag))
            inserted.append({"index": idx, "value": val, "at": insert_at,
                             "bytes": len(new_tag),
                             "ancestors": [a["name"] for a in ancestors]})
            print(f"    [ins] unit{unit_idx}.Stats[{idx}] = {val}（插入 {len(new_tag)}B "
                  f"@0x{insert_at:x}，祖先 Size += {len(new_tag)}: "
                  f"{[a['name'] for a in ancestors]}）")
    return bytes(out), {"op": "set-stats", "unit": unit_idx, "values": vals,
                        "edited": len(vals) - len(inserted), "inserted": inserted}


# ---- unit wargear slots (the equipment mechanism the game actually uses) ----

def _unit_int_field(b: bytes, unit_idx: int, field: str) -> list[dict]:
    return [t for t in SA.scan_tags(b)
            if t["name"] == field and t["type"] == "IntProperty"
            and SGP._owning_unit(b, t["pos"]) == unit_idx]


def _unit_array_field(b: bytes, unit_idx: int, field: str) -> list[dict]:
    return [t for t in SA.scan_tags(b)
            if t["name"] == field and t["type"] == "ArrayProperty"
            and SGP._owning_unit(b, t["pos"]) == unit_idx]


def _unit_name_anchors(b: bytes, unit_idx: int) -> list[int]:
    """End of the UnitName value in each replica of a unit (insertion anchor; empty shells without UnitName are skipped)."""
    out = []
    for t in SA.scan_tags(b):
        if t["name"] == "UnitName" and t["type"] == "StrProperty" \
                and SGP._owning_unit(b, t["pos"]) == unit_idx:
            old, v_end = SGP.lps(b, t["value_pos"])
            out.append(v_end)
    return out


def _insert_at(b: bytearray, at: int, blob: bytes) -> list[str]:
    """Insert blob at `at` and update the Size of every ancestor container covering the insertion point; returns ancestor names.

    Note: only **containers** (ObjectProperty/ArrayProperty) get a Size update. Leaf properties
    (Int/Str/Float/Bool/Byte) have a Size describing their own value; if the insertion point equals
    their data_end, that is "after" them and their Size must not change (a triple slot insert once
    bumped UnitName Size from 17 to 308, and the game crashed on the StrProperty Size check).
    """
    leaf = {"IntProperty", "UInt32Property", "FloatProperty", "BoolProperty",
            "StrProperty", "ByteProperty"}
    ancestors = [t for t in SA.scan_tags(bytes(b))
                 if t.get("size_pos") is not None
                 and t["type"] not in leaf
                 and t["data_start"] < at <= t["data_end"]]
    b[at:at] = blob
    for a in ancestors:
        cur = struct.unpack_from("<i", b, a["size_pos"])[0]
        struct.pack_into("<i", b, a["size_pos"], cur + len(blob))
    return [a["name"] for a in ancestors]


def set_wargear_slots(b: bytes, unit_idx: int, slots: int | None = None,
                      ids: list[int] | None = None,
                      templates: list[str] | None = None,
                      ranged_inv_id: int | None = None,
                      melee_inv_id: int | None = None) -> tuple[bytes, dict]:
    """Write a unit's "wargear slot" fields -- **the equipment mechanism the game actually reads**.

    Changing only an inventory entry's `EquippedTeamRosterMemberID` is **not enough**: the game reads
    four fields on the unit record (verified by a save diff after unlocking 3 slots and equipping a
    frag grenade in slot 0):

    | Field | Type | Observed (unit2) | Meaning |
    |---|---|---|---|
    | `NumUnlockedWargearSlots` | IntProperty | 3 | number of unlocked consumable slots |
    | `WargearInventoryIDArray` | ArrayProperty(IntProperty) | `[13,-1,-1]` | inventory ID per slot; empty = **-1** |
    | `WargearInventoryTemplateNameArray` | ArrayProperty(StrProperty) | `['WTP_FragGrenade','','']` | mirror; empty = **i32(0), no payload** (not a 5-byte empty string) |
    | `Ranged/MeleeWargearInventoryID` | IntProperty | 3 / 2 | weapons point by **inventory ID** (inv3 flamer / inv2 chainsword) |

    The property stream is name-keyed and sequential, so insertion position does not affect reading;
    this implementation always inserts right after the `UnitName` value, and patches / replaces array
    data in place when the field exists. Each replica is written separately (active mission = 3).
    """
    out = bytearray(b)
    notes = []
    # 1) scalar fields: fixed-length patch if present, insert if missing
    for field, val in (("NumUnlockedWargearSlots", slots),
                       ("RangedWargearInventoryID", ranged_inv_id),
                       ("MeleeWargearInventoryID", melee_inv_id)):
        if val is None:
            continue
        hits = _unit_int_field(bytes(out), unit_idx, field)
        if hits:
            for t in hits:
                old = struct.unpack_from("<i", out, t["value_pos"])[0]
                struct.pack_into("<i", out, t["value_pos"], int(val))
                print(f"    [set] unit{unit_idx}.{field}: {old} -> {val}")
            notes.append({"field": field, "mode": "patch", "value": int(val),
                          "copies": len(hits)})
        else:
            tag = (SA.L(field) + SA.L("IntProperty")
                   + struct.pack("<i", 4) + struct.pack("<i", 0)
                   + struct.pack("<i", int(val)))
            anchors = sorted(_unit_name_anchors(bytes(out), unit_idx), reverse=True)
            if not anchors:
                raise RuntimeError(f"unit{unit_idx} 无 UnitName（空壳副本不可插 {field}）")
            done = []
            for at in anchors:
                anc = _insert_at(out, at, tag)
                done.append({"at": at, "ancestors": anc})
            print(f"    [ins] unit{unit_idx}.{field} = {val}（{len(tag)}B ×{len(anchors)} 副本）")
            notes.append({"field": field, "mode": "insert", "value": int(val),
                          "bytes": len(tag), "inserts": done})
    # 2) array fields: replace data range (update ancestor Size) if present, insert if missing
    for field, inner, elems in (("WargearInventoryIDArray", "IntProperty", ids),
                                ("WargearInventoryTemplateNameArray", "StrProperty", templates)):
        if elems is None:
            continue
        n = len(elems)
        if inner == "IntProperty":
            new_data = struct.pack("<i", n) + b"".join(
                struct.pack("<i", int(e) if e is not None else -1) for e in elems)
        else:
            new_data = struct.pack("<i", n) + b"".join(
                SA.L(e) if e else struct.pack("<i", 0) for e in elems)
        hits = _unit_array_field(bytes(out), unit_idx, field)
        if hits:
            for t in sorted(hits, key=lambda x: x["data_start"], reverse=True):
                old_num = struct.unpack_from("<i", out, t["data_start"])[0]
                delta = len(new_data) - (t["data_end"] - t["data_start"])
                out[t["data_start"]:t["data_end"]] = new_data
                struct.pack_into("<i", out, t["size_pos"], len(new_data))
                tags = SA.scan_tags(bytes(out))
                for a in tags:
                    if a["size"] >= 200 and a["data_start"] < t["data_start"] <= a["data_end"]:
                        cur = struct.unpack_from("<i", out, a["size_pos"])[0]
                        struct.pack_into("<i", out, a["size_pos"], cur + delta)
                print(f"    [set] unit{unit_idx}.{field}: num {old_num} -> {n} "
                      f"(delta={delta:+d}B)")
            notes.append({"field": field, "mode": "replace", "elements": elems,
                          "copies": len(hits)})
        else:
            # layout (verified in the reference save): Name, Type, Size, ArrayIndex, InnerType, Num, elems
            # (Size = Num + element data, excluding the inner lps; inner comes after size/aidx)
            blob = (SA.L(field) + SA.L("ArrayProperty")
                    + struct.pack("<ii", len(new_data), 0) + SA.L(inner) + new_data)
            anchors = sorted(_unit_name_anchors(bytes(out), unit_idx), reverse=True)
            if not anchors:
                raise RuntimeError(f"unit{unit_idx} 无 UnitName（空壳副本不可插 {field}）")
            done = []
            for at in anchors:
                anc = _insert_at(out, at, blob)
                done.append({"at": at, "ancestors": anc})
            print(f"    [ins] unit{unit_idx}.{field} = {elems}（{len(blob)}B ×{len(anchors)} 副本）")
            notes.append({"field": field, "mode": "insert", "elements": elems,
                          "bytes": len(blob), "inserts": done})
    return bytes(out), {"op": "set-wargear-slots", "unit": unit_idx,
                        "slots": slots, "ids": ids, "templates": templates,
                        "ranged_inv_id": ranged_inv_id, "melee_inv_id": melee_inv_id,
                        "edits": notes}


def set_ability_unlock(b: bytes, unit_idx: int, ability: str,
                       status: str = "Unlocked") -> tuple[bytes, dict]:
    """Insert / rewrite UnlockStatus (learned flag) on one ability instance of a unit.

    Verified by a save diff after buying an ability (+52 B):
    - the ability list is unchanged; the only new tag is a trailing `UnlockStatus` ByteProperty
      (with embedded enum name EUnlockableStatus) inside that ability instance
      (AbilityMetagameInstance_N), with value lps = "EUnlockableStatus::Unlocked" (size=32 covers
      exactly this lps), right before the element's None terminator.
    - a missing tag means not learned (in the observed save this applied to one ABP_Alacrity).
    - the unit's Stats[12] (XP) was deducted and its entry removed (4000 spent, zeroed).

    Inserted tag is 96 B: name(17) + type(17) + size(4) + arrayidx(4)
                    + embedded enum-name lps(22) + value lps(32).
    Locate by finding the element with the matching ability suffix in the unit's Passive/ActiveAbilities
    array, take its last None terminator (between it and the next AbilityMetagameInstance or the array
    end), and insert right before it; if present, patch the value lps in place (equal or variable length).
    """
    val_lps = SA.L("EUnlockableStatus::" + status)   # the prefix includes \0 (=28), matching the game-written original
    new_tag = (SA.L("UnlockStatus") + SA.L("ByteProperty")
               + struct.pack("<i", len(val_lps))     # Size = full length of the value lps (32 in the observed save)
               + struct.pack("<i", 0)                # ArrayIndex
               + SA.L("EUnlockableStatus")           # ByteProperty embedded enum-name lps
               + val_lps)
    NONE = b"\x05\x00\x00\x00None\x00\x00\x00\x00\x00"
    INST = re.compile(rb"AbilityMetagameInstance_\d+")
    out = bytearray(b)
    notes = []
    # collect all targets (apply in descending order), then patch them
    plan = []
    for t in SA.scan_tags(bytes(out)):
        if t["name"] not in ("PassiveAbilities", "ActiveAbilities") \
                or t["type"] != "ArrayProperty":
            continue
        if SGP._owning_unit(bytes(out), t["pos"]) != unit_idx:
            continue
        seg = bytes(out)[t["data_start"]:t["data_end"]]
        for m in re.finditer(rb"/Game/Abilities/(?:Passive|Active)/[A-Za-z0-9_/]+", seg):
            if not m.group().decode().endswith("/" + ability):
                continue
            path_abs = t["data_start"] + m.start()
            nxt = INST.search(bytes(out), path_abs + 10, t["data_end"])
            limit = nxt.start() if nxt else t["data_end"]
            nones = [mm.start() for mm in
                     re.finditer(re.escape(NONE), bytes(out)[path_abs:limit])]
            if not nones:
                raise RuntimeError(f"unit{unit_idx} {ability} 元素内找不到 None 终止符")
            insert_at = path_abs + nones[-1]        # right before the element's None terminator
            plan.append(insert_at)
    if not plan:
        raise RuntimeError(f"unit{unit_idx} 未找到技能 {ability} 的任何实例")
    for insert_at in sorted(set(plan), reverse=True):
        snap = bytes(out)
        ancestors = [t for t in SA.scan_tags(snap)
                     if t.get("size_pos") is not None and t["name"] != "UnlockStatus"
                     and t["data_start"] < insert_at < t["data_end"]]
        out[insert_at:insert_at] = new_tag
        for a in ancestors:
            cur = struct.unpack_from("<i", out, a["size_pos"])[0]
            struct.pack_into("<i", out, a["size_pos"], cur + len(new_tag))
        notes.append({"at": insert_at, "bytes": len(new_tag), "status": status,
                      "ancestors": [a["name"] for a in ancestors]})
        print(f"    [ins] unit{unit_idx} {ability}.UnlockStatus = {status} "
              f"({len(new_tag)}B @0x{insert_at:x}, 祖先={[a['name'] for a in ancestors]})")
    return bytes(out), {"op": "set-ability-unlock", "unit": unit_idx,
                        "ability": ability, "status": status, "edits": notes}


def set_unopened_packs(b: bytes, count: int) -> tuple[bytes, dict]:
    """Add unopened packs directly: set Wallet.NumUnopenedPacks (IntProperty).

    A save diff confirmed that after buying one pack the game writes, inside the Wallet object:
        NumUnopenedPacks : IntProperty, Size=4, ArrayIndex=0, value=1
    right after Requisition and before the object's 'None' terminator.
    **UE4 skips this property at 0 packs** (default values are not serialized), which is why an
    earlier static scan concluded the save had no pack field: the field exists, it just had no data.

    If the property is absent, insert a full FPropertyTag and update Wallet's Size;
    if present, patch the fixed 4-byte value (no offset risk).
    """
    if count < 0:
        raise ValueError("count 必须 >= 0")
    tags = SA.scan_tags(b)
    existing = [t for t in tags
                if t["name"] == "NumUnopenedPacks" and t["type"] == "IntProperty"]
    if existing:
        out = bytearray(b)
        old = struct.unpack_from("<i", out, existing[0]["value_pos"])[0]
        struct.pack_into("<i", out, existing[0]["value_pos"], count)
        print(f"    [set] NumUnopenedPacks: {old} -> {count}（定长改值）")
        return bytes(out), {"op": "set-unopened-packs", "count": count,
                            "mode": "existing_value_patch", "before": old}

    wallet = next((t for t in tags
                   if t["name"] == "Wallet" and t["type"] == "ObjectProperty"), None)
    req = next((t for t in tags
                if t["name"] == "Requisition" and t["type"] == "IntProperty"), None)
    if not wallet or not req:
        raise RuntimeError("未找到 Wallet / Requisition，无法插入 NumUnopenedPacks")

    new_tag = (SA.L("NumUnopenedPacks") + SA.L("IntProperty")
               + struct.pack("<i", 4)      # Size
               + struct.pack("<i", 0)      # ArrayIndex
               + struct.pack("<i", count))  # value
    insert_at = req["value_pos"] + 4       # after Requisition's value = before the object's 'None'
    out = bytearray(b)
    out[insert_at:insert_at] = new_tag
    struct.pack_into("<i", out, wallet["size_pos"], wallet["size"] + len(new_tag))
    print(f"    [set] NumUnopenedPacks: 属性不存在 -> 插入 {len(new_tag)}B tag, "
          f"Wallet.Size {wallet['size']} -> {wallet['size'] + len(new_tag)}, value={count}")
    return bytes(out), {"op": "set-unopened-packs", "count": count,
                        "mode": "insert_property_tag",
                        "inserted_bytes": len(new_tag),
                        "wallet_size": [wallet["size"], wallet["size"] + len(new_tag)]}


# ---------------------------------------------------------------- add unit
UNIT_ELEM_HEAD = re.compile(
    rb"\x17\x00\x00\x00/Script/DeathwatchGame\x00"
    rb"\x15\x00\x00\x00UnitMetagameInstance\x00"
    rb"(?:....)UnitMetagameInstance_(\d+)\x00"     # the object name carries its own 4-byte length prefix (digit count may vary)
    rb"\x12\x00\x00\x00/Engine/Transient\x00", re.S)


def _patch_str_tag(blob: bytearray, field: str, newval: str) -> None:
    """Patch a StrProperty tag's value in place within blob (re-locate to tolerate shifts from earlier patches)."""
    pat = SA.L(field) + SA.L("StrProperty")
    i = blob.find(pat)
    if i < 0:
        raise RuntimeError(f"克隆记录里找不到 {field}")
    p = i + len(pat)                     # Size(4) ArrayIndex(4) value(lps)
    old_len = struct.unpack_from("<i", blob, p + 8)[0]
    vbytes = (4 + old_len) if old_len >= 0 else (4 + (-old_len) * 2)
    new_lps = encode_lps(newval)
    blob[p + 8:p + 8 + vbytes] = new_lps
    struct.pack_into("<i", blob, p, len(new_lps))   # this tag's Size


def _patch_int_tag(blob: bytearray, field: str, newval: int) -> None:
    """Patch an IntProperty tag's value in place within blob (re-locate to tolerate shifts)."""
    pat = SA.L(field) + SA.L("IntProperty")
    i = blob.find(pat)
    if i < 0:
        raise RuntimeError(f"克隆记录里找不到 {field}")
    struct.pack_into("<i", blob, i + len(pat) + 8, newval)  # value follows Size(4) + ArrayIndex(4)


def _append_array(b: bytes, tag: dict, blobs: list[bytes]) -> tuple[bytes, int, list[str]]:
    """Append elements to an ArrayProperty payload: **rewrite Num as well** (data_start is the Num field)."""
    old = b[tag["data_start"]:tag["data_end"]]
    new_data = (struct.pack("<i", tag["num"] + len(blobs)) + old[4:]
                + b"".join(blobs))
    return _rewrite_one_array(b, tag, new_data)


def _wargear_entry(inv_id: int, wt_name: str, roster_id: int) -> bytes:
    """Build one WargearList element (layout matches the reference save's inv0 byte for byte):

    [4 lps headers: /Script/DeathwatchGame · WargearMetagameInstance ·
              WargearMetagameInstance_<id> · /Engine/Transient]
    + WargearInventoryID(IntProperty) + EquippedTeamRosterMemberID(IntProperty)
    + None terminator + i32(0) + bare WT_ name lps.
    Reference-save invariant: instance N == WargearInventoryID.
    """
    e = SA.L("/Script/DeathwatchGame") + SA.L("WargearMetagameInstance")
    e += SA.L(f"WargearMetagameInstance_{inv_id}")
    e += SA.L("/Engine/Transient")
    e += SA.L("WargearInventoryID") + SA.L("IntProperty")
    e += struct.pack("<iii", 4, 0, inv_id)
    e += SA.L("EquippedTeamRosterMemberID") + SA.L("IntProperty")
    e += struct.pack("<iii", 4, 0, roster_id)
    e += SA.L("None") + struct.pack("<i", 0)
    e += SA.L(wt_name)
    return e


def add_unit(b: bytes, name: str = "Black Shield",
             template: str = "UTP_Assault_DarkAngel",
             active: list[str] | None = None,
             passive: list[str] | None = None,
             ranged: str | None = "WT_T4_SW_Flamer",
             melee: str | None = "WT_T4_Chainsword",
             consumables: list[str] | None = None,
             unlock_abilities: bool = False,
             force: bool = False) -> tuple[bytes, dict]:
    """Append a brand-new marine at the end of MarineList (the roster's main table).

    Design goal: this operation works standalone regardless of save state (reference, played, or
    with a mission in progress):
      * touches **MarineList only**: a unique container with no ancestor arrays; does not modify
        TeamRosterMembers or mission blocks; adding the new member to the squad is done in the game UI.
      * record body = clone the last UTP_Assault* record in MarineList (or the last record if none
        found) so the byte structure is valid; patch 4 fields in place:
        object name N (unique save-wide), TeamRosterID (current max + 1), UnitName, TemplateName.
      * ability arrays are then rebuilt by set_abilities (instance N allocated save-wide), and
        set_ability_unlock marks each as learned.
      * CollectedMarineNames gets the new name (via _rewrite_one_array, updating ancestors).
    Idempotence: refuses if a marine with the same UnitName already exists in MarineList (--force overrides).
    """
    tags = SA.scan_tags(b)
    marines = [t for t in tags
               if t["name"] == "MarineList" and t["type"] == "ArrayProperty"]
    if len(marines) != 1:
        raise RuntimeError(f"MarineList 应恰有 1 个，实得 {len(marines)}")
    marine = marines[0]
    assert struct.unpack_from("<i", b, marine["data_start"])[0] == marine["num"], \
        "MarineList data_start 应指向 Num 字段"
    payload = b[marine["data_start"]:marine["data_end"]]

    # idempotence: refuse if a marine with the same name exists (byte-level: matching UnitName lps in the MarineList payload)
    name_lps = encode_lps(name)
    if not force and payload.find(name_lps) >= 0:
        raise RuntimeError(f"MarineList 已存在名为 {name!r} 的战士（--force 覆盖）")

    # element boundaries
    starts = [(int(m.group(1)), m.start()) for m in UNIT_ELEM_HEAD.finditer(payload)]
    if not starts:
        raise RuntimeError("MarineList 内未找到任何 UnitMetagameInstance 记录头")
    spans = []
    for k, (n, s) in enumerate(starts):
        e = starts[k + 1][1] if k + 1 < len(starts) else len(payload)
        spans.append((n, s, e))
    donor = next((sp for sp in reversed(spans)
                  if b"UTP_Assault" in payload[sp[1]:sp[2]]), spans[-1])
    blob = bytearray(payload[donor[1]:donor[2]])

    # new N / new roster id (current save-wide max + 1)
    used_n = [int(m.group(1)) for m in re.finditer(rb"UnitMetagameInstance_(\d+)", b)]
    new_n = max(used_n, default=0) + 1
    rids = [t for t in tags if t["name"] == "TeamRosterID" and t["type"] == "IntProperty"]
    new_rid = max((struct.unpack_from("<i", b, t["value_pos"])[0] for t in rids),
                  default=0) + 1

    # 1) record-header object name N (length may change, so rebuild the 4 leading lps fields)
    head = UNIT_ELEM_HEAD.match(bytes(blob))
    if not head:
        raise RuntimeError("donor 记录头不匹配（头部 4 段 lps）")
    new_head = (SA.L("/Script/DeathwatchGame") + SA.L("UnitMetagameInstance")
                + SA.L(f"UnitMetagameInstance_{new_n}") + SA.L("/Engine/Transient"))
    blob[:] = new_head + blob[head.end():]
    # 1b) nested objects' outer references (e.g. StatSheet's
    #   "/Engine/Transient.UnitMetagameInstance_<donor>"); a stale reference would attribute
    #   Stats/abilities back to the donor, so rewrite the whole lps; if its length changes, update the enclosing
    #   ObjectProperty tag's Size (the nested object's size).
    donor_name = b"UnitMetagameInstance_%d\x00" % donor[0]
    repl_name = b"UnitMetagameInstance_%d\x00" % new_n
    pos = len(new_head)
    while True:
        j = blob.find(donor_name, pos)
        if j < 0:
            break
        lps_start = None
        for p in range(max(0, j - 4 - 96), j - 3):
            val = struct.unpack_from("<i", blob, p)[0]
            if 0 < val < 512 and p + 4 + val == j + len(donor_name):
                lps_start = p
                break
        if lps_start is None:
            raise RuntimeError("嵌套 outer 引用的 lps 前缀定位失败")
        old_str = bytes(blob[lps_start + 4:j + len(donor_name)])
        new_str = old_str.replace(donor_name[:-1], repl_name[:-1])
        delta = len(new_str) - len(old_str)
        # Note: old_str/new_str already include the trailing lps NUL (up to donor_name's \x00).
        #   The first version added an extra \x00 and +1 length: structurally self-consistent (strict parse passed), but
        #   the outer string value contained an embedded NUL, so the game's name lookup missed ->
        #   and the game crashed on a null pointer during name lookup.
        blob[lps_start + 4:j + len(donor_name)] = new_str
        struct.pack_into("<i", blob, lps_start, len(new_str))
        if delta:
            k = blob.rfind(b"\x0f\x00\x00\x00ObjectProperty\x00", 0, lps_start)
            if k < 0:
                raise RuntimeError("找不到嵌套对象外层 ObjectProperty tag")
            sz_off = k + len(b"\x0f\x00\x00\x00ObjectProperty\x00")
            struct.pack_into("<i", blob, sz_off,
                             struct.unpack_from("<i", blob, sz_off)[0] + delta)
        pos = lps_start + 4 + len(new_str)
    # 1c) give the StatSheet instance a fresh number (game behavior: StatSheet_N = current max + 1, e.g. 1..6 -> 7).
    #   same-width replacement is safe; skip a width change (e.g. _9 -> _10) (duplicate names are valid in the original --
    #   the TeamSheet replica and MarineList already share names).
    m_ss = re.findall(rb"StatSheet_(\d+)", b)
    j_ss = blob.find(b"StatSheet_\x00") if False else None
    mm = re.search(rb"StatSheet_(\d+)\x00", bytes(blob))
    if m_ss and mm:
        fresh_ss = max(int(x) for x in m_ss) + 1
        old_nm = mm.group(0)
        new_nm = b"StatSheet_%d\x00" % fresh_ss
        if len(old_nm) == len(new_nm):
            blob[mm.start():mm.start()+len(old_nm)] = new_nm

    # 2) UnitName (later in the record, patch first); 3) TemplateName; 4) TeamRosterID -- each re-located
    _patch_str_tag(blob, "UnitName", name)
    _patch_str_tag(blob, "TemplateName", template)
    pat_rid = SA.L("TeamRosterID") + SA.L("IntProperty")
    i = blob.find(pat_rid)
    if i < 0:
        raise RuntimeError("克隆记录里找不到 TeamRosterID")
    struct.pack_into("<i", blob, i + len(pat_rid) + 8, new_rid)  # value follows Size(4) + ArrayIndex(4)
    # 5) weapons (T4 flamer + T4 chainsword): patch 4 fields in the record,
    #   inventory entries are created after insertion (rule: changing only unit strings is reverted from inventory)
    new_wids = []
    if ranged or melee:
        for f in ("RangedWargearTemplateName", "MeleeWargearTemplateName",
                  "RangedWargearInventoryID", "MeleeWargearInventoryID"):
            if blob.find(SA.L(f)) < 0:
                raise RuntimeError(f"donor 记录缺 {f}，无法指定武器（换 donor 或置空 ranged/melee）")
        tags_wn = [t for t in tags
                   if t["name"] == "NextWargearInventoryID" and t["type"] == "IntProperty"]
        if not tags_wn:
            raise RuntimeError("存档无 NextWargearInventoryID，无法新建库存条目")
        next_wid = struct.unpack_from("<i", b, tags_wn[0]["value_pos"])[0]
        used_wn = [int(m.group(1)) for m in
                   re.finditer(rb"WargearMetagameInstance_(\d+)", b)]
        base_wn = max(max(used_wn, default=-1) + 1, next_wid)   # keep the N == invID invariant
        new_wids = [base_wn, base_wn + 1]
        if ranged:
            _patch_str_tag(blob, "RangedWargearTemplateName", ranged)
            _patch_int_tag(blob, "RangedWargearInventoryID", new_wids[0])
        if melee:
            _patch_str_tag(blob, "MeleeWargearTemplateName", melee)
            _patch_int_tag(blob, "MeleeWargearInventoryID", new_wids[1])

    # 5b) consumable slots: allocate inventory IDs (all field writes go through set_wargear_slots --
    #   a verified operation; see its docstring -- do not hand-craft bytes in the cloned blob)
    slot_wids = []
    if consumables:
        tags_wn2 = [t for t in tags
                    if t["name"] == "NextWargearInventoryID" and t["type"] == "IntProperty"]
        next_wid2 = struct.unpack_from("<i", b, tags_wn2[0]["value_pos"])[0] if tags_wn2 else 0
        used_wn2 = [int(m.group(1)) for m in
                    re.finditer(rb"WargearMetagameInstance_(\d+)", b)]
        start_wn = (new_wids[-1] + 1) if new_wids else \
            max(max(used_wn2, default=-1) + 1, next_wid2)
        slot_wids = [start_wn + i for i in range(len(consumables))]

    # append at the end of MarineList (_append_array updates **Num**/Size; the first version forgot Num, so
    # the game parsed 6 elements and read the 7th record as property data -> misalignment -> OOM crash)
    b, _, ancestors = _append_array(b, marine, [bytes(blob)])
    print(f"    [add] unit{new_n} {name!r} template={template} roster_id={new_rid}"
          f"（donor=UnitMetagameInstance_{donor[0]}，+{len(blob)}B，Num "
          f"{marine['num']}->{marine['num'] + 1}）")

    # 6) increment the NextTeamRosterID allocator (game behavior: reference 7 -> 8 after rid=7;
    #   the first version skipped this, so rid collided with the next unissued value and name lookup crashed)
    tags_ntr = [t for t in SA.scan_tags(b)
                if t["name"] == "NextTeamRosterID" and t["type"] == "IntProperty"]
    if tags_ntr:
        out_ntr = bytearray(b)
        struct.pack_into("<i", out_ntr, tags_ntr[0]["value_pos"], new_rid + 1)
        b = bytes(out_ntr)
        print(f"    [add] NextTeamRosterID -> {new_rid + 1}")

    # rebuild ability arrays and unlock all (reuse existing mechanisms; paths completed from stems)
    def _full(stem):
        pref = ("/Game/Abilities/Active/" if stem.startswith("ABA_")
                else "/Game/Abilities/Passive/" if stem.startswith("ABP_")
                else None)
        if not pref:
            raise RuntimeError(f"技能词干必须以 ABA_/ABP_ 开头：{stem!r}")
        # Note: paths must be probed from the real asset tree; some abilities live in subdirectories (e.g. ABA_T4_StasisField
        #   under Abilities/Active/MiscWargear/). Hard-coding the root yields a nonexistent path ->
        #   the game's name lookup misses -> null-pointer crash
        #   (verified across T1..T5).
        hits = list(CONTENT.rglob(f"{stem}.uasset"))
        if hits:
            rel = hits[0].relative_to(CONTENT).as_posix()          # Abilities/Active/MiscWargear/ABA_X.uasset
            return "/Game/" + rel[:-len(".uasset")]
        return pref + stem   # fall back to root-path assembly when the sandbox lacks the asset (keeps old behavior; may warn)

    b, note_ab = set_abilities(b, new_n,
                               active=[_full(s) for s in (active or [])],
                               passive=[_full(s) for s in (passive or [])])
    if unlock_abilities:
        # the game does not write UnlockStatus for new marines (verified in a written save: ability elements contain only
        # AbilityTemplateClass); enable this switch manually only when the "learned" UI state is needed.
        for stem in list(active or []) + list(passive or []):
            b, _ = set_ability_unlock(b, new_n, stem)

    # append to CollectedMarineNames (update Num/Size/ancestors)
    cmn = next((t for t in SA.scan_tags(b)
                if t["name"] == "CollectedMarineNames" and t["type"] == "ArrayProperty"),
               None)
    if cmn is not None:
        b, _, _ = _append_array(b, cmn, [name_lps])
        print(f"    [add] CollectedMarineNames Num {cmn['num']}->{cmn['num'] + 1}")

    # wargear inventory entries (WargearList append + NextWargearInventoryID + CollectedWargearNames)
    if new_wids or slot_wids:
        pairs = ([(new_wids[0], ranged), (new_wids[1], melee)] if new_wids else []) \
            + list(zip(slot_wids, consumables or []))
        wl = next(t for t in SA.scan_tags(b)
                  if t["name"] == "WargearList" and t["type"] == "ArrayProperty")
        new_blob = [_wargear_entry(wid, wt, new_rid) for wid, wt in pairs]
        b, _, anc_wl = _append_array(b, wl, new_blob)
        print(f"    [add] WargearList Num {wl['num']}->{wl['num'] + len(new_blob)}，"
              f"新增库存 {new_wids} = {[wt for _, wt in pairs]}（equipped→roster {new_rid}），祖先={anc_wl}")
        tags_nw = [t for t in SA.scan_tags(b)
                   if t["name"] == "NextWargearInventoryID" and t["type"] == "IntProperty"]
        all_new = [wid for wid, _ in pairs]
        out2 = bytearray(b)
        struct.pack_into("<i", out2, tags_nw[0]["value_pos"], max(all_new) + 1)
        b = bytes(out2)
        print(f"    [add] NextWargearInventoryID -> {max(all_new) + 1}")
        cwn = next((t for t in SA.scan_tags(b)
                    if t["name"] == "CollectedWargearNames" and t["type"] == "ArrayProperty"),
                   None)
        if cwn is not None:
            # game behavior: CollectedWargearNames records only **first-seen** WT_ names
            # (verified in a written save: +4 inventory entries added only 1 name; duplicates are not recorded)
            have = set(re.findall(rb"WT[P]?_[A-Za-z0-9_]+",
                                  b[cwn["data_start"]:cwn["data_end"]]))
            add_names = [SA.L(wt) for _, wt in pairs if wt and wt.encode() not in have]
            if add_names:
                b, _, _ = _append_array(b, cwn, add_names)
                print(f"    [add] CollectedWargearNames Num {cwn['num']}->{cwn['num'] + len(add_names)}（去重后）")

    # 7) the four wargear-slot fields (verified set_wargear_slots: NUS=3 + IDArray +
    #    TemplateNameArray; weapon fields were patched in step 5, so only slots and their mirror are written here)
    if consumables:
        b, note_ws = set_wargear_slots(b, new_n, slots=len(consumables),
                                       ids=slot_wids, templates=consumables)
        print(f"    [add] 装备槽 ×{len(consumables)}: {slot_wids} = {consumables}")

    # structural self-check: MarineList Num must equal the actual record-header count (guards against a missing Num update)
    tags_v = SA.scan_tags(b)
    ml_v = next(t for t in tags_v
                if t["name"] == "MarineList" and t["type"] == "ArrayProperty")
    n_heads = len(list(UNIT_ELEM_HEAD.finditer(b[ml_v["data_start"]:ml_v["data_end"]])))
    if n_heads != ml_v["num"]:
        raise RuntimeError(f"MarineList Num={ml_v['num']} 与实际记录数 {n_heads} 不符")

    return b, {"op": "add-unit", "unit": new_n, "name": name, "template": template,
               "roster_id": new_rid, "donor_n": donor[0],
               "active": list(active or []), "passive": list(passive or []),
               "ranged": ranged, "melee": melee, "new_wargear_ids": new_wids,
               "ancestors_updated": ancestors}


def apply_ops(b: bytes, ops: list[dict]) -> tuple[bytes, list[dict]]:
    """Apply an array of injection operations; returns (new_bytes, semantic notes). Supports legacy op names."""
    out = b
    notes = []
    for op in ops:
        k = op.get("op")
        if k == "set-cardpacks":
            r = set_cardpacks(out, int(op["count"]))
            out = r.pop("bytes")
            notes.append(r)
        elif k == "set-requisition":
            out = set_requisition(out, int(op["value"]))
            notes.append({"op": k, "value": int(op["value"])})
        elif k in ("set_cardpack_seed", "set-seed"):
            out = set_seed(out, int(op["seed"]))
            notes.append({"op": k, "seed": int(op["seed"]),
                          "reproducible": "UNKNOWN（待实机验证）"})
        elif k == "set-unit":
            out = set_unit(out, int(op["unit"]),
                           template=op.get("template"), name=op.get("name"),
                           ranged=op.get("ranged"), melee=op.get("melee"))
            notes.append({"op": k, "unit": int(op["unit"]),
                          "template": op.get("template"), "name": op.get("name"),
                          "ranged": op.get("ranged"), "melee": op.get("melee")})
        elif k == "add-wargear":
            # legacy implementation was never verified; keep the call but flag the risk
            out = SGP.add_wargear(out, op["template"])
            notes.append({"op": k, "template": op["template"],
                          "verified": False, "risk": "数组插入实现未做实机回读验证"})
        elif k == "set-abilities":
            out, note = set_abilities(out, int(op["unit"]),
                                      active=op.get("active"), passive=op.get("passive"))
            notes.append(note)
        elif k == "set-wargear-template":
            out, note = set_wargear_template(out, int(op["inventory_id"]),
                                             op["template"],
                                             equipped_to=op.get("equipped_to"))
            notes.append(note)
        elif k == "set-stats":
            out, note = set_stats(out, int(op["unit"]), op["values"])
            notes.append(note)
        elif k == "set-wargear-slots":
            out, note = set_wargear_slots(out, int(op["unit"]),
                                          slots=op.get("slots"),
                                          ids=op.get("ids"),
                                          templates=op.get("templates"),
                                          ranged_inv_id=op.get("ranged_inv_id"),
                                          melee_inv_id=op.get("melee_inv_id"))
            notes.append(note)
        elif k == "set-ability-unlock":
            out, note = set_ability_unlock(out, int(op["unit"]), op["ability"],
                                           status=op.get("status", "Unlocked"))
            notes.append(note)
        elif k == "add-unit":
            out, note = add_unit(out,
                                 name=op.get("name", "Black Shield"),
                                 template=op.get("template", "UTP_Assault_DarkAngel"),
                                 active=op.get("active"),
                                 passive=op.get("passive"),
                                 ranged=op.get("ranged", "WT_T4_SW_Flamer"),
                                 melee=op.get("melee", "WT_T4_Chainsword"),
                                 consumables=op.get("consumables"),
                                 unlock_abilities=bool(op.get("unlock", False)),
                                 force=bool(op.get("force", False)))
            notes.append(note)
        elif k in ("set-unopened-packs", "set-cardpacks"):
            out, note = set_unopened_packs(out, int(op.get("count", op.get("value", 0))))
            notes.append(note)
        else:
            raise SystemExit(f"[ERR] 未知注入操作 {k!r}")

    # ---- writeback verification: every set-unit field must equal its target ----
    problems = []
    units_after = {u["unit"]: u for u in list_units(out)}
    for op in ops:
        if op.get("op") != "set-unit":
            continue
        u = int(op["unit"])
        if u not in units_after:
            problems.append(f"unit{u} 注入后无法解析")
            continue
        for key, fld in (("template", "TemplateName"), ("name", "UnitName"),
                         ("ranged", "RangedWargearTemplateName"),
                         ("melee", "MeleeWargearTemplateName")):
            if op.get(key) is None:
                continue
            got = units_after[u].get(fld)
            if got != op[key]:
                problems.append(f"unit{u}.{fld} 期望 {op[key]!r} 实得 {got!r}")
    # ---- ability-array readback: every replica's array must equal the target ----
    for op in ops:
        if op.get("op") != "set-abilities":
            continue
        u = int(op["unit"])
        for kind, name in (("active", "ActiveAbilities"), ("passive", "PassiveAbilities")):
            want = op.get(kind)
            if want is None:
                continue
            tags = _unit_array_tags(out, u, name)
            if not tags:
                problems.append(f"unit{u} {name} 回读不到数组")
                continue
            for t in tags:
                got = SA.parse_ability_paths(out, t)
                if got != want:
                    problems.append(f"unit{u} {name}@{t['data_start']} 期望{len(want)}个"
                                    f"实得 {len(got)}个")
    # ---- Stats readback ----
    for op in ops:
        if op.get("op") != "set-stats":
            continue
        u = int(op["unit"])
        want = {int(k): int(v) for k, v in op["values"].items()}
        rows = [r for r in SA.parse_stats(out) if r["unit"] == u]
        # replica count is not always 2: B/5 may be an empty shell (no Stats); >=1 matching copies is enough
        rows = [r for r in rows if r["Stats"]]          # skip empty-shell replicas
        if not rows:
            problems.append(f"unit{u} Stats 回读 0 份")
        for r in rows:
            for k, v in want.items():
                if r["Stats"].get(k) != v:
                    problems.append(f"unit{u} 副本{r['replica']} Stats[{k}] 实得 {r['Stats'].get(k)} != {v}")
    # ---- inventory readback: template name (including WTP_ consumables) and owner ID must match ----
    for op in ops:
        if op.get("op") != "set-wargear-template":
            continue
        inv = int(op["inventory_id"])
        rows = [r for r in SA.parse_wargear(out) if r["index"] == inv]
        if not rows:
            problems.append(f"inv{inv} 回读不到库存条目")
            continue
        for r in rows:
            if r["template"] != op["template"]:
                problems.append(f"inv{inv} 模板期望 {op['template']!r} 实得 {r['template']!r}")
            want_eq = op.get("equipped_to")
            if want_eq is not None:
                got_eq = r.get("equipped_member")
                if got_eq != int(want_eq):
                    problems.append(f"inv{inv} 装备者期望 {want_eq} 实得 {got_eq}")
    # ---- slot readback: NumUnlockedWargearSlots / both arrays / weapon inventory IDs ----
    for op in ops:
        if op.get("op") != "set-wargear-slots":
            continue
        u = int(op["unit"])
        for field, want in (("NumUnlockedWargearSlots", op.get("slots")),
                            ("RangedWargearInventoryID", op.get("ranged_inv_id")),
                            ("MeleeWargearInventoryID", op.get("melee_inv_id"))):
            if want is None:
                continue
            hits = _unit_int_field(out, u, field)
            if not hits:
                problems.append(f"unit{u} {field} 回读不到")
            for t in hits:
                got = struct.unpack_from("<i", out, t["value_pos"])[0]
                if got != int(want):
                    problems.append(f"unit{u} {field} 期望 {want} 实得 {got}")
        for field, want in (("WargearInventoryIDArray", op.get("ids")),
                            ("WargearInventoryTemplateNameArray", op.get("templates"))):
            if want is None:
                continue
            hits = _unit_array_field(out, u, field)
            if not hits:
                problems.append(f"unit{u} {field} 回读不到")
            for t in hits:
                p = t["data_start"]
                num = struct.unpack_from("<i", out, p)[0]
                p += 4
                got = []
                for _i in range(num):
                    if field == "WargearInventoryIDArray":
                        got.append(struct.unpack_from("<i", out, p)[0]); p += 4
                    else:
                        ln = struct.unpack_from("<i", out, p)[0]
                        if ln > 0:
                            got.append(out[p+4:p+4+ln-1].decode("utf-8", "replace")); p += 4 + ln
                        else:
                            got.append(""); p += 4
                if got != list(want):
                    problems.append(f"unit{u} {field} 期望 {list(want)} 实得 {got}")
    # ---- unopened-pack count readback ----
    for op in ops:
        if op.get("op") not in ("set-unopened-packs", "set-cardpacks"):
            continue
        want = int(op.get("count", op.get("value", 0)))
        hits = [t for t in SA.scan_tags(out)
                if t["name"] == "NumUnopenedPacks" and t["type"] == "IntProperty"]
        if len(hits) != 1 or hits[0]["value"] != want:
            problems.append(f"NumUnopenedPacks 期望 {want} 实得 "
                            f"{[h['value'] for h in hits]}")
    # ---- overall parseability: unit count = before + number of add-unit ops (other ops must not change it) ----
    try:
        n_before = len(SA.parse_units(b))
        n_after = len(SA.parse_units(out))
        n_add = sum(1 for o in ops if o.get("op") == "add-unit")
        if n_after != n_before + n_add:
            problems.append(f"整体单位记录数 {n_before} -> {n_after}"
                            f"（期望 {n_before + n_add}），存档结构可能已损坏")
        SGP.parse_props(out)
    except Exception as e:
        problems.append(f"整体解析抛异常：{e}")
    if problems:
        raise RuntimeError("注入后回读校验失败，拒绝写出：\n  - " + "\n  - ".join(problems))
    notes.append({"stage": "writeback_verify", "ok": True,
                  "units_parsed": len(SA.parse_units(out)),
                  "checked_fields": sum(1 for o in ops if o.get("op") == "set-unit"
                                        for k in ("template", "name", "ranged", "melee")
                                        if o.get(k) is not None)})
    return out, notes


def diff(a: bytes, b: bytes) -> dict:
    """Field-by-field diff to confirm that only target fields changed (writeback verification)."""
    sa, sb = scan_structure(a), scan_structure(b)
    pa = {(p["name"], p["type"]): p["count"] for p in sa["props"]}
    pb = {(p["name"], p["type"]): p["count"] for p in sb["props"]}
    changed_scalars = {}
    for k in sa["scalars"]:
        if sa["scalars"][k]["values"] != sb["scalars"][k]["values"]:
            changed_scalars[k] = {"before": sa["scalars"][k]["values"],
                                  "after": sb["scalars"][k]["values"]}
    changed_units = []
    for ua, ub in zip(sa["units"], sb["units"]):
        for fld in UNIT_STR_FIELDS:
            if ua.get(fld) != ub.get(fld):
                changed_units.append({"unit": ua["unit"], "field": fld,
                                      "before": ua.get(fld), "after": ub.get(fld)})
    return {"size": [len(a), len(b)],
            "props_added": [f"{n}:{t}" for (n, t) in set(pb) - set(pa)],
            "props_removed": [f"{n}:{t}" for (n, t) in set(pa) - set(pb)],
            "props_count_changed": [{"name": n, "type": t, "before": pa[(n, t)],
                                     "after": pb[(n, t)]}
                                    for (n, t) in set(pa) & set(pb)
                                    if pa[(n, t)] != pb[(n, t)]],
            "scalars_changed": changed_scalars,
            "units_changed": changed_units,
            "parse_ok": len(sb["units"]) > 0}


# ── CLI ────────────────────────────────────────────────────────────

def default_save_path() -> pathlib.Path:
    base = pathlib.Path(os.environ.get("LOCALAPPDATA")
                        or pathlib.Path.home() / "AppData" / "Local")
    return base / "DeathwatchGame" / "Saved" / "SaveGames" / "dw_metagame.save"


def preflight_save(path: str | None = None) -> dict:
    """Read-only preflight: check that the save exists and parses. The installer must run this before injection."""
    p = pathlib.Path(path) if path else default_save_path()
    if not p.is_file():
        return {"exists": False, "parse_ok": False, "path": str(p),
                "reason": "save_not_found"}
    try:
        data = p.read_bytes()
        if not SGP.parse_props(data):
            raise ValueError("未识别到任何已知属性，可能不是 Deathwatch 存档")
        units = list_units(data)
        return {"exists": True, "parse_ok": True, "path": str(p),
                "units": len(units), "size": len(data), "reason": ""}
    except Exception as exc:                      # noqa: BLE001
        return {"exists": True, "parse_ok": False, "path": str(p),
                "reason": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    if "--check-save" in sys.argv:
        i = sys.argv.index("--check-save")
        path = sys.argv[i + 1] if i + 1 < len(sys.argv) else None
        r = preflight_save(path)
        print(json.dumps(r, ensure_ascii=False))
        return 0 if (r["exists"] and r["parse_ok"]) else 2
    ap = argparse.ArgumentParser(description="Save injection / structure inspection")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("scan"); p.add_argument("path", nargs="?")
    p = sub.add_parser("units"); p.add_argument("path", nargs="?")
    p = sub.add_parser("templates"); p.add_argument("kind", choices=["unit", "wargear"])
    p.add_argument("keyword", nargs="?", default="")
    p = sub.add_parser("abilities"); p.add_argument("keyword", nargs="?", default="")
    p = sub.add_parser("inject"); p.add_argument("ops"); p.add_argument("--out", required=True)
    p.add_argument("--save", default=None)
    p = sub.add_parser("diff"); p.add_argument("a"); p.add_argument("b")
    p = sub.add_parser("add-unit", help="Append one marine at the end of MarineList")
    p.add_argument("path", nargs="?")
    p.add_argument("--name", default="Black Shield")
    p.add_argument("--template", default="UTP_Assault_DarkAngel")
    p.add_argument("--active", default="",
                   help="Comma-separated ABA_ stems; empty means no active ability")
    p.add_argument("--passive",
                   default="ABP_Alacrity,ABP_Medicae,ABP_Crusader,ABP_FortuneOfTerra,ABP_Praetorian,ABP_Unforgiven",
                   help="Comma-separated ABP_ stems (max 6)")
    p.add_argument("--ranged", default="WT_T4_SW_Flamer",
                   help="Primary weapon; creates a new inventory entry (empty keeps the donor weapon)")
    p.add_argument("--melee", default="WT_T4_Chainsword",
                   help="Melee weapon (empty keeps the donor weapon)")
    p.add_argument("--consumables", default="WTP_T4_StasisField",
                   help="Comma-separated WTP_ consumable stems; opens 3 slots and creates inventory entries (empty skips). Default keeps the +4AP stasis field")
    p.add_argument("--no-unlock", dest="unlock", action="store_false",
                   help="Do not write ability UnlockStatus (default writes all as learned)")
    p.add_argument("--out", default=None,
                   help="Overwrite input by default (writes <input>.bak-<timestamp> first)")
    p.add_argument("--force", action="store_true", help="Append even if a marine with the same name exists")
    a = ap.parse_args()

    default = str(SGP.SAMPLE)
    if a.cmd == "scan":
        b = pathlib.Path(a.path or default).read_bytes()
        print(json.dumps(scan_structure(b), ensure_ascii=False, indent=2))
    elif a.cmd == "units":
        b = pathlib.Path(a.path or default).read_bytes()
        print(json.dumps(list_units(b), ensure_ascii=False, indent=2))
    elif a.cmd == "templates":
        names = list_templates(a.kind, a.keyword)
        print(f"=== {len(names)} {a.kind} template(s) (recomputed from assets) ===")
        for n in names:
            print("  ", n)
    elif a.cmd == "abilities":
        paths = list_ability_assets(a.keyword)
        print(f"=== {len(paths)} ability asset path(s) (recomputed from Content/Abilities) ===")
        for n in paths:
            print("  ", n)
    elif a.cmd == "inject":
        ops = json.loads(pathlib.Path(a.ops).read_text(encoding="utf-8"))
        b = pathlib.Path(a.save or default).read_bytes()
        out, notes = apply_ops(b, ops)
        pathlib.Path(a.out).write_bytes(out)
        print(json.dumps({"out": a.out, "size": len(out), "notes": notes},
                         ensure_ascii=False, indent=2))
    elif a.cmd == "diff":
        print(json.dumps(diff(pathlib.Path(a.a).read_bytes(),
                              pathlib.Path(a.b).read_bytes()),
                         ensure_ascii=False, indent=2))
    elif a.cmd == "add-unit":
        src = pathlib.Path(a.path or default)
        b = src.read_bytes()
        out, note = apply_ops(b, [{
            "op": "add-unit", "name": a.name, "template": a.template,
            "active": [s.strip() for s in a.active.split(",") if s.strip()],
            "passive": [s.strip() for s in a.passive.split(",") if s.strip()],
            "ranged": a.ranged or None, "melee": a.melee or None,
            "consumables": [s.strip() for s in (a.consumables or "").split(",") if s.strip()],
            "unlock": a.unlock, "force": a.force}])
        dst = pathlib.Path(a.out) if a.out else src
        if a.out is None:   # force a backup before overwriting the live save
            import time
            bak = src.with_suffix(src.suffix + f".bak-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}")
            bak.write_bytes(b)
            print(f"  [bak] {bak}")
        dst.write_bytes(out)
        print(json.dumps({"out": str(dst), "size": len(out), "note": note},
                         ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
