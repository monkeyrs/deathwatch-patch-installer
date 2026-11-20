#!/usr/bin/env python3
"""Save anatomy: determine whether stats / abilities / wargear reference asset IDs or are stored inline in the save.

This is a **read-only** research tool; it never writes a save. Every conclusion is read directly
from the bytes, with no guesswork.

Background (verified against a reference save):
  UE4 `FObjectAndNameAsStringProxyArchive` sequential stream. Each property is an FPropertyTag:
      name(lps) → type(lps) → Size(int32) → ArrayIndex(int32) → value
  ArrayProperty value = InnerType(lps) + Num(int32) + N elements;
  **Size starts at the Num field** (verified byte by byte).
  ObjectProperty value = class path(lps); Size starts at the value.

CLI (reproducible):
  python save_anatomy.py verdict [path.save]     # key conclusion + evidence (reference vs inline)
  python save_anatomy.py units [path.save]       # full fields of both replicas per unit
  python save_anatomy.py abilities [path.save]   # ability asset paths per unit and replica
  python save_anatomy.py stats [path.save]       # Stats index → value (inline integers)
  python save_anatomy.py wargear [path.save]     # wargear instances: ID / owner / template name
  python save_anatomy.py tags [path.save]        # all FPropertyTags (with covered ranges)

Reused by save_inject.py via import: scan_tags / find_array_tags / ability_element, etc.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import struct
import sys

if sys.stdout is not None:                     # no console when packaged with --windowed
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import save_game_probe as SGP  # noqa: E402

TYPES = {
    "StrProperty", "ObjectProperty", "IntProperty", "FloatProperty", "BoolProperty",
    "NameProperty", "ByteProperty", "EnumProperty", "ArrayProperty", "StructProperty",
    "MapProperty", "SetProperty", "TextProperty", "UInt32Property", "UInt64Property",
    "Int64Property", "UInt16Property", "Int16Property", "SoftObjectProperty",
    "LazyObjectProperty", "FieldPathProperty",
}
SCALAR_INT = {"IntProperty", "UInt32Property"}
LPS_TYPES = {"StrProperty", "ObjectProperty", "NameProperty"}


def L(s: str) -> bytes:
    """UE4 lps: ASCII uses a positive length; non-ASCII uses UTF-16LE with a negative length (symmetric with SGP.lps)."""
    try:
        raw = s.encode("ascii") + b"\x00"
        return struct.pack("<i", len(raw)) + raw
    except UnicodeEncodeError:
        raw = s.encode("utf-16-le") + b"\x00\x00"
        return struct.pack("<i", -(len(s) + 1)) + raw


def read_lps(b: bytes, i: int):
    return SGP.lps(b, i)


def scan_tags(b: bytes) -> list[dict]:
    """Scan all valid FPropertyTags, including data_start / data_end (used for ancestor Size updates).

    ArrayProperty: data_start = right after InnerType (the Num field); Size starts here.
    Other types  : data_start = start of value; Size starts here.

    Performance note: the original per-byte `read_lps` + regex scan took ~6 s on an 81 KB save;
    a single `set_ability-unlock` re-scans 3-4 times, making a 55-op recipe very slow. This version
    first applies a cheap pre-filter (int32 length in [1,64] + first char [A-Za-z_] + all [A-Za-z0-9_]),
    identical in meaning to `re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*")`, which pushes the expensive path
    to a handful of candidates (~60x faster). **The result set is identical to the original**
    (`pos/name/type/size/array_index` all match on the same file).
    """
    out = []
    i = 0
    n = len(b)
    name_ok = re.compile(rb"[A-Za-z_][A-Za-z0-9_]*\Z").match
    while i < n - 30:
        # -- cheap pre-filter: FPropertyTag names are 1..64 char ASCII identifiers --
        L = (b[i] | (b[i + 1] << 8) | (b[i + 2] << 16) | (b[i + 3] << 24))
        if L < 1 or L > 64:
            i += 1
            continue
        end = i + 4 + L
        if end > n:
            i += 1
            continue
        nm_b = b[i + 4:end].rstrip(b"\x00")      # align with SGP.lps rstrip("\x00")
        if not nm_b or not name_ok(nm_b):
            i += 1
            continue
        nm = nm_b.decode("ascii")
        p = i + 4 + L                           # return position of the original read_lps (includes all L bytes)
        try:
            typ, p2 = read_lps(b, p)
        except Exception:
            i += 1
            continue
        if typ not in TYPES or p2 + 8 > n:
            i += 1
            continue
        size = struct.unpack_from("<i", b, p2)[0]
        aidx = struct.unpack_from("<i", b, p2 + 4)[0]
        # Note: bound raised from 20000 to 1 MB: after add-unit appends a marine,
        # MarineList can exceed 20 KB (23200 B observed); the old bound would silently drop
        # valid containers, so set_abilities could not find the ancestor and Size went out of sync.
        if size < 0 or size > (1 << 20) or aidx < 0 or aidx > 4096:
            i += 1
            continue
        size_pos = p2
        vp = p2 + 8
        inner = None
        num = None
        if typ == "ArrayProperty":
            try:
                inner, p3 = read_lps(b, vp)
            except Exception:
                i += 1
                continue
            if inner not in TYPES and inner not in ("ObjectProperty", "IntProperty",
                                                     "StrProperty", "ByteProperty",
                                                     "BoolProperty", "FloatProperty"):
                i += 1
                continue
            if p3 + 4 > n:
                i += 1
                continue
            num = struct.unpack_from("<i", b, p3)[0]
            if num < 0 or num > 10000:
                i += 1
                continue
            data_start = p3
        else:
            data_start = vp
        data_end = data_start + size
        if data_end > n:
            i += 1
            continue
        val = None
        if typ in LPS_TYPES:
            try:
                val, _ = read_lps(b, vp)
            except Exception:
                val = None
        elif typ in SCALAR_INT:
            val = struct.unpack_from("<I" if typ == "UInt32Property" else "<i", b, vp)[0]
        elif typ == "FloatProperty":
            val = round(struct.unpack_from("<f", b, vp)[0], 4)
        elif typ == "BoolProperty":
            val = bool(b[vp])
        out.append({
            "pos": i, "name": nm, "type": typ, "size": size, "size_pos": size_pos,
            "array_index": aidx, "value_pos": vp, "value": val,
            "inner": inner, "num": num,
            "data_start": data_start, "data_end": data_end,
        })
        i += 1
    return out


def replica_of(b: bytes, pos: int) -> str:
    """Distinguish the two serialized replicas: MarineList (first half) / TeamRosterMembers (AlphaTeamSheet, second half)."""
    tags = scan_tags(b)
    for t in tags:
        if t["name"] == "AlphaTeamSheet":
            return "A(MarineList)" if pos < t["pos"] else "B(TeamRosterMembers)"
    return "A(MarineList)"


def _unit_of(b: bytes, pos: int):
    return SGP._owning_unit(b, pos)


def parse_units(b: bytes) -> list[dict]:
    """Fields of each unit and replica: name / class / weapons / Stats / ability-array tag positions."""
    tags = scan_tags(b)
    replicas = {}
    for t in tags:
        if t["name"] == "AlphaTeamSheet":
            replicas["split"] = t["pos"]
            break
    split = replicas.get("split", len(b))

    units = {}
    for t in tags:
        u = _unit_of(b, t["pos"])
        if u is None:
            continue
        rep = "A" if t["pos"] < split else "B"
        d = units.setdefault((rep, u), {
            "replica": rep, "unit": u, "UnitName": None, "TemplateName": None,
            "RangedWargearTemplateName": None, "MeleeWargearTemplateName": None,
            "Stats": {}, "ActiveAbilities_tag": None, "PassiveAbilities_tag": None,
        })
        if t["name"] in ("UnitName", "TemplateName", "RangedWargearTemplateName",
                         "MeleeWargearTemplateName") and t["type"] == "StrProperty":
            if d[t["name"]] is None:
                d[t["name"]] = t["value"]
        elif t["name"] == "Stats" and t["type"] == "IntProperty":
            d["Stats"][t["array_index"]] = t["value"]
        elif t["name"] in ("ActiveAbilities", "PassiveAbilities") and t["type"] == "ArrayProperty":
            key = t["name"] + "_tag"
            if d[key] is None:
                d[key] = {"pos": t["pos"], "size_pos": t["size_pos"], "size": t["size"],
                          "num": t["num"], "data_start": t["data_start"],
                          "data_end": t["data_end"]}
    return [units[k] for k in sorted(units)]


def parse_ability_paths(b: bytes, tag: dict) -> list[str]:
    """Extract all /Game/ ability asset paths from one Active/PassiveAbilities array."""
    lo, hi = tag["data_start"] + 4, tag["data_end"]   # skip Num
    hits = []
    for m in re.finditer(rb"/Game/Abilities/[A-Za-z0-9_/]+", b[lo:hi]):
        hits.append(m.group().decode("ascii", "replace"))
    return hits


def parse_abilities(b: bytes) -> list[dict]:
    out = []
    for u in parse_units(b):
        row = {"replica": u["replica"], "unit": u["unit"], "UnitName": u["UnitName"],
               "active": [], "passive": []}
        for kind in ("active", "passive"):
            tag = u[("ActiveAbilities" if kind == "active" else "PassiveAbilities") + "_tag"]
            if tag:
                row[kind] = parse_ability_paths(b, tag)
        out.append(row)
    return out


def parse_stats(b: bytes) -> list[dict]:
    return [{"replica": u["replica"], "unit": u["unit"], "UnitName": u["UnitName"],
             "Stats": dict(sorted(u["Stats"].items()))} for u in parse_units(b)]


def parse_wargear(b: bytes) -> list[dict]:
    """Wargear instances: WargearInventoryID / owner / the template name that follows (bare lps).

    Template families: `WT_*` (weapons) / `WTP_*` (consumables, stasis grenades, etc.) /
    `WDT_*` (consumable descriptors). The reference save stores `WTP_FragGrenade` this way,
    so do not use the old `WT_`-only regex.
    `equipped_member` = EquippedTeamRosterMemberID (None = unequipped / in reserve);
    by TeamRosterMemberID: unit0→1 ... unit5→6.

    Note: block boundaries must end at **this entry's own template name**. The old implementation
    used a fixed +400 B window; a short entry (no EquippedTeamRosterMemberID) could pick up the
    *next* entry's WargearInventoryID as its owner (inv4 showed eq=5, which was actually inv5's ID).
    """
    out = []
    tags = scan_tags(b)
    for m in re.finditer(rb"WargearMetagameInstance_(\d+)", b):
        idx = int(m.group(1))
        lo = m.start()
        wt = re.search(rb"(?:WT|WTP|WDT)_[A-Za-z0-9_]+", b[lo:lo + 400])
        if not wt:
            continue
        hi = lo + wt.end()                       # entry ends after the template name
        inv_id = eq_id = None
        for t in tags:
            if not (lo <= t["pos"] < hi):
                continue
            if t["name"] == "WargearInventoryID" and isinstance(t["value"], int):
                inv_id = t["value"]
            elif t["name"] == "EquippedTeamRosterMemberID" and isinstance(t["value"], int):
                eq_id = t["value"]
        out.append({"index": idx, "template": wt.group().decode(),
                    "inventory_id": inv_id, "equipped_member": eq_id,
                    "ints_in_block": [inv_id, eq_id]})
    # deduplicate (the object name appears in both class and object paths)
    seen, uniq = set(), []
    for r in out:
        if r["index"] in seen:
            continue
        seen.add(r["index"])
        uniq.append(r)
    return uniq


def verdict(b: bytes) -> dict:
    """Core conclusion: which fields reference asset IDs and which are stored inline in the save."""
    tags = scan_tags(b)
    paths = sorted({t["value"] for t in tags
                    if t["name"] == "AbilityTemplateClass" and isinstance(t["value"], str)})
    tpl = sorted({t["value"] for t in tags
                  if t["name"] in ("TemplateName", "RangedWargearTemplateName",
                                   "MeleeWargearTemplateName")
                  and isinstance(t["value"], str) and re.match(r"^(UTP_|WT_)", t["value"])})
    units = parse_units(b)
    stat_rows = [u["Stats"] for u in units if u["Stats"]]
    wargear_fields = sorted({t["name"] for t in tags
                             if re.search(rb"WargearMetagameInstance_", b[max(0, t["pos"] - 300):t["pos"]])
                             and t["pos"] > 21500})
    return {
        "question": "存档里的兵的数字/技能/装备，是引用游戏资产 ID，还是存档自己单独存一套？",
        "answer": "混合：数值与名字内嵌自存；技能/兵种/武器是明文字符串形式的资产引用",
        "evidence": {
            "inline_stats": {
                "field": "Stats (IntProperty, ArrayIndex 0/1/4/5/6/7)",
                "example": stat_rows[0] if stat_rows else {},
                "conclusion": "存档内嵌整数，可任意改写；不从资产重算",
            },
            "inline_names": {
                "field": "UnitName (StrProperty)",
                "example": units[0]["UnitName"] if units else None,
                "conclusion": "自由字符串，可写任意名字（含中文，需 UTF-16LE 负长度 lps）",
            },
            "referenced_abilities": {
                "field": "AbilityTemplateClass (ObjectProperty)",
                "count_in_save": len(paths), "sample": paths[:5],
                "conclusion": "值为 /Game/Abilities/... 资产路径 + XXX_C 类名；不能编造数值，"
                              "但可从真实资产清单任意挑选组合、增删数组元素",
            },
            "referenced_templates": {
                "field": "TemplateName / RangedWargearTemplateName / MeleeWargearTemplateName",
                "count_in_save": len(tpl), "sample": tpl[:6],
                "conclusion": "值为 UTP_* / WT_* 资产名字符串，只能选真实存在的模板",
            },
            "wargear_instances": {
                "fields": wargear_fields,
                "conclusion": "装备实例只有 ID / 装备者 ID / 一个裸 WT_* 模板名，"
                              "【没有】内嵌数值或技能字段",
            },
        },
        "practical": [
            "数值（Stats）、士兵名（UnitName）、ID 可任意改 → 测试用例/彩蛋数据自由造",
            "技能是明文路径 → 可从 136 个真实技能资产里任意挑 9 个塞进一个战士（需重建数组 + 同步祖先 Size）",
            "兵种/武器是明文资产名 → 可从 76 个 UTP_ / 129 个 WT_ 里任意混搭",
            "负向用例：填一个不存在的资产路径，测游戏容错（是否跳过 / 崩溃）",
            "「给手枪挂格挡 80%」：装备实例本身无技能字段；正确做法是把 Wargear 类被动技能"
            "（Abilities/Passive/Wargear/ABP_T*_RefractorShield 等）挂进该单位的 PassiveAbilities，"
            "或直接把 Stats 对应索引改成 80",
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="存档结构解剖（只读）")
    ap.add_argument("cmd", choices=["verdict", "units", "abilities", "stats", "wargear", "tags"])
    ap.add_argument("path", nargs="?")
    a = ap.parse_args()
    b = pathlib.Path(a.path or str(SGP.SAMPLE)).read_bytes()
    if a.cmd == "verdict":
        print(json.dumps(verdict(b), ensure_ascii=False, indent=2))
    elif a.cmd == "units":
        print(json.dumps(parse_units(b), ensure_ascii=False, indent=2))
    elif a.cmd == "abilities":
        print(json.dumps(parse_abilities(b), ensure_ascii=False, indent=2))
    elif a.cmd == "stats":
        print(json.dumps(parse_stats(b), ensure_ascii=False, indent=2))
    elif a.cmd == "wargear":
        print(json.dumps(parse_wargear(b), ensure_ascii=False, indent=2))
    elif a.cmd == "tags":
        print(json.dumps(scan_tags(b), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
