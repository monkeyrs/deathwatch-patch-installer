# -*- coding: utf-8 -*-
"""Build-time export: freeze the single Chinese source zh_lexicon.xlsx into installer runtime data.

Usage (system Python + openpyxl, run once):
    python INSTALLER/tools/export_zh_runtime.py
    python INSTALLER/tools/export_zh_runtime.py --lexicon <xlsx> --out src/zh_runtime.json

Output `INSTALLER/src/zh_runtime.json`, split by domain:
    locres          -> [{ns, key, hash, zh}]
    flairname_asset -> {sha256(en): zh}
    asset_text      -> [{asset_file, key, zh, usage}]
    bfl             -> {sha256(en_lower): zh}

The Excel file remains the single editing source; after changing it, re-run this script and repackage.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
APP = HERE.parent / "src"
DEFAULT_LEXICON = (HERE.parents[2] / "427" / "Tools" / "zh_lex" / "out"
                   / "unified" / "zh_lexicon.xlsx")


def sha256_file(p: pathlib.Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def tkey(text: str) -> str:
    """Store only a hash key for the English source string; never ship the original text in the exe."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def norm_ns(v) -> str:
    s = "" if v is None else str(v).strip()
    return "" if s.lower() in ("(empty)", "(none)", "none", "null", "-") else s


def eligible(v) -> bool:
    s = "" if v is None else str(v).strip()
    return s not in ("", "-")


def export(lexicon: pathlib.Path) -> dict:
    from openpyxl import load_workbook

    wb = load_workbook(lexicon, read_only=True, data_only=True)
    try:
        ws = wb["keys"]
        it = ws.iter_rows(values_only=True)
        header = ["" if h is None else str(h).strip() for h in next(it)]
        need = ["domain", "key", "namespace", "source_hash_hex",
                "en_source", "zh_to_fill", "asset_file"]
        missing = [c for c in need if c not in header]
        if missing:
            raise SystemExit(f"[ERR] keys missing columns {missing}; header={header}")
        ix = {c: header.index(c) for c in header}

        locres, flair, asset_text, bfl = [], {}, [], {}
        for row in it:
            def get(name):
                i = ix.get(name)
                if i is None or i >= len(row):
                    return ""
                return "" if row[i] is None else row[i]

            dom = str(get("domain") or "").strip()
            zh = str(get("zh_to_fill") or "").strip()
            en = str(get("en_source") or "").strip()
            if dom == "locres":
                key = str(get("key") or "").strip()
                if not key or not eligible(zh):
                    continue
                raw = str(get("source_hash_hex") or "").strip()
                try:
                    hv = int(raw, 16)
                except ValueError:
                    continue
                locres.append({"ns": norm_ns(get("namespace")), "key": key,
                               "hash": hv, "zh": zh})
            elif dom == "flairname_asset":
                if en and eligible(zh):
                    flair[tkey(en)] = zh
            elif dom == "asset_text":
                key = str(get("key") or "").strip()
                asset = str(get("asset_file") or "").strip()
                if key and asset and eligible(zh):
                    asset_text.append({"asset_file": asset, "key": key,
                                        "zh": zh,
                                        "usage": str(get("usage") or "")})
            elif dom == "bfl":
                if en and eligible(zh):
                    bfl[tkey(en.lower())] = zh
    finally:
        wb.close()

    vmaj = (sys.version_info.major, sys.version_info.minor)
    try:
        source_label = lexicon.resolve().relative_to(HERE.parents[2]).as_posix()
    except ValueError:
        source_label = lexicon.name
    return {
        "meta": {
            "source": source_label,
            "source_sha256": sha256_file(lexicon),
            "sheet": "keys",
            "generated_utc": _dt.datetime.now(_dt.timezone.utc)
                               .strftime("%Y-%m-%dT%H:%M:%SZ"),
            "python": "%d.%d" % vmaj,
            "counts": {"locres": len(locres), "flairname": len(flair),
                       "asset_text": len(asset_text), "bfl": len(bfl)},
        },
        "locres": locres,
        "flairname": flair,
        "asset_text": asset_text,
        "bfl": bfl,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Export installer Chinese runtime data")
    ap.add_argument("--lexicon", default=str(DEFAULT_LEXICON))
    ap.add_argument("--out", default=str(APP / "zh_runtime.json"))
    a = ap.parse_args(argv)

    lex = pathlib.Path(a.lexicon)
    if not lex.is_file():
        raise SystemExit(f"[ERR] master lexicon not found: {lex}")
    data = export(lex)
    out = pathlib.Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    c = data["meta"]["counts"]
    print(f"[OK] {out}  locres={c['locres']} flairname={c['flairname']} "
          f"asset_text={c['asset_text']} bfl={c['bfl']}")
    for k in ("locres", "flairname", "asset_text", "bfl"):
        if c[k] <= 0:
            print(f"[ERR] domain {k} has 0 entries")
            return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
