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
check("zh: 拒绝 ../ 资产路径", _bad_rel)
_rels_bad = zh_runtime.target_rels(Path("."), {"asset_text": [
    {"asset_file": "../evil.uasset"}, {"asset_file": "C:/evil.uasset"}]})
check("zh: target_rels 过滤非法 asset_file", all("evil" not in r for r in _rels_bad))
_um = zh_runtime.ue4prop.UM
zh_runtime.ue4prop.UM = Path("__missing_umodel__.exe")
check("ue4prop: UModel 缺失时静默返回 None",
      zh_runtime.ue4prop.umodel_list("x") is None)
zh_runtime.ue4prop.UM = _um
# 1) language detection mapping
check("langid zh-CN(0x0804)→zh", primary_lang_id(0x0804) == "zh")
check("langid en-US(0x0409)→en", primary_lang_id(0x0409) == "en")

# 2) registry integrity
ids = [f["id"] for f in m.FEATURES]
check("steam_ini 不入注册表", "bonus_steam_ini" not in ids)
_vo = next(f for f in m.FEATURES if f["id"] == "vo_zh")
check("vo_zh 仅 DIY 入口：无载荷 / 不可勾选 / 非分发",
      _vo["packages"] == [] and _vo["enabled"] is False
      and _vo.get("diy_prompt") is True and _vo["default_checked"] is False)
_zh = next(f for f in m.FEATURES if f["id"] == "zh_text")
check("zh_text 为运行时生成模式",
      _zh.get("locres_mode") == "generate_at_install"
      and _zh.get("runtime") == "zh_text" and _zh["packages"] == [])
_bug = next(f for f in m.FEATURES if f["id"] == "bugfix")
check("bugfix 首版启用且 7 个已实装包", _bug["enabled"] is True
      and len(_bug["packages"]) == 7 and "bugfix_lod" not in _bug["packages"])
check("bugfix 清单中英资源在包内",
      all((m.APP_DIR / p).is_file() for p in _bug["detail_doc"].values()))
check("bugfix 默认勾选", _bug["default_checked"] is True)
_cam = next(f for f in m.FEATURES if f["id"] == "camera")
check("camera 启用且默认不勾选、勾选时弹提醒",
      _cam["enabled"] is True and _cam["default_checked"] is False
      and _cam.get("warning_modal") is True)
check("camera 属核心特性（会进入安装计划）",
      any(f["id"] == "camera" for f in m.CORE_FEATURES))
vis = next(f for f in m.FEATURES if f["id"] == "visual")
check("光影默认档=独显 tier_dgpu", vis["default_variant"] == "tier_dgpu"
      and vis["detail_doc"]["zh"] == "visual_list_zh.md"
      and (m.APP_DIR / "visual_list_zh.md").is_file()
      and (m.APP_DIR / "visual_list_en.md").is_file())
check("光影三档 id/文案（核显/独显/最高）",
      [v["id"] for v in vis["variants"]] == ["tier_igpu", "tier_dgpu", "tier_max"]
      and [v["short"]["zh"] for v in vis["variants"]] == ["核显", "独显", "最高"])
check("bonus 标记齐备", next(f for f in m.FEATURES if f["id"] == "bonus_blackshield")["bonus"] is True)
check("打包 spec datas 覆盖全部详情文档（防 exe 内清单缺失，2026-11-19）",
      all(str((m.APP_DIR / p).name) in
          (Path(__file__).resolve().parent.parent / "DeathwatchPatchInstaller.spec").read_text(encoding="utf-8")
          for f in m.FEATURES for p in (f.get("detail_doc") or {}).values()))
check("黑盾带存档预检标记",
      next(f for f in m.FEATURES if f["id"] == "bonus_blackshield").get("save_check") == "dw_metagame.save")
check("CORE/BONUS 划分闭合",
      len(m.CORE_FEATURES) + len(m.BONUS) == len(m.FEATURES)
      and not ({f["id"] for f in m.CORE_FEATURES} & {f["id"] for f in m.BONUS}))
check("黑盾卡名不带 Bonus 前缀",
      next(f for f in m.FEATURES if f["id"] == "bonus_blackshield")["name"]["zh"] == "插入一名黑盾")
check("「Bonus」全界面只出现一次（区块标题）",
      sum(v["zh"].count("Bonus") for v in S.values()) == 1
      and sum(v["en"].count("Bonus") for v in S.values()) == 1
      and all("Bonus" not in f["name"]["zh"] + f["desc"]["zh"] for f in m.FEATURES)
      and all("Bonus" not in f["name"]["en"] + f["desc"]["en"] for f in m.FEATURES))

# 3) exe derivation (pure function)
tmp = Path(os.environ.get("TEMP", ".")) / "dw_installer_smoke_root"
(tmp / "deathwatchgame" / "Content").mkdir(parents=True, exist_ok=True)
(tmp / "deathwatchgame" / "Binaries" / "Win32").mkdir(parents=True, exist_ok=True)
check("根 exe 有效推导", m.derive_root(str(tmp / "DeathwatchGame.exe")) == tmp)
check("Win32 exe 有效推导",
      m.derive_root(str(tmp / "deathwatchgame" / "Binaries" / "Win32" / "DeathwatchGame-Win32-Shipping.exe")) == tmp)
check("无关 exe 拒绝", m.derive_root(r"C:\Windows\notepad.exe") is None)
check("无 Content 拒绝", m.derive_root(r"C:\Game\DeathwatchGame.exe") is None)

# 4) Chinese runtime data and generation-chain entries
data = zh_runtime.load_data(m.APP_DIR / "zh_runtime.json")
check("zh_runtime.json 四域齐全且非空",
      all(len(data[k]) > 0 for k in ("locres", "flairname", "asset_text", "bfl")))
check("locres 行只含 ns/key/hash/zh（不含英文源串）",
      all(set(("ns", "key", "hash", "zh")) <= set(r) and "en" not in r
          for r in data["locres"][:50]))
check("zh 数据不携带 en 源串（locres/asset_text/flairname/bfl）",
      all("en" not in r for r in data["asset_text"])
      and all(len(k) == 64 and all(c in "0123456789abcdef" for c in k)
              for k in list(data["flairname"])[:10])
      and all(len(k) == 64 and all(c in "0123456789abcdef" for c in k)
              for k in list(data["bfl"])[:10]))
check("四条链函数可调用",
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
    check("清单包投放成功并校验 dst hash", r["ok"] and target.read_bytes() == b"PATCHED!")
    sess.finalize()
    check("备份会话含原件与 session.json",
          (sess.dir / "files" / rel).read_bytes() == b"ORIGINAL"
          and (sess.dir / "session.json").is_file())
    rr = patcher.restore_session(work, sess.dir)
    check("按备份还原原版", rr["ok"] and target.read_bytes() == b"ORIGINAL")
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
    check("还原会删除本会话新建的文件", not (work / rel2).exists())
    check("黑盾预检：无存档 → exists=False",
          patcher.blackshield_preflight(str(work / "nope.save"))["exists"] is False)
    fake = work / "fake.save"
    fake.write_bytes(b"not a save")
    pre = patcher.blackshield_preflight(str(fake))
    check("黑盾预检：坏存档 → 不抛异常且 parse_ok=False",
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
    check("相机补丁：等长变换成功", rc["ok"] and rc["applied"] == 1
          and cam_p.read_bytes() == want_cam)
    rc2 = patcher.apply_camera_patch(work, cam_rules, cs, log=lambda *_: None)
    check("相机补丁：重复执行幂等跳过",
          rc2["ok"] and rc2["skipped"] == 1 and rc2["applied"] == 0)
    cam_p.write_bytes(b"HEAD" + b"\x11\x22\x33\x44" + b"TAIL")
    rc3 = patcher.apply_camera_patch(work, cam_rules, cs, log=lambda *_: None)
    check("相机补丁：版本不符拒绝强改",
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
    check("P0: 二次安装无实际改动不创建空 session",
          p2 is None and not a2.dir.exists())

    # an old Black Shield session with only a save backup and no files must not shadow the install backup
    a3 = patcher.new_session(p0, "20260103T000000Z")
    (a3.dir / "save").mkdir(parents=True, exist_ok=True)
    (a3.dir / "save" / "dw_metagame.save").write_bytes(b"SAVE")
    a3.finalize()
    rr0 = patcher.restore_all(p0)
    check("P0: save-only session 不遮蔽安装备份，还原到最早原版",
          rr0["ok"] and rr0["restored"] == 1
          and rr0["deleted"] == 0 and target0.read_bytes() == b"ORIGINAL")

    # P0-1 pre-restore before reapply: revert to the original when current differs from the earliest backup, then apply
    target0.write_bytes(b"PATCHED!")
    pre0 = patcher.pre_restore_selected(p0, [rel0], log=lambda *_: None)
    check("P0: 重打前按选中项预还原到最早原版",
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
    check("P0-4: 等长 transform 生效且重复执行幂等",
          rt1["ok"] and rt1["applied"] == 1
          and rt2["ok"] and rt2["applied"] == 0 and rt2["skipped"] == 1
          and target_t.read_bytes() == b"AZC")
    st.finalize()
    patcher.restore_all(p0)
    check("P0-4: transform 文件可还原",
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
    check("recipe: 无 uasset payload，按 edits 从原版计算",
          rr["ok"] and rr["applied"] == 1
          and target_r.read_bytes() == b"ABC" + b"Z\x00\x00\x00")
    patcher.restore_all(p0)
    check("recipe: 可还原", target_r.read_bytes() == b"ABC" + b"B\x00\x00\x00")

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
    check("balance crippling: zh 2%→8% 三处等长、1% 不动",
          rc["ok"] and rc["applied"] == 3 and _dc.count(_zh8) == 3
          and _dc.count(_zh2) == 0 and _dc.count(_zh1) == 1 and len(_dc) == len(loc_c.read_bytes()))
    check("balance crippling: 重跑幂等跳过多余改写",
          patcher.apply_balance_crippling_text(p0, sr_c, log=lambda *_: None)["skipped"] == 1)
    loc_c.write_bytes(b"HDR" + "2% Chance to reduce target's AP by 1 per shot".encode("utf-16-le") + b"TAIL")
    sr_c2 = patcher.new_session(p0, "20260109T000000Z")
    re_c = patcher.apply_balance_crippling_text(p0, sr_c2, log=lambda *_: None)
    sr_c2.finalize()
    _en8 = "8% Chance to reduce target's AP by 1 per shot".encode("utf-16-le")
    _en2 = "2% Chance to reduce target's AP by 1 per shot".encode("utf-16-le")
    check("balance crippling: en 2%→8% 等长",
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
        check("P0: 存档解析失败时原存档不被覆盖",
              not bad["ok"] and save0.read_bytes() == b"SAVE-ORIGINAL")

        class _FakeGood(_FakeM):
            @staticmethod
            def apply_ops(b, ops):
                return b"GOOD-SAVE", []

        patcher._save_inject = lambda: _FakeGood
        good = patcher.inject_blackshield(sess0, log=lambda *_: None)
        check("P0: 存档先写临时副本，校验通过才替换",
              good["ok"] and save0.read_bytes() == b"GOOD-SAVE"
              and Path(good["backup"]).read_bytes() == b"SAVE-ORIGINAL"
              and not (p0 / "dw_metagame.save.dwnew").exists())
        check("P0: save_inject.CONTENT 指向用户游戏 Content",
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
check("共 4 屏（4 步）", len(w.pages) == 4)
check("左侧四个任务步骤齐全",
      w.rail.width() == 220
      and w.rail.labels == ["协议", "安装", "特性", "完成"])
check("起始当前步=0", w.current() == 0)
check("当前步骤在左侧导航同步", w.rail.current == 0 and w.rail.accent.name() == m.BLUE.lower())
check("标题栏保留品牌标识", "Deathwatch" in w.windowTitle())
check("语言单选在底部导航（位于页面栈下方）",
      w.lang_zh.mapTo(w, QPoint(0, 0)).y() > w.stack.geometry().bottom()
      and not hasattr(w, "lang_combo"))

# 7) disclaimer gate + updated notice
body_zh = w.pages[0].body.text()
check("新免责声明含非官方/合法正版/不绕过 DRM",
      "非官方" in body_zh and "合法购买的正版" in body_zh and "不绕过 DRM" in body_zh)
check("免责声明从注册表渲染版本号",
      str(m.REGISTRY["game_appid"]) in body_zh
      and str(m.REGISTRY["game_buildid"]) in body_zh)
check("免责声明与作者文案不再出现「打赏」",
      "打赏" not in body_zh and not any("打赏" in v["zh"] for v in S.values())
      and not any("打赏" in v["en"] for v in S.values()))
check("未接受免责不可下一步", not w.next_btn.isEnabled())
w.pages[0].accept.click()
check("接受后可下一步", w.next_btn.isEnabled())
w.next_page()
check("到安装页（路径+模式）", w.current() == 1)

# 8) install-page gating: path and mode are both required; full-card highlight + green/blue mutual exclusion
setup = w.pages[1]
check("空路径不可下一步", not w.next_btn.isEnabled())
QTest.mouseClick(setup.install_card, Qt.MouseButton.LeftButton)
check("只选模式仍不可下一步（缺路径）", not w.next_btn.isEnabled())
setup.path_edit.setText(str(tmp / "DeathwatchGame.exe"))
check("路径+模式齐备可下一步", w.next_btn.isEnabled())
check("打补丁=绿主题色", w.mode == "install" and w.accent == m.GREEN)
check("模式卡整卡点亮（绿底）", setup.install_card.isChecked()
      and setup.install_card.backgroundColor.name() == m.GREEN.lower())
check("模式卡无可见单选点", not setup.install_card.findChildren(QRadioButton))
QTest.mouseClick(setup.restore_card, Qt.MouseButton.LeftButton)
check("还原=蓝主题（同组互斥）", w.mode == "restore" and w.accent == m.BLUE)
check("模式互斥", setup.restore_card.isChecked() and not setup.install_card.isChecked())
check("还原卡整卡点亮（蓝底）", setup.restore_card.backgroundColor.name() == m.BLUE.lower())
bs0 = w.pages[3].cards["bonus_blackshield"]
check("P0: 还原模式禁用完成页黑盾",
      not bs0._enabled and "还原模式" in w.pages[3].section.picker.hint.text())
QTest.mouseClick(setup.install_card, Qt.MouseButton.LeftButton)
check("切回打补丁=绿", w.mode == "install" and w.accent == m.GREEN)
check("P0: 打补丁模式恢复完成页黑盾",
      bs0._enabled and "点击卡片" in w.pages[3].section.picker.hint.text())
w.next_page()
check("到特性页", w.current() == 2)

# 9) features page: uniform card grid + card interactions
fp = w.pages[2]
check("特性页用 FlowLayout", isinstance(fp.flow, m.FlowLayout))
check("特性卡 2~3 卡/行：980 窗卡片宽度均分且不小于 300",
      len({c.width() for c in fp.cards.values()}) == 1
      and fp.cards["zh_text"].width() >= 300)
check("特性卡高度统一（全部拉齐到 balance 卡样式）",
      len({c.height() for c in fp.cards.values()}) == 1
      and fp.cards["balance"].height() >= 60)
check("zh: 汉化默认勾选", fp.cards["zh_text"].isChecked())
check("zh: 光影默认勾选", fp.cards["visual"].isChecked())
check("默认勾选=整卡点亮（绿底）",
      fp.cards["zh_text"].backgroundColor.name() == m.GREEN.lower())
check("特性卡无可见勾选框", not fp.cards["zh_text"].findChildren(QCheckBox))
app.processEvents()
check("特性网格用普通 FlowLayout（不做行内等高拉伸）",
      type(fp.flow) is m.FlowLayout)
check("两张中文卡同处第一行",
      fp.cards["zh_text"].y() == fp.cards["vo_zh"].y()
      and fp.cards["zh_text"].x() < fp.cards["vo_zh"].x()
      and fp.cards["visual"].y() > fp.cards["zh_text"].y())
check("bugfix 启用且默认勾选 / camera 启用但默认不勾选",
      fp.cards["bugfix"]._enabled and fp.cards["bugfix"].isChecked()
      and fp.cards["camera"]._enabled and not fp.cards["camera"].isChecked())
check("bugfix 卡名=Bug 修正 且不再列 3 条清单",
      fp.cards["bugfix"].title.text() == "Bug 修正"
      and fp.cards["bugfix"].desc.text().count("·") == 0)
check("bugfix 卡带「Bug 详情」小超链接",
      fp.cards["bugfix"].detail_link is not None
      and "Bug 详情" in fp.cards["bugfix"].detail_link.text())
check("光影卡带「参数说明」小超链接",
      fp.cards["visual"].detail_link is not None
      and "参数说明" in fp.cards["visual"].detail_link.text())
check("balance 卡带「数值说明」小超链接",
      fp.cards["balance"].detail_link is not None
      and "数值说明" in fp.cards["balance"].detail_link.text())
check("camera 卡带「谨慎」小超链接",
      fp.cards["camera"].detail_link is not None
      and "谨慎" in fp.cards["camera"].detail_link.text())
check("其它卡无详情链接",
      all(c.detail_link is None for fid, c in fp.cards.items()
          if fid not in ("bugfix", "visual", "balance", "camera", "vo_zh")))
QTest.mouseClick(fp.cards["bugfix"], Qt.MouseButton.LeftButton)
check("bugfix 可取消（整卡熄灭）",
      not fp.cards["bugfix"].isChecked()
      and fp.cards["bugfix"].backgroundColor.name() != m.GREEN.lower())
QTest.mouseClick(fp.cards["bugfix"], Qt.MouseButton.LeftButton)
check("bugfix 可重新勾选（整卡点亮）",
      fp.cards["bugfix"].isChecked()
      and fp.cards["bugfix"].backgroundColor.name() == m.GREEN.lower())
dlg = m.BugDetailDialog(w)
body = dlg.view.toPlainText()
check("Bug 详情弹窗展示中文清单全文",
      "Bug 修正说明" in body and "招架" in body and "致残射击" in body)
check("Bug 详情弹窗标题/按钮",
      dlg.windowTitle() == "Bug 修正清单" and dlg.yesButton.text() == "关闭")
dlg.close()
dlg_bal = m.BugDetailDialog(w, "balance")
body_bal = dlg_bal.view.toPlainText()
check("balance 数值说明弹窗展示中文通用口径",
      "爱好者" in body_bal and "1AP 可用变体" in body_bal and "base" in body_bal
      and "动力剑" in body_bal and "重型爆弹枪" not in body_bal and "通用口径" not in body_bal)
check("balance 数值说明无重复条目",
      body_bal.count("1AP 可用变体") == 1 and body_bal.count("导弹发射器") == 1
      and body_bal.count("致残射击") == 1)
check("balance 数值说明弹窗标题",
      dlg_bal.windowTitle() == "武器平衡性补丁 · 数值说明")
check("balance 数值说明含致残射击 2%→8%",
      "致残射击" in body_bal and "2%" in body_bal and "8%" in body_bal)
dlg_bal.close()

# 9b) visual: three-way segmented choice (integrated / discrete / max) + parameter dialog on card click
check("光影档位=现代分段三选一（无下拉框/无单选点）",
      len(fp.variant_buttons) == 3
      and not hasattr(fp.cards["visual"], "variant_combo")
      and not fp.cards["visual"].findChildren(QRadioButton)
      and all(b.isVisible() for b in fp.variant_buttons))
check("光影档位文案不含内部代号（tier_/g9/g11/g12/g13）",
      all(("tier_" not in b._text.lower() and "g9" not in b._text.lower()
           and "g11" not in b._text.lower() and "g12" not in b._text.lower()
           and "g13" not in b._text.lower())
          for b in fp.variant_buttons))
check("默认档=独显",
      fp.cards["visual"].variant_choice.current_key() == "tier_dgpu"
      and "独显" in fp.cards["visual"].current_variant_label("zh"))
QTest.mouseClick(fp.variant_buttons[0], Qt.MouseButton.LeftButton)
check("点分段按钮整块切换档位且卡不被取消勾选",
      fp.cards["visual"].variant_choice.current_key() == "tier_igpu"
      and fp.cards["visual"].isChecked())
QTest.mouseClick(fp.variant_buttons[2], Qt.MouseButton.LeftButton)
check("第 3 档=最高可选中",
      fp.cards["visual"].variant_choice.current_key() == "tier_max")
QTest.mouseClick(fp.variant_buttons[0], Qt.MouseButton.LeftButton)
check("第 1 档=核显可选中",
      fp.cards["visual"].variant_choice.current_key() == "tier_igpu")
QTest.mouseClick(fp.variant_buttons[1], Qt.MouseButton.LeftButton)
check("切回默认档独显",
      fp.cards["visual"].variant_choice.current_key() == "tier_dgpu")
check("光影档位随勾选显示", not fp.cards["visual"].variant_row.isHidden())
check("卡内无「档位」多余标题",
      not any("档位" in lbl.text() for lbl in fp.cards["visual"].findChildren(m.BodyLabel)))
# clicking the card only toggles selection; details come from the title-row link, and the dialog does not change selection
_before_vis = fp.cards["visual"].isChecked()
QTest.mouseClick(fp.cards["visual"], Qt.MouseButton.LeftButton)
check("点击光影卡只切换勾选、不自动弹说明",
      fp.cards["visual"].isChecked() != _before_vis)
QTest.mouseClick(fp.cards["visual"], Qt.MouseButton.LeftButton)
check("再点光影卡恢复勾选", fp.cards["visual"].isChecked() == _before_vis)
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
check("光影说明链接弹窗且不改勾选",
      _FakeBugDlg.opened == 1 and fp.cards["visual"].isChecked() == _before_vis)
m.BugDetailDialog = _old_bug_dlg
dlg_vis = m.BugDetailDialog(w, "visual")
_vis_body = dlg_vis.view.toPlainText()
check("光影参数弹窗：三档 + 底座共有项 + 原版走还原（无未改参数/内部键名）",
      "【底座】" in _vis_body and "核显" in _vis_body and "独显" in _vis_body
      and "最高" in _vis_body and "原版" in _vis_body
      and "帧率上限 144 帧" in _vis_body and "资源加载时限 10 / 30" in _vis_body
      and "已实机验证" in _vis_body
      and "尚未实机验证" not in _vis_body
      and "OneFrameThreadLag" not in _vis_body
      and "AsyncLoadingTimeLimit=" not in _vis_body
      and "r.VSync=" not in _vis_body)
check("弹窗段落标题【】突出且无 markdown 残留",
      _vis_body.count("【") >= 5 and "## " not in _vis_body)
check("光影参数弹窗标题/按钮",
      dlg_vis.windowTitle() == "光影增强 · 档位参数" and dlg_vis.yesButton.text() == "关闭")
dlg_vis.close()
_dlg_close = m.BugDetailDialog(w, "visual")
_dlg_close.show()
app.processEvents()
_dlg_close.yesButton.click()
app.processEvents()
check("说明弹窗右下角关闭按钮生效", not _dlg_close.isVisible())
_dlg_close.close()

# 9c) camera card: click only toggles selection; the "caution" link opens a red-text dialog without changing selection
CAMERA_VERBATIM = "【谨慎】摄像机可能撞墙。当你找不到人物时：按住R（3 秒钟），然后再按 Tab 就能恢复。"
check("camera 卡内不再常显警示（改为弹窗）", fp.cards["camera"].warn.isHidden())
check("camera 卡可点击（手型指针）",
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
    check("点击镜头卡只勾选、不自动弹提醒",
          fp.cards["camera"].isChecked() and _FakeCamDlg.opened == 0
          and fp.cards["camera"].backgroundColor.name() == m.GREEN.lower())
    QTest.mouseClick(fp.cards["camera"], Qt.MouseButton.LeftButton)
    check("再点取消勾选、仍不弹提醒",
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
check("特性页不再出现 Bonus 卡", "bonus_blackshield" not in fp.cards)
check("完成页含黑盾合同卡", "bonus_blackshield" in w.pages[3].cards)
check("特性页无作者卡", not hasattr(fp, "author"))
check("特性页含滚动容器", bool(fp.findChildren(m.SingleDirectionScrollArea)))

# 10) vo_zh DIY card: greyed out, not selectable, with a DIY badge
vo = fp.cards["vo_zh"]
check("vo_zh 卡不可勾选且永不入 selected",
      not vo._enabled and "vo_zh" not in fp.selected())
check("vo_zh 卡显示 DIY 徽标", not vo.badge.isHidden() and vo.badge.text() == "DIY")
check("vo_zh 说明链接可独立打开提示词",
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
    check("vo_zh 说明链接弹窗且不改勾选",
          _FakeDiy.opened == 1 and not vo.isChecked())
finally:
    m.VoDiyDialog = _old_diy

# 10b) balance card: unchecked by default, selectable into the install plan, click opens the intro (enabled per spec)
bal = fp.cards["balance"]
check("balance 卡可勾选且默认不勾选",
      bal._enabled and not bal.isChecked() and "balance" not in fp.selected())
check("balance 卡内不常显警示（弹窗型）", bal.warn.isHidden())
_bal_feat = next(f for f in m.FEATURES if f["id"] == "balance")
check("balance 声明只留爱好者/非官方/回滚说明，不再重复六方向",
      "爱好者" in _bal_feat["warning"]["zh"]
      and "非官方" in _bal_feat["warning"]["zh"]
      and "等字节" in _bal_feat["warning"]["zh"]
      and "①" not in _bal_feat["warning"]["zh"]
      and "⑥" not in _bal_feat["warning"]["zh"])
_bal_list = (m.APP_DIR / "balance_list_zh.md").read_text(encoding="utf-8")
check("balance 数值清单统一承载六方向",
      all(x in _bal_list for x in ("1AP", "激光炮", "导弹发射器", "热熔", "重力枪", "致残射击")))
check("balance 包指向 r14（含致残射击 BUFF .edit）",
      _bal_feat["packages"] == ["balance_user_r14"])
check("balance 默认不勾选且 enabled", _bal_feat["default_checked"] is False
      and _bal_feat["enabled"] is True)
QTest.mouseClick(bal, Qt.MouseButton.LeftButton)
check("balance 点击只负责勾选（说明走数值链接）",
      bal.isChecked() and "balance" in fp.selected())
bal.setChecked(False, emit=False)   # reset to avoid affecting later selected assertions

# 11) Black Shield offer (completion page): text + unchecked by default; click behavior in section 16
bs = w.pages[3].cards["bonus_blackshield"]
check("黑盾文案逐字在", bs.desc.text() == "接收一名黑盾（可能是堕天使）加入你的守望要塞。")
check("黑盾默认不勾选", not bs.isChecked())
check("完成页 Bonus 提示=先查存档再注入",
      "先检查存档" in w.pages[3].section.picker.hint.text())

# 12) back navigation and language switching (English default on a non-Chinese system)
w.prev_page()
check("回退到安装页", w.current() == 1)
w.next_page()
check("回到特性页", w.current() == 2)
w.set_lang("en")
fp = w.pages[2]
check("en: 汉化默认不勾", not fp.cards["zh_text"].isChecked())
check("en: 光影仍默认勾选", fp.cards["visual"].isChecked())
check("en: 左侧导航英文化", w.rail.labels[2] == "Features")
check("en: 语言单选同步为 English", w.lang_en.isChecked() and not w.lang_zh.isChecked())
check("en: 主题色保持绿", w.accent == m.GREEN)
check("en: 标题栏品牌英文化", "Deathwatch Patch Installer" in w.windowTitle())
check("en: bugfix 链接英文化", "Bug details" in fp.cards["bugfix"].detail_link.text())
check("en: 光影档位不含内部代号",
      all(("tier_" not in b._text.lower() and "g9" not in b._text.lower()
           and "g11" not in b._text.lower() and "g12" not in b._text.lower()
           and "g13" not in b._text.lower())
          for b in fp.variant_buttons))
check("en: 光影档位英文文案",
      fp.cards["visual"].variant_choice.current_key() == "tier_dgpu"
      and "Discrete" in fp.cards["visual"].current_variant_label("en")
      and "Tier details" in fp.cards["visual"].detail_link.text())
check("en: balance 链接英文化", "Numeric details" in fp.cards["balance"].detail_link.text())
dlg_en = m.BugDetailDialog(w)
check("en: Bug 详情弹窗展示英文清单", "Bug Fixes" in dlg_en.view.toPlainText())
dlg_en.close()
dlg_vis_en = m.BugDetailDialog(w, "visual")
check("en: 光影参数弹窗英文（玩家口吻，无内部键名）",
      "[Base] shared" in dlg_vis_en.view.toPlainText()
      and "vanilla is not a tier" in dlg_vis_en.view.toPlainText()
      and "frame cap 144" in dlg_vis_en.view.toPlainText()
      and "AsyncLoadingTimeLimit=" not in dlg_vis_en.view.toPlainText())
dlg_vis_en.close()
dlg_cam_en = fp.build_camera_warning()
check("en: 镜头提醒同语义", "hold R for 3 seconds" in dlg_cam_en.body.text())
dlg_cam_en.close()

# 13) stability under repeated interaction
for _ in range(3):
    w.set_lang("zh")
    w.set_lang("en")
    QTest.mouseClick(w.pages[1].restore_card, Qt.MouseButton.LeftButton)
    QTest.mouseClick(w.pages[1].install_card, Qt.MouseButton.LeftButton)
check("反复切换后模式=install", w.mode == "install" and w.accent == m.GREEN)
check("反复切换后默认策略仍正确", not w.pages[2].cards["zh_text"].isChecked())

# 14) ownership notice / English disclaimer
w.set_lang("zh")
own = w.pages[1].ownership.text()
check("正版声明含注册表 appid/build",
      str(m.REGISTRY["game_appid"]) in own and str(m.REGISTRY["game_buildid"]) in own)
check("正版声明含合法持有措辞", "合法持有" in own)
w.set_lang("en")
check("英文声明同样渲染版本号", str(m.REGISTRY["game_buildid"]) in w.pages[1].ownership.text())
check("英文免责声明为非官方声明",
      "not affiliated" in w.pages[0].body.text()
      and "does not bypass DRM" in w.pages[0].body.text())
check("英文安装页标题", "Locate" in w.pages[1].title.text())

# 15) Chinese voice DIY dialog: three essentials (TTS / directory / replacement)
w.set_lang("zh")
fp = w.pages[2]
dlg = fp.build_diy_dialog()
body = dlg.editor.toPlainText()
check("DIY 弹窗正文非空且来自资源文件", len(body) > 1500)
check("DIY ①TTS 选型 = IndexTTS 2.0",
      "IndexTTS 2.0" in body and "index-tts/index-tts" in body and "IndexTeam/IndexTTS-2" in body)
check("DIY ②目录含 Tyranid Invasion + Audio\\VO",
      "Tyranid Invasion" in body and "Content\\Audio\\VO" in body)
check("DIY ③替换方法：等长 + OUT-OF-BUDGET + 备份",
      "等长" in body and "OUT-OF-BUDGET" in body and "备份" in body)
check("DIY 提示词禁分发口径在",
      "不得对外分发" in body or "不外发" in body or "禁分发" in body)
check("DIY 弹窗按钮=复制/关闭",
      dlg.yesButton.text() == "复制提示词" and dlg.cancelButton.text() == "关闭")
w.set_lang("en")
body_en = fp.build_diy_dialog().editor.toPlainText()
check("DIY 英文版同样三要素", "IndexTTS 2.0" in body_en and "Tyranid Invasion" in body_en
      and "equal-length" in body_en and "OUT-OF-BUDGET" in body_en)

# 16) execution: page 3 button runs directly, page 4 shows the result
w.set_lang("zh")
fp = w.pages[2]
check("执行前默认：汉化/光影/Bug 勾选、镜头未勾选",
      fp.cards["zh_text"].isChecked() and fp.cards["visual"].isChecked()
      and fp.cards["bugfix"].isChecked() and not fp.cards["camera"].isChecked())
_orig_cam = fp.open_camera_warning
fp.open_camera_warning = lambda: None
try:
    QTest.mouseClick(fp.cards["camera"], Qt.MouseButton.LeftButton)
finally:
    fp.open_camera_warning = _orig_cam
check("勾选镜头限制解除后进入安装计划",
      "camera" in [a["id"] for a in w._build_plan()])
QTest.mouseClick(fp.cards["camera"], Qt.MouseButton.LeftButton)   # uncheck (no warning dialog)
_orig_vis16 = fp.open_visual_notice
fp.open_visual_notice = lambda: None
for cid in ("zh_text", "visual", "bugfix"):
    QTest.mouseClick(fp.cards[cid], Qt.MouseButton.LeftButton)
fp.open_visual_notice = _orig_vis16
check("全部取消：空计划仍可执行",
      not fp.selected() and w.next_btn.text() == "执行" and w.next_btn.isEnabled())
QTest.mouseClick(w.next_btn, Qt.MouseButton.LeftButton)
check("执行中：按钮变灰且文案=执行中…",
      w.running and not w.next_btn.isEnabled() and w.next_btn.text() == "执行中…")
check("执行中：上一步与语言切换禁用",
      not w.back_btn.isEnabled() and not w.lang_en.isEnabled())
for _ in range(100):
    QTest.qWait(100)
    if not w.running:
        break
check("执行完自动进入完成页", w.current() == 3 and not w.running)
log = w.pages[3].log.toPlainText()
check("完成页写备份目录", "备份目录" in log)
check("完成页不再出现预览/模拟口径",
      "模拟结果" not in log and "UI 预览" not in log)
check("完成页底部按钮=完成", w.next_btn.text() == "完成" and w.next_btn.isEnabled())
check("完成页上一步禁用（执行后不可回退）", not w.back_btn.isEnabled())
check("完成页下半=黑盾+作者",
      w.pages[3].cards["bonus_blackshield"].isVisible()
      and w.pages[3].author.isVisible())
check("完成页作者简介是 GitHub/Ko-fi 引导且无打赏/咖啡",
      "GitHub" in w.pages[3].section.author_desc.text()
      and "Ko-fi" in w.pages[3].section.author_desc.text()
      and "支持" in w.pages[3].section.author_desc.text()
      and "打赏" not in w.pages[3].section.author_desc.text()
      and "咖啡" not in w.pages[3].section.author_desc.text())
check("完成页含 GitHub + Ko-fi 作者链接且不含 B 站",
      "github.com/monkeyrs" in w.pages[3].section.links_label.text()
      and "ko-fi.com/monkeyrs" in w.pages[3].section.links_label.text()
      and "bilibili" not in w.pages[3].section.links_label.text().lower()
      and not w.pages[3].section.links_label.isHidden())
check("完成页含第三方许可入口且文件随包",
      ((m.APP_DIR / "THIRD_PARTY_NOTICES.md").is_file()
       or (m.APP_DIR.parent / "THIRD_PARTY_NOTICES.md").is_file())
      and not w.pages[3].section.notice_link.isHidden()
      and "第三方许可" in w.pages[3].section.notice_link.text())

# 16b) Black Shield offer: no save -> ask to return later; save present -> inject and lock
_notices = []
_orig_notice = m.NoticeDialog


class _FakeNotice:
    def __init__(self, win, title, body, danger=True):
        _notices.append((title, body))

    def exec(self):
        return 0


m.NoticeDialog = _FakeNotice
_orig_pre = patcher.blackshield_preflight
try:
    patcher.blackshield_preflight = lambda *a, **k: {
        "exists": False, "parse_ok": False, "path": "smoke://none",
        "reason": "save_not_found"}
    QTest.mouseClick(bs, Qt.MouseButton.LeftButton)
    check("无存档：合同卡回退为未勾选", not bs.isChecked())
    check("无存档：提示先打完教程再回来",
          bool(_notices) and _notices[-1][0] == "还没有存档"
          and "打完教程" in _notices[-1][1])
    patcher.blackshield_preflight = lambda *a, **k: {
        "exists": True, "parse_ok": True, "path": "smoke://save"}
    _orig_new, _orig_inject = patcher.new_session, patcher.inject_blackshield
    patcher.new_session = lambda *a, **k: type(
        "S", (), {"finalize": lambda self: None})()
    patcher.inject_blackshield = lambda sess, log=print: {
        "ok": True, "applied": 1, "skipped": 0, "errors": [], "detail": "smoke"}
    try:
        QTest.mouseClick(bs, Qt.MouseButton.LeftButton)
        check("有存档：注入成功、锁定合同卡",
              bs.isChecked() and bs._locked
              and "黑盾已加入存档" in w.pages[3].log.toPlainText())
        QTest.mouseClick(bs, Qt.MouseButton.LeftButton)
        check("注入后合同卡锁定、点击不再切换", bs.isChecked())
    finally:
        patcher.new_session, patcher.inject_blackshield = _orig_new, _orig_inject
finally:
    patcher.blackshield_preflight = _orig_pre
    m.NoticeDialog = _orig_notice

print("SMOKE PASS: %d/%d" % (len(PASS), len(PASS)))
for name in PASS:
    print("  ok -", name)
w.close()
