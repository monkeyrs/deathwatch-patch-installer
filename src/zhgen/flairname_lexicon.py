# -*- coding: utf-8 -*-
"""flairname_lexicon.py — UnitTemplate.FlairName Chinese text layer (master lexicon ↔ full patch).

Design decisions:
  ① Chinese translations are filled directly into the master lexicon spreadsheet;
  ② Prefer computed lengths over equal-length substitution: variable-length edits are allowed (export SerialSize is recomputed), so Chinese length is unrestricted.

Subcommands:
  python "steam patching/tools/flairname_lexicon.py" init
      Create/refresh two helper sheets in the master spreadsheet (idempotent; **preserves filled-in Chinese**):
        helper sheet A: 16 distinct English strings × 1 row; only column E is filled in
        helper sheet B: 62 UTP_* assets → English string mapping (read-only reference)
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
    "method": "All 62 assets rolled out to sandbox → auto_test.py run "
              "\"steam patching/tools/auto_test_scenarios/flairname_ab.json\" --tag varlen4",
    "canonical_scenario": "steam patching/tools/auto_test_scenarios/flairname_ab.json"
                          " (committed; this is the exact scenario varlen4 runs)",
    "l2": "verdict=PASS; the game opened in 6.16s on cold start with no crash through the full run; "
          "the non-black rate matched the main-menu baseline; the save auto-restored the golden "
          "5e5e6963… and the process exited cleanly.",
    "l3": "All 6 outpost cards' small class text switched to Chinese: CLEUTIN 老兵 / MARCELLUS 毁灭者 / "
          "THORGIR 突击兵 / REGULIUS 战术兵 / MAGNUS 战术兵 / VARRAS 极限战士药剂师; "
          "VARRAS's 7-character Chinese, when cropped and zoomed in, showed complete, clear glyphs "
          "with no tofu blocks and no truncation, proving that long variable-length Chinese works "
          "in-game too.",
    "note": "PNG screenshots are gitignored and kept only locally; sharing across machines requires "
            "a committable text summary. Zoomed image: steam patching/out/varlen4_varras_zoom.png.",
    # L2/L3 evidence only applies to the term set that was rolled out; once column E changes, the fingerprint no longer matches,
    # and the generator downgrades the package to "L0 PASS / L2+L3 pending re-verification" rather than claiming in-game validation.
    # After the final term revisions,
    # the variable-length test was rerun and the fingerprint updated to the current set.
    "terms_sha256": "5b7c0c622ae8ee5c",
    "terms_note": "sha256(sorted(en,zh))[:16]; the 16-term set used in the varlen4 in-game rollout "
                  "(c78b14e6453e6689=varlen2 and d34174dd0debf695=varlen3 are historical sets).",
}


def term_fingerprint(terms: dict) -> str:
    """Fingerprint of the term set: determines whether the L2/L3 in-game evidence still applies to the current build."""
    raw = json.dumps(sorted(terms.items()), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

SHEET_KEYS = "keys"
SHEET_TERMS = "FlairName_待填"
SHEET_ASSETS = "FlairName_资产清单"
SHEET_ORDER = [SHEET_KEYS, SHEET_TERMS, SHEET_ASSETS]

TERM_COLS = ["English FlairName", "Asset count", "Exact translation in master", "Master term hints",
             "Chinese translation (fill this column)", "Source", "Suggested length", "Notes",
             "Data-table translation (reference)", "Data-table basis"]
TERM_WIDTHS = [26, 9, 18, 46, 22, 20, 11, 56, 22, 46]
ASSET_COLS = ["No.", "Asset file", "English FlairName", "Original EN length", "FText offset",
              "Used in save", "Chinese to apply", "loc_key (FText)"]
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
                    raise RuntimeError("FlairName tag metadata incomplete (tag_off/value_off/size)")
                if p.get("type") != "TextProperty":
                    raise RuntimeError(f"FlairName is not TextProperty: {p.get('type')}")
                return ft, tag_off, tag_size
    return None, None, None


def source_fstring_bounds(data: bytes, r: dict) -> tuple[int, int, int, int, int]:
    """Return (src_len_off, old_fs_total, ftext_off, tag_off, tag_size)."""
    ft, tag_off, tag_size = locate_ftext_off(data, r)
    if ft is None:
        raise RuntimeError("FlairName FText not located")
    loc_len_off = ft + 9                      # flags(4) + source_flags(4) + history(1)
    loc_len = struct.unpack_from("<i", data, loc_len_off)[0]
    if loc_len <= 0:
        raise RuntimeError(f"loc_key FString is not ANSI (len={loc_len}); needs separate handling")
    slo = loc_len_off + 4 + loc_len
    old_len = struct.unpack_from("<i", data, slo)[0]
    if old_len <= 0:
        raise RuntimeError(f"source FString is already UTF-16LE (len={old_len}); not the original")
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
        return "(no related short word in master; choose a name yourself)"
    return "；".join(f"{e}={z}" for e, z in hits[:4])


def read_terms() -> dict[str, str]:
    """Read the Chinese layer: from the single sheet take en_source->zh_to_fill where domain=flairname_asset; the legacy layout reads column E of the old helper sheet."""
    from openpyxl import load_workbook
    if not LEXICON.is_file():
        raise SystemExit(f"[ERR] Master spreadsheet not found: {LEXICON}")
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
        # 2. Legacy multi-sheet layout: column E of the old sheet
        if SHEET_TERMS not in wb.sheetnames:
            raise SystemExit(f"[ERR] Master has neither a keys.domain column nor sheet {SHEET_TERMS!r}; run init first")
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
        return ["empty value"]
    if any(ord(c) < 0x20 for c in zh):
        errs.append("contains control characters")
    if "{" in zh or "}" in zh:
        errs.append("contains curly braces (FText treats them as placeholders)")
    if not any(ord(c) > 0x7F for c in zh):
        errs.append("all ASCII (no Chinese characters, i.e. not localized)")
    if len(zh) > 32:
        errs.append(f"too long: {len(zh)} characters (>32, card may overflow)")
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
        raise SystemExit(f"[ERR] Master spreadsheet not found: {LEXICON}")
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
    print(f"Backed up master → {bak}  ({bak.stat().st_size} B)")

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
        "Fill only this column: enter the Chinese translation for the English FlairName in column A.\n"
        "All UTP_* assets sharing an English string use this one translation (16 English strings → 62 assets).\n"
        "Since 2026-11-12 variable-length transforms are supported, so length is not limited by the English source;\n"
        "however card layout space is limited, so stay within the \"Suggested length\" column.\n"
        "Leave blank = the English string keeps its original English and no patch is generated.\n"
        "After saving, run: python \"steam patching/tools/flairname_lexicon.py\" check",
        "3DS-DW")
    for en in terms:
        group = by_en[en]
        hint = term_hints(en, pairs)
        auto = exact.get(en) or exact.get(en.lower()) or ""
        k = kept.get(en, {})
        zh = k.get("E") or auto
        if k.get("E"):
            src = k.get("F") or "filled by user"
        elif auto:
            src = "exact master match (editable)"
        else:
            src = "to be filled by user"
        soft = max(2, (len(en) + 1) // 2)
        wt.append([en, len(group), auto or "(none)", hint, zh, src, soft,
                   f"affects {len(group)} assets, see {SHEET_ASSETS}; variable length supported, no length limit",
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
                   f"=IFERROR(VLOOKUP(C{row},'{SHEET_TERMS}'!$A:$E,5,FALSE),\"(unfilled)\")",
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
    print(f"Master updated: {LEXICON}")
    print(f"  sheets = {SHEET_ORDER}  (active = {SHEET_TERMS})")
    print(f"  terms {len(terms)} / assets {len(assets)} / translated {filled} / to fill {len(terms)-filled}")
    print(f"  user-filled entries preserved = {kept_n}")
    print("  Next: fill column E (light yellow) of `FlairName_待填` in Excel, save, then run check")
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
        raise SystemExit(f"[ERR] No <script id=\"DATA\"> JSON block in {path}")
    D = json.loads(m.group(1))
    if "units" not in D:
        raise SystemExit("[ERR] DATA JSON missing units array")
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
            kind = ("units.name (only 1 asset for this English string)" if len(group) == 1
                    else f"units.name (all {len(group)} assets agree)")
            if len(group) == 1:
                kind += "; note: data-table name may be a unit nickname rather than a direct FlairName translation, please review"
        elif en in GENERIC_FROM_MASTER:
            key, expect_cls = GENERIC_FROM_MASTER[en]
            cand = exact.get(key) or exact.get(key.lower()) or ""
            kind = (f"HTML units.name has {len(names)} variants (unit nicknames, not used); "
                    f"using master short word `{key}={cand}` instead, "
                    f"{'matching' if cls == expect_cls else 'DIFFERING from (please recheck)'} "
                    f"HTML units.cls=`{cls}`")
        else:
            kind = (f"HTML units.name has {len(names)} variants (unit nicknames, not used); "
                    f"HTML units.cls=`{cls}` for reference only; no consistent translation, left for the user")
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
            raise SystemExit(f"[ERR] Data table not found: {src}")
        D = parse_html_units(src)
        assets = scan_assets()
        exact, _ = master_translations()
        derived = derive_html_translations(assets, D, exact)
        payload = {
            "source_file": str(src),
            "source_sha256": sha(src.read_bytes()),
            "extracted_at_utc": stamp,
            "rule": "Only a unique/consistent units.name counts as a translation; multiple names = unit nickname; "
                    "generic class strings use the master's authoritative short word",
            "html_totals": {k: len(v) for k, v in D.items()},
            "by_en": derived,
        }
        HTML_MAP_OUT.parent.mkdir(parents=True, exist_ok=True)
        HTML_MAP_OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                encoding="utf-8")
        print(f"Extracted from data table → {HTML_MAP_OUT}  sha256={payload['source_sha256'][:16]}…")
    else:
        if not HTML_MAP_OUT.is_file():
            raise SystemExit("[ERR] No extracted mapping found; first run once with --file <data-table.html>")
        payload = json.loads(HTML_MAP_OUT.read_text(encoding="utf-8"))
        derived = payload["by_en"]
        print(f"Replaying extracted mapping ← {HTML_MAP_OUT}  ({payload.get('extracted_at_utc')})")

    bak_dir = BACKUP_ROOT / f"{stamp}_before_html_import"
    bak_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(LEXICON, bak_dir / LEXICON.name)
    print(f"Backed up master → {bak_dir / LEXICON.name}")

    wb = load_workbook(LEXICON)
    if SHEET_TERMS not in wb.sheetnames:
        raise SystemExit(f"[ERR] Master missing sheet {SHEET_TERMS!r}; run init first")
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
        basis = str(d.get("basis") or "(string not in data table)")
        if d.get("unmatched"):
            basis += "; missing data-table keys: " + ", ".join(d["unmatched"][:3])
        cI, cJ = ws.cell(row=row, column=9), ws.cell(row=row, column=10)
        cI.value = cand or "(no consistent translation)"
        cJ.value = basis
        for c in (cI, cJ):
            c.font = Font(color=LOCK_FONT)
            c.fill = PatternFill("solid", fgColor=GREY_FILL)
            c.alignment = Alignment(vertical="center", wrap_text=(c is cJ))

        cur = str(ws.cell(row=row, column=5).value or "").strip()
        if cur:
            n_keep += 1
            cJ.value = "[Excel already has a translation, not overwritten] " + cJ.value
            continue
        if cand and not validate_zh(cand):
            ws.cell(row=row, column=5).value = cand
            ws.cell(row=row, column=6).value = "data-table translation (editable)"
            n_fill += 1
        else:
            n_blank += 1
    ws.auto_filter.ref = f"A1:J{ws.max_row}"
    wb.save(LEXICON)
    wb.close()
    print(f"=== import-html done ===")
    print(f"  translations added {n_fill} / kept {n_keep} (**not overwritten**) / still blank {n_blank}")
    print("  Columns I/J now hold data-table translations and their basis; column E (light yellow) is editable")
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
        # 2. Legacy multi-sheet layout: column E of the old sheet
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
    print(f"=== FlairName translation health check (read-only) ===")
    print(f"  term rows {len(rows)} / assets {len(assets)} / filled & valid {len(filled)} / blank {len(empty)} / invalid {len(bad)}")
    print(f"  assets that would change = {sum(n for _, _, n in filled)} / {len(assets)}")
    for en, zh, n in filled:
        print(f"    OK  {en:26s} → {zh:12s} ({n} assets)")
    if empty:
        print("  Blank (keeps original English):")
        for en in empty:
            print(f"    --  {en:26s} ({by_en.get(en, 0)} assets)")
    for en, zh, errs in bad:
        print(f"    !!  {en:26s} → {zh!r}  {errs}")
    print("  After saving, run build to generate `patches/flairname_zh_full/`.")
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
        raise RuntimeError("Missing PACKAGE_FILE_TAG at end of file; refusing to rewrite")
    ex = r["exports"]
    if len(ex) != 1 or ex[0]["off"] != r["headers_size"]:
        raise RuntimeError("Not a 'single export + data at end of header' layout; refusing variable length")
    eo = r["summary"]["export_offset"]
    if struct.unpack_from("<i", data, eo + 0x18)[0] != ex[0]["size"]:
        raise RuntimeError("export SerialSize field location check failed")
    if struct.unpack_from("<i", data, eo + 0x1C)[0] != ex[0]["off"]:
        raise RuntimeError("export SerialOffset field location check failed")
    if struct.unpack_from("<i", data, tag_off + 16)[0] != tag_size:
        raise RuntimeError("FPropertyTag.Size field location check failed")

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
        raise RuntimeError(f"{stem}: readback parse failed {r2.get('why')}")
    if r2["file_size"] != len(new_data):
        raise RuntimeError(f"{stem}: readback file_size mismatch {r2['file_size']} != {len(new_data)}")
    props = [p for w in r2.get("walks", []) for p in w.get("props", []) if isinstance(p, dict)]
    if len(props) != expect_props:
        raise RuntimeError(f"{stem}: readback property count mismatch {len(props)} != {expect_props}")
    flair = [p for p in props if p.get("name") == "FlairName"]
    if len(flair) != 1:
        raise RuntimeError(f"{stem}: readback FlairName count {len(flair)}")
    got = str(flair[0].get("value", {}).get("source", ""))
    if got != zh:
        raise RuntimeError(f"{stem}: readback source={got!r} != {zh!r}")
    return dict(ok=True, source=got, n_props=len(props),
                file_size=r2["file_size"], export_size=r2["exports"][0]["size"])


def selftest_both_directions(assets: list[dict]) -> dict:
    """Show that Δ can be positive or negative: transform the same asset with shorter and longer Chinese strings and read back statically.

    Take "Veteran" (src_len=7, FString 12B):
      short CJK string (2 chars) -> FString 10B, delta = -2
      long CJK string (6 chars)  -> FString 18B, delta = +6
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
        raise RuntimeError(f"bidirectional variable-length selftest failed: {out}")
    return dict(asset=a["file"], **out)


def cmd_build(_args) -> int:
    assets = scan_assets()
    terms = read_terms()   # single sheet (domain=flairname_asset) or legacy multi-sheet layout; both return {en: zh}

    problems = [(e, z, validate_zh(z)) for e, z in terms.items() if validate_zh(z)]
    if problems:
        for e, z, errs in problems:
            print(f"[REFUSE] {e} → {z!r}: {errs}")
        return 3
    if not terms:
        raise SystemExit("[REFUSE] Column E of the term sheet is entirely empty, no translations to generate. "
                         "Fill in Chinese in the master first.")

    fp = term_fingerprint(terms)
    l2_current = fp == L2L3_EVIDENCE["terms_sha256"]
    if not l2_current:
        print(f"  [NOTE] term-set fingerprint {fp} != L2/L3 evidence {L2L3_EVIDENCE['terms_sha256']}: "
              f"current package is L0 only; L2/L3 require in-game re-verification")

    selftest = selftest_both_directions(assets)
    print(f"  Bidirectional variable-length selftest PASS: {selftest['asset']} "
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
            skipped.append(dict(file=a["file"], en=a["en"], reason="not filled in term sheet"))
            continue
        src = ORIG_CONTENT / PKG_REL_DIR / a["file"]
        data = src.read_bytes()
        r = ue4prop.read_package(f"{PKG_REL_DIR}/{a['stem']}")
        if not r.get("ok"):
            raise RuntimeError(f"{a['stem']}: original parse failed")
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
        "description": "UnitTemplate.FlairName full Chinese package: driven by the Unify master "
                       "`zh_lexicon.xlsx` sheet `FlairName_待填`, broadcast by English string to all "
                       "UTP_* assets; uses a variable-length transform (recomputes export SerialSize), "
                       "so Chinese length is not limited by the original English.",
        "grading": "L0 PASS: every generated file is strictly re-read with ue4prop "
                   "(parse ok / file_size matches / all properties walkable / FlairName source == sheet translation); "
                   "includes bidirectional selftests with Δ<0 and Δ>0."
                   + ("L2 PASS: after full rollout the game cold-started without a crash, full run verdict=PASS. "
                      "L3 PASS: all 6 outpost cards' small class text displayed clear Chinese (see the l2_l3 field)."
                      if l2_current else
                      "L2 / L3: apply only to the previous term set (fingerprint mismatch); "
                      "the current Chinese batch has not been verified in-game yet — re-apply once the lock is "
                      "free and rerun the varlen scenario."),
        "translation_fingerprint": fp,
        "l2_l3": dict(L2L3_EVIDENCE,
                      current_terms_sha256=fp,
                      applies_to_current_build=l2_current,
                      current_build_status=("L2 + L3 PASS" if l2_current
                                            else "L0 PASS / L2 + L3 PENDING_REVERIFY")),
        "source": {
            "lexicon": "427/Tools/zh_lex/out/unified/zh_lexicon.xlsx",
            "sheet": SHEET_TERMS, "column": "E (Chinese translation - fill this column)",
            "filled_terms": len(terms), "assets_total": len(assets),
            "assets_patched": len(entries), "assets_skipped": len(skipped),
        },
        "variable_length_selftest": selftest,
        "method": {
            "fstring": "locres_v3.write_fstring(zh) → UTF-16LE, len = -(char count + 1) (includes trailing NUL)",
            "variable_length": "replace the source FString (variable length) → the trailing 4-byte PACKAGE_FILE_TAG "
                               "shifts with the data → FPropertyTag.Size(@tag_off+16) += Δ → the export record's "
                               "SerialSize at +0x18 += Δ; SerialOffset / TotalHeaderSize unchanged.",
            "why_safe": "76/76 UTP_* are single-export, with serialized data running from headers_size to end of "
                        "file; a 4-byte-aligned header scan finds no int32 pointing to file_size.",
            "critical": "tag.Size must be updated too, otherwise the engine skips the property value using the old "
                        "size → the property stream shifts (same class of bug as the -3 crash)."
        },
        "entries": entries,
        "skipped": skipped,
    }
    (PATCH_DIR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# flairname_zh_full (master-driven, variable-length full package)", "",
        "## How to change the Chinese text", "",
        "1. Open `427/Tools/zh_lex/out/unified/zh_lexicon.xlsx` and switch to sheet `FlairName_待填`;",
        "2. Only edit **column E \"Chinese translation (fill this column)\"** (light yellow); all assets "
        "under the same English string share one translation;",
        "3. Save → run `python \"steam patching/tools/flairname_lexicon.py\" check` for the health check;",
        "4. `python \"steam patching/tools/flairname_lexicon.py\" build` regenerates this package;",
        "5. `python \"steam patching/tools/patch_sandbox.py\" apply flairname_zh_full`;",
        "6. `python \"steam patching/tools/auto_test.py\" run \"steam patching/tools/auto_test_scenarios/flairname_ab.json\" --tag <tag>` "
        "for in-game acceptance.", "",
        "Blank English strings → the corresponding assets keep their original English and are not "
        "included in the patch package.", "",
        "## Why variable length is safe", "",
        "In the original every UTP_* is a **single export**, whose serialized data runs from "
        "`headers_size` all the way to end of file,",
        "with the last 4 bytes being `PACKAGE_FILE_TAG` (0x9E2A83C1). A 4-byte-aligned scan of the "
        "whole header finds",
        "**no field pointing to the total file length**. A variable-length edit must update **two** "
        "lengths in sync:",
        "① `FPropertyTag.Size` (`@tag_off+16`, the byte length of the TextProperty value);",
        "② the export record's `SerialSize` at `+0x18`. `SerialOffset` / `TotalHeaderSize` are unchanged.",
        "Changing only ② and not ① makes the engine skip the property value using the old size → the "
        "property stream shifts (the same class of bug as the `-3` crash,",
        "caught directly by the L0 static readback on 2026-11-12 as `self-table stream incomplete`).",
        "To match, `build` automatically runs a bidirectional selftest on every run (the same asset is "
        "generated once with Δ<0 and once with Δ>0, each read back statically).", "",
        "FString negative-length semantics: `|n|` **includes the trailing NUL** (the previous version "
        "wrote `-3`, consuming 2 bytes too few and crashing at load time,",
        "see `LOG.md` §4); this tool always goes through `locres_v3.write_fstring()` and never computes "
        "it by hand.", "",
        "## Package statistics", "",
        f"- Terms filled **{len(terms)}** / assets changed **{len(entries)}** / skipped **{len(skipped)}**",
        f"- Total byte change Δ = {sum(i['delta'] for i in infos):+d}",
        f"- Term-set fingerprint `{fp}` (L2/L3 evidence `{L2L3_EVIDENCE['terms_sha256']}`"
        + (", match)" if l2_current else ", **MISMATCH**)"), "",
        "| Asset | English | Chinese | Δ bytes | FString |", "|---|---|---|---:|---|",
    ]
    for i in infos:
        lines.append(f"| `{i['file']}` | {i['en']} | {i['zh']} | {i['delta']:+d} | "
                     f"{i['old_fs_total']}B → {i['new_fs_total']}B |")
    lines += [
        "", "## Strict static readback (L0)", "",
        "Each generated file is written to a temporary Content tree and re-parsed by "
        "`ue4prop.read_package()`, asserting each item:",
        "`ok=True` / `file_size == new byte count` / property count matches the original / "
        "`FlairName.source == sheet translation`.",
        "Any mismatch aborts with an error; no patch package is written.",
        "`build` also runs a bidirectional selftest every time: the same `Veteran` asset is generated "
        "once with a 2-character (Δ<0) and once with a 6-character (Δ>0) Chinese string, each read back.", "",
        *(
            [
                "## L2 / L3 in-game (2026-11-12, both PASS, term set matches this package)", "",
                f"- Term-set fingerprint `{fp}` matches the evidence; report: `{L2L3_EVIDENCE['report']}` "
                "(verdict=PASS, no game crash)",
                f"- Screenshot: `{L2L3_EVIDENCE['shot']}`, cropped and zoomed in for review: "
                "CLEUTIN `老兵` / MARCELLUS `毁灭者` / THORGIR `突击兵` /",
                "  REGULIUS `战术兵` / MAGNUS `战术兵` / VARRAS `新兵` — clear glyphs, no tofu blocks.",
            ] if l2_current else [
                "## L2 / L3 in-game (2026-11-12, applies only to the **previous term set**)", "",
                f"- ⚠ This package's term-set fingerprint `{fp}` ≠ the in-game one "
                f"`{L2L3_EVIDENCE['terms_sha256']}`: the master's `FlairName_待填!E` has been changed; "
                "**the current Chinese batch is L0 only, L2/L3 not yet re-verified**; "
                "the evidence below only proves the variable-length transform works in-game, not that the "
                "current translations display correctly in-game.",
                f"- Previous term-set report: `{L2L3_EVIDENCE['report']}`; screenshot: `{L2L3_EVIDENCE['shot']}`",
                "  (review: CLEUTIN `老兵` / MARCELLUS `毁灭者` / THORGIR `突击兵` /",
                "  REGULIUS `战术兵` / MAGNUS `战术兵` / VARRAS `新兵` — clear glyphs, no tofu blocks).",
                "- Once the in-game lock is free and no game process is running: "
                "`patch_sandbox.py apply flairname_zh_full` → "
                "`auto_test.py run \"steam patching/tools/auto_test_scenarios/flairname_ab.json\" --tag <tag>`; "
                "after reviewing the PNG, set `L2L3_EVIDENCE.terms_sha256` to this package's fingerprint "
                "and rebuild to restore L2/L3 PASS.",
            ]
        ),
        "- ⇒ The variable-length transform (edit FString + FPropertyTag.Size + export SerialSize) works "
        "in-game, and Chinese length is no longer constrained by the original English.", "",
        "## Conventions", "",
        "- The original source tree `427/assets_drop/steam_original/` is read-only, never written;",
        "- All deployment goes through `patch_sandbox.py apply` (built-in unlink + backup + sha256 check);",
        "- Binary `*.uasset` files are gitignored and not committed; this package only commits "
        "`manifest.json` / `notes.md`;",
        "- In-game captures follow `steam patching/docs/AUTO_TEST_SYSTEM.md`: keep the window always on "
        "top, warm up the first click, no manual intervention;",
        "- PNGs must be reviewed with an image tool; never use grep as a substitute for visual acceptance.", "",
    ]
    (PATCH_DIR / "notes.md").write_text("\n".join(lines), encoding="utf-8")

    print(f"=== build done ===")
    print(f"  terms {len(terms)} / assets generated {len(entries)} / skipped {len(skipped)}")
    print(f"  Δ total {sum(i['delta'] for i in infos):+d} bytes; L0 static readback {len(infos)}/{len(infos)} PASS")
    print(f"  patch package: {PATCH_DIR}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="FlairName Chinese layer: Unify master ↔ variable-length full patch")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create/refresh master helper sheets (preserves user-filled translations)")
    p_html = sub.add_parser("import-html", help="merge existing translations from the Deathwatch data-table HTML (no overwrite)")
    p_html.add_argument("--file", default=None,
                        help="first run: pass the HTML path; omit to replay the extracted JSON mapping")
    sub.add_parser("check", help="read-only health check of fill status")
    sub.add_parser("build", help="generate variable-length full patch from the sheet + L0 strict static readback")
    args = ap.parse_args(argv)
    return {"init": cmd_init, "import-html": cmd_import_html,
            "check": cmd_check, "build": cmd_build}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
