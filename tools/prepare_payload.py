# -*- coding: utf-8 -*-
"""Build-time: stage the patch packages to ship with the exe into INSTALLER/src/payload/.

Copies only manifest.json + assets/ and generates payload_index.json (per-file sha256).
Usage:
    python INSTALLER/tools/prepare_payload.py
    python INSTALLER/tools/prepare_payload.py --patches <patch source dir> --out <payload dir>
A standalone repo does not assume a parent directory exists; use the DW_PATCHES environment
variable or --patches to specify the patch source.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import pathlib
import shutil
import sys

HERE = pathlib.Path(__file__).resolve().parent
APP = HERE.parent / "src"

_ap = argparse.ArgumentParser()
_ap.add_argument("--patches", default=os.environ.get(
    "DW_PATCHES", str(HERE.parents[1] / "patches")))
_ap.add_argument("--out", default="")
_args = _ap.parse_args()
PATCHES = pathlib.Path(_args.patches)
OUT = pathlib.Path(_args.out) if _args.out else APP / "payload"

REGISTRY = json.loads((APP / "features.json").read_text(encoding="utf-8"))


def _feature(fid: str) -> dict:
    return next(f for f in REGISTRY["features"] if f["id"] == fid)


BUGFIX = list(_feature("bugfix").get("packages") or [])
VISUAL = [v["id"] for v in (_feature("visual").get("variants") or [])]
# legacy g-series installer tiers (archived): keep their dst sha in the acceptable starting states,
# so users of older installers can switch tiers directly without a manual restore first.
VISUAL_LEGACY_ARCHIVE = HERE.parents[1] / "patches" / "archive" / "visual_enhance_g_variants_2026-11-19"
VISUAL_LEGACY = ["g9_glow_down30", "g11_bright_dim_glow",
                 "g12_bright_dim_glow_plus", "g13_bright_maxfx"]
BALANCE = list(_feature("balance").get("packages") or [])
# Full-file balance packages ship their uassets (no asset_edits recipe), e.g. granted weapon skills.
BALANCE_FULLFILE = {"weapon_skills_v1"}

# the installer payload no longer ships full uassets for these packages: only manifest.recipe,
# computed at runtime from the user's original assets by INSTALLER/src/zhgen/asset_recipes.py.
BUGFIX_RECIPE = {
    "bugfix_parry": "parry",
    "bugfix_wargear_material": "wargear_materials",
    "bugfix_chapter_icons": "chapter_icons",
    "bugfix_alacrity_ap": "alacrity_ap",
    "bugfix_alacrity_maxap": "maxap",
    "bugfix_praetorian_mitigation": "praetorian",
}
BALANCE_RECIPE = "asset_edits"


def sha256(p: pathlib.Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def scan_files(dst: pathlib.Path) -> list[dict]:
    files = []
    for f in sorted(dst.rglob("*")):
        if f.is_file():
            files.append({"rel": f.relative_to(dst).as_posix(),
                          "sha256": sha256(f), "size": f.stat().st_size})
    return files


def copy_pkg(src: pathlib.Path, dst: pathlib.Path, mutate_manifest=None) -> list[dict]:
    if not (src / "manifest.json").is_file():
        raise SystemExit(f"[ERR] 缺 manifest: {src}")
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    shutil.copy2(src / "manifest.json", dst / "manifest.json")
    if (src / "assets").is_dir():
        shutil.copytree(src / "assets", dst / "assets")
    drop: list[str] = []
    if mutate_manifest:
        drop = mutate_manifest(dst / "manifest.json") or []
    for rel in drop:
        p = dst / "assets" / pathlib.PurePosixPath(rel)
        if p.is_file():
            p.unlink()
    return scan_files(dst)


def apply_equal_length_transform(data: bytes, transform: dict) -> bytes:
    buf = bytearray(data)
    for op in transform.get("operations") or []:
        off = int(op["offset"], 16)
        before = bytes.fromhex(op["before_hex"])
        after = bytes.fromhex(op["after_hex"])
        if not before or len(before) != len(after):
            raise SystemExit(f"[ERR] transform 规则非等长: {op.get('label')}")
        if off < 0 or off + len(before) > len(buf) or bytes(buf[off:off + len(before)]) != before:
            raise SystemExit(f"[ERR] transform 前置字节不符: {op.get('label')}")
        buf[off:off + len(after)] = after
    return bytes(buf)


def bugfix_mutate(path: pathlib.Path) -> list[str]:
    """Convert transform entries into installer rules and drop the full assets."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    pkg = path.parent.name
    recipe = BUGFIX_RECIPE.get(pkg)
    if recipe:
        doc["recipe"] = recipe
    drop: list[str] = []
    for e in doc.get("entries") or []:
        rel = e.get("relative_path")
        if not rel:
            continue
        if recipe and rel.endswith(".uasset"):
            drop.append(rel)
            continue
        tr = e.get("transform")
        if not tr:
            continue
        asset = path.parent / "assets" / pathlib.PurePosixPath(rel)
        if asset.is_file():
            # source assets are already patched (dst); verify by reversing to the original, never treating dst as src.
            if e.get("dst_sha256") and sha256(asset) != e["dst_sha256"]:
                raise SystemExit(f"[ERR] 源包资产不是 dst_sha256: {rel}")
            rev = {
                "kind": "equal_length_bytes",
                "operations": [
                    {"offset": op["offset"],
                     "before_hex": op["after_hex"],
                     "after_hex": op["before_hex"]}
                    for op in tr.get("operations") or []
                ],
            }
            back = apply_equal_length_transform(asset.read_bytes(), rev)
            if e.get("src_sha256") and sha256_bytes(back) != e["src_sha256"]:
                raise SystemExit(f"[ERR] transform 反向结果与 src_sha256 不符: {rel}")
        drop.append(rel)
    if recipe or drop:
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    return drop


def balance_mutate(path: pathlib.Path) -> list[str]:
    """For balance packages keep only manifest.edits; full uassets stay out of the payload."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["recipe"] = BALANCE_RECIPE
    drop = [e["relative_path"] for e in (doc.get("entries") or [])
            if e.get("relative_path", "").endswith(".uasset")]
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    return drop


def visual_predecessors() -> dict[str, list[str]]:
    """Collect valid starting-state hashes for the visual DefaultEngine.ini: original + four tiers + legacy g tiers.

    This allows switching tiers directly in a directory that already has a tier (or a legacy g tier) applied:
    any target file matching this list counts as a replaceable starting state; it no longer has to be the Steam
    original. Applies to the visual payload only, not bugfix.
    """
    ok: dict[str, set[str]] = {}
    pkg_dirs = [(PATCHES / "visual_enhance", "tier_original")] + \
               [(PATCHES / "visual_enhance", vid) for vid in VISUAL] + \
               [(VISUAL_LEGACY_ARCHIVE, vid) for vid in VISUAL_LEGACY]
    for src_dir, vid in pkg_dirs:
        doc = json.loads((src_dir / vid / "manifest.json").read_text(encoding="utf-8"))
        for e in doc["entries"]:
            bucket = ok.setdefault(e["relative_path"], set())
            for h in (e.get("src_sha256"), e.get("dst_sha256")):
                if h:
                    bucket.add(h)
    return {k: sorted(v) for k, v in ok.items()}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    index = {"built_utc": _dt.datetime.now(_dt.timezone.utc)
                                  .strftime("%Y-%m-%dT%H:%M:%SZ"),
             "packages": []}
    total = 0
    for pid in BUGFIX:
        files = copy_pkg(PATCHES / pid, OUT / "bugfix" / pid,
                         mutate_manifest=bugfix_mutate)
        total += sum(x["size"] for x in files)
        index["packages"].append({"id": pid, "kind": "bugfix", "files": files})
        print(f"[OK] bugfix/{pid}: {len(files)} files")
    preds = visual_predecessors()
    vroot = OUT / "visual"
    if vroot.is_dir():
        for d in list(vroot.iterdir()):
            if d.is_dir() and d.name not in VISUAL:
                shutil.rmtree(d)

    def _visual_mut(path: pathlib.Path) -> list[str]:
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["recipe"] = "visual_ini"
        doc["tier"] = path.parent.name
        for e in doc["entries"]:
            e["acceptable_src_sha256"] = preds.get(e["relative_path"], [])
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        return [e["relative_path"] for e in doc["entries"]
                if e.get("relative_path", "").endswith(".ini")]

    for vid in VISUAL:
        files = copy_pkg(PATCHES / "visual_enhance" / vid, OUT / "visual" / vid,
                         mutate_manifest=_visual_mut)
        total += sum(x["size"] for x in files)
        index["packages"].append({"id": vid, "kind": "visual", "files": files})
        print(f"[OK] visual/{vid}: {len(files)} files")
    for pid in BALANCE:
        src = PATCHES / pid
        dst = OUT / "balance" / pid
        if pid in BALANCE_FULLFILE:
            # Full-file uassets are required here (variable-length edits); keep them intact.
            files = copy_pkg(src, dst)
        elif not (src / "manifest.json").is_file() and (dst / "manifest.json").is_file():
            # the source package may be archived or rebuilt in a parallel session; keep a valid existing payload to avoid a wrong-version swap.
            print(f"[WARN] balance/{pid}: 源包缺失，沿用现有 payload")
            drop = balance_mutate(dst / "manifest.json")
            for rel in drop:
                p = dst / "assets" / pathlib.PurePosixPath(rel)
                if p.is_file():
                    p.unlink()
            files = scan_files(dst)
        else:
            files = copy_pkg(src, dst, mutate_manifest=balance_mutate)
        total += sum(x["size"] for x in files)
        index["packages"].append({"id": pid, "kind": "balance", "files": files})
        print(f"[OK] balance/{pid}: {len(files)} files")
    # the legacy weapon_skills payload category is gone; drop any stale staged copy
    stale = OUT / "weapon_skills"
    if stale.is_dir():
        shutil.rmtree(stale)
    (OUT / "payload_index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[OK] payload total {total/1024/1024:.2f} MB -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
