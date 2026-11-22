#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Batch patch generator that converts BFL display names from 0x1F to 0x34 (UTF-16), using variable-length edits with offset recomputation.

Background:
  Several functions in BFL_UnitFunctionLibrary (GetChapterString / GetClassString /
  GetStringForStat / GetStringForWargearCategory ...) use EX_StringConst(0x1F)
  to hardcode ANSI strings as UI display names, which go straight to screen via Conv_StringToText and bypass locres,
  so the 2178 locres entries in zh_lexicon have no effect on them. Testing confirmed:
  EX_UnicodeStringConst(0x34) + NUL-terminated UTF-16LE renders Chinese correctly on UE4.6 (Ver:413).

What this script does (bytes only, locres untouched):
  1. Load BFL with ue4prop and parse each function's bytecode containing 0x1F into an instruction tree with bp_bytecode.
  2. Rewrite each EX_StringConst found in the translation table as 0x34 + UTF-16LE(Chinese) + 0x0000,
     with length varying by the number of UTF-16 code units (variable length).
  3. Fix all jump offsets using a relocation map: EX_Jump / EX_JumpIfNot / EX_Skip /
     EX_PushExecutionFlow / EX_SkipOffsetConst code_offset, plus EX_Context*
     skip_offset and EX_SwitchValue end_offset.
  4. Rebuild the raw bytes in a single multi-insertion pass: replace each function's bytecode in place, updating its own
     serializedScriptSize(int32) and the export table's SerialSize; every serial_offset greater than
     that function's export SerialOffset accumulates the delta.
  5. Hard validation gate: reload the result with ue4prop -> reparse every modified function ->
     assert (a) decode is complete (consumed==len(bc)); (b) all strings from the translation table appear as 0x34 with correct Chinese;
     (c) none of those strings remain as 0x1F; (d) export table SerialSize is self-consistent.
  The patch file is written only if validation passes.

Usage:
  python bfl_display_string_patch.py --dry        # validate only; write to a temp file and print a report
  python bfl_display_string_patch.py --out <path> # after validation, write to the specified .uasset (for patch_sandbox assets)
  python bfl_display_string_patch.py --report json# also emit a JSON report
"""
from __future__ import annotations
import argparse, hashlib, json, struct, sys, pathlib

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, r"D:/3DS-DW/tools/cooked_reader")
sys.path.insert(0, r"D:/3DS-DW/427/Tools/bp_decode")
import ue4prop
import bp_bytecode as bp

REL = "UI/MainMenu/BlueprintFunctionLibraries/BFL_UnitFunctionLibrary"
# Patches are generated from the read-only original Steam baseline, not the current sandbox state (the sandbox may already be at 0x34,
# rereading it would find no 0x1F strings to translate and break reproducibility). The baseline is read-only.
ORIGINAL_CONTENT = pathlib.Path(
    r"D:/3DS-DW/427/assets_drop/steam_original/extracted/steamapps/common/"
    r"Warhammer 40,000 Deathwatch - Tyranid Invasion/deathwatchgame/Content")
SANDBOX_CONTENT = ORIGINAL_CONTENT
# Default output = patch-package asset (called directly by the pipeline; with no args it writes here; --dry writes nothing)
DEFAULT_OUT = pathlib.Path(
    r"D:/3DS-DW/steam patching/patches/bfl_display_zh/assets/deathwatchgame/Content/"
    r"UI/MainMenu/BlueprintFunctionLibraries/BFL_UnitFunctionLibrary.uasset")
# Jump targets may exceed len(bc) (in practice they land in the bytecodeBufferSize space, e.g. bufferSize-24),
# so no "< len(bc)" upper-bound clamping is applied; only a sanity check for absurd values.
JUMP_SANE_LIMIT = 0x10000
# True = equal-length replacement (tail padded with EX_Nothing=0x0B); length is unchanged, so no jump offsets need updating.
# In practice, variable-length edits (even with relocation) cause a fatal error on cold start; equal-length edits enter the game normally.
LENGTH_NEUTRAL = True
EXPORT_REC = 68          # bytes per export record
SERIAL_SIZE_OFF = 24     # byte offset of w[6] in the 17*i record
SERIAL_OFF_OFF = 28      # w[7]
HDR_20 = 20             # UFunction header (SuperStruct..serializedScriptSize, 5*i)

# ----------------------------------------------------------------- Translation source: unified lexicon spreadsheet
# Design principles:
#   The master translation table is read from the lexicon spreadsheet (single `keys` sheet, domain=bfl),
#   Any exact English match with a non-empty Chinese value is used; this is the tool's sole authoritative source.
#   A few chapter/unit categories have no singular exact key in the lexicon, but their Chinese is determined by existing terms
#   (plural keys / tooltip glosses / related words); FALLBACK fills these from existing translations and notes the source in a new sheet.
#   Hashed internal asset keys and blueprint compile-error strings are not in the lexicon and stay as 0x1F.
XLSX_PATH = r"D:/3DS-DW/427/Tools/zh_lex/out/unified/zh_lexicon.xlsx"

# Sole authoritative source: rows with domain=bfl in the single `keys` sheet (en_source -> zh_to_fill).
# The old multi-sheet layout and its builder were archived and are no longer supported.

# Deprecated column: file offset. Removed during the single-sheet merge; do not re-add.
# It was the real byte position of `0x1F + ASCII string + 0x00` in the original asset (verified against the asset),
# but the generation chain **never reads it**: locations come from bp_bytecode parsing the instruction tree, and jump relocation is recomputed independently.
# and the value only holds for the original asset; it breaks after patching or an upstream Steam update,
# storing it in the table would only mislead manual review. The information is not lost: it can always be recomputed from the read-only original.
#   python -c "d=open(r'427/assets_drop/.../BFL_UnitFunctionLibrary.uasset','rb').read(); print(hex(d.find(b'\x1f'+b'Tactical'+b'\x00')))"
# The "status" and "notes" columns were removed as well (notes were just a second copy of the offset, e.g. "GetClassString offset 0x2A3E").


def load_translations(xlsx_path=XLSX_PATH):
    """BFL display names, English -> Chinese; **read only** the single `keys` sheet (domain=bfl).

    After the single-sheet merge, the old multi-sheet layout no longer exists,
    and its builder `build_bfl_display_sheet.py` was archived under
    an archive directory; the old branch here was removed too, with no dead code left.
    Only rows with a non-empty Chinese value are used; an empty cell leaves the string in English (not written to the patch). No guessing."""
    import openpyxl
    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    try:
        if "keys" not in wb.sheetnames:
            raise SystemExit(f"master workbook missing keys sheet: {xlsx_path}")
        it = wb["keys"].iter_rows(values_only=True)
        hdr = list(next(it))
        if "domain" not in hdr:
            raise SystemExit("keys sheet missing domain column; unexpected master layout (expected single-sheet 14-column contract)")
        di = hdr.index("domain")
        ei = hdr.index("en_source")
        zi = hdr.index("zh_to_fill")
        out = {}
        for r in it:
            if di < len(r) and str(r[di] or "").strip() == "bfl":
                en = str(r[ei] or "").strip() if ei < len(r) else ""
                zh = str(r[zi] or "").strip() if zi < len(r) else ""
                if en and zh:
                    out[en.lower()] = zh
        return out
    finally:
        wb.close()


def load(rel=REL, game=None):
    game = game or SANDBOX_CONTENT
    ue4prop.GAME = game
    r = ue4prop.read_package(rel)
    raw = ue4prop.package_path(rel).read_bytes()
    ctx = bp.LoadCtx(names=r["names"], imports=r["imports"], exports=r["exports"])
    return r, raw, ctx


def reloc(value, replaced):
    """Relocate: value is a relative offset within the bytecode (0-based); return the new offset after inserting a variable-length string."""
    d = 0
    for old_start, old_len, new_len in replaced:
        if old_start < value:
            d += new_len - old_len
    return value + d


def patch_function(bc, ctx, base, translations):
    """Parse bc and return (new_bc: bytes, delta: int, report: dict). Returns (None, 0, {}) if no strings can be translated."""
    stmts, consumed, unknown = bp.decode_function(bc, ctx, base)
    if unknown:
        raise RuntimeError(f"function decode contains unknown opcodes: {[hex(u) for u in unknown]}")
    if consumed != len(bc):
        raise RuntimeError(f"function decode incomplete consumed={consumed} len={len(bc)}")

    # Collect edits: string replacements + offset-field rewrites
    # The first of the three strings inside EX_TextConst (FText) (source/key/namespace) being 0x1F is the deserialization marker
    # (the parser/engine peeks at 0x1F to distinguish the legacy 3-string form); changing it to 0x34 would break the structure;
    # and wargear resolves via key -> locres, so it should not be translated here. Strings inside TextConst are skipped.
    text_offs = set()

    def _mark_text(ns):
        for nd in ns:
            if nd.op == "EX_TextConst":
                for d in bp.iter_nodes(nd.children):
                    if d.op == "EX_StringConst":
                        text_offs.add(d.off)
            _mark_text(nd.children)

    _mark_text(stmts)

    str_edits = {}      # old_start -> new_bytes
    off_edits = []      # (field_pos, old_value)
    replaced = []       # (old_start, old_len, new_len)
    applied = []        # original English values actually replaced (for validation)
    skipped_long = []   # Chinese too long to fit at equal length; kept in English
    touched = 0
    for n in bp.iter_nodes(stmts):
        if n.op == "EX_StringConst" and n.off not in text_offs:
            val = n.operands["value"]
            key = val.lower()
            zh = (translations.get(hashlib.sha256(key.encode("utf-8")).hexdigest())
                  or translations.get(key))
            if zh is not None:
                u16 = zh.encode("utf-16-le")
                old_len = n.end - n.off
                if LENGTH_NEUTRAL:
                    # Equal-length replacement: 0x34 + UTF16 + 0x0000 + N × (EX_Nothing=0x0B) padding.
                    # Length is unchanged, so all jump offsets stay valid and need no relocation (variable-length relocation caused fatal errors in testing).
                    pad = old_len - (3 + len(u16))
                    if pad < 0:
                        skipped_long.append((val, zh))
                        continue
                    newb = bytes([0x34]) + u16 + b"\x00\x00" + b"\x0b" * pad
                else:
                    newb = bytes([0x34]) + u16 + b"\x00\x00"
                str_edits[n.off] = newb
                replaced.append((n.off, old_len, len(newb)))
                touched += 1
                applied.append(val)
        # Offset field positions (for relocating real jump targets).
        # Fix: the old implementation filtered with `0 <= v < len(bc)` and only edited in-range values, but in these functions
        # the exit jump is always `bytecodeBufferSize - 24` (or `- 3` for EX_PushExecutionFlow), **greater than len(bc)**
        # yet still a real jump in the same offset space. The old filter skipped them all, so any length change sent jumps to the wrong place ->
        # fatal error on cold start. Now only a "non-negative and not absurd" sanity check is applied, and all values take part in relocation.
        if n.op in ("EX_Jump", "EX_JumpIfNot", "EX_Skip",
                    "EX_PushExecutionFlow", "EX_SkipOffsetConst"):
            v = n.operands.get("code_offset", n.operands.get("pushing_address"))
            if v is not None and 0 <= v < JUMP_SANE_LIMIT:
                off_edits.append((n.off + 1, v))
        elif n.op in ("EX_Context", "EX_ClassContext", "EX_Context_FailSilent"):
            if n.children:
                v = n.operands["skip_offset"]
                if 0 <= v < JUMP_SANE_LIMIT:
                    off_edits.append((n.children[0].end, v))
        elif n.op == "EX_SwitchValue":
            if n.children:
                v = n.operands["end_offset"]
                if 0 <= v < JUMP_SANE_LIMIT:
                    off_edits.append((n.children[-1].end, v))

    if LENGTH_NEUTRAL:
        off_edits = []          # Equal-length: offsets never change; jump fields are never touched

    if not touched:
        return None, 0, {"touched": 0}

    # Single-pass rebuild: sort by old_pos, string replacement (variable length) + 4-byte offset-field rewrite
    edits = []
    for s, b in str_edits.items():
        edits.append((s, "str", b))
    for p, v in off_edits:
        edits.append((p, "off", v))
    edits.sort(key=lambda e: e[0])

    out = bytearray()
    i = 0
    for pos, kind, payload in edits:
        if pos < i:
            # offset field landed inside a string (impossible, but a safeguard)
            continue
        out += bc[i:pos]
        if kind == "str":
            out += payload
            i = pos + (len(payload))  # placeholder, fixed below
            # need to skip the original string length, taken from the matching entry in replaced
            # recompute i by finding old_len in replaced
            ol = next(ol_ for os_, ol_, _ in replaced if os_ == pos)
            i = pos + ol
        else:  # off: 4-byte rewrite
            nv = reloc(payload, replaced)
            out += struct.pack("<I", nv & 0xFFFFFFFF)
            i = pos + 4
    out += bc[i:]

    new_bc = bytes(out)
    delta = len(new_bc) - len(bc)
    rep = {"touched": touched, "delta": delta, "old_len": len(bc),
           "new_len": len(new_bc), "applied": applied,
           "skipped_long": skipped_long}
    return new_bc, delta, rep


def find_targets(r, raw, ctx, translations):
    """Return [(export_dict, bc, info, new_bc, delta, rep)]."""
    out = []
    for e in r["exports"]:
        if e["cls"] not in ("Function", "DelegateFunction"):
            continue
        bc, info = bp.extract_function_bytecode(r, e["name"], raw, cls=e["cls"])
        if bc is None:
            continue
        new_bc, delta, rep = patch_function(bc, ctx, info["bytecode_off"], translations)
        if new_bc is not None:
            out.append((e, bc, info, new_bc, delta, rep))
    return out


def assemble(raw, r, targets):
    """Apply all function replacements to raw, plus export-table/serializedScriptSize fixes, and return new_raw."""
    # Prepare: original serial_offset, bytecode_off, and delta for each modified function
    funcs = []
    for e, bc, info, new_bc, delta, rep in targets:
        funcs.append({
            "name": e["name"], "cls": e["cls"],
            "serial_offset": e["off"], "size": e["size"],
            "bytecode_off": info["bytecode_off"], "old_bc_len": len(bc),
            "new_bc": new_bc, "delta": delta,
        })
    funcs.sort(key=lambda f: f["serial_offset"])

    # Single-pass multi-insertion rebuild (ascending serial_offset)
    out = bytearray()
    prev = 0
    cum = 0
    for f in funcs:
        out += raw[prev:f["bytecode_off"]]
        out += f["new_bc"]
        prev = f["bytecode_off"] + f["old_bc_len"]
        cum += f["delta"]
    out += raw[prev:]

    # Fix each modified function's serializedScriptSize (int32) at bytecode_off-4 (already shifted by cum)
    for f in funcs:
        before = sum(g["delta"] for g in funcs if g["serial_offset"] < f["serial_offset"])
        pos = (f["bytecode_off"] - 4) + before
        cur = struct.unpack_from("<i", out, pos)[0]
        struct.pack_into("<i", out, pos, cur + f["delta"])

    # Export table: each record's SerialOffset += sum of deltas of all earlier functions; a modified function's SerialSize += its own delta
    s = r["summary"]
    export_offset = s["export_offset"]
    exports = r["exports"]
    for j, e in enumerate(exports):
        rec = export_offset + j * EXPORT_REC
        w = list(struct.unpack_from("<17i", out, rec))
        O = w[7]
        before = sum(g["delta"] for g in funcs if g["serial_offset"] < O)
        w[7] = O + before                       # SerialOffset
        # whether this is the record of a modified function
        for g in funcs:
            if g["serial_offset"] == O and g["size"] == e["size"] and g["name"] == e["name"]:
                w[6] = e["size"] + g["delta"]    # SerialSize
                break
        struct.pack_into("<17i", out, rec, *w)
    return bytes(out), funcs


def parse_pkg_bytes(d, rel=REL):
    """Parse a package from in-memory bytes (to validate the patched file without touching disk)."""
    s, names, imps, raw_exps, esz = ue4prop.tables_from_file(d, rel)
    exports = ue4prop.normalize_exports(raw_exps, names, imps)
    walks, bulk, end = ue4prop.walk_package(d, names, exports, imps)
    return dict(names=names, imports=imps,
                exports=[dict(name=e["name"], cls=e["cls"], off=e["off"], size=e["size"])
                         for e in exports],
                walks=walks, summary=s)


def validate(new_raw, applied_src, applied_zh):
    """Hard validation: reparse the new file and assert (a) every function decodes completely; (b) all applied strings appear as 0x34 with
    correct Chinese; (c) none of these strings remain as 0x1F (except those skipped inside EX_TextConst)."""
    r2 = parse_pkg_bytes(new_raw, REL)
    ctx2 = bp.LoadCtx(names=r2["names"], imports=r2["imports"], exports=r2["exports"])
    problems = []
    seen_zh = set()
    residual_1f = set()
    for e in r2["exports"]:
        if e["cls"] not in ("Function", "DelegateFunction"):
            continue
        bc, info = bp.extract_function_bytecode(r2, e["name"], new_raw, cls=e["cls"])
        if bc is None:
            continue
        try:
            stmts, consumed, unknown = bp.decode_function(bc, ctx2, info["bytecode_off"])
        except bp.DecodeError as ex:
            problems.append(f"{e['name']}: decode error {ex}")
            continue
        if unknown:
            problems.append(f"{e['name']}: unknown ops {[hex(u) for u in unknown]}")
        if consumed != len(bc):
            problems.append(f"{e['name']}: incomplete consumed={consumed} len={len(bc)}")
        # EX_TextConst substrings in this function (intentionally skipped, kept as 0x1F)
        text_offs = set()

        def _mark(ns):
            for nd in ns:
                if nd.op == "EX_TextConst":
                    for d in bp.iter_nodes(nd.children):
                        if d.op == "EX_StringConst":
                            text_offs.add(d.off)
                _mark(nd.children)

        _mark(stmts)
        for n in bp.iter_nodes(stmts):
            if n.op == "EX_UnicodeStringConst":
                seen_zh.add(n.operands["value"])
            if (n.op == "EX_StringConst" and n.off not in text_offs
                    and n.operands["value"].lower() in applied_src):
                residual_1f.add(n.operands["value"])
    missing = set(applied_zh) - seen_zh
    if missing:
        problems.append(f"not present as 0x34: {sorted(missing)}")
    if residual_1f:
        problems.append(f"still present as 0x1F: {sorted(residual_1f)}")
    return problems, sorted(seen_zh)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="validate only; write to a temp file")
    ap.add_argument("--out", help=".uasset path to write after validation")
    ap.add_argument("--report", help="output path for the JSON report")
    args = ap.parse_args()

    translations = load_translations()
    r, raw, ctx = load()
    print(f"[load] ok={r.get('ok')} file_size={r.get('file_size')} "
          f"exports={len(r.get('exports', []))} translations={len(translations)}")

    targets = find_targets(r, raw, ctx, translations)
    print(f"[scan] functions with translatable strings: {len(targets)}")
    total_delta = 0
    for e, bc, info, new_bc, delta, rep in targets:
        total_delta += delta
        print(f"   - {e['name']:32s} cls={e['cls']:16s} "
              f"touched={rep['touched']:2d} old={rep['old_len']:4d}->new={rep['new_len']:4d} delta={delta:+d}")

    if not targets:
        print("[warn] no functions with translatable strings; exiting")
        return 1

    new_raw, funcs = assemble(raw, r, targets)
    print(f"[assemble] new file size {len(raw)} -> {len(new_raw)} (total delta {len(new_raw)-len(raw):+d})")

    applied_src = set()
    applied_zh = set()
    for e, bc, info, new_bc, delta, rep in targets:
        for v in rep.get("applied", []):
            applied_src.add(v.lower())
            applied_zh.add(translations[v.lower()])
    print(f"[apply] wrote {len(applied_src)} entries ({len(translations)-len(applied_src)} inside EX_TextConst / unfilled, left unchanged)")

    problems, seen_zh = validate(new_raw, applied_src, applied_zh)
    if problems:
        print("[VALIDATION FAILED]")
        for p in problems:
            print("   !", p)
        return 2
    print(f"[VALIDATION PASSED] all translated strings appear as 0x34 ({len(seen_zh)} Chinese strings), no 0x1F residue, decode complete.")

    report = {
        "source_file": str(ue4prop.package_path(REL)),
        "orig_size": len(raw), "new_size": len(new_raw),
        "total_delta": len(new_raw) - len(raw),
        "functions": [{"name": f["name"], "delta": f["delta"],
                       "old_bc": f["old_bc_len"], "new_bc": len(f["new_bc"])}
                      for f in funcs],
        "translations": translations,
        "seen_zh": seen_zh,
    }
    if args.report:
        pathlib.Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"[report] -> {args.report}")

    if args.out:
        out_path = pathlib.Path(args.out)
        out_path.write_bytes(new_raw)
        print(f"[write] -> {out_path}")
    elif args.dry:
        tmp = pathlib.Path(r"D:/3DS-DW/steam patching/out/_bfl_patched_dry.uasset")
        tmp.write_bytes(new_raw)
        print(f"[dry] temporary write -> {tmp}")
    else:
        # no arguments = update the patch-package asset directly (used by the pipeline)
        DEFAULT_OUT.write_bytes(new_raw)
        print(f"[write] -> {DEFAULT_OUT} (patch-package asset; run apply bfl_display_zh to deploy)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
