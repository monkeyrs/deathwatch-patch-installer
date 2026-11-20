# -*- coding: utf-8 -*-
"""Installer self-test (clean sandbox only): apply all payloads in the real install order, then
restore and verify byte-for-byte against the Steam original.

Usage:
    python "steam patching/INSTALLER/tools/selftest_installer.py" --root "<install root>"

Install root = a directory containing deathwatchgame/Content (this repo uses
`steam patching/sandbox_installer` by default). The order matches APPLY_ORDER in src/main.py:
7 bugfix packages -> zh_text -> default visual tier -> camera. The internal regression sandbox
helper is not used.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
APP = HERE.parent / "src"
ROOT = HERE.parents[2]
BASE_GAME = (ROOT / "427/assets_drop/steam_original/extracted/steamapps/common"
             "/Warhammer 40,000 Deathwatch - Tyranid Invasion/deathwatchgame")
LOC_REL = "deathwatchgame/Content/Localization/Game/en/Game.locres"
for p in (str(APP), str(APP / "zhgen"), str(APP / "saveinject"),
          str(ROOT / "427/Tools/zh_lex")):
    if p not in sys.path:
        sys.path.insert(0, p)

import patcher                      # noqa: E402
import locres_v3                    # noqa: E402
from zhgen import zh_runtime        # noqa: E402

FAILS: list[str] = []


def sha(p: pathlib.Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def expect(label: str, path: pathlib.Path, want: str) -> None:
    if not path.is_file():
        FAILS.append(f"{label}: 缺少 {path}")
        return
    got = sha(path)
    if got != want:
        FAILS.append(f"{label}: {got[:12]} != {want[:12]}")
    else:
        print(f"[OK] {label}")


def loc_value(path: pathlib.Path, prefix: str) -> str:
    for ns, key, _h, val in locres_v3.parse_any(path).entries:
        if key.startswith(prefix):
            return val
    raise KeyError(prefix)


def feat(fid: str) -> dict:
    reg = json.loads((APP / "features.json").read_text(encoding="utf-8"))
    return next(f for f in reg["features"] if f["id"] == fid)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--variant", default="tier_dgpu",
                    choices=["tier_igpu", "tier_dgpu", "tier_max"])
    ap.add_argument("--no-restore", action="store_true")
    a = ap.parse_args()
    root = pathlib.Path(a.root).resolve()
    content = root / "deathwatchgame" / "Content"
    if not content.is_dir():
        print(f"[ERR] 不是安装根（缺 deathwatchgame/Content）: {root}")
        return 2
    payload = APP / "payload"
    cam = json.loads((APP / "camera_rules.json").read_text(encoding="utf-8"))
    bug = feat("bugfix")["packages"]
    pre_loc = sha(BASE_GAME / "Content/Localization/Game/en/Game.locres")

    # clean-state preconditions
    expect("pre locres == Steam 原版", root / LOC_REL, pre_loc)
    expect("pre exe", root / cam["targets"][0]["relative_path"], cam["targets"][0]["src_sha256"])
    expect("pre BP_DropgamePlayerPawn", root / cam["targets"][1]["relative_path"],
           cam["targets"][1]["src_sha256"])
    print(f"[..] root={root}")

    session = patcher.new_session(root)
    print(f"[..] backup={session.dir}")
    try:
        # 1) 7 bugfix packages (English layer; crippling edits the en locres)
        for pkg in bug:
            r = patcher.apply_manifest_package(root, payload / "bugfix" / pkg, session,
                                               log=lambda s: print("   ", s))
            print(f"[{'OK' if r['ok'] else 'FAIL'}] {pkg}: {r['detail']}")
            if not r["ok"]:
                FAILS.append(f"{pkg}: {r['errors']}")
            if pkg == "bugfix_crippling_shot_desc":
                en = loc_value(root / LOC_REL, "5AC6786D")
                print(f"   en 描述 = {en!r}")
                if "2%" not in en or "1%" in en:
                    FAILS.append(f"英文层未修复: {en!r}")
        # 2) Chinese chain (overlay layer, overwrites with Chinese; hash anchors unchanged)
        data = zh_runtime.load_data(APP / "zh_runtime.json")
        for r in zh_runtime.apply_zh_text(root, data, session.dir, log=lambda s: print("   ", s)):
            ok = r["ok"] and not r["errors"]
            print(f"[{'OK' if ok else 'FAIL'}] zh/{r['chain']}: {r['detail']}")
            if not ok:
                FAILS.append(f"zh/{r['chain']}: {r['errors']}")
        zh_val = loc_value(root / LOC_REL, "5AC6786D")
        print(f"   zh 描述 = {zh_val!r}")
        if "2%" not in zh_val:
            FAILS.append(f"汉化层未显示 2%: {zh_val!r}")
        # 3) visual
        r = patcher.apply_manifest_package(root, payload / "visual" / a.variant, session,
                                           log=lambda s: print("   ", s))
        print(f"[{'OK' if r['ok'] else 'FAIL'}] visual/{a.variant}: {r['detail']}")
        if not r["ok"]:
            FAILS.append(f"visual: {r['errors']}")
        # 4) camera
        r = patcher.apply_camera_patch(root, APP / "camera_rules.json", session,
                                       log=lambda s: print("   ", s))
        print(f"[{'OK' if r['ok'] else 'FAIL'}] camera: {r['detail']}")
        if not r["ok"]:
            FAILS.append(f"camera: {r['errors']}")
    finally:
        session.finalize()

    # byte-for-byte presence (crippling's locres is overwritten by zh; accept via already_fixed)
    for pkg in bug:
        m = json.loads((payload / "bugfix" / pkg / "manifest.json").read_text(encoding="utf-8"))
        for e in m["entries"]:
            p = root / e["relative_path"]
            accept = [e["dst_sha256"]] + (e.get("already_fixed_sha256") or [])
            if not p.is_file() or sha(p) not in accept:
                FAILS.append(f"{pkg} · {e['relative_path'].split('/')[-1]}: 不在位")
            else:
                print(f"[OK] {pkg} · {e['relative_path'].split('/')[-1]}")
    vm = json.loads((payload / "visual" / a.variant / "manifest.json").read_text(encoding="utf-8"))
    for e in vm["entries"]:
        expect(f"visual · {e['relative_path'].split('/')[-1]}",
               root / e["relative_path"], e["dst_sha256"])
    for t in cam["targets"]:
        expect(f"camera · {t['relative_path'].split('/')[-1]}", root / t["relative_path"],
               t["dst_sha256"])
    # WDI must stay at the Steam original (the crippling package must not modify it)
    crippling = json.loads((payload / "bugfix" / "bugfix_crippling_shot_desc" / "manifest.json")
                           .read_text(encoding="utf-8"))
    if any(e["relative_path"].endswith("WDI_CripplingShot.uasset")
           for e in crippling["entries"]):
        FAILS.append("bugfix_crippling_shot_desc 不应包含 WDI 资产")

    tmp = list(root.rglob("*.dwtmp"))
    if tmp:
        FAILS.append(f"残留临时文件 {len(tmp)} 个")

    if a.no_restore:
        print("SKIP restore")
    else:
        sess = patcher.latest_session(root)
        res = patcher.restore_all(root)
        print(f"[{'OK' if res['ok'] else 'FAIL'}] restore: restored={res['restored']} "
              f"deleted={res['deleted']} errors={res['errors'][:3]}")
        if not res["ok"]:
            FAILS.append(f"restore: {res['errors'][:3]}")
        if sess is None:
            FAILS.append("restore: 没有可核对的 session.json")
            doc = {"files": []}
        else:
            doc = json.loads((sess / "session.json").read_text(encoding="utf-8"))
        miss = 0
        for e in doc["files"]:
            p = root / e["relative_path"]
            if not p.is_file() or sha(p) != e["sha256"]:
                miss += 1
        if miss:
            FAILS.append(f"还原后有 {miss} 个文件未回到原 hash")
        else:
            print(f"[OK] restore 逐字节复核 {len(doc['files'])} 个文件")
        # byte-for-byte comparison with the Steam original (stronger than session.json: catches a backup overwritten by a later layer)
        # if the sandbox started with non-original content (e.g. a visual tier already applied), compare only files that started original;
        # pre-seeded files are covered by the session start-hash check above.
        miss_base = 0
        preexisting = 0
        for e in doc["files"]:
            b = BASE_GAME / e["relative_path"].split("deathwatchgame/", 1)[-1]
            if not b.is_file():
                continue
            base_sha = sha(b)
            if e["sha256"] != base_sha:
                preexisting += 1
                continue
            if sha(root / e["relative_path"]) != base_sha:
                miss_base += 1
                print(f"   ! 与原版不同: {e['relative_path']}")
        if preexisting:
            print(f"[OK] {preexisting} 个文件起始即非原版（预置内容），已按 session 起始态复核")
        if miss_base:
            FAILS.append(f"还原后 {miss_base} 个文件与 Steam 原版不一致")
        elif preexisting:
            print("[OK] restore 与 Steam 原版一致（预置文件除外）")
        else:
            print("[OK] restore 与 Steam 原版逐字节一致")
        expect("post-restore exe", root / cam["targets"][0]["relative_path"],
               cam["targets"][0]["src_sha256"])

    print("verdict=%s" % ("PASS" if not FAILS else "FAIL"))
    for f in FAILS:
        print("  !", f)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
