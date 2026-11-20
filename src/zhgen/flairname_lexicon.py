# -*- coding: utf-8 -*-
"""flairname_lexicon.py — UnitTemplate.FlairName Chinese text layer (master lexicon ↔ full patch).

Design decisions:
  ① Chinese translations are filled directly into the master lexicon spreadsheet;
  ② Prefer computed lengths over equal-length substitution: variable-length edits are allowed (export SerialSize is recomputed), so Chinese length is unrestricted.

Subcommands:
  python "steam patching/tools/flairname_lexicon.py" init
      Create/refresh two helper sheets in the master spreadsheet (idempotent; **preserves filled-in Chinese**):
        `FlairName_待填`      16 distinct English strings × 1 row; only column E is filled in
        `FlairName_资产清单`  62 UTP_* assets → English string mapping (read-only reference)
  python "steam patching/tools/flairname_lexicon.py" check
      check: read-only; reports fill progress, translation validity, and the number of assets that would change; writes nothing.
  python "steam patching/tools/flairname_lexicon.py" build
      Read column E → broadcast by English string to the assets → variable-length FString replacement + export SerialSize recomputation
      → strict static readback (full property walk with ue4prop) → write to the patch directory.
      **Only the patch package is written; the sandbox, original assets, and master spreadsheet are untouched.**

Why the variable-length transform is safe (verified at byte level across all UTP_* assets):
  * Each UTP_* is a **single export** whose serialized data runs from `headers_size` to end of file;
  * The last 4 bytes are PACKAGE_FILE_TAG (0x9E2A83C1), which must stay at the end rather than at a fixed address;
  * A 4-byte-aligned scan of the whole header finds **no int32 pointing to file_size / file_size-4**
    (the 3 unaligned hits are cross-field false positives);
  * The only length-dependent field is `SerialSize` at `export_offset + 0x18` in the export record
    (`+0x1c` is `SerialOffset`, pointing to the end of the header; **unchanged**);
  * `TotalHeaderSize` @0x18 = header length, **unchanged**.
  ⇒ It suffices to replace the source FString and add Δ to `SerialSize`; the trailing tag shifts with the data.

Recompute / verification:
  python "steam patching/tools/flairname_lexicon.py" check
  python "steam patching/tools/flairname_lexicon.py" build
  python "steam patching/tools/patch_sandbox.py" apply flairname_zh_full
  python "steam patching/tools/auto_test.py" run "steam patching/tools/auto_test_scenarios/flairname_ab.json" --tag <tag>
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import pathlib
import re
import shutil
import struct
import sys
import tempfile
from collections import Counter, defaultdict

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "cooked_reader"))
sys.path.insert(0, str(ROOT / "427" / "Tools" / "zh_lex"))
import ue4prop          # noqa: E402
import locres_v3        # noqa: E402

ORIG_CONTENT = (ROOT / "427/assets_drop/steam_original/extracted/steamapps/common" /
                "Warhammer 40,000 Deathwatch - Tyranid Invasion/deathwatchgame/Content")
ue4prop.GAME = ORIG_CONTENT
PKG_REL_DIR = "Characters/UnitTemplates"
SB_REL_DIR = "deathwatchgame/Content/Characters/UnitTemplates"

LEXICON = ROOT / "427/Tools/zh_lex/out/unified/zh_lexicon.xlsx"
BACKUP_ROOT = ROOT / "427/Tools/zh_lex/out/unified/backups"
HTML_MAP_OUT = ROOT / "427/Tools/zh_lex/out/unified/flairname_html_source.json"
PATCH_DIR = ROOT / "steam patching/patches/flairname_zh_full"

# In-game evidence after a full rollout of this package. PNGs/reports are gitignored and kept only locally;
# The written conclusions are stored with the manifest so they are traceable across machines.
L2L3_EVIDENCE = {
    "verdict": "L2 + L3 PASS",
    "report": "steam patching/reports/flairname_ab_varlen4_20261112T100345Z/report.json",
    "shot": "steam patching/reports/flairname_ab_varlen4_20261112T100345Z/05_outpost_final.png",
    "method": "62 个资产全量投放 sandbox → auto_test.py run "
              "\"steam patching/tools/auto_test_scenarios/flairname_ab.json\" --tag varlen4",
    "canonical_scenario": "steam patching/tools/auto_test_scenarios/flairname_ab.json"
                          "（已入库，varlen4 直接跑的就是这一份）",
    "l2": "verdict=PASS，游戏冷启动 6.16s 开窗、全流程未崩；非黑率与主菜单基线一致，"
          "存档自动还原黄金 5e5e6963…、进程正常收尾。",
    "l3": "前哨站 6 张卡职业小字全部变中文：CLEUTIN 老兵 / MARCELLUS 毁灭者 / THORGIR 突击兵 / "
          "REGULIUS 战术兵 / MAGNUS 战术兵 / VARRAS 极限战士药剂师；"
          "VARRAS 的 7 字长中文裁剪放大回看为完整清晰汉字、非豆腐块、未截断，"
          "证明变长的「长中文」在真机同样成立。",
    "note": "PNG 截图被 gitignore，仅本机留档；跨机器共享需另存可入库文本。"
            "放大图 steam patching/out/varlen4_varras_zoom.png。",
    # L2/L3 evidence only applies to the term set that was rolled out; once column E changes, the fingerprint no longer matches,
    # and the generator downgrades the package to "L0 PASS / L2+L3 pending re-verification" rather than claiming in-game validation.
    # After the final term revisions,
    # the variable-length test was rerun and the fingerprint updated to the current set.
    "terms_sha256": "5b7c0c622ae8ee5c",
    "terms_note": "sha256(sorted(en,zh))[:16]；对应 varlen4 实机投放的 16 条译名集"
                  "（c78b14e6453e6689=varlen2、d34174dd0debf695=varlen3 为历史集合）。",
}


def term_fingerprint(terms: dict) -> str:
    """Fingerprint of the term set: determines whether the L2/L3 in-game evidence still applies to the current build."""
    raw = json.dumps(sorted(terms.items()), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

SHEET_KEYS = "keys"
SHEET_TERMS = "FlairName_待填"
SHEET_ASSETS = "FlairName_资产清单"
SHEET_ORDER = [SHEET_KEYS, SHEET_TERMS, SHEET_ASSETS]

TERM_COLS = ["英文 FlairName", "资产数", "主表精确译文", "主表术语线索",
             "中文译文（填这列）", "来源", "建议字数", "备注",
             "数据表译名（参考）", "数据表依据"]
TERM_WIDTHS = [26, 9, 18, 46, 22, 20, 11, 56, 22, 46]
ASSET_COLS = ["序号", "资产文件", "英文 FlairName", "原英文字数", "FText 偏移",
              "存档在用", "将采用的中文", "loc_key（FText）"]
ASSET_WIDTHS = [7, 48, 26, 14, 14, 12, 22, 36]

# Templates actually used by the 6 units in the saved game (recompute with the units command)
SAVE_UNITS = {
    "UTP_Tactical_Ultramarine_Support": "Magnus",
    "UTP_Tactical_Ultramarine_Overwatcher": "Regulus",
    "UTP_Assault_SpaceWolf_Zombie": "Thorgir the Mighty",
    "UTP_Devastator_Ultramarine_BoyScout": "Marcellus",
    "UTP_Tactical_BloodAngel_Veteran": "Cleutin",
    "UTP_Apothecary_Ultramarine": "Varras",
}

HDR_FILL = "FFD9D9D9"
EDIT_FILL = "FFFFF2CC"      # 8-digit ARGB; writing 6 digits ("FFF2CC") pads 00 alpha and breaks the highlight
LOCK_FONT = "FF808080"
GREY_FILL = "FFF7F7F7"

# Term hints from the master spreadsheet: exclude placeholder strings, untranslated strings, and Tyranid units (otherwise "Guard" pulls in "Hive Guard")
ENEMY_PREFIXES = ("hive ", "tyrant ", "tyranid", "genestealer", "brood",
                  "carnifex", "biovore", "hormagaunt", "termagant")
ALIAS_WORDS = {
    "sanguinary": "Blood Angel", "long": "Space Wolves", "fang": "Space Wolves",
    "wolf": "Space Wolves", "ultramarine": "ULTRAMARINES",
    "tyrannic": "Tyranid", "apothecary": "Apothecaries",
}


# --------------------------------------------------------------------------- #
# Shared: read-only scan of FlairName in the original UTP_* assets
# --------------------------------------------------------------------------- #
def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def locate_ftext_off(data: bytes, r: dict) -> tuple[int | None, int | None, int | None]:
    """Return (FText start offset, FPropertyTag start offset, tag.Size).

    Use the tag_off / size / value_off already parsed by the walker; more reliable than walking tags manually
    (StructProperty and similar have extra FNames, so manual walking shifts offsets).
    """
    for w in r.get("walks", []):
        for p in w.get("props", []):
            if isinstance(p, dict) and p.get("name") == "FlairName":
                ft = p.get("value_off")
                tag_off = p.get("tag_off")
                tag_size = p.get("size")
                if ft is None or tag_off is None or tag_size is None:
                    raise RuntimeError("FlairName tag 元数据不全（tag_off/value_off/size）")
                if p.get("type") != "TextProperty":
                    raise RuntimeError(f"FlairName 不是 TextProperty: {p.get('type')}")
                return ft, tag_off, tag_size
    return None, None, None


def source_fstring_bounds(data: bytes, r: dict) -> tuple[int, int, int, int, int]:
    """Return (src_len_off, old_fs_total, ftext_off, tag_off, tag_size)."""
    ft, tag_off, tag_size = locate_ftext_off(data, r)
    if ft is None:
        raise RuntimeError("FlairName FText 未定位")
    loc_len_off = ft + 9                      # flags(4) + source_flags(4) + history(1)
    loc_len = struct.unpack_from("<i", data, loc_len_off)[0]
    if loc_len <= 0:
        raise RuntimeError(f"loc_key FString 非 ANSI (len={loc_len})，需单独处理")
    slo = loc_len_off + 4 + loc_len
    old_len = struct.unpack_from("<i", data, slo)[0]
    if old_len <= 0:
        raise RuntimeError(f"source FString 已是 UTF-16LE (len={old_len})，非原版")
    return slo, 4 + old_len, ft, tag_off, tag_size


def scan_assets() -> list[dict]:
    """Read-only scan of UTP_* assets containing FlairName; returns English strings, offsets, and other metadata."""
    out = []
    files = sorted((ORIG_CONTENT / PKG_REL_DIR).glob("UTP_*.uasset"))
    for f in files:
        r = ue4prop.read_package(f"{PKG_REL_DIR}/{f.stem}")
        if not r.get("ok"):
            continue
        flair = None
        for w in r.get("walks", []):
            for p in w.get("props", []):
                if isinstance(p, dict) and p.get("name") == "FlairName":
                    flair = p.get("value", {})
        if flair is None:
            continue
        d = f.read_bytes()
        slo, old_total, ft, tag_off, tag_size = source_fstring_bounds(d, r)
        out.append({
            "file": f.name,
            "stem": f.stem,
            "en": str(flair.get("source", "")),
            "loc_key": str(flair.get("loc_key", "")),
            "ftext_off": ft,
            "tag_off": tag_off,
            "tag_size": tag_size,
            "src_len_off": slo,
            "old_fs_total": old_total,
            "file_size": len(d),
            "export_offset": r["summary"]["export_offset"],
            "export_size": r["exports"][0]["size"],
            "headers_size": r["headers_size"],
            "n_exports": len(r["exports"]),
            "n_props": sum(len(w.get("props", [])) for w in r.get("walks", [])),
        })
    return out


def master_translations() -> tuple[dict, dict]:
    """Read the master spreadsheet's keys sheet for (exact en→zh, keyword→hint lists). Read-only."""
    from openpyxl import load_workbook
    wb = load_workbook(LEXICON, read_only=True, data_only=True)
    ws = wb[SHEET_KEYS]
    it = ws.iter_rows(values_only=True)
    hdr = [str(x).strip() if x is not None else "" for x in next(it)]
    ie, iz = hdr.index("en_source"), hdr.index("zh_to_fill")
    exact, pairs = {}, []
    for row in it:
        en = str(row[ie] or "").strip()
        zh = str(row[iz] or "").strip()
        if not en or not zh or zh in ("-",):
            continue
        pairs.append((en, zh))
        exact.setdefault(en, zh)
        exact.setdefault(en.lower(), zh)
    wb.close()
    return exact, dict(pairs=pairs)


def term_hints(en: str, pairs: list[tuple[str, str]]) -> str:
    """Pick up to 4 related short-word translations from the master spreadsheet as hints; reference only, names are not chosen automatically."""
    def usable(e: str, z: str) -> bool:
        if "{" in e or "}" in e or e.strip() == z:
            return False
        return not any(e.lower().startswith(x) for x in ENEMY_PREFIXES)

    words = [w for w in re.split(r"[^A-Za-z]+", en) if len(w) >= 3]
    cands = words + [ALIAS_WORDS[w.lower()] for w in words if w.lower() in ALIAS_WORDS]
    hits: list[tuple[str, str]] = []
    for w in cands:
        low = w.lower()
        for mode in ("exact", "contains"):
            for e, z in pairs:
                if not usable(e, z):
                    continue
                ok = (e.strip().lower() == low) if mode == "exact" else (
                    low in e.lower() and len(e) <= 24)
                if ok and (e, z) not in hits:
                    hits.append((e, z))
                    break
            if len(hits) >= 4:
                break
        if len(hits) >= 4:
            break
    if not hits:
        return "（主表无相关短词，请自行定名）"
    return "；".join(f"{e}={z}" for e, z in hits[:4])


def read_terms() -> dict[str, str]:
    """Read Chinese: from the single sheet take en_source->zh_to_fill where domain=flairname_asset; the old layout reads FlairName_待填!E."""
    from openpyxl import load_workbook
    if not LEXICON.is_file():
        raise SystemExit(f"[ERR] 主表不存在：{LEXICON}")
    wb = load_workbook(LEXICON, read_only=True, data_only=True)
    try:
        # 1. Single-sheet layout: keys has a domain column
        if SHEET_KEYS in wb.sheetnames:
            it = wb[SHEET_KEYS].iter_rows(values_only=True)
            hdr = [str(x).strip() if x is not None else "" for x in next(it)]
            if "domain" in hdr:
                di = hdr.index("domain")
                ie = hdr.index("en_source")
                iz = hdr.index("zh_to_fill")
                out = {}
                for r in it:
                    if di < len(r) and str(r[di] or "").strip() == "flairname_asset":
                        en = str(r[ie] or "").strip() if ie < len(r) else ""
                        zh = str(r[iz] or "").strip() if iz < len(r) else ""
                        if en and zh:
                            out[en] = zh
                return out
        # 2. Old multi-sheet layout: column E of FlairName_待填
        if SHEET_TERMS not in wb.sheetnames:
            raise SystemExit(f"[ERR] 主表既无 keys.domain 列，也无 sheet {SHEET_TERMS!r}，请先跑 init")
        ws = wb[SHEET_TERMS]
        out = {}
        for row in ws.iter_rows(min_row=2, values_only=True):
            en = str(row[0] or "").strip()
            zh = str(row[4] or "").strip() if len(row) > 4 and row[4] is not None else ""
            if en and zh:
                out[en] = zh
        return out
    finally:
        wb.close()


def validate_zh(zh: str) -> list[str]:
    errs = []
    if not zh or not zh.strip():
        return ["空值"]
    if any(ord(c) < 0x20 for c in zh):
        errs.append("含控制字符")
    if "{" in zh or "}" in zh:
        errs.append("含花括号（会被 FText 当占位符）")
    if not any(ord(c) > 0x7F for c in zh):
        errs.append("全 ASCII（没有汉字，等于未汉化）")
    if len(zh) > 32:
        errs.append(f"过长 {len(zh)} 字（>32，卡片可能溢出）")
    return errs


# --------------------------------------------------------------------------- #
# init: create / refresh helper sheets
# --------------------------------------------------------------------------- #
def _style_header(ws, ncols):
    from openpyxl.styles import Alignment, Font, PatternFill
    for c in range(1, ncols + 1):
        cell = ws.cell(row=1, column=c)
        cell.fill = PatternFill("solid", fgColor=HDR_FILL)
        cell.font = Font(bold=True)
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    ws.row_dimensions[1].height = 30


def cmd_init(_args) -> int:
    from openpyxl import load_workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.comments import Comment

    if not LEXICON.is_file():
        raise SystemExit(f"[ERR] 主表不存在：{LEXICON}")
    assets = scan_assets()
    exact, extra = master_translations()
    pairs = extra["pairs"]
    by_en = defaultdict(list)
    for a in assets:
        by_en[a["en"]].append(a)
    terms = sorted(by_en)

    # Timestamped backup (copy only; the original is not deleted)
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bak_dir = BACKUP_ROOT / f"{stamp}_before_flairname_sheet"
    bak_dir.mkdir(parents=True, exist_ok=True)
    bak = bak_dir / LEXICON.name
    shutil.copy2(LEXICON, bak)
    print(f"已备份主表 → {bak}  ({bak.stat().st_size} B)")

    wb = load_workbook(LEXICON)
    # Preserve filled-in column E plus F/I/J reference values (keyed by English string) and restore them after rebuild;
    # Rerunning init is idempotent and never overwrites filled-in translations.
    kept: dict[str, dict] = {}
    if SHEET_TERMS in wb.sheetnames:
        old = wb[SHEET_TERMS]
        for row in old.iter_rows(min_row=2, values_only=True):
            en = str(row[0] or "").strip()
            if not en:
                continue
            def gv(i):
                return str(row[i]).strip() if len(row) > i and row[i] is not None else ""
            kept[en] = {"E": gv(4), "F": gv(5), "I": gv(8), "J": gv(9)}
        del wb[SHEET_TERMS]
    if SHEET_ASSETS in wb.sheetnames:
        del wb[SHEET_ASSETS]

    # ---- sheet 1: terms to fill (16 rows) ----
    wt = wb.create_sheet(SHEET_TERMS)
    wt.append(TERM_COLS)
    for i, w in enumerate(TERM_WIDTHS, 1):
        wt.column_dimensions[chr(64 + i)].width = w
    _style_header(wt, len(TERM_COLS))
    wt["E1"].comment = Comment(
        "只填这一列：填该英文 FlairName 对应的中文。\n"
        "同一英文串下的所有 UTP_* 资产共用此译文（共 16 个英文串 → 62 个资产）。\n"
        "2026-11-12 起已支持变长变换，字数不受原英文长度限制；\n"
        "但卡片排版空间有限，建议不超过「建议字数」列。\n"
        "留空 = 该英文串保持原版英文，不生成补丁。\n"
        "存盘后跑：python \"steam patching/tools/flairname_lexicon.py\" check",
        "3DS-DW")
    for en in terms:
        group = by_en[en]
        hint = term_hints(en, pairs)
        auto = exact.get(en) or exact.get(en.lower()) or ""
        k = kept.get(en, {})
        zh = k.get("E") or auto
        if k.get("E"):
            src = k.get("F") or "用户已填"
        elif auto:
            src = "主表精确匹配（可改）"
        else:
            src = "待用户填写"
        soft = max(2, (len(en) + 1) // 2)
        wt.append([en, len(group), auto or "（无）", hint, zh, src, soft,
                   f"影响 {len(group)} 个资产，见 {SHEET_ASSETS}；变长已支持，字数不限",
                   k.get("I", ""), k.get("J", "")])
        r = wt.max_row
        for c in range(1, len(TERM_COLS) + 1):
            cell = wt.cell(row=r, column=c)
            cell.alignment = Alignment(vertical="center", wrap_text=(c in (4, 8, 10)))
            if c != 5:
                cell.font = Font(color=LOCK_FONT)
                cell.fill = PatternFill("solid", fgColor=GREY_FILL)
        e = wt.cell(row=r, column=5)
        e.fill = PatternFill("solid", fgColor=EDIT_FILL)
        e.font = Font(bold=True, color="FF000000")
    wt.freeze_panes = "A2"
    wt.auto_filter.ref = f"A1:J{wt.max_row}"

    # ---- sheet 2: asset list (62 rows, read-only reference) ----
    wa = wb.create_sheet(SHEET_ASSETS)
    wa.append(ASSET_COLS)
    for i, w in enumerate(ASSET_WIDTHS, 1):
        wa.column_dimensions[chr(64 + i)].width = w
    _style_header(wa, len(ASSET_COLS))
    for i, a in enumerate(assets, 1):
        row = i + 1
        who = SAVE_UNITS.get(a["stem"], "")
        wa.append([i, a["file"], a["en"], len(a["en"]),
                   f"0x{a['ftext_off']:x}", f"✓ {who}" if who else "",
                   f"=IFERROR(VLOOKUP(C{row},'{SHEET_TERMS}'!$A:$E,5,FALSE),\"(未填)\")",
                   a["loc_key"][:16] + "…" if a["loc_key"] else ""])
        for c in range(1, len(ASSET_COLS) + 1):
            cell = wa.cell(row=row, column=c)
            cell.font = Font(color=LOCK_FONT)
            cell.fill = PatternFill("solid", fgColor=GREY_FILL)
            cell.alignment = Alignment(vertical="center")
    wa.freeze_panes = "A2"
    wa.auto_filter.ref = f"A1:H{wa.max_row}"

    # keys must be the first sheet (zh_unify.py reads the first sheet by default)
    order = [wb[name] for name in wb.sheetnames]
    order.sort(key=lambda s: SHEET_ORDER.index(s.title) if s.title in SHEET_ORDER else 99)
    wb._sheets = order
    # Excel opens directly on the fill-in sheet (keys is still the first sheet, so import tools are unaffected)
    wb.active = SHEET_ORDER.index(SHEET_TERMS)
    wb.save(LEXICON)
    wb.close()

    filled = sum(1 for en in terms
                 if (kept.get(en, {}).get("E") or exact.get(en) or exact.get(en.lower())))
    kept_n = sum(1 for v in kept.values() if v.get("E"))
    print(f"主表已更新: {LEXICON}")
    print(f"  sheets = {SHEET_ORDER}  (active = {SHEET_TERMS})")
    print(f"  术语 {len(terms)} 条 / 资产 {len(assets)} 个 / 已有译文 {filled} 条 / 待填 {len(terms)-filled} 条")
    print(f"  用户已填保留 = {kept_n} 条")
    print("  下一步：在 Excel 里填 `FlairName_待填` 的 E 列（浅黄），存盘后跑 check")
    return 0


# --------------------------------------------------------------------------- #
# import-html: merge existing translations from the data table HTML into the master spreadsheet (without overwriting existing values)
# --------------------------------------------------------------------------- #
GENERIC_FROM_MASTER = {
    # For generic unit strings, the HTML name is a personalized unit nickname (one string maps to 8-9 different names),
    # so it cannot serve as a FlairName translation; instead use the authoritative short word from the master spreadsheet, consistent with the HTML units.cls.
    "Assault Marine": ("Assault Marines", "突击"),
    "Tactical Marine": ("Tactical", "战术"),
}


def parse_html_units(path: pathlib.Path) -> dict:
    s = path.read_text(encoding="utf-8")
    m = re.search(r'<script id="DATA" type="application/json">(.*?)</script>', s, re.S)
    if not m:
        raise SystemExit(f"[ERR] {path} 里没有 <script id=\"DATA\"> JSON 块")
    D = json.loads(m.group(1))
    if "units" not in D:
        raise SystemExit("[ERR] DATA JSON 缺少 units 数组")
    return D


def derive_html_translations(assets: list[dict], D: dict, exact: dict) -> dict:
    """Derive translation candidates grouped by English FlairName.

    Rules (to avoid mistaking a unit nickname for a class title):
      * If the string maps to 1 asset, or all assets share the same units.name → use the name as the translation;
      * If it maps to multiple assets with differing names → the name is a personalized nickname, **not a translation**;
        Generic unit strings use the master spreadsheet's authoritative short word (must match HTML units.cls); otherwise leave blank for manual entry.
    """
    H = {u["key"]: u for u in D["units"]}
    by_en = defaultdict(list)
    for a in assets:
        by_en[a["en"]].append(a)
    out = {}
    for en, group in by_en.items():
        hs = [H[a["stem"]] for a in group if a["stem"] in H]
        names = Counter(str(h.get("name") or "").strip() for h in hs
                        if str(h.get("name") or "").strip())
        clss = Counter(str(h.get("cls") or "").strip() for h in hs
                       if str(h.get("cls") or "").strip())
        unmatched = [a["stem"] for a in group if a["stem"] not in H]
        cls = clss.most_common(1)[0][0] if clss else ""
        cand, kind = "", ""
        if len(names) == 1 and names:
            cand = next(iter(names))
            kind = ("units.name（该英文串仅 1 个资产）" if len(group) == 1
                    else f"units.name（{len(group)} 个资产全部一致）")
            if len(group) == 1:
                kind += "；注意数据表 name 可能是单位昵称而非 FlairName 直译，请审阅"
        elif en in GENERIC_FROM_MASTER:
            key, expect_cls = GENERIC_FROM_MASTER[en]
            cand = exact.get(key) or exact.get(key.lower()) or ""
            kind = (f"HTML units.name 有 {len(names)} 种（单位昵称，不采用）；"
                    f"改取主表短词 `{key}={cand}`，与 HTML units.cls=`{cls}` "
                    f"{'一致' if cls == expect_cls else '不一致（请复核）'}")
        else:
            kind = (f"HTML units.name 有 {len(names)} 种（单位昵称，不采用）；"
                    f"HTML units.cls=`{cls}` 供参考；无统一译名，留给用户填")
        out[en] = dict(candidate=cand, basis=kind, n_assets=len(group),
                       names=dict(names), cls=dict(clss), unmatched=unmatched)
    return out


def cmd_import_html(args) -> int:
    from openpyxl import load_workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if args.file:
        src = pathlib.Path(args.file)
        if not src.is_file():
            raise SystemExit(f"[ERR] 数据表不存在：{src}")
        D = parse_html_units(src)
        assets = scan_assets()
        exact, _ = master_translations()
        derived = derive_html_translations(assets, D, exact)
        payload = {
            "source_file": str(src),
            "source_sha256": sha(src.read_bytes()),
            "extracted_at_utc": stamp,
            "rule": "units.name 唯一/一致才当译名；多种 name = 单位昵称，通用兵种串改取主表权威短词",
            "html_totals": {k: len(v) for k, v in D.items()},
            "by_en": derived,
        }
        HTML_MAP_OUT.parent.mkdir(parents=True, exist_ok=True)
        HTML_MAP_OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                encoding="utf-8")
        print(f"已从数据表提取 → {HTML_MAP_OUT}  sha256={payload['source_sha256'][:16]}…")
    else:
        if not HTML_MAP_OUT.is_file():
            raise SystemExit("[ERR] 没有已提取的映射；请先用 --file <数据表.html> 跑一次")
        payload = json.loads(HTML_MAP_OUT.read_text(encoding="utf-8"))
        derived = payload["by_en"]
        print(f"重放已提取映射 ← {HTML_MAP_OUT}  ({payload.get('extracted_at_utc')})")

    bak_dir = BACKUP_ROOT / f"{stamp}_before_html_import"
    bak_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(LEXICON, bak_dir / LEXICON.name)
    print(f"已备份主表 → {bak_dir / LEXICON.name}")

    wb = load_workbook(LEXICON)
    if SHEET_TERMS not in wb.sheetnames:
        raise SystemExit(f"[ERR] 主表缺少 {SHEET_TERMS!r}，请先跑 init")
    ws = wb[SHEET_TERMS]
    for col, name, width in ((9, TERM_COLS[8], TERM_WIDTHS[8]),
                             (10, TERM_COLS[9], TERM_WIDTHS[9])):
        c = ws.cell(row=1, column=col)
        c.value = name
        c.fill = PatternFill("solid", fgColor=HDR_FILL)
        c.font = Font(bold=True)
        c.alignment = Alignment(vertical="center", wrap_text=True)
        ws.column_dimensions[chr(64 + col)].width = width

    n_fill = n_keep = n_blank = 0
    for row in range(2, ws.max_row + 1):
        en = str(ws.cell(row=row, column=1).value or "").strip()
        if not en:
            continue
        d = derived.get(en, {})
        cand = str(d.get("candidate") or "")
        basis = str(d.get("basis") or "（数据表无此串）")
        if d.get("unmatched"):
            basis += "；数据表缺 key: " + "、".join(d["unmatched"][:3])
        cI, cJ = ws.cell(row=row, column=9), ws.cell(row=row, column=10)
        cI.value = cand or "（无统一译名）"
        cJ.value = basis
        for c in (cI, cJ):
            c.font = Font(color=LOCK_FONT)
            c.fill = PatternFill("solid", fgColor=GREY_FILL)
            c.alignment = Alignment(vertical="center", wrap_text=(c is cJ))

        cur = str(ws.cell(row=row, column=5).value or "").strip()
        if cur:
            n_keep += 1
            cJ.value = "【Excel 已有译文，未覆盖】" + cJ.value
            continue
        if cand and not validate_zh(cand):
            ws.cell(row=row, column=5).value = cand
            ws.cell(row=row, column=6).value = "数据表译名（可改）"
            n_fill += 1
        else:
            n_blank += 1
    ws.auto_filter.ref = f"A1:J{ws.max_row}"
    wb.save(LEXICON)
    wb.close()
    print(f"=== import-html 完成 ===")
    print(f"  补入译文 {n_fill} 条 / 保留已有 {n_keep} 条（**未覆盖**）/ 仍留空 {n_blank} 条")
    print(f"  I/J 列已写入数据表译名与依据；E 列（浅黄）可直接改")
    return 0


# --------------------------------------------------------------------------- #
# check: read-only health check
# --------------------------------------------------------------------------- #
def cmd_check(_args) -> int:
    from openpyxl import load_workbook
    assets = scan_assets()
    rows = []   # (en, zh)
    wb = load_workbook(LEXICON, read_only=True, data_only=True)
    try:
        if SHEET_KEYS in wb.sheetnames:
            it = wb[SHEET_KEYS].iter_rows(values_only=True)
            hdr = [str(x).strip() if x is not None else "" for x in next(it)]
            if "domain" in hdr:
                di = hdr.index("domain")
                ei = hdr.index("en_source")
                zi = hdr.index("zh_to_fill")
                seen = set()
                for r in it:
                    if di < len(r) and str(r[di] or "").strip() == "flairname_asset":
                        en = str(r[ei] or "").strip() if ei < len(r) else ""
                        zh = str(r[zi] or "").strip() if zi < len(r) else ""
                        if en and en not in seen:
                            seen.add(en)
                            rows.append((en, zh))
        # 2. Old multi-sheet layout: column E of FlairName_待填
        if not rows and SHEET_TERMS in wb.sheetnames:
            ws = wb[SHEET_TERMS]
            for row in ws.iter_rows(min_row=2, values_only=True):
                en = str(row[0] or "").strip()
                zh = str(row[4] or "").strip() if len(row) > 4 and row[4] is not None else ""
                if en:
                    rows.append((en, zh))
    finally:
        wb.close()
    by_en = Counter(a["en"] for a in assets)
    filled, empty, bad = [], [], []
    for en, zh in rows:
        if not zh:
            empty.append(en)
            continue
        errs = validate_zh(zh)
        if errs:
            bad.append((en, zh, errs))
        else:
            filled.append((en, zh, by_en.get(en, 0)))
    print(f"=== FlairName 译文体检（只读） ===")
    print(f"  术语行 {len(rows)} / 资产 {len(assets)} / 已填且合法 {len(filled)} / 留空 {len(empty)} / 非法 {len(bad)}")
    print(f"  将被改动的资产 = {sum(n for _, _, n in filled)} / {len(assets)}")
    for en, zh, n in filled:
        print(f"    OK  {en:26s} → {zh:12s} ({n} 个资产)")
    if empty:
        print("  留空（保持原版英文）:")
        for en in empty:
            print(f"    --  {en:26s} ({by_en.get(en, 0)} 个资产)")
    for en, zh, errs in bad:
        print(f"    !!  {en:26s} → {zh!r}  {errs}")
    print("  存盘后跑 build 生成 `patches/flairname_zh_full/`。")
    return 1 if bad else 0


# --------------------------------------------------------------------------- #
# build: variable-length transform + strict static readback
# --------------------------------------------------------------------------- #
def transform_variable(data: bytes, r: dict, zh: str) -> tuple[bytes, dict]:
    """Variable-length replacement of the source FString; keep FPropertyTag.Size and export SerialSize in sync; the trailing tag stays at the end.

    In the UE4 property stream each tag has an `int32 Size` (@tag_off+16) = the byte length of the property value.
    When the FString inside FText changes length, both this Size and the export's SerialSize must += Δ,
    otherwise the engine/parser skips the value using the old size and the rest of the property stream shifts (the same class of bug as the -3 crash).
    """
    slo, old_total, ft, tag_off, tag_size = source_fstring_bounds(data, r)
    new_fs = locres_v3.write_fstring(zh)
    new_len = struct.unpack_from("<i", new_fs, 0)[0]
    delta = len(new_fs) - old_total

    if data[-4:] != b"\xc1\x83\x2a\x9e":
        raise RuntimeError("文件尾缺少 PACKAGE_FILE_TAG，拒绝改写")
    ex = r["exports"]
    if len(ex) != 1 or ex[0]["off"] != r["headers_size"]:
        raise RuntimeError("非「单 export + 数据在 header 末尾」结构，拒绝变长")
    eo = r["summary"]["export_offset"]
    if struct.unpack_from("<i", data, eo + 0x18)[0] != ex[0]["size"]:
        raise RuntimeError("export SerialSize 字段定位校验失败")
    if struct.unpack_from("<i", data, eo + 0x1C)[0] != ex[0]["off"]:
        raise RuntimeError("export SerialOffset 字段定位校验失败")
    if struct.unpack_from("<i", data, tag_off + 16)[0] != tag_size:
        raise RuntimeError("FPropertyTag.Size 字段定位校验失败")

    out = bytearray(data[:slo] + new_fs + data[slo + old_total:-4] + data[-4:])
    struct.pack_into("<i", out, tag_off + 16, tag_size + delta)
    struct.pack_into("<i", out, eo + 0x18, ex[0]["size"] + delta)
    info = dict(ftext_off=ft, src_len_off=slo, old_len=old_total - 4,
                new_len=new_len, old_fs_total=old_total,
                new_fs_total=len(new_fs), delta=delta,
                old_tag_size=tag_size, new_tag_size=tag_size + delta,
                old_size=ex[0]["size"], new_size=ex[0]["size"] + delta,
                old_file_size=len(data), new_file_size=len(out))
    assert len(out) == len(data) + delta
    return bytes(out), info


def static_verify(new_data: bytes, stem: str, zh: str, expect_props: int) -> dict:
    """Place the output in a temporary Content tree and reparse it strictly with ue4prop."""
    with tempfile.TemporaryDirectory(prefix="flairname_verify_") as td:
        tmp = pathlib.Path(td) / "Content" / PKG_REL_DIR
        tmp.mkdir(parents=True, exist_ok=True)
        (tmp / f"{stem}.uasset").write_bytes(new_data)
        saved = ue4prop.GAME
        try:
            ue4prop.GAME = pathlib.Path(td) / "Content"
            r2 = ue4prop.read_package(f"{PKG_REL_DIR}/{stem}")
        finally:
            ue4prop.GAME = saved
    if not r2.get("ok"):
        raise RuntimeError(f"{stem}: 回读 parse 失败 {r2.get('why')}")
    if r2["file_size"] != len(new_data):
        raise RuntimeError(f"{stem}: 回读 file_size 不符 {r2['file_size']} != {len(new_data)}")
    props = [p for w in r2.get("walks", []) for p in w.get("props", []) if isinstance(p, dict)]
    if len(props) != expect_props:
        raise RuntimeError(f"{stem}: 回读属性数不符 {len(props)} != {expect_props}")
    flair = [p for p in props if p.get("name") == "FlairName"]
    if len(flair) != 1:
        raise RuntimeError(f"{stem}: 回读 FlairName 数 {len(flair)}")
    got = str(flair[0].get("value", {}).get("source", ""))
    if got != zh:
        raise RuntimeError(f"{stem}: 回读 source={got!r} != {zh!r}")
    return dict(ok=True, source=got, n_props=len(props),
                file_size=r2["file_size"], export_size=r2["exports"][0]["size"])


def selftest_both_directions(assets: list[dict]) -> dict:
    """Show that Δ can be positive or negative: transform the same asset with shorter and longer Chinese strings and read back statically.

    Take "Veteran" (src_len=7, FString 12B):
      老兵 (2 chars) → FString 10B, Δ=-2
      资深老兵战士 (6 chars) → FString 18B, Δ=+6
    """
    a = next((x for x in assets if x["en"] == "Veteran"), assets[0])
    data = (ORIG_CONTENT / PKG_REL_DIR / a["file"]).read_bytes()
    r = ue4prop.read_package(f"{PKG_REL_DIR}/{a['stem']}")
    out = {}
    for tag, zh in (("shorter", "老兵"), ("longer", "资深老兵战士")):
        nd, info = transform_variable(data, r, zh)
        v = static_verify(nd, a["stem"], zh, a["n_props"])
        out[tag] = dict(zh=zh, delta=info["delta"],
                        old_bytes=info["old_fs_total"], new_bytes=info["new_fs_total"],
                        file_size=info["new_file_size"], verify_ok=v["ok"],
                        readback_source=v["source"])
    if not (out["shorter"]["delta"] < 0 < out["longer"]["delta"]):
        raise RuntimeError(f"双向变长自测失败: {out}")
    return dict(asset=a["file"], **out)


def cmd_build(_args) -> int:
    assets = scan_assets()
    terms = read_terms()   # single sheet (domain=flairname_asset) or old layout (FlairName_待填); both return {en: zh}

    problems = [(e, z, validate_zh(z)) for e, z in terms.items() if validate_zh(z)]
    if problems:
        for e, z, errs in problems:
            print(f"[REFUSE] {e} → {z!r}: {errs}")
        return 3
    if not terms:
        raise SystemExit("[REFUSE] 术语表 E 列全空，没有可生成的译文。请先在主表填中文。")

    fp = term_fingerprint(terms)
    l2_current = fp == L2L3_EVIDENCE["terms_sha256"]
    if not l2_current:
        print(f"  [注意] 译名集指纹 {fp} != L2/L3 证据的 {L2L3_EVIDENCE['terms_sha256']}："
              f"当前包只到 L0，L2/L3 须重新实机复验")

    selftest = selftest_both_directions(assets)
    print(f"  双向变长自测 PASS：{selftest['asset']} "
          f"Δ={selftest['shorter']['delta']:+d} / {selftest['longer']['delta']:+d}")

    ASSET_OUT = PATCH_DIR / "assets" / SB_REL_DIR
    ASSET_OUT.mkdir(parents=True, exist_ok=True)
    # Remove stale assets from the previous build so skipped translations do not leave outdated files
    for f in ASSET_OUT.glob("UTP_*.uasset"):
        f.unlink()

    entries, infos, skipped = [], [], []
    for a in assets:
        zh = terms.get(a["en"])
        if not zh:
            skipped.append(dict(file=a["file"], en=a["en"], reason="术语表未填"))
            continue
        src = ORIG_CONTENT / PKG_REL_DIR / a["file"]
        data = src.read_bytes()
        r = ue4prop.read_package(f"{PKG_REL_DIR}/{a['stem']}")
        if not r.get("ok"):
            raise RuntimeError(f"{a['stem']}: 原版解析失败")
        new_data, info = transform_variable(data, r, zh)
        v = static_verify(new_data, a["stem"], zh, a["n_props"])
        dst = ASSET_OUT / a["file"]
        dst.write_bytes(new_data)
        info.update(file=a["file"], en=a["en"], zh=zh, verify=v)
        infos.append(info)
        entries.append({
            "relative_path": f"{SB_REL_DIR}/{a['file']}",
            "src_sha256": sha(data), "dst_sha256": sha(new_data),
            "size": len(new_data), "differs_from_steam": True,
            "zh": zh, "delta_bytes": info["delta"],
        })

    manifest = {
        "name": "flairname_zh_full",
        "description": "UnitTemplate.FlairName 中文全量包：由 Unify 主表 "
                       "`zh_lexicon.xlsx` 的 `FlairName_待填` sheet 驱动，"
                       "按英文串广播到全部 UTP_* 资产；采用变长变换（重算 export SerialSize），"
                       "中文长度不受原英文限制。",
        "grading": "L0 PASS：每个生成物均经 ue4prop 静态严格回读"
                   "（parse ok / file_size 一致 / 全部属性可 walk / FlairName source == 表中译文）；"
                   "含 Δ<0 与 Δ>0 双向自测。"
                   + ("L2 PASS：全量投放后游戏冷启动未崩、流程 verdict=PASS。"
                      "L3 PASS：前哨站 6 张卡职业小字全部显示清晰中文（详见 l2_l3 字段）。"
                      if l2_current else
                      "L2 / L3：仅对应上一版译名集（fingerprint 不一致）；"
                      "当前这批中文尚未实机复验，需在锁空闲后重新 apply + 跑 varlen 场景。"),
        "translation_fingerprint": fp,
        "l2_l3": dict(L2L3_EVIDENCE,
                      current_terms_sha256=fp,
                      applies_to_current_build=l2_current,
                      current_build_status=("L2 + L3 PASS" if l2_current
                                            else "L0 PASS / L2 + L3 PENDING_REVERIFY")),
        "source": {
            "lexicon": "427/Tools/zh_lex/out/unified/zh_lexicon.xlsx",
            "sheet": SHEET_TERMS, "column": "E（中文译文（填这列））",
            "filled_terms": len(terms), "assets_total": len(assets),
            "assets_patched": len(entries), "assets_skipped": len(skipped),
        },
        "variable_length_selftest": selftest,
        "method": {
            "fstring": "locres_v3.write_fstring(zh) → UTF-16LE，len = -(字符数+1)（含结尾 NUL）",
            "variable_length": "替换 source FString（长度可变）→ 尾部 4 字节 PACKAGE_FILE_TAG 随数据平移 → "
                               "FPropertyTag.Size(@tag_off+16) += Δ → export 记录 +0x18 的 SerialSize += Δ；"
                               "SerialOffset / TotalHeaderSize 不变。",
            "why_safe": "76/76 个 UTP_* 均为单 export、序列化数据从 headers_size 一直到文件尾；"
                        "4 字节对齐扫描 header 无任何 int32 指向 file_size。",
            "critical": "必须同时改 tag.Size，否则引擎按旧 size 跳过属性值 → 属性流错位（与 -3 崩溃同款）。"
        },
        "entries": entries,
        "skipped": skipped,
    }
    (PATCH_DIR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# flairname_zh_full（主表驱动 · 变长全量包）", "",
        "## 怎么改中文", "",
        "1. 打开 `427/Tools/zh_lex/out/unified/zh_lexicon.xlsx`，切到 sheet `FlairName_待填`；",
        "2. 只改 **E 列「中文译文（填这列）」**（浅黄）；同一英文串下所有资产共用一条译文；",
        "3. 存盘 → `python \"steam patching/tools/flairname_lexicon.py\" check` 看体检；",
        "4. `python \"steam patching/tools/flairname_lexicon.py\" build` 重新生成本包；",
        "5. `python \"steam patching/tools/patch_sandbox.py\" apply flairname_zh_full`；",
        "6. `python \"steam patching/tools/auto_test.py\" run \"steam patching/tools/auto_test_scenarios/flairname_ab.json\" --tag <tag>` 实机验收。", "",
        "留空的英文串 → 对应资产保持原版英文，不进补丁包。", "",
        "## 为什么可以不等长", "",
        "原版每个 UTP_* 都是**单个 export**，其序列化数据从 `headers_size` 起一直到文件末尾，",
        "末尾 4 字节是 `PACKAGE_FILE_TAG` (0x9E2A83C1)。4 字节对齐扫描整个 header，",
        "**没有任何字段指向文件总长**。变长要同步改 **两处** 长度：",
        "① `FPropertyTag.Size`（`@tag_off+16`，TextProperty 值的字节数）；",
        "② export 记录 `+0x18` 的 `SerialSize`。`SerialOffset` / `TotalHeaderSize` 不变。",
        "只改 ② 不改 ① 会让引擎按旧 size 跳过属性值 → 属性流错位（与 `-3` 崩溃同款，",
        "2026-11-12 由 L0 静态回读 `self-table stream incomplete` 直接抓出）。",
        "配套：`build` 每次自动跑双向自测（同一资产 Δ<0 与 Δ>0 各生成一次并静态回读）。", "",
        "FString 负长度语义：`|n|` **包含结尾 NUL**（上一棒写成 `-3` 少消费 2 字节导致加载期崩溃，",
        "见 `LOG.md` §4）；本工具统一走 `locres_v3.write_fstring()`，不再手算。", "",
        "## 本包统计", "",
        f"- 术语已填 **{len(terms)}** 条 / 资产改动 **{len(entries)}** 个 / 跳过 **{len(skipped)}** 个",
        f"- 字节变化合计 Δ = {sum(i['delta'] for i in infos):+d}",
        f"- 译名集指纹 `{fp}`（L2/L3 证据对应 `{L2L3_EVIDENCE['terms_sha256']}`"
        + ("，一致）" if l2_current else "，**不一致**）"), "",
        "| 资产 | 英文 | 中文 | Δ 字节 | FString |", "|---|---|---|---:|---|",
    ]
    for i in infos:
        lines.append(f"| `{i['file']}` | {i['en']} | {i['zh']} | {i['delta']:+d} | "
                     f"{i['old_fs_total']}B → {i['new_fs_total']}B |")
    lines += [
        "", "## 静态严格回读（L0）", "",
        "每个生成物都写到临时 Content 树后由 `ue4prop.read_package()` 重新解析，逐项断言：",
        "`ok=True` / `file_size == 新字节数` / 属性条数与原版一致 / `FlairName.source == 表中译文`。",
        "任何一项不符即中止并报错，不会写出补丁包。",
        "`build` 每次还自动跑双向自测：同一 `Veteran` 资产用 2 字（Δ<0）与 6 字（Δ>0）中文各生成一次并回读。", "",
        *(
            [
                "## L2 / L3 实机（2026-11-12，均 PASS，译名集与本包一致）", "",
                f"- 译名集指纹 `{fp}` 与证据一致；报告：`{L2L3_EVIDENCE['report']}`（verdict=PASS，游戏未崩）",
                f"- 截图：`{L2L3_EVIDENCE['shot']}` 裁剪放大回看："
                "CLEUTIN `老兵` / MARCELLUS `毁灭者` / THORGIR `突击兵` /",
                "  REGULIUS `战术兵` / MAGNUS `战术兵` / VARRAS `新兵`，清晰汉字、非豆腐块。",
            ] if l2_current else [
                "## L2 / L3 实机（2026-11-12，仅对应**上一版译名集**）", "",
                f"- ⚠ 本包译名集指纹 `{fp}` ≠ 实机时的 `{L2L3_EVIDENCE['terms_sha256']}`："
                "主表 `FlairName_待填!E` 已被改动，**当前这批中文只到 L0，L2/L3 尚未复验**；"
                "下列证据只能证明「变长变换在真机成立」，不能证明当前译文在真机显示正确。",
                f"- 旧译名集报告：`{L2L3_EVIDENCE['report']}`；截图：`{L2L3_EVIDENCE['shot']}`",
                "  （回看：CLEUTIN `老兵` / MARCELLUS `毁灭者` / THORGIR `突击兵` /",
                "  REGULIUS `战术兵` / MAGNUS `战术兵` / VARRAS `新兵`，清晰汉字、非豆腐块）。",
                "- 待实机锁空闲、无游戏进程后：`patch_sandbox.py apply flairname_zh_full` → "
                "`auto_test.py run \"steam patching/tools/auto_test_scenarios/flairname_ab.json\" --tag <tag>`，回看 PNG 后把 "
                "`L2L3_EVIDENCE.terms_sha256` 更新为本包指纹，重新 build 即可恢复 L2/L3 PASS。",
            ]
        ),
        "- ⇒ 变长变换（改 FString + FPropertyTag.Size + export SerialSize）在真机成立，"
        "中文长度不再受原英文长度约束。", "",
        "## 纪律", "",
        "- 原版源树 `427/assets_drop/steam_original/` 只读，绝不写；",
        "- 投放一律走 `patch_sandbox.py apply`（自带断链 + 备份 + sha256 校验）；",
        "- 二进制 `*.uasset` 被 gitignore、不入库；本包只提交 `manifest.json` / `notes.md`；",
        "- 实机抓图按 `steam patching/docs/AUTO_TEST_SYSTEM.md`：窗口全程置顶、预热首次点击、禁止人工介入；",
        "- PNG 必须图像工具回看，禁止 grep 冒充视觉验收。", "",
    ]
    (PATCH_DIR / "notes.md").write_text("\n".join(lines), encoding="utf-8")

    print(f"=== build 完成 ===")
    print(f"  术语 {len(terms)} 条 / 生成 {len(entries)} 个资产 / 跳过 {len(skipped)} 个")
    print(f"  Δ 合计 {sum(i['delta'] for i in infos):+d} 字节；L0 静态回读 {len(infos)}/{len(infos)} PASS")
    print(f"  补丁包: {PATCH_DIR}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="FlairName 中文层：Unify 主表 ↔ 变长全量补丁")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="建/刷新主表辅助 sheet（保留用户已填译文）")
    p_html = sub.add_parser("import-html", help="整合《死亡守望数据表.html》的现成译名（不覆盖已有）")
    p_html.add_argument("--file", default=None,
                        help="首次运行传 HTML 路径；省略则从已提取的 JSON 映射重放")
    sub.add_parser("check", help="只读体检填写状态")
    sub.add_parser("build", help="读表生成变长全量补丁 + L0 静态严格回读")
    args = ap.parse_args(argv)
    return {"init": cmd_init, "import-html": cmd_import_html,
            "check": cmd_check, "build": cmd_build}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
