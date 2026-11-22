# -*- mode: python ; coding: utf-8 -*-
"""真实安装器构建（含全部载荷）。与 Demo spec 的唯一区别：随包 app/payload 且不写 demo_mode.txt。

Payload: 8 bugfix packages + 3 visual tiers + weapon skills + balance r22
+ zh_runtime.json (generated at install) + camera_rules.json (equal-length camera edits).
All source paths are absolutized via SPECPATH: PyInstaller resolves datas/pathex relative to the CWD.
"""
from pathlib import Path

HERE = Path(SPECPATH).resolve()
APP = HERE / "src"

a = Analysis(
    [str(APP / "main.py")],
    pathex=[str(APP), str(APP / "zhgen"), str(APP / "saveinject")],
    binaries=[],
    datas=[
        (str(APP / "features.json"), '.'),
        (str(APP / "support.json"), '.'),
        (str(APP / "voice_diy_prompt_zh.txt"), '.'),
        (str(APP / "voice_diy_prompt_en.txt"), '.'),
        (str(APP / "bugfix_list_zh.md"), '.'),
        (str(APP / "bugfix_list_en.md"), '.'),
        (str(APP / "balance_list_zh.md"), '.'),
        (str(APP / "balance_list_en.md"), '.'),
        (str(APP / "weapon_skills_list_zh.md"), '.'),
        (str(APP / "weapon_skills_list_en.md"), '.'),
        (str(APP / "visual_list_zh.md"), '.'),
        (str(APP / "visual_list_en.md"), '.'),
        (str(APP / "zh_runtime.json"), '.'),
        (str(APP / "camera_rules.json"), '.'),
        (str(APP / "payload"), 'payload'),
        (str(HERE / "THIRD_PARTY_NOTICES.md"), '.'),
        (str(HERE / "licenses"), 'licenses'),
    ],
    hiddenimports=[
        'save_inject', 'save_game_probe', 'save_anatomy',
        'zh_runtime', 'locres_v3', 'ue4prop', 'uasset_mutate', 'asset_recipes',
        'asset_text_lexicon', 'flairname_lexicon',
        'bfl_display_string_patch', 'bp_bytecode',
    ],
    hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[],
    noarchive=False, optimize=0,
)

# 方案 A：保留 PySide6/Qt，但裁掉当前 Qt Widgets UI 未用到的 Qt 模块/插件。
# 只按归档目标路径过滤，不碰 app/payload 资源。
_EXCLUDE_PREFIXES = (
    "pyside6/opengl32sw.dll",
    "pyside6/qt6quick.dll", "pyside6/qtquick.pyd",
    "pyside6/qt6qml.dll", "pyside6/qtqml.pyd",
    "pyside6/qt6qmlmodels.dll", "pyside6/qtqmlmodels.pyd",
    "pyside6/qt6qmlmeta.dll", "pyside6/qt6qmlworkerscript.dll",
    "pyside6/qt6pdf.dll", "pyside6/qtpdf.pyd",
    "pyside6/plugins/imageformats/qpdf.dll",
    "pyside6/qt6network.dll", "pyside6/qtnetwork.pyd",
    "pyside6/qt6svg.dll", "pyside6/qtsvg.pyd",
    "pyside6/qt6opengl.dll", "pyside6/qtopengl.pyd",
    "pyside6/plugins/iconengines/qsvgicon.dll",
    "pyside6/plugins/imageformats/qsvg.dll",
    "pyside6/plugins/imageformats/qtiff.dll",
    "pyside6/plugins/networkinformation/",
    "pyside6/plugins/tls/",
    "pyside6/translations/",
)


def _keep_archive_entry(entry):
    dest = str(entry[0]).replace("\\", "/").lower()
    return not any(dest.startswith(p) for p in _EXCLUDE_PREFIXES)


a.binaries = [x for x in a.binaries if _keep_archive_entry(x)]
a.datas = [x for x in a.datas if _keep_archive_entry(x)]

pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name='DeathwatchPatchInstaller',
    debug=False, bootloader_ignore_signals=False, strip=False, upx=True,
    upx_exclude=[], runtime_tmpdir=None, console=False,
    disable_windowed_traceback=False, argv_emulation=False,
    target_arch=None, codesign_identity=None,
    entitlements_file=None,
)
