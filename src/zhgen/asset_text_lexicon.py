# -*- coding: utf-8 -*-
"""asset_text_lexicon.py — Chinese text layer for FText embedded in assets (keys not present in locres).

Background (from in-game screenshots on 2026-11-13):
  Combat HUD unit names and tutorial banners are **FText embedded in assets**; their loc_key is not among
  the 2068 entries in en locres, so the zh_cn text, FlairName, and BFL chains cannot cover them.
  Evidence:
    * UNT_Hormagaunt.CharacterSheetComponent.CharacterName
      key=70B45F4443980C787BFE5AA55AF7B98E  source="Hormagaunt"
    * WI_OverwatchExplanation.TextBlock_890.Text
      key=659B067F417742FAEE13509FF3DCB4D1  source="CLICK WHERE YOU WANT TO AIM"
  Neither key exists in locres for any of the 6 cultures (adding keys was disproven in testing).

Approach (same variable-length transform as the FlairName package, but with multi-export support):
  Only the FText source FString is replaced (loc_key is unchanged; the engine falls back to the source string when a key is missing),
  three lengths must be kept in sync:
    ① FPropertyTag.Size（tag_off + 16）
    ② the owning export's SerialSize (export table + 0x18)
    ③ the SerialOffset of every later export (export table + 0x1C) += Δ
  and the "file length minus 4" field in the summary trailer. HeadersSize and the export table position stay unchanged
  (the insertion point is in the export data area after the header).

The sole source of Chinese edits is the `keys` sheet of the lexicon spreadsheet,
rows with domain=asset_text: key / en_source / zh_to_fill / usage / asset_file.

Subcommands:
  python "steam patching/tools/asset_text_lexicon.py" check
  python "steam patching/tools/asset_text_lexicon.py" build
  python "steam patching/tools/patch_sandbox.py" apply asset_text_zh
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import pathlib
import struct
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "cooked_reader"))
sys.path.insert(0, str(ROOT / "427" / "Tools" / "zh_lex"))
import ue4prop          # noqa: E402
import locres_v3        # noqa: E402
from uasset_mutate import parse_summary_pos  # noqa: E402

ORIG_CONTENT = (ROOT / "427/assets_drop/steam_original/extracted/steamapps/common" /
                "Warhammer 40,000 Deathwatch - Tyranid Invasion/deathwatchgame/Content")
LEXICON = ROOT / "427/Tools/zh_lex/out/unified/zh_lexicon.xlsx"
PATCH_DIR = ROOT / "steam patching/patches/asset_text_zh"
DOMAIN = "asset_text"
TAG = b"\xc1\x83\x2a\x9e"
EXPORT_REC = 68
EXP_SIZE_OFF = 24
EXP_OFF_OFF = 28


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# --------------------------------------------------------------------------- #
# Read the master spreadsheet
# --------------------------------------------------------------------------- #
def load_rows() -> list[dict]:
    from openpyxl import load_workbook
    wb = load_workbook(LEXICON, read_only=True, data_only=True)
    try:
        ws = wb["keys"]
        rows = list(ws.iter_rows(values_only=True))
    finally:
        wb.close()
    hdr = [str(h).strip() for h in rows[0]]
    ix = {h: i for i, h in enumerate(hdr)}
    need = ["domain", "key", "en_source", "zh_to_fill", "asset_file"]
    miss = [c for c in need if c not in ix]
    if miss:
        raise SystemExit(f"[ERR] 主表缺列 {miss}")
    out = []
    for r in rows[1:]:
        dv = str(r[ix["domain"]] or "").strip()
        if dv != DOMAIN:
            continue
        key = str(r[ix["key"]] or "").strip()
        if not key:
            continue
        out.append({
            "key": key,
            "en": str(r[ix["en_source"]] or ""),
            "zh": str(r[ix["zh_to_fill"]] or "").strip(),
            "asset_file": str(r[ix["asset_file"]] or "").strip().replace("\\", "/"),
            "usage": str(r[ix["usage"]] or "") if "usage" in ix else "",
        })
    return out


def validate(rows: list[dict]) -> list[dict]:
    errs, ok, seen = [], [], set()
    for x in rows:
        tag = f"{x['asset_file']} :: {x['key']}"
        if x["key"] in seen:
            errs.append(f"{tag}: key 重复")
            continue
        seen.add(x["key"])
        if not x["asset_file"]:
            errs.append(f"{tag}: asset_file 为空")
        if not x["en"]:
            errs.append(f"{tag}: en_source 为空")
        if not x["zh"]:
            errs.append(f"{tag}: zh_to_fill 为空（留空表示不生成，不写补丁）")
            continue
        if not any("\u4e00" <= ch <= "\u9fff" for ch in x["zh"]):
            errs.append(f"{tag}: 译文不含汉字 {x['zh']!r}")
            continue
        ok.append(x)
    return errs, ok


# --------------------------------------------------------------------------- #
# Locate FString / FText
# --------------------------------------------------------------------------- #
def parse_fstring(data: bytes, pos: int) -> tuple[str, int]:
    n = struct.unpack_from("<i", data, pos)[0]
    if n == 0:
        return "", pos + 4
    if n > 0:
        raw = data[pos + 4:pos + 4 + n]
        return raw.split(b"\x00", 1)[0].decode("utf-8", "replace"), pos + 4 + n
    cnt = -n
    raw = data[pos + 4:pos + 4 + 2 * cnt - 2]
    return raw.decode("utf-16-le", "replace"), pos + 4 + 2 * cnt


def pkg_rel(rel: str) -> str:
    return rel[:-7] if rel.lower().endswith(".uasset") else rel


def find_prop(rel: str, key: str) -> tuple[dict, dict, dict]:
    """Return (parsed_package, walk, prop). Requires a unique match; raises otherwise."""
    r = ue4prop.read_package(pkg_rel(rel))
    hits = []
    for w in r.get("walks") or []:
        for p in w.get("props") or []:
            if not isinstance(p, dict):
                continue
            v = p.get("value")
            if not isinstance(v, dict):
                continue
            if p.get("type") == "TextProperty" and v.get("loc_key") == key:
                hits.append((w, p))
    if len(hits) != 1:
        raise RuntimeError(f"{rel}: loc_key {key} 命中 {len(hits)} 处（要求 1）")
    w, p = hits[0]
    if p.get("value_off") is None or p.get("tag_off") is None or p.get("size") is None:
        raise RuntimeError(f"{rel}: FText tag 元数据不全")
    return r, w, p


def source_bounds(data: bytes, p: dict) -> dict:
    """Value layout: flags(4) source_flags(4) history(1) key FString src FString."""
    v = p.get("value") or {}
    if (v.get("flags"), v.get("source_flags"), v.get("history_type")) != (0, 0, 0):
        raise RuntimeError(f"FText 头不是 (0,0,0): {v}")
    vo = p["value_off"]
    key, key_end = parse_fstring(data, vo + 9)
    src, src_end = parse_fstring(data, key_end)
    return dict(ftext_off=vo, src_len_off=key_end, key=key,
                src=src, old_total=src_end - key_end, tag_size=p["size"])


# --------------------------------------------------------------------------- #
# Variable-length transform (multiple exports)
# --------------------------------------------------------------------------- #
def transform(data: bytes, r: dict, p: dict, key: str, en: str | None, zh: str):
    sb = source_bounds(data, p)
    if sb["key"] != key:
        raise RuntimeError(f"key 不符 {sb['key']} != {key}")
    if en is not None and sb["src"] != en:
        raise RuntimeError(f"源串不符 {sb['src']!r} != {en!r}")
    if data[-4:] != TAG:
        raise RuntimeError("文件尾缺少 PACKAGE_FILE_TAG")
    s = parse_summary_pos(data)
    ex = r["exports"]
    slo = sb["src_len_off"]
    hits = [i for i, e in enumerate(ex) if e["off"] <= slo < e["off"] + e["size"]]
    if len(hits) != 1:
        raise RuntimeError(f"source 偏移 {slo} 未唯一落在 export 内: {hits}")
    ei = hits[0]
    e = ex[ei]
    eo = r["summary"]["export_offset"]
    base = eo + ei * EXPORT_REC
    if struct.unpack_from("<i", data, base + EXP_SIZE_OFF)[0] != e["size"]:
        raise RuntimeError("export SerialSize 字段定位失败")
    if struct.unpack_from("<i", data, base + EXP_OFF_OFF)[0] != e["off"]:
        raise RuntimeError("export SerialOffset 字段定位失败")
    if struct.unpack_from("<i", data, p["tag_off"] + 16)[0] != sb["tag_size"]:
        raise RuntimeError("FPropertyTag.Size 字段定位失败")
    if struct.unpack_from("<i", data, s["pos"]["headers_size"])[0] != ex[0]["off"]:
        raise RuntimeError("HeadersSize != 首 export SerialOffset")

    new_fs = locres_v3.write_fstring(zh)
    delta = len(new_fs) - sb["old_total"]
    out = bytearray(data[:slo] + new_fs + data[slo + sb["old_total"]:])
    struct.pack_into("<i", out, p["tag_off"] + 16, sb["tag_size"] + delta)
    struct.pack_into("<i", out, base + EXP_SIZE_OFF, e["size"] + delta)
    for j, e2 in enumerate(ex):
        if e2["off"] > slo:
            struct.pack_into("<i", out, eo + j * EXPORT_REC + EXP_OFF_OFF, e2["off"] + delta)
    if s.get("tail1_pos"):
        struct.pack_into("<i", out, s["tail1_pos"], len(out) - 4)
    if len(out) != len(data) + delta:
        raise RuntimeError("重建长度不符")
    info = dict(delta=delta, old_fs=sb["old_total"], new_fs=len(new_fs),
                old_size=len(data), new_size=len(out), export_index=ei,
                export_name=e["name"], old_export_size=e["size"],
                new_export_size=e["size"] + delta, src_off=slo)
    return bytes(out), info, s, ei


def verify(dst: bytes, rel: str, key: str, zh: str, orig: bytes, r0: dict, info: dict) -> dict:
    with tempfile.TemporaryDirectory(prefix="asset_text_verify_") as td:
        p = pathlib.Path(td) / "Content" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(dst)
        saved = ue4prop.GAME
        try:
            ue4prop.GAME = pathlib.Path(td) / "Content"
            r2 = ue4prop.read_package(pkg_rel(rel))
        finally:
            ue4prop.GAME = saved
    if r2.get("file_size") != len(dst):
        raise RuntimeError(f"{rel}: 回读 file_size 不符")
    p2 = None
    for w in r2.get("walks") or []:
        for q in w.get("props") or []:
            vq = q.get("value") if isinstance(q, dict) else None
            if isinstance(vq, dict) and vq.get("loc_key") == key:
                p2 = q
    if p2 is None or not isinstance(p2.get("value"), dict) or p2["value"].get("source") != zh:
        raise RuntimeError(f"{rel}: 回读 source != {zh!r}")

    s2 = parse_summary_pos(dst)
    if dst[-4:] != TAG:
        raise RuntimeError(f"{rel}: 文件尾 tag 丢失")
    if struct.unpack_from("<i", dst, s2["tail1_pos"])[0] != len(dst) - 4:
        raise RuntimeError(f"{rel}: 尾块文件长字段未同步")
    ex0, ex2 = r0["exports"], r2["exports"]
    if len(ex0) != len(ex2):
        raise RuntimeError(f"{rel}: export 数变化")
    eo = r0["summary"]["export_offset"]
    d = info["delta"]
    for i, (a, b) in enumerate(zip(ex0, ex2)):
        want_off = a["off"] + (d if a["off"] > info["src_off"] else 0)
        want_sz = a["size"] + (d if i == info["export_index"] else 0)
        if b["off"] != want_off or b["size"] != want_sz:
            raise RuntimeError(f"{rel}: export[{i}] 偏移/大小不符 {b} vs ({want_off},{want_sz})")
        if struct.unpack_from("<i", dst, eo + i * EXPORT_REC + EXP_OFF_OFF)[0] != want_off:
            raise RuntimeError(f"{rel}: export[{i}] SerialOffset 字段不符")
        if struct.unpack_from("<i", dst, eo + i * EXPORT_REC + EXP_SIZE_OFF)[0] != want_sz:
            raise RuntimeError(f"{rel}: export[{i}] SerialSize 字段不符")
        # Unchanged exports: data is byte-for-byte identical
        if i != info["export_index"]:
            a0 = orig[a["off"]:a["off"] + a["size"]]
            b0 = dst[b["off"]:b["off"] + b["size"]]
            if a0 != b0:
                raise RuntimeError(f"{rel}: export[{i}] 数据被改动")
    return dict(ok=True, file_size=len(dst), export_index=info["export_index"],
                export_name=info["export_name"], source=zh)


# --------------------------------------------------------------------------- #
# Subcommands
# --------------------------------------------------------------------------- #
def cmd_check(_a) -> int:
    rows = load_rows()
    errs, ok = validate(rows)
    for e in errs:
        print("[REFUSE]", e)
    if errs:
        return 3
    ue4prop.GAME = ORIG_CONTENT
    print(f"asset_text 行 {len(rows)}，可生成 {len(ok)}")
    for x in ok:
        rel = x["asset_file"]
        try:
            r, w, p = find_prop(rel, x["key"])
            d = (ORIG_CONTENT / rel).read_bytes()
            sb = source_bounds(d, p)
            new = locres_v3.write_fstring(x["zh"])
            print(f"  OK  {rel:<58} {sb['src']!r:>28} -> {x['zh']!r}  Δ={len(new)-sb['old_total']:+d}")
        except Exception as exc:
            print(f"  ERR {rel}: {exc}")
            return 4
    return 0


def cmd_build(_a) -> int:
    rows = load_rows()
    errs, ok = validate(rows)
    if errs:
        for e in errs:
            print("[REFUSE]", e)
        return 3
    if not ok:
        raise SystemExit("[REFUSE] 没有可生成的 asset_text 行")
    ue4prop.GAME = ORIG_CONTENT
    ASSET_OUT = PATCH_DIR / "assets" / "deathwatchgame" / "Content"
    if ASSET_OUT.exists():
        for f in ASSET_OUT.rglob("*.uasset"):
            f.unlink()
    ASSET_OUT.mkdir(parents=True, exist_ok=True)

    entries, table = [], []
    for x in ok:
        rel = x["asset_file"]
        src = ORIG_CONTENT / rel
        data = src.read_bytes()
        r, w, p = find_prop(rel, x["key"])
        dst, info, _s, _ei = transform(data, r, p, x["key"], x["en"], x["zh"])
        v = verify(dst, rel, x["key"], x["zh"], data, r, info)
        out = ASSET_OUT / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(dst)
        entries.append({
            "relative_path": "deathwatchgame/Content/" + rel,
            "src_sha256": sha(data),
            "dst_sha256": sha(dst),
            "size": len(dst),
            "differs_from_steam": True,
            "key": x["key"],
            "en_source": x["en"],
            "zh": x["zh"],
            "delta_bytes": info["delta"],
        })
        table.append(dict(asset=rel, en=x["en"], zh=x["zh"], delta=info["delta"],
                          old_fs=info["old_fs"], new_fs=info["new_fs"], usage=x["usage"]))
        print(f"  BUILD {rel:<58} {x['en']!r} -> {x['zh']!r}  Δ={info['delta']:+d}  "
              f"export[{info['export_index']}] {info['export_name']}  L0 PASS")

    total_delta = sum(t["delta"] for t in table)
    manifest = {
        "name": "asset_text_zh",
        "description": "资产内嵌 FText 中文包（locres 未收录 loc_key）：14 个 UNT_* 单位/场景物显示名 + WI_OverwatchExplanation 瞄准提示。由 zh_lexicon.xlsx（domain=asset_text）驱动，变长替换源串、loc_key 不变。",
        "grading": "L0 PASS：15 个生成物均经 ue4prop 严格回读（source==主表译文 / export 偏移与 SerialSize 同步 / 未改动 export 数据逐字节一致 / 尾块文件长字段同步）。L2/L3 待实机复验（目标屏为战斗 HUD，需可复现战斗场景）。",
        "installable": True,
        "source": {
            "lexicon": "427/Tools/zh_lex/out/unified/zh_lexicon.xlsx",
            "sheet": "keys",
            "domain": DOMAIN,
            "entries": len(entries),
        },
        "method": {
            "fstring": "locres_v3.write_fstring(zh) → UTF-16LE，负长度 |n| 含结尾 NUL",
            "variable_length": "替换 FText.source；FPropertyTag.Size += Δ；所属 export SerialSize += Δ；其后 export SerialOffset += Δ；summary 尾块文件长-4 += Δ。",
            "why_keyless": "Both loc keys are absent from the locres files for all six cultures, so the engine falls back to the source string. Only source strings are edited; adding keys was verified ineffective.",
        },
        "l2_l3": {
            "verdict": "PENDING",
            "note": "目标文本出现在战斗 HUD 与教程浮条，需可复现的战斗场景才能实机验收；静态 L0 已过。",
        },
        "entries": sorted(entries, key=lambda e: e["relative_path"]),
        "skipped": [],
    }
    PATCH_DIR.mkdir(parents=True, exist_ok=True)
    (PATCH_DIR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")

    lines = [
        "# asset_text_zh（资产内嵌 FText · locres 未收录键）",
        "",
        "## 由来",
        "",
        "战斗 HUD 左上单位名与教程浮条是**资产内嵌 FText**，其 loc_key 不在 en locres 的 2068 条里，"
        "因此 `zh_cn` / `flairname_zh_full` / `bfl_display_zh` 三条链都覆盖不到。",
        "Adding locres keys was verified ineffective; this package keeps the variable-length source-string replacement route",
        "（loc_key 保持不变，引擎查不到 key 时回退 source）。",
        "",
        "## 怎么改中文",
        "",
        "1. 打开 `427/Tools/zh_lex/out/unified/zh_lexicon.xlsx`，`keys` sheet；",
        "2. 找 `domain=asset_text` 的行，只改 **F 列 `zh_to_fill`**；",
        "3. 推荐一条命令全量同步（UI / FlairName / asset_text / BFL + 可选配音）：",
        "   `python 427/Tools/zh_lex/zh_unify.py --sync-all --apply`；",
        "4. 只重建本包：`python \"steam patching/tools/asset_text_lexicon.py\" check` → `build`；",
        "5. 投放：`python \"steam patching/tools/patch_sandbox.py\" apply asset_text_zh`。",
        "",
        "## 变长规则（多 export）",
        "",
        "插入点在 header 之后的 export 数据区，因此 export 表位置不变；需要同步：",
        "① `FPropertyTag.Size`（tag_off+16）；② 所属 export 的 `SerialSize`（+0x18）；",
        "③ 其后所有 export 的 `SerialOffset`（+0x1C）+= Δ；④ summary 尾块「文件长-4」。",
        "只改 ② 不改 ① 会让引擎按旧 size 跳过属性值 → 属性流错位。",
        "",
        "## 本包统计",
        "",
        f"- 条目 **{len(entries)}** 个 / 字节变化合计 Δ = {total_delta:+d}",
        "",
        "| 资产 | 英文 | 中文 | Δ 字节 | FString |",
        "|---|---|---|---:|---|",
    ]
    for t in sorted(table, key=lambda t: t["asset"]):
        lines.append(f"| `{t['asset'].split('/')[-1]}` | {t['en']} | {t['zh']} | "
                     f"{t['delta']:+d} | {t['old_fs']}B → {t['new_fs']}B |")
    lines += ["", "## 验收入口", "",
              "- L0：`build` 已对 15 个生成物逐个严格回读；",
              "- L2/L3：待可复现战斗场景后实机确认「HORMAGAUNT → 刀虫」「CLICK WHERE… → 点击你想瞄准的位置」。",
              ""]
    (PATCH_DIR / "notes.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\nBUILD OK  条目 {len(entries)}  Δ合计 {total_delta:+d}  → {PATCH_DIR}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="asset_text 中文层（locres 未收录 FText）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="只读体检").set_defaults(func=cmd_check)
    sub.add_parser("build", help="生成补丁包（不碰沙盒/原版）").set_defaults(func=cmd_build)
    a = ap.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
