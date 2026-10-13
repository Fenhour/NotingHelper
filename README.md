# NotingHelper

NotingHelper（划词助手）是 Windows 上的本地阅读与记录工具，包含记录单词、记录问题和 Obsidian 高光三种功能。

桌面版本标识为 `2.3.0`，随附 Obsidian 桥接插件为 `2.5.4`。实际仓库运行的插件版本以重新加载后的插件为准。

## 功能

- **记录单词**：划选英文后点击记录入口，优先查本地 ECDICT，按需调用模型 API，勾选结果后写入单词库；支持 Markdown、TXT、JSON 和 DOCX。
- **记录问题**：将划定内容带入原文编辑区，补充概述、代码、图片、标签和来源链接后保存为 Markdown。原文保留空格与 Tab 缩进，补充与原文之间可拖动分隔条调整高度。
- **添加高光**：在 Obsidian 的 Markdown/PDF 内划选后点击颜色，插件保存定位记录并绘制高光浮层，源文件保持原样；支持注释、图片、标签、代码格式及按需导出。

划词、悬停入口不读写剪贴板。点击“记录问题”或“记录单词”后才读取内容：Obsidian 优先使用插件选区快照，外部应用或桥接无法提供选区时才复制。高光直接使用插件选区，全程不复制。

## 环境与启动

需要 Windows、Python 3（含 tkinter）及 Pillow。Obsidian 高光需要 Obsidian 桌面版和随附桥接插件。

```powershell
py -3 -m pip install Pillow
```

双击 `启动划词助手.bat`，或运行：

```powershell
py -3 word_picker.py
```

启动脚本会寻找已安装的 Python 及 `pythonw.exe`。排查启动错误时使用 `调试启动.bat`。

公开仓库包含 `assets/ui/icons/` 中当前程序使用的 18 个界面图标，用于导航、记录入口、模式切换、按钮与下拉箭头。角色动画、图标总览图、未使用图标及其他素材继续保留在本地，不上传；缺少角色素材时不显示动画，记录与高光功能仍可使用。

## 单词与 API

通过 `下载ECDICT词典.bat` 下载词典到本地 `data/`，或在设置中指定已有的 ECDICT 文件。词典与用户生成的数据不进入 Git。

模型 API 为按需补充功能，请在“配置 API”页填写自己的配置。密钥、个人设置和记录保存在本地，不应上传到仓库。

## Obsidian 配置与插件更新

记录问题、高光分别配置各自的 Obsidian 仓库。修改其中一套不会改变另一套；高光的单条导出与完整导出可指定不同的目的仓库。

点击“更新插件”后确认自动识别的目标仓库，也可以手动选择。随后在 Obsidian 的社区插件设置中启用或重新加载 `noting-helper-bridge`。更新会保留 `data.json`；复制文件不等于运行中的插件已经重新加载。

高光实时数据保存在来源仓库插件目录内的 `storage/highlights.jsonl`，图片在 `storage/imagehighlights/`。导出按需生成 Markdown 与图片副本。跨仓库导出要求来源库运行 2.5.1 或更高版本。

## 代码格式与侧栏筛选

在问题原文、内容补充或高光注释中先选中文字，再打开“代码块”菜单，选择自动推荐或具体语言。没有选区、或只选中空白时提示并保留正文。高光注释浏览代码菜单时保持原生选区高光，转换成功后才取消选区。

高光侧栏“当前文档”右边的颜色筛选提供“全部”及所有文档现有记录中的颜色，包含历史颜色。候选与配置页选出的三种入口颜色无关，可与浏览范围、文字搜索组合使用。

## 源码目录

- `word_picker.py`：桌面应用、记录与配置逻辑。
- `pipe_bridge.py`：本地桥接通信。
- `front/`：桌面界面、标签、悬浮入口与分隔条。
- `assets/ui/icons/`：程序实际使用的界面图标。
- `tools/`：启动、来源识别、文档转换、迁移及回归测试。
- `obsidian-plugin/noting-helper-bridge/`：Obsidian 插件源码、构建文件和使用说明。

插件已附带 `main.js`。修改插件源码后重新构建：

```powershell
cd obsidian-plugin/noting-helper-bridge
npm ci
npm run build
```

## 开发验证

```powershell
py -3 -m unittest test_features -q
py -3 tools/test_independent_config.py -q
py -3 tools/test_question_popup_layout.py -q
node tools/test_obsidian_plugin.js
node tools/test_obsidian_editor_dom.js
```

插件 DOM 测试需要先在插件目录执行 `npm ci`。部分 GUI 测试需要可用的桌面环境与随附的界面图标，真实应用的焦点、划选和视觉效果还需人工检查。

## 本地保留内容

`.claude/`、`log/`、`video/`、`docs/`、`data/`、`backup/`、开发素材工具、缓存、依赖目录及运行数据库均被排除。`assets/` 仅放行当前程序引用的 18 个界面图标，其余素材仍被排除。它们可以继续留在本地，公开仓库仅包含功能源码、必要脚本、回归测试、插件构建文件、运行所需图标和使用说明。
