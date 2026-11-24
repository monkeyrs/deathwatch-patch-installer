# Deathwatch Patch Installer · 项目说明

面向 Steam 版 *Deathwatch*（AppID 394450，Build 1837603）的社区补丁安装器。安装器只对你本机已有的正版游戏做**备份、计算、写入、还原**，不分发游戏本体或游戏素材。

当前版本：`v1.4`。源码许可：GPL-3.0。

## 1. 目标

- 提供一个 Windows 单文件 exe，最终用户无需安装 Python、无需联网。
- 中英双语界面，按系统语言自动选择，也可手动切换。
- 所有改动基于规则 / recipe，对用户原版文件就地计算；写入前逐文件备份。
- 支持还原到原版；备份和日志只写在游戏目录内。

## 2. 功能范围

| 功能 | 说明 | 默认 |
| --- | --- | --- |
| 中文文本 | 界面、称号、战团标签等中文文本，安装时由映射表生成；不直接分发 `.locres` | 系统语言为中文时勾选 |
| Bug 修正 | 8 项已验证修复，详见 `src/bugfix_list_zh.md` / `src/bugfix_list_en.md` | 勾选 |
| 光影增强 | 核显 / 独显 / 最高三档配置级增强；原版通过「还原到原版」恢复 | 独显 |
| 镜头限制解除 | 解除镜头高度 / 俯仰限制，并给出使用提醒 | 不勾选 |
| 武器平衡 | 已实机验证的武器数值调整（当前 r30，42 OK 槽）+ 三件 T2 战团武器授予技能，可还原 | 不勾选 |
| 武器技能 | 恢复三件 T2 战团武器的原版赋予技能与军械库图标 | 不勾选 |
| Bonus：黑盾 | 向存档加入一名黑盾；注入前自动备份存档 | 完成页手动触发 |

不包含中文配音，也不下载中文配音；界面只提供本地 DIY 提示词。

## 3. 交互流程

1. **免责声明**：阅读并接受后才可继续。
2. **路径与模式**：选择游戏 exe，自动推导游戏根目录并校验 Build 1837603；选择「打补丁」或「还原到原版」。
3. **特性**：勾选需要的项目；光影选择档位；镜头与武器平衡给出风险提示。
4. **完成**：显示执行结果、备份目录；黑盾 Bonus 在完成页单独触发。

## 4. 数据与备份

- 工作目录：`<游戏根>/.dwpatch_backup/`。
- 每次写入前，把原文件复制到 `.dwpatch_backup/<UTC>/files/<相对路径>`。
- `index.json` 记录跨多次安装的最早原文件，还原始终以最早备份为准。
- SHA-256 状态检测区分原版 / 已打 / 部分打 / 未知版本；重打前按选中项预还原。
- 存档注入前备份 `dw_metagame.save`，并做回读校验。还原存档会丢失注入之后的游玩进度，界面会显式提示。

## 5. 架构

- `src/main.py`：PySide6 安装器入口。
- `src/ui_kit.py`：本地控件层，不依赖第三方 UI 组件库。
- `src/patcher.py`：备份 / 状态检测 / recipe 执行 / 还原引擎。
- `src/i18n.py`：中英双语资源。
- `src/features.json`：特性注册表，是功能清单和包来源的唯一数据源。
- `src/payload/`：仅包含 manifest / recipe，不含游戏资源文件。
- `src/zhgen/`：中文文本与资产运行时生成器。
- `src/saveinject/`：存档解析与注入。
- `tools/`：维护者工具；`tests/smoke_test.py`：离屏烟雾测试。

载荷分为三类：

- **规则类**：对原文件做确定性等长变换，只带规则不带改后二进制。
- **生成类**：中文文本由映射表在用户本机运行时生成，不携带原版 `.locres`。
- **配置类**：光影三档从用户原版配置计算，不携带完整 `.ini`。

## 6. 技术选型

- Python 3.13
- PySide6 6.12.0
- PyInstaller 6.22.3，`--onefile` 打包
- openpyxl 仅用于维护者工具

当前构建入口：`build.ps1`；产物：`dist/DeathwatchPatchInstaller.exe`。spec 会裁掉未使用的 Qt 模块；如需恢复模块，修改 `DeathwatchPatchInstaller.spec` 的 `_EXCLUDE_PREFIXES`，不要向 payload 添加游戏资源。

## 7. 验收

- `tests/smoke_test.py` 使用 `QT_QPA_PLATFORM=offscreen`，不弹窗、不写真实游戏目录。
- 发布前检查：Build 校验、备份索引、还原流程、中英双语文案、单文件 exe 启动、第三方许可证随包。
- 分发包内不得出现任何未改动的原版游戏文件或游戏素材。

## 8. 合规与分发边界

- 本项目是非官方社区 MOD，与游戏发行商、开发商、Steam 无任何关联或背书关系；相关商标与版权归各自权利人所有。
- 只面向**用户本人合法持有**的 Steam 正版游戏；不提供游戏本体、序列号、破解或绕过正版验证的能力。
- 不修改启动校验 / DRM 组件；不随包提供 CODEX、Steam 模拟器、注入器或 ReShade。
- 不分发 `.uasset`、`.locres` 原文件、音频、贴图、字体、完整配置文件或其他游戏素材。
- 中文配音不在分发范围内；相关能力仅保留为本地 DIY 提示。
- 本仓库代码按 [GPL-3.0](../LICENSE) 提供；第三方组件许可见 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) 与 `licenses/`。
- 软件按“现状”提供；使用前请自行备份游戏和存档。项目不对游戏内容授予任何权利。

## 9. 文档入口

- [README（English）](../README.md)
- [README（简体中文）](lang/README.zh-CN.md)
- [Third-party notices](../THIRD_PARTY_NOTICES.md)
- [Feature registry](../src/features.json)
