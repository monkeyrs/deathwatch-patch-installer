# -*- coding: utf-8 -*-
"""PySide6 installer offscreen smoke test (no dialogs, no writes to the real game directory).

Run: INSTALLER/.venv/Scripts/python.exe INSTALLER/tests/smoke_test.py
Covers: registry contract / exe derivation / four-page gating / theme and mutual exclusion /
        default selection / full-card highlight / Chinese runtime data and four chain entries /
        Bug details dialog / backup-restore round trip / Black Shield save preflight /
        no gap in combos / camera notice text / ownership notice version rendering /
        DIY dialog essentials / bilingual (zh/en) UI.
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QCheckBox, QRadioButton  # noqa: E402
import main as m  # noqa: E402
import patcher  # noqa: E402
from zhgen import zh_runtime  # noqa: E402
from i18n import S, primary_lang_id  # noqa: E402

PASS = []


def check(name, cond):
    assert cond, "FAIL: " + name
    PASS.append(name)


app = QApplication([])

# 0) path safety: zh_runtime rejects absolute paths, drive letters, and ..
try:
    zh_runtime._safe_rel("../evil.uasset")
    _bad_rel = False
except Exception:
    _bad_rel = True
check("zh: reject ../ asset paths", _bad_rel)
_rels_bad = zh_runtime.target_rels(Path("."), {"asset_text": [
    {"asset_file": "../evil.uasset"}, {"asset_file": "C:/evil.uasset"}]})
check("zh: target_rels filters invalid asset_file", all("evil" not in r for r in _rels_bad))
_um = zh_runtime.ue4prop.UM
zh_runtime.ue4prop.UM = Path("__missing_umodel__.exe")
check("ue4prop: UModel missing returns None silently",
      zh_runtime.ue4prop.umodel_list("x") is None)
zh_runtime.ue4prop.UM = _um
# 1) language detection mapping
check("langid zh-CN(0x0804)→zh", primary_lang_id(0x0804) == "zh")
check("langid en-US(0x0409)→en", primary_lang_id(0x0409) == "en")

# 2) registry integrity
ids = [f["id"] for f in m.FEATURES]
check("steam_ini not registered", "bonus_steam_ini" not in ids)
_vo = next(f for f in m.FEATURES if f["id"] == "vo_zh")
check("vo_zh is DIY-only: no payload / not selectable / not distributed",
      _vo["packages"] == [] and _vo["enabled"] is False
      and _vo.get("diy_prompt") is True and _vo["default_checked"] is False)
_zh = next(f for f in m.FEATURES if f["id"] == "zh_text")
check("zh_text uses runtime generation mode",
      _zh.get("locres_mode") == "generate_at_install"
      and _zh.get("runtime") == "zh_text" and _zh["packages"] == [])
_bug = next(f for f in m.FEATURES if f["id"] == "bugfix")
check("bugfix enabled at launch with 8 implemented packages", _bug["enabled"] is True
      and len(_bug["packages"]) == 8 and "bugfix_lod" not in _bug["packages"]
      and "bugfix_if_heavybolter_emissive" in _bug["packages"])
check("bugfix list zh/en resources are bundled",
      all((m.APP_DIR / p).is_file() for p in _bug["detail_doc"].values()))
check("bugfix checked by default", _bug["default_checked"] is True)
_cam = next(f for f in m.FEATURES if f["id"] == "camera")
check("camera enabled but unchecked by default, warns when selected",
      _cam["enabled"] is True and _cam["default_checked"] is False
      and _cam.get("warning_modal") is True)
check("camera is a core feature (enters install plan)",
      any(f["id"] == "camera" for f in m.CORE_FEATURES))
vis = next(f for f in m.FEATURES if f["id"] == "visual")
check("visual default tier = discrete (tier_dgpu)", vis["default_variant"] == "tier_dgpu"
      and vis["detail_doc"]["zh"] == "visual_list_zh.md"
      and (m.APP_DIR / "visual_list_zh.md").is_file()
      and (m.APP_DIR / "visual_list_en.md").is_file())
check("visual three tiers: ids and labels (integrated/discrete/max)",
      [v["id"] for v in vis["variants"]] == ["tier_igpu", "tier_dgpu", "tier_max"]
      and [v["short"]["zh"] for v in vis["variants"]] == ["核显", "独显", "最高"])
check("bonus flag present", next(f for f in m.FEATURES if f["id"] == "bonus_blackshield")["bonus"] is True)
check("packaging spec datas cover all detail docs (prevents missing manifest in exe, 2026-11-19)",
      all(str((m.APP_DIR / p).name) in
          (Path(__file__).resolve().parent.parent / "DeathwatchPatchInstaller.spec").read_text(encoding="utf-8")
          for f in m.FEATURES for p in (f.get("detail_doc") or {}).values()))
check("Black Shield has save preflight flag",
      next(f for f in m.FEATURES if f["id"] == "bonus_blackshield").get("save_check") == "dw_metagame.save")
check("CORE/BONUS partition is complete",
      len(m.CORE_FEATURES) + len(m.BONUS) == len(m.FEATURES)
      and not ({f["id"] for f in m.CORE_FEATURES} & {f["id"] for f in m.BONUS}))
check("Black Shield card name has no Bonus prefix",
      next(f for f in m.FEATURES if f["id"] == "bonus_blackshield")["name"]["zh"] == "黑盾入队")
check("Bonus appears once across UI (section title only)",
      sum(v["zh"].count("Bonus") for v in S.values()) == 1
      and sum(v["en"].count("Bonus") for v in S.values()) == 1
      and all("Bonus" not in f["name"]["zh"] + f["desc"]["zh"] for f in m.FEATURES)
      and all("Bonus" not in f["name"]["en"] + f["desc"]["en"] for f in m.FEATURES))

# 3) exe derivation (pure function)
tmp = Path(os.environ.get("TEMP", ".")) / "dw_installer_smoke_root"
(tmp / "deathwatchgame" / "Content").mkdir(parents=True, exist_ok=True)
(tmp / "deathwatchgame" / "Binaries" / "Win32").mkdir(parents=True, exist_ok=True)
check("root exe derived correctly", m.derive_root(str(tmp / "DeathwatchGame.exe")) == tmp)
check("Win32 exe derived correctly",
      m.derive_root(str(tmp / "deathwatchgame" / "Binaries" / "Win32" / "DeathwatchGame-Win32-Shipping.exe")) == tmp)
check("unrelated exe rejected", m.derive_root(r"C:\Windows\notepad.exe") is None)
check("missing Content rejected", m.derive_root(r"C:\Game\DeathwatchGame.exe") is None)

# 4) Chinese runtime data and generation-chain entries
data = zh_runtime.load_data(m.APP_DIR / "zh_runtime.json")
check("zh_runtime.json has all four domains and none empty",
      all(len(data[k]) > 0 for k in ("locres", "flairname", "asset_text", "bfl")))
check("locres rows contain only ns/key/hash/zh (no English source strings)",
      all(set(("ns", "key", "hash", "zh")) <= set(r) and "en" not in r
          for r in data["locres"][:50]))
check("zh data carries no en source strings (locres/asset_text/flairname/bfl)",
      all("en" not in r for r in data["asset_text"])
      and all(len(k) == 64 and all(c in "0123456789abcdef" for c in k)
              for k in list(data["flairname"])[:10])
      and all(len(k) == 64 and all(c in "0123456789abcdef" for c in k)
              for k in list(data["bfl"])[:10]))
check("four chain functions are callable",
      all(callable(getattr(zh_runtime, n)) for n in
          ("apply_locres", "apply_flairname", "apply_asset_text", "apply_bfl")))

# 5) backup / manifest-package apply / restore round trip (all in a temp directory)
work = Path(tempfile.mkdtemp(prefix="dw_smoke_engine_"))
try:
    rel = "deathwatchgame/Content/test/asset.bin"
    target = work / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"ORIGINAL")
    pkg = work / "pkg"
    (pkg / "assets" / rel).parent.mkdir(parents=True, exist_ok=True)
    (pkg / "assets" / rel).write_bytes(b"PATCHED!")
    (pkg / "manifest.json").write_text(json.dumps({
        "entries": [{"relative_path": rel,
                     "src_sha256": patcher.sha256_bytes(b"ORIGINAL"),
                     "dst_sha256": patcher.sha256_bytes(b"PATCHED!")}]
    }), encoding="utf-8")
    sess = patcher.new_session(work, "smoke")
    r = patcher.apply_manifest_package(work, pkg, sess, log=lambda *_: None)
    check("manifest package applied and dst hash verified", r["ok"] and target.read_bytes() == b"PATCHED!")
    sess.finalize()
    check("backup session contains original and session.json",
          (sess.dir / "files" / rel).read_bytes() == b"ORIGINAL"
          and (sess.dir / "session.json").is_file())
    rr = patcher.restore_session(work, sess.dir)
    check("restore original from backup", rr["ok"] and target.read_bytes() == b"ORIGINAL")
    # files created by the session are removed on restore
    rel2 = "deathwatchgame/Content/test/new.bin"
    pkg2 = work / "pkg2"
    (pkg2 / "assets" / rel2).parent.mkdir(parents=True, exist_ok=True)
    (pkg2 / "assets" / rel2).write_bytes(b"NEW")
    (pkg2 / "manifest.json").write_text(json.dumps({
        "entries": [{"relative_path": rel2,
                     "dst_sha256": patcher.sha256_bytes(b"NEW")}]
    }), encoding="utf-8")
    s2 = patcher.new_session(work, "smoke2")
    patcher.apply_manifest_package(work, pkg2, s2, log=lambda *_: None)
    s2.finalize()
    patcher.restore_session(work, s2.dir)
    check("restore removes files created by the session", not (work / rel2).exists())
    check("Black Shield preflight: no save -> exists=False",
          patcher.blackshield_preflight(str(work / "nope.save"))["exists"] is False)
    fake = work / "fake.save"
    fake.write_bytes(b"not a save")
    pre = patcher.blackshield_preflight(str(fake))
    check("Black Shield preflight: bad save -> no exception and parse_ok=False",
          pre["exists"] is True and pre["parse_ok"] is False and pre["reason"])
    # camera patch: equal-length byte transform / idempotent skip / refusal to force a wrong-version change
    cam_rel = "deathwatchgame/Binaries/Win32/fake.exe"
    cam_p = work / cam_rel
    cam_p.parent.mkdir(parents=True, exist_ok=True)
    src_b, dst_b = b"\x00\x00\xf0\x41", b"\x00\x00\xb2\x42"
    orig_cam = b"HEAD" + src_b + b"TAIL"
    want_cam = b"HEAD" + dst_b + b"TAIL"
    cam_p.write_bytes(orig_cam)
    cam_rules = work / "camera_rules.json"
    cam_rules.write_text(json.dumps({"targets": [{
        "relative_path": cam_rel,
        "src_sha256": patcher.sha256_bytes(orig_cam),
        "dst_sha256": patcher.sha256_bytes(want_cam),
        "rules": [{"label": "maxpitch", "offset": "0x4",
                   "before_hex": src_b.hex(), "after_hex": dst_b.hex()}],
    }]}), encoding="utf-8")
    cs = patcher.new_session(work, "camsmoke")
    rc = patcher.apply_camera_patch(work, cam_rules, cs, log=lambda *_: None)
    check("camera patch: equal-length transform succeeds", rc["ok"] and rc["applied"] == 1
          and cam_p.read_bytes() == want_cam)
    rc2 = patcher.apply_camera_patch(work, cam_rules, cs, log=lambda *_: None)
    check("camera patch: repeated run is idempotent skip",
          rc2["ok"] and rc2["skipped"] == 1 and rc2["applied"] == 0)
    cam_p.write_bytes(b"HEAD" + b"\x11\x22\x33\x44" + b"TAIL")
    rc3 = patcher.apply_camera_patch(work, cam_rules, cs, log=lambda *_: None)
    check("camera patch: version mismatch refuses forced change",
          not rc3["ok"]
          and cam_p.read_bytes() == b"HEAD" + b"\x11\x22\x33\x44" + b"TAIL")
    cs.finalize()
finally:
    shutil.rmtree(work, ignore_errors=True)

# 5b) P0 data safety: global backup index / empty session / save-only does not shadow / validate save before writing
p0 = Path(tempfile.mkdtemp(prefix="dw_smoke_p0_"))
try:
    rel0 = "deathwatchgame/Content/test/p0.bin"
    target0 = p0 / rel0
    target0.parent.mkdir(parents=True, exist_ok=True)
    target0.write_bytes(b"ORIGINAL")
    pkg0 = p0 / "pkg0"
    (pkg0 / "assets" / rel0).parent.mkdir(parents=True, exist_ok=True)
    (pkg0 / "assets" / rel0).write_bytes(b"PATCHED!")
    (pkg0 / "manifest.json").write_text(json.dumps({"entries": [{
        "relative_path": rel0,
        "src_sha256": patcher.sha256_bytes(b"ORIGINAL"),
        "dst_sha256": patcher.sha256_bytes(b"PATCHED!")}]}), encoding="utf-8")

    a1 = patcher.new_session(p0, "20260101T000000Z")
    patcher.apply_manifest_package(p0, pkg0, a1, log=lambda *_: None)
    a1.finalize()
    a2 = patcher.new_session(p0, "20260102T000000Z")
    patcher.apply_manifest_package(p0, pkg0, a2, log=lambda *_: None)
    p2 = a2.finalize()
    check("P0: reinstall with no changes creates no empty session",
          p2 is None and not a2.dir.exists())

    # an old Black Shield session with only a save backup and no files must not shadow the install backup
    a3 = patcher.new_session(p0, "20260103T000000Z")
    (a3.dir / "save").mkdir(parents=True, exist_ok=True)
    (a3.dir / "save" / "dw_metagame.save").write_bytes(b"SAVE")
    a3.finalize()
    rr0 = patcher.restore_all(p0)
    check("P0: save-only session does not shadow install backup, restores earliest original",
          rr0["ok"] and rr0["restored"] == 1
          and rr0["deleted"] == 0 and target0.read_bytes() == b"ORIGINAL")

    # P0-1 pre-restore before reapply: revert to the original when current differs from the earliest backup, then apply
    target0.write_bytes(b"PATCHED!")
    pre0 = patcher.pre_restore_selected(p0, [rel0], log=lambda *_: None)
    check("P0: pre-restore selected items to earliest original before reapply",
          pre0["ok"] and pre0["restored"] == 1
          and target0.read_bytes() == b"ORIGINAL")
    patcher.apply_manifest_package(p0, pkg0, patcher.new_session(p0, "20260105T000000Z"),
                                   log=lambda *_: None)

    # P0-4 equal-length transform: patch without a full asset in the payload; repeated runs are idempotent
    rel_t = "deathwatchgame/Content/test/transform.bin"
    target_t = p0 / rel_t
    target_t.parent.mkdir(parents=True, exist_ok=True)
    target_t.write_bytes(b"ABC")
    pkg_t = p0 / "pkg_transform"
    pkg_t.mkdir()
    (pkg_t / "manifest.json").write_text(json.dumps({"entries": [{
        "relative_path": rel_t,
        "src_sha256": patcher.sha256_bytes(b"ABC"),
        "dst_sha256": patcher.sha256_bytes(b"AZC"),
        "transform": {"kind": "equal_length_bytes", "operations": [{
            "label": "B_to_Z", "offset": "0x1",
            "before_hex": "42", "after_hex": "5a"}]}}]}), encoding="utf-8")
    st = patcher.new_session(p0, "20260106T000000Z")
    rt1 = patcher.apply_manifest_package(p0, pkg_t, st, log=lambda *_: None)
    rt2 = patcher.apply_manifest_package(p0, pkg_t, st, log=lambda *_: None)
    check("P0-4: equal-length transform applies and repeated runs are idempotent",
          rt1["ok"] and rt1["applied"] == 1
          and rt2["ok"] and rt2["applied"] == 0 and rt2["skipped"] == 1
          and target_t.read_bytes() == b"AZC")
    st.finalize()
    patcher.restore_all(p0)
    check("P0-4: transform file is restorable",
          target_t.read_bytes() == b"ABC")

    # recipe mode: payload has only a manifest; compute from the original at runtime
    rel_r = "deathwatchgame/Content/test/recipe.bin"
    target_r = p0 / rel_r
    target_r.parent.mkdir(parents=True, exist_ok=True)
    target_r.write_bytes(b"ABC" + b"B\x00\x00\x00")
    pkg_r = p0 / "pkg_recipe"
    pkg_r.mkdir()
    (pkg_r / "manifest.json").write_text(json.dumps({
        "recipe": "asset_edits",
        "entries": [{
            "relative_path": rel_r,
            "src_sha256": patcher.sha256_bytes(b"ABC" + b"B\x00\x00\x00"),
            "dst_sha256": patcher.sha256_bytes(b"ABC" + b"Z\x00\x00\x00"),
            "edits": [{"slot": 3, "old": 66, "new": 90, "off": 3, "kind": "base"}],
        }],
    }), encoding="utf-8")
    sr = patcher.new_session(p0, "20260107T000000Z")
    rr = patcher.apply_manifest_package(p0, pkg_r, sr, log=lambda *_: None)
    sr.finalize()
    check("recipe: no uasset payload, computed from original via edits",
          rr["ok"] and rr["applied"] == 1
          and target_r.read_bytes() == b"ABC" + b"Z\x00\x00\x00")
    patcher.restore_all(p0)
    check("recipe: restorable", target_r.read_bytes() == b"ABC" + b"B\x00\x00\x00")

    # r14 crippling-shot display string 2% -> 8% (equal-length UTF-16LE, en/zh aware, same approach as 35% -> 99%)
    rel_c = patcher.CRIPPLING_DESC_REL
    loc_c = p0 / rel_c
    loc_c.parent.mkdir(parents=True, exist_ok=True)
    _zh2 = "每次射击，有 2% 几率：-1 AP 于目标".encode("utf-16-le")
    _zh8 = "每次射击，有 8% 几率：-1 AP 于目标".encode("utf-16-le")
    _zh1 = "每次射击，有 1% 几率：-1 AP 于目标".encode("utf-16-le")
    loc_c.write_bytes(b"HDR" + _zh2 + _zh2 + _zh2 + _zh1 + b"TAIL")
    sr_c = patcher.new_session(p0, "20260108T000000Z")
    rc = patcher.apply_balance_crippling_text(p0, sr_c, log=lambda *_: None)
    sr_c.finalize()
    _dc = loc_c.read_bytes()
    check("balance crippling: zh 2%->8% in three places (equal length), 1% unchanged",
          rc["ok"] and rc["applied"] == 3 and _dc.count(_zh8) == 3
          and _dc.count(_zh2) == 0 and _dc.count(_zh1) == 1 and len(_dc) == len(loc_c.read_bytes()))
    check("balance crippling: rerun idempotently skips redundant edits",
          patcher.apply_balance_crippling_text(p0, sr_c, log=lambda *_: None)["skipped"] == 1)
    loc_c.write_bytes(b"HDR" + "2% Chance to reduce target's AP by 1 per shot".encode("utf-16-le") + b"TAIL")
    sr_c2 = patcher.new_session(p0, "20260109T000000Z")
    re_c = patcher.apply_balance_crippling_text(p0, sr_c2, log=lambda *_: None)
    sr_c2.finalize()
    _en8 = "8% Chance to reduce target's AP by 1 per shot".encode("utf-16-le")
    _en2 = "2% Chance to reduce target's AP by 1 per shot".encode("utf-16-le")
    check("balance crippling: en 2%->8% equal length",
          re_c["ok"] and re_c["applied"] == 1
          and _en8 in loc_c.read_bytes() and _en2 not in loc_c.read_bytes())
    patcher.restore_all(p0)

    # write a temporary copy of the save and validate it; bad data must never overwrite the original
    save0 = p0 / "dw_metagame.save"
    save0.write_bytes(b"SAVE-ORIGINAL")
    sess0 = patcher.new_session(p0, "20260104T000000Z")
    _orig_pre, _orig_si = patcher.blackshield_preflight, patcher._save_inject

    class _FakeSGP:
        @staticmethod
        def parse_props(b):
            if b.startswith(b"BROKEN"):
                raise ValueError("broken save")

    class _FakeM:
        SGP = _FakeSGP
        content_path = None

        @classmethod
        def use_content(cls, path):
            cls.content_path = path

        @staticmethod
        def apply_ops(b, ops):
            return b"BROKEN-SAVE", []

    patcher.blackshield_preflight = lambda *a, **k: {
        "exists": True, "parse_ok": True, "path": str(save0)}
    patcher._save_inject = lambda: _FakeM
    try:
        bad = patcher.inject_blackshield(sess0, log=lambda *_: None)
        check("P0: original save not overwritten when parse fails",
              not bad["ok"] and save0.read_bytes() == b"SAVE-ORIGINAL")

        class _FakeGood(_FakeM):
            @staticmethod
            def apply_ops(b, ops):
                return b"GOOD-SAVE", []

        patcher._save_inject = lambda: _FakeGood
        good = patcher.inject_blackshield(sess0, log=lambda *_: None)
        check("P0: save written to temp copy and replaced only after validation",
              good["ok"] and save0.read_bytes() == b"GOOD-SAVE"
              and Path(good["backup"]).read_bytes() == b"SAVE-ORIGINAL"
              and not (p0 / "dw_metagame.save.dwnew").exists())
        check("P0: save_inject.CONTENT points to user game Content",
              Path(_FakeGood.content_path) == p0 / "deathwatchgame" / "Content")
    finally:
        patcher.blackshield_preflight, patcher._save_inject = _orig_pre, _orig_si
finally:
    shutil.rmtree(p0, ignore_errors=True)

# 6) four-step flow + left-side task navigation
w = m.InstallerWindow(lang="zh")
w.resize(980, 720)
w.show()
app.processEvents()
check("four pages (four steps)", len(w.pages) == 4)
check("four left-side task steps present",
      w.rail.width() == 220
      and w.rail.labels == ["协议", "安装", "特性", "完成"])
check("initial step = 0", w.current() == 0)
check("current step synced in left navigation", w.rail.current == 0 and w.rail.accent.name() == m.BLUE.lower())
check("title bar keeps brand name", "Deathwatch" in w.windowTitle())
check("title bar shows current app version", "v1.4" in w.windowTitle())
check("README badge uses current app version",
      "download-v1.4" in (m.APP_DIR.parent / "README.md").read_text(encoding="utf-8"))
check("language radios in bottom nav (below page stack)",
      w.lang_zh.mapTo(w, QPoint(0, 0)).y() > w.stack.geometry().bottom()
      and not hasattr(w, "lang_combo"))

# 7) disclaimer gate + updated notice
body_zh = w.pages[0].body.text()
check("new disclaimer mentions unofficial / genuine copy / no DRM bypass",
      "非官方" in body_zh and "合法购买的正版" in body_zh and "不绕过 DRM" in body_zh)
check("disclaimer renders version from registry",
      str(m.REGISTRY["game_appid"]) in body_zh
      and str(m.REGISTRY["game_buildid"]) in body_zh)
check("disclaimer and author text no longer mention donations",
      "打赏" not in body_zh and not any("打赏" in v["zh"] for v in S.values())
      and not any("打赏" in v["en"] for v in S.values()))
check("cannot proceed before accepting disclaimer", not w.next_btn.isEnabled())
w.pages[0].accept.click()
check("can proceed after accepting", w.next_btn.isEnabled())
w.next_page()
check("navigates to install page (path + mode)", w.current() == 1)

# 8) install-page gating: path and mode are both required; full-card highlight + green/blue mutual exclusion
setup = w.pages[1]
check("empty path blocks Next", not w.next_btn.isEnabled())
QTest.mouseClick(setup.install_card, Qt.MouseButton.LeftButton)
check("mode alone still blocks Next (missing path)", not w.next_btn.isEnabled())
setup.path_edit.setText(str(tmp / "DeathwatchGame.exe"))
check("path + mode together enable Next", w.next_btn.isEnabled())
check("patch mode = green accent", w.mode == "install" and w.accent == m.GREEN)
check("mode card fully highlighted (green background)", setup.install_card.isChecked()
      and setup.install_card.backgroundColor.name() == m.GREEN.lower())
check("mode card has no visible radio dot", not setup.install_card.findChildren(QRadioButton))
QTest.mouseClick(setup.restore_card, Qt.MouseButton.LeftButton)
check("restore = blue theme (mutually exclusive in group)", w.mode == "restore" and w.accent == m.BLUE)
check("modes are mutually exclusive", setup.restore_card.isChecked() and not setup.install_card.isChecked())
check("restore card fully highlighted (blue background)", setup.restore_card.backgroundColor.name() == m.BLUE.lower())
bs0 = w.pages[3].cards["bonus_blackshield"]
check("P0: restore mode disables Black Shield on completion page",
      not bs0._enabled and "还原模式" in w.pages[3].section.picker.hint.text())
QTest.mouseClick(setup.install_card, Qt.MouseButton.LeftButton)
check("switch back to patch = green", w.mode == "install" and w.accent == m.GREEN)
check("P0: patch mode re-enables Black Shield on completion page",
      bs0._enabled and "门外那位还在等回复" in w.pages[3].section.picker.hint.text())
w.next_page()
check("navigates to features page", w.current() == 2)
# Restore is all-or-nothing: the grid is masked on the features page and unmasked on install.
w.set_mode("restore")
check("restore masks the feature grid with the all-or-nothing notice",
      w.pages[2].restore_overlay.isVisible()
      and "还原全部改动" in w.pages[2].restore_overlay._text
      and "原版" in w.pages[2].restore_overlay._text)
w.set_mode("install")
check("install mode unmasks the feature grid",
      not w.pages[2].restore_overlay.isVisible())

# 9) features page: uniform card grid + card interactions
fp = w.pages[2]
check("features page uses FlowLayout", isinstance(fp.flow, m.FlowLayout))
check("feature cards 2~3 per row: equal width at 980px and at least 300",
      len({c.width() for c in fp.cards.values()}) == 1
      and fp.cards["zh_text"].width() >= 300)
check("feature cards have uniform height (all aligned to balance card style)",
      len({c.height() for c in fp.cards.values()}) == 1
      and fp.cards["balance"].height() >= 60)
check("zh: localization checked by default", fp.cards["zh_text"].isChecked())
check("zh: visual checked by default", fp.cards["visual"].isChecked())
check("checked by default = fully highlighted (green background)",
      fp.cards["zh_text"].backgroundColor.name() == m.GREEN.lower())
check("feature cards have no visible checkbox", not fp.cards["zh_text"].findChildren(QCheckBox))
app.processEvents()
check("feature grid uses plain FlowLayout (no row equal-height stretch)",
      type(fp.flow) is m.FlowLayout)
check("two Chinese cards are on the first row",
      fp.cards["zh_text"].y() == fp.cards["vo_zh"].y()
      and fp.cards["zh_text"].x() < fp.cards["vo_zh"].x()
      and fp.cards["visual"].y() > fp.cards["zh_text"].y())
check("bugfix enabled and checked by default / camera enabled but unchecked",
      fp.cards["bugfix"]._enabled and fp.cards["bugfix"].isChecked()
      and fp.cards["camera"]._enabled and not fp.cards["camera"].isChecked())
check("bugfix card name = Bug Fixes, no longer lists 3 items",
      fp.cards["bugfix"].title.text() == "Bug 修正"
      and fp.cards["bugfix"].desc.text().count("·") == 0)
check("bugfix card has Bug details link",
      fp.cards["bugfix"].detail_link is not None
      and "Bug 详情" in fp.cards["bugfix"].detail_link.text())
check("visual card has Tier details link",
      fp.cards["visual"].detail_link is not None
      and "参数说明" in fp.cards["visual"].detail_link.text())
check("balance card has Numeric details link",
      fp.cards["balance"].detail_link is not None
      and "数值说明" in fp.cards["balance"].detail_link.text())
check("camera card has Caution link",
      fp.cards["camera"].detail_link is not None
      and "谨慎" in fp.cards["camera"].detail_link.text())
check("no standalone weapon skills card (merged into balance)",
      "weapon_skills" not in fp.cards)
check("other cards have no detail links",
      all(c.detail_link is None for fid, c in fp.cards.items()
          if fid not in ("bugfix", "visual", "balance", "camera", "vo_zh")))

# Detail links open their dialog without toggling the parent tile (regression: visual notice did not open).
_orig_dialogs = (m.BugDetailDialog, m.CameraWarningDialog, m.VoDiyDialog)
class _CountedDialog:
    opened = 0
    def __init__(self, *args, **kwargs):
        pass
    def exec(self):
        type(self).opened += 1
class _FakeBugDialog(_CountedDialog): pass
class _FakeCameraDialog(_CountedDialog): pass
class _FakeDiyDialog(_CountedDialog): pass
m.BugDetailDialog = _FakeBugDialog
m.CameraWarningDialog = _FakeCameraDialog
m.VoDiyDialog = _FakeDiyDialog
try:
    for _fid in ("bugfix", "visual", "balance", "camera", "vo_zh"):
        _card = fp.cards[_fid]
        _before = _card.isChecked()
        QTest.mouseClick(_card.detail_link, Qt.MouseButton.LeftButton)
        check(f"{_fid} detail link does not toggle card",
              _card.isChecked() == _before)
    check("all detail links open their dialogs",
          _FakeBugDialog.opened == 3 and _FakeCameraDialog.opened == 1
          and _FakeDiyDialog.opened == 1)
finally:
    m.BugDetailDialog, m.CameraWarningDialog, m.VoDiyDialog = _orig_dialogs

QTest.mouseClick(fp.cards["bugfix"], Qt.MouseButton.LeftButton)
check("bugfix can be unchecked (card unhighlighted)",
      not fp.cards["bugfix"].isChecked()
      and fp.cards["bugfix"].backgroundColor.name() != m.GREEN.lower())
QTest.mouseClick(fp.cards["bugfix"], Qt.MouseButton.LeftButton)
check("bugfix can be rechecked (card highlighted)",
      fp.cards["bugfix"].isChecked()
      and fp.cards["bugfix"].backgroundColor.name() == m.GREEN.lower())
dlg = m.BugDetailDialog(w)
body = dlg.view.toPlainText()
check("Bug details dialog shows full Chinese list",
      "Bug 修正说明" in body and "招架" in body and "致残射击" in body)
check("Bug details dialog title/buttons",
      dlg.windowTitle() == "Bug 修正清单" and dlg.yesButton.text() == "关闭")
dlg.close()
dlg_bal = m.BugDetailDialog(w, "balance")
body_bal = dlg_bal.view.toPlainText()
check("balance numeric dialog shows tested wording",
      "实机验证" in body_bal and "冷板凳增强" in body_bal
      and "1AP 可用变体" in body_bal and "动力剑" in body_bal
      and "爱好者" not in body_bal and "非官方" not in body_bal
      and "未实机验证" not in body_bal
      and "重型爆弹枪" not in body_bal and "通用口径" not in body_bal)
check("balance numeric dialog has no duplicate entries",
      body_bal.count("1AP 可用变体") == 1 and body_bal.count("导弹发射器") == 1
      # Crippling Shot appears once, as the 2%->8% balance bullet.
      and body_bal.count("致残射击") == 1)
check("balance numeric dialog title",
      dlg_bal.windowTitle() == "武器平衡性补丁 · 数值说明")
check("balance numeric dialog includes crippling shot 2%->8%",
      "致残射击" in body_bal and "2%" in body_bal and "8%" in body_bal)
check("balance dialog stays plain text without embedded images",
      "<img" not in body_bal and "<p" not in body_bal
      and "docs/balance-viz/index.html" in body_bal)
dlg_bal.close()

# 9b) visual: three-way segmented choice (integrated / discrete / max) + parameter dialog on card click
check("visual tiers = modern segmented three-way choice (no dropdown/no radio)",
      len(fp.variant_buttons) == 3
      and not hasattr(fp.cards["visual"], "variant_combo")
      and not fp.cards["visual"].findChildren(QRadioButton)
      and all(b.isVisible() for b in fp.variant_buttons))
check("visual tier labels contain no internal codenames (tier_/g9/g11/g12/g13)",
      all(("tier_" not in b._text.lower() and "g9" not in b._text.lower()
           and "g11" not in b._text.lower() and "g12" not in b._text.lower()
           and "g13" not in b._text.lower())
          for b in fp.variant_buttons))
check("default tier = discrete",
      fp.cards["visual"].variant_choice.current_key() == "tier_dgpu"
      and "独显" in fp.cards["visual"].current_variant_label("zh"))
QTest.mouseClick(fp.variant_buttons[0], Qt.MouseButton.LeftButton)
check("segmented button switches whole tier without unchecking card",
      fp.cards["visual"].variant_choice.current_key() == "tier_igpu"
      and fp.cards["visual"].isChecked())
QTest.mouseClick(fp.variant_buttons[2], Qt.MouseButton.LeftButton)
check("third tier = max is selectable",
      fp.cards["visual"].variant_choice.current_key() == "tier_max")
QTest.mouseClick(fp.variant_buttons[0], Qt.MouseButton.LeftButton)
check("first tier = integrated is selectable",
      fp.cards["visual"].variant_choice.current_key() == "tier_igpu")
QTest.mouseClick(fp.variant_buttons[1], Qt.MouseButton.LeftButton)
check("switch back to default discrete tier",
      fp.cards["visual"].variant_choice.current_key() == "tier_dgpu")
check("visual tiers shown when checked", not fp.cards["visual"].variant_row.isHidden())
check("card has no extra Tier heading",
      not any("档位" in lbl.text() for lbl in fp.cards["visual"].findChildren(m.BodyLabel)))
# clicking the card only toggles selection; details come from the title-row link, and the dialog does not change selection
_before_vis = fp.cards["visual"].isChecked()
QTest.mouseClick(fp.cards["visual"], Qt.MouseButton.LeftButton)
check("clicking visual card only toggles selection, no auto dialog",
      fp.cards["visual"].isChecked() != _before_vis)
QTest.mouseClick(fp.cards["visual"], Qt.MouseButton.LeftButton)
check("second click restores visual card selection", fp.cards["visual"].isChecked() == _before_vis)
_before_vis = fp.cards["visual"].isChecked()
_old_bug_dlg = m.BugDetailDialog
class _FakeBugDlg:
    opened = 0
    def __init__(self, *a, **k):
        pass
    def exec(self):
        _FakeBugDlg.opened += 1
m.BugDetailDialog = _FakeBugDlg
fp.cards["visual"].detail_link.linkActivated.emit("bug")
check("visual detail link opens dialog without changing selection",
      _FakeBugDlg.opened == 1 and fp.cards["visual"].isChecked() == _before_vis)
m.BugDetailDialog = _old_bug_dlg
dlg_vis = m.BugDetailDialog(w, "visual")
_vis_body = dlg_vis.view.toPlainText()
check("visual params dialog: three tiers + base shared items + vanilla via restore (no unchanged params/internal keys)",
      "【底座】" in _vis_body and "核显" in _vis_body and "独显" in _vis_body
      and "最高" in _vis_body and "原版" in _vis_body
      and "帧率上限 144 帧" in _vis_body and "资源加载时限 10 / 30" in _vis_body
      and "已实机验证" in _vis_body
      and "尚未实机验证" not in _vis_body
      and "OneFrameThreadLag" not in _vis_body
      and "AsyncLoadingTimeLimit=" not in _vis_body
      and "r.VSync=" not in _vis_body)
check("dialog section headings use brackets and no markdown residue",
      _vis_body.count("【") >= 5 and "## " not in _vis_body)
check("visual params dialog title/buttons",
      dlg_vis.windowTitle() == "光影增强 · 档位参数" and dlg_vis.yesButton.text() == "关闭")
dlg_vis.close()
_dlg_close = m.BugDetailDialog(w, "visual")
_dlg_close.show()
app.processEvents()
_dlg_close.yesButton.click()
app.processEvents()
check("dialog bottom-right close button works", not _dlg_close.isVisible())
_dlg_close.close()

# 9c) camera card: click only toggles selection; the "caution" link opens a red-text dialog without changing selection
CAMERA_VERBATIM = "【谨慎】摄像机可能撞墙。当你找不到人物时：按住R（3 秒钟），然后再按 Tab 就能恢复。"
check("camera card no longer shows warning inline (now a dialog)", fp.cards["camera"].warn.isHidden())
check("camera card is clickable (pointing hand cursor)",
      fp.cards["camera"].cursor().shape() == Qt.CursorShape.PointingHandCursor)
_old_cam = m.CameraWarningDialog
class _FakeCamDlg:
    opened = 0
    def __init__(self, *a, **k):
        pass
    def exec(self):
        _FakeCamDlg.opened += 1
m.CameraWarningDialog = _FakeCamDlg
try:
    QTest.mouseClick(fp.cards["camera"], Qt.MouseButton.LeftButton)
    check("clicking camera card only selects, no auto warning",
          fp.cards["camera"].isChecked() and _FakeCamDlg.opened == 0
          and fp.cards["camera"].backgroundColor.name() == m.GREEN.lower())
    QTest.mouseClick(fp.cards["camera"], Qt.MouseButton.LeftButton)
    check("second click unchecks, still no warning",
          not fp.cards["camera"].isChecked() and _FakeCamDlg.opened == 0)
    _before_cam = fp.cards["camera"].isChecked()
    fp.cards["camera"].detail_link.linkActivated.emit("x")
    check("Camera caution link opens dialog without changing selection",
          _FakeCamDlg.opened == 1 and fp.cards["camera"].isChecked() == _before_cam)
finally:
    m.CameraWarningDialog = _old_cam
dlg_cam = fp.build_camera_warning()
check("Camera warning body matches required verbatim text (Chinese)", dlg_cam.body.text() == CAMERA_VERBATIM)
check("Camera warning title and buttons",
      dlg_cam.windowTitle() == "镜头限制解除 · 谨慎"
      and dlg_cam.yesButton.text() == "关闭")
dlg_cam.close()
# clicking the small "caution" link opens the same CameraWarningDialog (verbatim body text)
_seen_cam = []
_orig_cw = m.CameraWarningDialog.exec
m.CameraWarningDialog.exec = lambda self: _seen_cam.append(self.body.text())
try:
    fp.cards["camera"]._open_detail()
finally:
    m.CameraWarningDialog.exec = _orig_cw
check("camera caution link opens warning with required verbatim body",
      _seen_cam == [CAMERA_VERBATIM])

# 9d) Black Shield offer moved to the completion page; author card only there
check("no Bonus card on features page", "bonus_blackshield" not in fp.cards)
check("completion page has Black Shield offer card", "bonus_blackshield" in w.pages[3].cards)
check("no author card on features page", not hasattr(fp, "author"))
check("features page has scroll container", bool(fp.findChildren(m.SingleDirectionScrollArea)))

# 10) vo_zh DIY card: greyed out, not selectable, with a DIY badge
vo = fp.cards["vo_zh"]
check("vo_zh card not selectable and never in selected",
      not vo._enabled and "vo_zh" not in fp.selected())
check("vo_zh card shows DIY badge", not vo.badge.isHidden() and vo.badge.text() == "DIY")
check("vo_zh detail link opens prompt independently",
      vo.detail_link is not None and "提示词" in vo.detail_link.text())
_old_diy = m.VoDiyDialog
class _FakeDiy:
    opened = 0
    def __init__(self, *a, **k):
        pass
    def exec(self):
        _FakeDiy.opened += 1
m.VoDiyDialog = _FakeDiy
try:
    vo.detail_link.linkActivated.emit("x")
    check("vo_zh detail link opens dialog without changing selection",
          _FakeDiy.opened == 1 and not vo.isChecked())
finally:
    m.VoDiyDialog = _old_diy

# 10b) balance card: unchecked by default, selectable into the install plan, click opens the intro (enabled per spec)
bal = fp.cards["balance"]
check("balance card selectable but unchecked by default",
      bal._enabled and not bal.isChecked() and "balance" not in fp.selected())
check("balance card shows no warning inline (dialog type)", bal.warn.isHidden())
_bal_feat = next(f for f in m.FEATURES if f["id"] == "balance")
check("balance notice states verified wording and rollback, no repeated directions",
      "实机验证" in _bal_feat["warning"]["zh"]
      and "爱好者" not in _bal_feat["warning"]["zh"]
      and "非官方" not in _bal_feat["warning"]["zh"]
      and "等字节" in _bal_feat["warning"]["zh"]
      and "①" not in _bal_feat["warning"]["zh"]
      and "⑥" not in _bal_feat["warning"]["zh"])
_bal_list = (m.APP_DIR / "balance_list_zh.md").read_text(encoding="utf-8")
check("balance numeric list carries the listed directions",
      all(x in _bal_list for x in ("1AP", "导弹发射器", "热熔", "重力枪", "致残射击")))
check("balance card packages = r30 numbers + weapon skills",
      _bal_feat["packages"] == ["balance_user_r30", "weapon_skills_v1"])
_r30 = json.loads((m.APP_DIR / "payload/balance/balance_user_r30/manifest.json").read_text(encoding="utf-8"))
check("balance r30: final-table payload has 52 entries / 97 edits",
      len(_r30["entries"]) == 52
      and sum(len(e["edits"]) for e in _r30["entries"]) == 97)
_t2 = next(e for e in _r30["entries"] if e["relative_path"].endswith("WST_T2_MissileLauncher.uasset"))
check("balance r30: T2 missile launcher range key 13->8 / value 0->-3 equal-byte edits in place",
      any(e["off"] == 763 and e["old"] == 13 and e["new"] == 8 and e["kind"] == "key_redirect"
          for e in _t2["edits"])
      and any(e["off"] == 767 and e["old"] == 0 and e["new"] == -3
              for e in _t2["edits"]))
check("balance r30: key_redirect only on T2 missile range (no damage inflation elsewhere)",
      all(e["kind"] != "key_redirect"
          for entry in _r30["entries"]
          if not entry["relative_path"].endswith("WST_T2_MissileLauncher.uasset")
          for e in entry["edits"]))
_pc3 = next(e for e in _r30["entries"] if e["relative_path"].endswith("WST_T3_PlasmaCannon.uasset"))
check("balance r30: T3 plasma cannon crit-dmg slot5 delta 0->1",
      any(e["off"] == 674 and e["old"] == 0 and e["new"] == 1 and e["kind"] == "delta"
          for e in _pc3["edits"]))
_t4las = next(e for e in _r30["entries"] if e["relative_path"].endswith("WST_T4_SW_Lascannon.uasset"))
check("balance r30: T4 lascannon crit delta -3->-1 (no damage inflation)",
      any(e["off"] == 671 and e["old"] == -3 and e["new"] == -1 and e["kind"] == "delta"
          for e in _t4las["edits"])
      and not any(e.get("new", 0) > 100 for e in _t4las["edits"]))
_t4plas = next(e for e in _r30["entries"] if e["relative_path"].endswith("WST_T4_UL_PlasmaCannon.uasset"))
check("balance r30: T4 plasma cannon slot7 delta 5->6 (no tier inflation)",
      any(e["off"] == 736 and e["old"] == 5 and e["new"] == 6 and e["kind"] == "delta"
          for e in _t4plas["edits"]))
# r29 core change: three T2 chapter items reach T3 strength via writable crit slots
# (damage slots stay at vanilla, so no equal-byte conflict remains).
for _rel, _off, _new in (("WST_T2_IF_HeavyBolter.uasset", 677, 2),
                         ("WST_T2_DA_PlasmaGun.uasset", 671, 4),
                         ("WST_T2_BT_Powersword.uasset", 674, 10)):
    _e = next(x for x in _r30["entries"] if x["relative_path"].endswith(_rel))
    check("balance r30: %s crit delta -> %d on writable slot" % (_rel, _new),
          any(e["off"] == _off and e["new"] == _new and e["kind"] == "delta" for e in _e["edits"])
          and not any(e["kind"] == "key_redirect" for e in _e["edits"]))
_ulpl = next(x for x in _r30["entries"] if x["relative_path"].endswith("WST_T3_UL_PlasmaGun.uasset"))
check("balance r30: T3 ultramarines plasma crit delta 0->1 (damage left writable)",
      any(e["off"] == 671 and e["old"] == 0 and e["new"] == 1 and e["kind"] == "delta"
          for e in _ulpl["edits"]))
check("balance unchecked by default and enabled", _bal_feat["default_checked"] is False
      and _bal_feat["enabled"] is True)
QTest.mouseClick(bal, Qt.MouseButton.LeftButton)
check("balance click only toggles selection (details via numeric link)",
      bal.isChecked() and "balance" in fp.selected())
bal.setChecked(False, emit=False)   # reset to avoid affecting later selected assertions

check("weapon skills merged into balance card packages",
      _bal_feat["packages"] == ["balance_user_r30", "weapon_skills_v1"])
_ws_doc_zh = (m.APP_DIR / "balance_list_zh.md").read_text(encoding="utf-8")
check("balance detail covers the three chapter weapons with chapter-styled skills",
      "暗黑天使" in _ws_doc_zh and "黑色圣堂" in _ws_doc_zh and "帝国之拳" in _ws_doc_zh
      and "授予" in _ws_doc_zh)
_ws_manifest = json.loads((m.APP_DIR / "payload/balance/weapon_skills_v1/manifest.json").read_text(encoding="utf-8"))
check("balance payload carries six full-file weapon-skill uasset entries",
      len(_ws_manifest["entries"]) == 6
      and all(e["relative_path"].endswith(".uasset") and e.get("dst_sha256")
              for e in _ws_manifest["entries"])
      and all((m.APP_DIR / "payload/balance/weapon_skills_v1/assets" / e["relative_path"]).is_file()
              for e in _ws_manifest["entries"]))
check("weapon skills have no recipe (full-file replacements, not asset_edits)",
      not _ws_manifest.get("recipe"))
# Regression guard: the plan must actually dispatch every selected package to apply_manifest_package.
# The old standalone weapon_skills card was in APPLY_ORDER but had no branch in _install(), so its
# payload never ran. Merging it into the balance packages makes the balance branch apply it; assert
# that every package id in the plan is covered by a dispatch branch in _install().
import inspect
_install_src = inspect.getsource(m.PatchWorker._install)
_bal_pkg_dispatch = 'aid == "balance"' in _install_src and 'act.get("packages")' in _install_src
check("balance dispatch applies all its packages (no orphan payload)",
      _bal_pkg_dispatch and len(_bal_feat["packages"]) == 2)

# 11) Black Shield offer (completion page): text + unchecked by default; click behavior in section 16
bs = w.pages[3].cards["bonus_blackshield"]
check("Black Shield description text verbatim",
      bs.desc.text() == ("你的守望前哨站之外，出现了一名绿色肩甲的星际战士。\n"
                         "你向暗黑天使询问。对方称战团查无此人。语气里有一丝愤怒。\n"
                         "你是否要接纳他，成为一名黑盾？"))
check("Black Shield unchecked by default", not bs.isChecked())
check("completion page Bonus hint checks save before recruit",
      "门外那位还在等回复" in w.pages[3].section.picker.hint.text())
check("Black Shield copy has no contract wording",
      "合同" not in w.pages[3].section.picker.hint.text()
      and "合同" not in bs.desc.text()
      and "合同" not in S["bs_nosave_body"]["zh"])

# 12) back navigation and language switching (English default on a non-Chinese system)
w.prev_page()
check("navigates back to install page", w.current() == 1)
w.next_page()
check("navigates back to features page", w.current() == 2)
w.set_lang("en")
fp = w.pages[2]
check("en: localization unchecked by default", not fp.cards["zh_text"].isChecked())
check("en: visual still checked by default", fp.cards["visual"].isChecked())
check("en: left navigation in English", w.rail.labels[2] == "Features")
check("en: language radio synced to English", w.lang_en.isChecked() and not w.lang_zh.isChecked())
check("en: accent stays green", w.accent == m.GREEN)
check("en: title bar brand in English", "Deathwatch Patch Installer" in w.windowTitle())
check("en: bugfix link in English", "Bug details" in fp.cards["bugfix"].detail_link.text())
check("en: visual tiers contain no internal codenames",
      all(("tier_" not in b._text.lower() and "g9" not in b._text.lower()
           and "g11" not in b._text.lower() and "g12" not in b._text.lower()
           and "g13" not in b._text.lower())
          for b in fp.variant_buttons))
check("en: visual tier labels in English",
      fp.cards["visual"].variant_choice.current_key() == "tier_dgpu"
      and "Discrete" in fp.cards["visual"].current_variant_label("en")
      and "Tier details" in fp.cards["visual"].detail_link.text())
check("en: balance link in English", "Numeric details" in fp.cards["balance"].detail_link.text())
dlg_en = m.BugDetailDialog(w)
check("en: Bug details dialog shows English list", "Bug Fixes" in dlg_en.view.toPlainText())
dlg_en.close()
dlg_vis_en = m.BugDetailDialog(w, "visual")
check("en: visual params dialog in English (player wording, no internal keys)",
      "[Base] shared" in dlg_vis_en.view.toPlainText()
      and "vanilla is not a tier" in dlg_vis_en.view.toPlainText()
      and "frame cap 144" in dlg_vis_en.view.toPlainText()
      and "AsyncLoadingTimeLimit=" not in dlg_vis_en.view.toPlainText())
dlg_vis_en.close()
dlg_cam_en = fp.build_camera_warning()
check("en: camera warning same meaning", "hold R for 3 seconds" in dlg_cam_en.body.text())
dlg_cam_en.close()

# 13) stability under repeated interaction
for _ in range(3):
    w.set_lang("zh")
    w.set_lang("en")
    QTest.mouseClick(w.pages[1].restore_card, Qt.MouseButton.LeftButton)
    QTest.mouseClick(w.pages[1].install_card, Qt.MouseButton.LeftButton)
check("mode = install after repeated toggling", w.mode == "install" and w.accent == m.GREEN)
check("default policy still correct after repeated toggling", not w.pages[2].cards["zh_text"].isChecked())

# 14) ownership notice / English disclaimer
w.set_lang("zh")
own = w.pages[1].ownership.text()
check("genuine-copy notice includes registry appid/build",
      str(m.REGISTRY["game_appid"]) in own and str(m.REGISTRY["game_buildid"]) in own)
check("genuine-copy notice includes lawful-possession wording", "合法持有" in own)
w.set_lang("en")
check("English notice also renders version", str(m.REGISTRY["game_buildid"]) in w.pages[1].ownership.text())
check("English disclaimer is an unofficial notice",
      "not affiliated" in w.pages[0].body.text()
      and "does not bypass DRM" in w.pages[0].body.text())
check("English install page title", "Locate" in w.pages[1].title.text())

# 15) Chinese voice DIY dialog: three essentials (TTS / directory / replacement)
w.set_lang("zh")
fp = w.pages[2]
dlg = fp.build_diy_dialog()
body = dlg.editor.toPlainText()
check("DIY dialog body nonempty and loaded from resource", len(body) > 1500)
check("DIY 1) TTS choice = IndexTTS 2.0",
      "IndexTTS 2.0" in body and "index-tts/index-tts" in body and "IndexTeam/IndexTTS-2" in body)
check("DIY 2) directory includes Tyranid Invasion + Audio\\VO",
      "Tyranid Invasion" in body and "Content\\Audio\\VO" in body)
check("DIY 3) replacement method: equal-length + OUT-OF-BUDGET + backup",
      "等长" in body and "OUT-OF-BUDGET" in body and "备份" in body)
check("DIY prompt includes no-redistribution wording",
      "不得对外分发" in body or "不外发" in body or "禁分发" in body)
check("DIY dialog buttons = Copy/Close",
      dlg.yesButton.text() == "复制提示词" and dlg.cancelButton.text() == "关闭")
w.set_lang("en")
body_en = fp.build_diy_dialog().editor.toPlainText()
check("DIY English version has the same three essentials", "IndexTTS 2.0" in body_en and "Tyranid Invasion" in body_en
      and "equal-length" in body_en and "OUT-OF-BUDGET" in body_en)

# 16) execution: page 3 button runs directly, page 4 shows the result
w.set_lang("zh")
fp = w.pages[2]
check("before execution defaults: localization/visual/bug checked, camera unchecked",
      fp.cards["zh_text"].isChecked() and fp.cards["visual"].isChecked()
      and fp.cards["bugfix"].isChecked() and not fp.cards["camera"].isChecked())
_orig_cam = fp.open_camera_warning
fp.open_camera_warning = lambda: None
try:
    QTest.mouseClick(fp.cards["camera"], Qt.MouseButton.LeftButton)
finally:
    fp.open_camera_warning = _orig_cam
check("selecting camera unlock enters install plan",
      "camera" in [a["id"] for a in w._build_plan()])
check("apply order has no standalone weapon skills step",
      m.InstallerWindow.APPLY_ORDER == ["bugfix", "zh_text", "vo_zh", "visual", "camera", "balance"])
check("balance runs after localization and is the last step",
      m.InstallerWindow.APPLY_ORDER.index("balance") > m.InstallerWindow.APPLY_ORDER.index("zh_text")
      and m.InstallerWindow.APPLY_ORDER[-1] == "balance")
QTest.mouseClick(fp.cards["camera"], Qt.MouseButton.LeftButton)   # uncheck (no warning dialog)
_orig_vis16 = fp.open_visual_notice
fp.open_visual_notice = lambda: None
for cid in ("zh_text", "visual", "bugfix"):
    QTest.mouseClick(fp.cards[cid], Qt.MouseButton.LeftButton)
fp.open_visual_notice = _orig_vis16
check("all unchecked: empty plan still executable",
      not fp.selected() and w.next_btn.text() == "执行" and w.next_btn.isEnabled())
QTest.mouseClick(w.next_btn, Qt.MouseButton.LeftButton)
check("during execution: button grayed out and text = Running...",
      w.running and not w.next_btn.isEnabled() and w.next_btn.text() == "执行中…")
check("during execution: Back and language switch disabled",
      not w.back_btn.isEnabled() and not w.lang_en.isEnabled())
for _ in range(100):
    QTest.qWait(100)
    if not w.running:
        break
check("automatically enters completion page when done", w.current() == 3 and not w.running)
log = w.pages[3].log.toPlainText()
check("completion page writes backup directory", "备份目录" in log)
check("completion page no longer shows preview/simulation wording",
      "模拟结果" not in log and "UI 预览" not in log)
check("completion page bottom button = Done", w.next_btn.text() == "完成" and w.next_btn.isEnabled())
check("completion page Back disabled (no going back after execution)", not w.back_btn.isEnabled())
check("completion page lower half = Black Shield + author",
      w.pages[3].cards["bonus_blackshield"].isVisible()
      and w.pages[3].author.isVisible())
check("author bio is GitHub/Ko-fi guidance with no donation/coffee wording",
      "GitHub" in w.pages[3].section.author_desc.text()
      and "Ko-fi" in w.pages[3].section.author_desc.text()
      and "支持" in w.pages[3].section.author_desc.text()
      and "打赏" not in w.pages[3].section.author_desc.text()
      and "咖啡" not in w.pages[3].section.author_desc.text())
check("completion page has GitHub + Ko-fi author links, no Bilibili",
      "github.com/monkeyrs" in w.pages[3].section.links_label.text()
      and "ko-fi.com/monkeyrs" in w.pages[3].section.links_label.text()
      and "bilibili" not in w.pages[3].section.links_label.text().lower()
      and not w.pages[3].section.links_label.isHidden())
check("completion page has third-party notices entry and bundled file",
      ((m.APP_DIR / "THIRD_PARTY_NOTICES.md").is_file()
       or (m.APP_DIR.parent / "THIRD_PARTY_NOTICES.md").is_file())
      and not w.pages[3].section.notice_link.isHidden()
      and "第三方许可" in w.pages[3].section.notice_link.text())

# 16b) Black Shield offer: card click only toggles; Done applies the selection
_notices = []
_inject_calls = []
_orig_notice, _orig_pre = m.NoticeDialog, patcher.blackshield_preflight
_orig_new, _orig_inject, _orig_close = patcher.new_session, patcher.inject_blackshield, w.close


class _FakeNotice:
    def __init__(self, win, title, body, danger=True):
        _notices.append((title, body))

    def exec(self):
        return 0


m.NoticeDialog = _FakeNotice
w.close = lambda: None        # keep the window alive while testing the Done action
patcher.new_session = lambda *a, **k: type("S", (), {"finalize": lambda self: None})()
patcher.inject_blackshield = lambda sess, log=print: (
    _inject_calls.append(1) or {"ok": True, "applied": 1, "skipped": 0,
                               "errors": [], "detail": "smoke"})
_accept_badge = w.pages[3].section.picker.accept_badge
try:
    check("accept badge hidden before selecting Black Shield",
          _accept_badge.text() == "接纳" and not _accept_badge.isVisible())
    patcher.blackshield_preflight = lambda *a, **k: {
        "exists": False, "parse_ok": False, "path": "smoke://none",
        "reason": "save_not_found"}
    QTest.mouseClick(bs, Qt.MouseButton.LeftButton)
    check("card click only toggles selection and shows ACCEPT badge",
          bs.isChecked() and _accept_badge.isVisible() and not _inject_calls)
    QTest.mouseClick(w.next_btn, Qt.MouseButton.LeftButton)
    check("no save on Done: card reverts and user is prompted",
          not bs.isChecked() and not _accept_badge.isVisible() and not _inject_calls
          and bool(_notices) and _notices[-1][0] == "还没有存档"
          and "打完教程" in _notices[-1][1])
    patcher.blackshield_preflight = lambda *a, **k: {
        "exists": True, "parse_ok": True, "path": "smoke://save"}
    QTest.mouseClick(bs, Qt.MouseButton.LeftButton)
    check("card toggles back on without injecting",
          bs.isChecked() and _accept_badge.isVisible() and not _inject_calls)
    QTest.mouseClick(w.next_btn, Qt.MouseButton.LeftButton)
    check("save present on Done: injection runs once and card locks",
          len(_inject_calls) == 1 and bs.isChecked() and bs._locked
          and "黑盾已加入存档" in w.pages[3].log.toPlainText())
finally:
    patcher.blackshield_preflight = _orig_pre
    patcher.new_session, patcher.inject_blackshield = _orig_new, _orig_inject
    m.NoticeDialog = _orig_notice
    w.close = _orig_close

_apply_ops_src = inspect.getsource(patcher._save_inject().apply_ops)
check("save writeback verifies logical unit IDs, not raw record headers",
      "新增单位编号" in _apply_ops_src and "整体单位记录数" not in _apply_ops_src)

print("SMOKE PASS: %d/%d" % (len(PASS), len(PASS)))
for name in PASS:
    print("  ok -", name)
w.close()
