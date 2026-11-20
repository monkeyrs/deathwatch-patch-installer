# -*- coding: utf-8 -*-
"""Bilingual resources and system language detection (Metro-style UI).

Language policy: follow the system locale. When the system is not Chinese,
Chinese text and Chinese voice options are not selected by default.
"""
import ctypes

S = {
    "brand": {"zh": "Deathwatch 补丁安装器", "en": "Deathwatch Patch Installer"},
    "skeleton_tag": {"zh": "v1.0 · 安装前自动备份", "en": "v1.0 · auto-backup before changes"},
    "lang_label": {"zh": "语言", "en": "Language"},
    "step_terms": {"zh": "协议", "en": "Terms"},
    "step_setup": {"zh": "安装", "en": "Setup"},
    "step_features": {"zh": "特性", "en": "Features"},
    "step_confirm": {"zh": "完成", "en": "Finish"},
    "step_count": {"zh": "步骤 {cur} / {total} · {name}", "en": "Step {cur} / {total} · {name}"},
    "disclaimer_title": {"zh": "用户协议与免责声明", "en": "Terms & Disclaimer"},
    "disclaimer_body": {
        "zh": (
            "本补丁是非官方、免费的社区作品，与 Games Workshop、Rodeo Games 及"
            "《Warhammer 40,000: Deathwatch – Enhanced Edition》的任何权利方均无隶属、合作、"
            "授权或背书关系。\n\n"
            "Warhammer 40,000、Deathwatch 及相关名称、标志、美术、音频与游戏数据归各自权利人所有。"
            "本补丁不包含游戏本体、破解文件或任何原版素材，只包含差异文件、翻译文本与作者自写工具。"
            "使用前请自备合法购买的正版（Steam AppID {appid}，Build {build}）。\n\n"
            "本补丁仅用于单机模式，不绕过 DRM，不提供盗版，不参与联机或商业分发。"
            "请勿将本补丁或其修改后的资产用于售卖、付费下载或二次打包收费；"
            "如果你为它付过钱，说明你被第三方骗了。\n\n"
            "安装前会在游戏目录内自动备份每个被改动的文件，并提供还原功能；"
            "涉及存档的功能会先备份存档，但还原存档可能丢失之后的游玩进度。"
            "请在安装前自行备份重要数据。\n\n"
            "本补丁按「现状」提供，不附带任何明示或暗示担保；游戏更新、其它模组冲突或误用"
            "都可能造成异常，作者不对由此产生的数据丢失或其它损失负责。"
            "使用即表示你理解并接受这些条件。\n\n"
            "它是免费发布的。如果你喜欢，最好的支持是反馈问题、分享截图，"
            "或通过 GitHub / Ko-fi 关注和支持作者的后续工作。"
        ),
        "en": (
            "This is an unofficial, free community patch. It is not affiliated with, authorized, "
            "or endorsed by Games Workshop, Rodeo Games, or any rights holder of "
            "Warhammer 40,000: Deathwatch – Enhanced Edition.\n\n"
            "Warhammer 40,000, Deathwatch and all related names, marks, art, audio and game data "
            "belong to their respective owners. This patch contains no game files, no cracks and no "
            "original assets — only diff files, translated text and tools written by the author. "
            "You must own a legitimate copy (Steam AppID {appid}, Build {build}).\n\n"
            "Single-player use only. It does not bypass DRM, does not provide piracy, and is not "
            "intended for multiplayer or commercial distribution. Do not sell it, put it behind a "
            "paywall, or repackage it for a fee; if you paid for it, you were scammed by a third party.\n\n"
            "Every modified file is backed up inside the game directory before changes and can be "
            "restored. Save-related features back up your save first, but restoring a save may lose "
            "progress made afterwards. Back up anything important before installing.\n\n"
            "Provided \"as is\", without any express or implied warranty. Game updates, conflicts with "
            "other mods or misuse may cause problems; the author is not liable for data loss or any "
            "other damage. Using it means you understand and accept these terms.\n\n"
            "It is released for free. If you enjoy it, the best support is reporting issues, sharing "
            "screenshots, or following/supporting the work via GitHub or Ko-fi."
        ),
    },
    "accept": {"zh": "我已阅读并接受上述条款", "en": "I have read and accept the terms"},
    "setup_title": {"zh": "选择游戏与操作", "en": "Locate your game & choose an action"},
    "exe_title": {"zh": "选择游戏安装路径", "en": "Locate your game"},
    "exe_hint": {
        "zh": "选择游戏的 DeathwatchGame.exe（根目录或 Binaries\\Win32 下均可），自动推导安装路径。",
        "en": "Pick DeathwatchGame.exe (game root or Binaries\\Win32). The install path is derived automatically.",
    },
    # Ownership notice: version values come from the registry keys game_appid/game_buildid;
    # do not hard-code numbers for the {appid}/{build} placeholders in the text.
    "path_ownership": {
        "zh": ("本工具仅适用于您本人合法持有的 Steam 正版 "
               "《Warhammer 40,000: Deathwatch – Enhanced Edition》"
               "（AppID {appid}，Build {build}）。目标文件与清单不符时，"
               "安装器会拒绝强改并报告；选中项已有备份时会先预还原。"),
        "en": ("This tool is for a legitimately owned Steam copy of "
               "Warhammer 40,000: Deathwatch – Enhanced Edition "
               "(AppID {appid}, Build {build}). Files that do not match the manifest are refused and reported; selected features with backups are pre-restored first."),
    },

    "browse": {"zh": "浏览…", "en": "Browse…"},
    "exe_filter": {"zh": "游戏 exe（DeathwatchGame*.exe）", "en": "Game exe (DeathwatchGame*.exe)"},
    "root_ok": {"zh": "安装根目录：", "en": "Install root: "},
    "root_bad": {
        "zh": "无法从该 exe 推导有效的游戏目录（需含 deathwatchgame/Content）。",
        "en": "Cannot derive a valid game directory from this exe (deathwatchgame/Content expected).",
    },
    "engine_pending": {
        "zh": "buildid 检测尚未实装；关键文件 hash 由各包清单在投放时校验，已安装项依赖 .dwpatch_backup/index.json 预还原。",
        "en": "buildid detection is not implemented yet; per-file hashes are checked from each package manifest, and applied features are pre-restored from .dwpatch_backup/index.json.",
    },
    "mode_title": {"zh": "你要做什么？", "en": "What would you like to do?"},
    "mode_install": {"zh": "打补丁", "en": "Install patches"},
    "mode_restore": {"zh": "还原到原版", "en": "Restore original"},
    "mode_install_desc": {
        "zh": "安装选中特性。改前自动备份。",
        "en": "Apply selected features. Auto-backup first.",
    },
    "mode_restore_desc": {
        "zh": "按全局备份索引还原到安装前状态；不覆盖存档。",
        "en": "Restore to the pre-install state from the global backup index; saves are not overwritten.",
    },
    "features_title": {"zh": "选择特性", "en": "Choose features"},
    "features_hint": {
        "zh": "点卡片即可勾选；灰色 = 暂未开放。勾选镜头卡时会先给出使用提醒。",
        "en": "Click a tile to toggle. Gray = coming soon. Selecting the camera tile shows a warning first.",
    },
    "bonus_title": {"zh": "Bonus 彩蛋（可选）", "en": "Bonus extras (optional)"},
    "bonus_hint": {
        "zh": "点击卡片接受合同；会先检查存档，再决定是否注入。",
        "en": "Click the tile to accept the contract; the save is checked before anything is injected.",
    },
    "bonus_hint_restore": {
        "zh": "当前为还原模式，不会注入额外内容；如需黑盾请先切回「打补丁」。",
        "en": "Restore mode does not inject extras; switch back to Install to add a Black Shield.",
    },
    "bs_nosave_title": {"zh": "还没有存档", "en": "No save yet"},
    "bs_nosave_body": {
        "zh": ("现在还没有游戏存档。建议先打完教程第一关，再回来运行本程序，"
               "接受这份黑盾合同。"),
        "en": ("There is no game save yet. Finish the tutorial / first chapter, then run this "
               "installer again to accept the Black Shield contract."),
    },
    "bs_fail_title": {"zh": "黑盾注入失败", "en": "Black Shield injection failed"},
    "camera_warning_title": {"zh": "镜头限制解除 · 谨慎", "en": "Camera Unlock · Caution"},
    # Chinese voice DIY dialog (nothing shipped or downloaded; the dialog lets users copy an agent prompt).
    "vo_diy_dialog_title": {"zh": "中文语音 · 本地 DIY 指南", "en": "Chinese voice · local DIY guide"},
    "vo_diy_dialog_note": {
        "zh": ("本补丁不下载、也不分发任何语音文件。把下面这段提示词整段复制，"
               "交给你本机的 AI 代理执行（需 NVIDIA 显卡，预计约 30 分钟）。"
               "生成的语音仅限本机自用，请勿对外分发。"),
        "en": ("Nothing is downloaded or shipped. Copy the prompt below and hand it to an AI "
               "agent on this PC (NVIDIA GPU required, ~30 minutes). Keep generated audio "
               "for personal use only — do not redistribute."),
    },
    "vo_diy_copy": {"zh": "复制提示词", "en": "Copy prompt"},
    "vo_diy_copied": {"zh": "已复制到剪贴板", "en": "Copied to clipboard"},
    "vo_diy_close": {"zh": "关闭", "en": "Close"},
    "vo_diy_link": {"zh": "查看提示词", "en": "View prompt"},
    "vo_diy_missing": {
        "zh": "提示词资源缺失（voice_diy_prompt_zh.txt 未随包找到）。",
        "en": "Prompt resource missing (voice_diy_prompt_zh.txt not found in package).",
    },
    "third_party_notices": {"zh": "第三方许可", "en": "Third-party notices"},
    "author_title": {"zh": "关于作者", "en": "About the author"},
    "author_desc": {
        "zh": ("这个补丁由一位喜欢拆游戏、写工具的独立开发者维护。"
               "问题反馈和后续更新在 GitHub：monkeyrs；"
               "如果它帮到了你，可以通过 Ko-fi 支持作者。"),
        "en": ("Maintained by an independent developer who enjoys taking games apart and "
               "writing tools. Issues and updates are on GitHub: monkeyrs; if it helped, "
               "you can support the work on Ko-fi."),
    },
    "summary_none": {"zh": "（未选择任何特性）", "en": "(no features selected)"},
    "exec_title": {"zh": "执行", "en": "Run"},
    "exec_running": {"zh": "执行中…", "en": "Running…"},
    "done_title": {"zh": "完成", "en": "Done"},
    "log_title": {"zh": "执行结果", "en": "Results"},
    "log_ok": {"zh": "{name} — 已安装", "en": "{name} — installed"},
    "log_restore": {"zh": "还原到原版（按备份）", "en": "Restore originals (from backups)"},
    "log_skip": {"zh": "{name} — 跳过（未选择）", "en": "{name} — skipped (not selected)"},
    "log_already": {"zh": "{name} — 已是最新", "en": "{name} — already up to date"},
    "log_failed": {"zh": "{name} — 失败", "en": "{name} — failed"},
    "log_preview": {
        "zh": "（以上为模拟结果，核心引擎未写入任何文件。）",
        "en": "(The results above are simulated; the core engine wrote no files.)",
    },
    "log_backup_dir": {"zh": "备份目录：{path}", "en": "Backup folder: {path}"},
    "log_no_changes": {
        "zh": "没有需要备份的改动（未创建备份目录）。",
        "en": "No changes needed a backup; no backup folder was created.",
    },
    "log_save_backup": {"zh": "存档备份：{path}", "en": "Save backup: {path}"},
    "log_pre_restore": {
        "zh": "选中项已有补丁：先还原 {restored} 个文件 / 删除 {deleted} 个新建文件，再按当前选择重打。",
        "en": "Selected features already had patches: pre-restored {restored} files / removed {deleted} created files, then re-applying the current selection.",
    },
    "log_restored": {"zh": "已还原 {n} 个文件。", "en": "Restored {n} files."},
    "log_restore_none": {"zh": "没有找到可还原的备份。", "en": "No backup found to restore."},
    "log_restore_save_kept": {
        "zh": "存档备份保留在 {path}，未自动还原（避免覆盖当前进度）。",
        "en": "The save backup is kept at {path} and was not restored automatically (to preserve current progress).",
    },
    "chain_locres": {"zh": "文本库 Game.locres", "en": "Text database (Game.locres)"},
    "chain_flairname": {"zh": "称号 / 战团名", "en": "Flair / chapter names"},
    "chain_asset_text": {"zh": "战斗 HUD 文本", "en": "Combat HUD text"},
    "chain_bfl": {"zh": "单位显示名", "en": "Unit display names"},
    "log_zh_missing": {
        "zh": "中文运行时数据缺失（zh_runtime.json 未随包找到）。",
        "en": "Runtime Chinese data is missing (zh_runtime.json not found in package).",
    },
    "log_save_missing": {
        "zh": "未检测到游戏存档：黑盾未注入。建议先打完教程，再回来运行本程序领取。",
        "en": "No game save found — Black Shield was not injected. Finish the tutorial, then run this installer again.",
    },
    "log_save_unreadable": {
        "zh": "存档存在但无法解析：黑盾未注入，原存档未改动。",
        "en": "The save exists but cannot be parsed — Black Shield was not injected; the save was left untouched.",
    },
    "log_save_already": {
        "zh": "存档中已有黑盾，未重复注入。",
        "en": "A Black Shield already exists in the save; nothing was injected.",
    },
    "log_save_ok": {
        "zh": "黑盾已加入存档（原存档已备份）。",
        "en": "Black Shield added to the save (original backed up).",
    },
    "log_save_failed": {"zh": "黑盾注入失败：{err}", "en": "Black Shield injection failed: {err}"},
    "bug_detail": {"zh": "Bug 详情", "en": "Bug details"},
    "bug_dialog_title": {"zh": "Bug 修正清单", "en": "Bug fix list"},
    "bug_dialog_missing": {"zh": "清单文件缺失。", "en": "The list file is missing."},
    "visual_detail": {"zh": "参数说明", "en": "Tier details"},
    "visual_notice_title": {"zh": "光影增强 · 档位参数", "en": "Visual Enhancement · Tier parameters"},
    "balance_detail": {"zh": "数值说明", "en": "Numeric details"},
    "camera_caution": {"zh": "谨慎", "en": "Caution"},
    "balance_dialog_title": {"zh": "武器平衡性补丁 · 数值说明", "en": "Weapon Balance Patch · Numeric details"},
    "next": {"zh": "下一步", "en": "Next"},
    "back": {"zh": "上一步", "en": "Back"},
    "finish": {"zh": "完成", "en": "Finish"},
    "cancel": {"zh": "取消", "en": "Cancel"},
}

STEP_KEYS = ["step_terms", "step_setup", "step_features", "step_confirm"]


def primary_lang_id(lang_id: int) -> str:
    """Map a Windows LANGID to 'zh' or 'en' using the primary language bits."""
    return "zh" if (lang_id & 0xFF) == 0x04 else "en"


def detect_system_lang() -> str:
    try:
        return primary_lang_id(ctypes.windll.kernel32.GetUserDefaultUILanguage())
    except Exception:
        return "en"


def t(key: str, lang: str) -> str:
    return S[key][lang]