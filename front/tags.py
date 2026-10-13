"""Obsidian 标签：解析、收集、补全。

两件事
------
1. **认出来**：按 Obsidian 的规则从文本里挑标签。规则里有几条容易漏，而且都会
   真的出现在笔记里，所以写在扫描器里而不是事后过滤：
   `#` 前必须是行首或空白（否则 `a#b` 会被误判）、`#` 后紧跟空白是 Markdown
   标题不是标签、整个标签必须含至少一个非数字字符（`#1984` 无效、`#y1984` 有效）、
   围栏代码块与行内代码里的 `#` 不算。
2. **别重名**：`#Pytorch` 与 `#pytorch` 是同一个标签。Obsidian 大小写不敏感、
   按首次创建的写法显示，这里照做 —— 这条归一化就是「防止同义不同名」的落点，
   补全列表里一个标签只出现一种写法，用户没有机会打出第二种。

补全弹层不抢焦点
----------------
弹层是 `wm_overrideredirect` 的无边框 Toplevel，且从不 `focus_set`：焦点始终留在
编辑框里，所以上下键与回车绑在编辑框上，弹层只负责显示。这样也避开了弹窗层已有的
`<BackSpace>` / `<Delete>` 绑定（见 word_picker._remove_question_image_from_key）——
按键根本不会落到列表控件上去。

不依赖 Pillow
-------------
这个模块只用 tkinter 与标准库，所以即使 Pillow 缺失、主题层整个退回旧界面，
标签的解析与写入仍然是对的（只是不高亮、不补全）。

配色从 theme.apply() 返回的 ui_colors 取，深浅主题跟着走，不写死颜色。
"""
from __future__ import annotations

import re
import tkinter as tk
from pathlib import Path

# Obsidian 允许标签里出现 `_` `-` `/`（`/` 用来做嵌套）以及字母、数字、CJK 等
# Unicode 字符；其余 ASCII 标点一律不可以。`#` 自己也不可以，标签得能当行首。
FORBIDDEN = set("'\"!$%&()*+,.:;<=>?@^`{|}~[]") | {"#"}
MAX_SUGGESTIONS = 8

_FENCE = re.compile(r"^\s*(?:```|~~~)")
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_PREFIX = re.compile(r"(?<!\S)#([^\s#]*)$")

# 扫库时跳过的目录：Obsidian 自己的配置、本项目存的来源副本，以及新仓库里
# NoteDataOfY/ 下的正文副本（正文是索引里那些行的副本，扫进去标签会数两遍）
SKIP_DIRS = {".obsidian", ".trash", "NotingHelper-Sources", "NoteDataOfY"}


# ---------------------------------------------------------------- 解析
def is_tag_char(char: str) -> bool:
    """这个字符能不能出现在标签里。"""
    return not char.isspace() and char not in FORBIDDEN


def normalize_tag(name: str) -> str:
    """清理成一个规范标签；不合法返回空串。

    写入磁盘前必须过这一关：一个带空格的「标签」写进标题行会把标题毁掉。
    """
    value = str(name).strip().lstrip("#").strip()
    if not value or value.startswith("/") or value.endswith("/"):
        return ""
    if not all(is_tag_char(char) for char in value):
        return ""
    if all(char.isdigit() for char in value):   # `#1984` 不是标签
        return ""
    return value


def scan_tags(text: str) -> list[tuple[int, int, str]]:
    """在原文里定位标签，返回 [(起点, 终点, 规范写法)]，按出现顺序。"""
    source = str(text)
    spans: list[tuple[int, int, str]] = []
    inside_fence = False
    line_start = 0
    for line in source.split("\n"):
        offset = line_start
        line_start += len(line) + 1
        if _FENCE.match(line):
            inside_fence = not inside_fence
            continue
        if inside_fence:
            continue
        blocked = [match.span() for match in _INLINE_CODE.finditer(line)]
        cursor = 0
        while True:
            start = line.find("#", cursor)
            if start < 0:
                break
            cursor = start + 1
            if start > 0 and not line[start - 1].isspace():
                continue                     # a#b 是正文，不是标签
            if any(low <= start < high for low, high in blocked):
                continue                     # `#标签` 在行内代码里，不算
            end = cursor
            while end < len(line) and is_tag_char(line[end]):
                end += 1
            cursor = max(end, cursor)        # 空标签（`# 标题`）时也要往前走
            tag = normalize_tag(line[start + 1:end])
            if tag:
                spans.append((offset + start, offset + end, tag))
    return spans


def extract_tags(text: str) -> list[str]:
    """取出文本里的标签，大小写归一后去重，保留首次出现的写法。"""
    found: list[str] = []
    seen: set[str] = set()
    for _start, _end, tag in scan_tags(text):
        key = tag.casefold()
        if key not in seen:
            seen.add(key)
            found.append(tag)
    return found


def strip_tags(text: str) -> str:
    """删掉标签本身，其余文字原样保留。

    标题用得到：标签由写入端统一拼到标题行末尾，标题正文里不该再留一份，
    否则同一个标签会写两遍。
    """
    source = str(text)
    for start, end, _tag in reversed(scan_tags(source)):
        source = source[:start] + source[end:]
    return re.sub(r"[ \t]{2,}", " ", source).strip()


def merge_tags(*groups) -> list[str]:
    """把几组标签并起来，大小写归一后去重，先出现的写法优先。"""
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for tag in group or ():
            cleaned = normalize_tag(tag)
            if not cleaned or cleaned.casefold() in seen:
                continue
            seen.add(cleaned.casefold())
            merged.append(cleaned)
    return merged


# ---------------------------------------------------------------- 收集
def _property_tags(text: str) -> list[str]:
    """读属性区（frontmatter）里 tags: 的值。

    Obsidian 的属性标签和正文标签是同一套标签，标签面板里一起显示；只扫正文
    会让这个列表比 Obsidian 少东西。
    """
    lines = str(text).split("\n")
    if not lines or lines[0].strip() != "---":
        return []
    block: list[str] = []
    for line in lines[1:]:
        if line.strip() == "---":
            break
        block.append(line)
    else:
        return []                            # 没有收尾的 ---，那只是正文里的一条横线
    tags: list[str] = []
    collecting = False
    for line in block:
        stripped = line.strip()
        if collecting:
            if stripped.startswith("- "):
                tags.extend(_as_tags(stripped[2:]))
                continue
            collecting = False
        if stripped.startswith("tags:"):
            rest = stripped[5:].strip()
            if rest:
                for piece in rest.strip("[]").split(","):
                    tags.extend(_as_tags(piece))
            else:
                collecting = True            # 换行写的 YAML 列表
    return tags


def _as_tags(piece: str) -> list[str]:
    """属性里的一项转成标签。属性里的标签一般不写 `#`，写了也要认。"""
    return extract_tags("#" + str(piece).strip().strip("'\"").lstrip("#"))


def scan_vault_tags(vault_path, limit: int = 400) -> list[str]:
    """扫一遍库里的 Markdown，按出现次数从多到少返回标签。

    频次排序是给补全列表用的：常用的排在前面，敲下 `#` 一眼就能看到。
    """
    root = Path(vault_path).expanduser()
    if not root.is_dir():
        return []
    try:
        candidates = list(root.rglob("*.md"))
    except OSError:
        return []
    counts: dict[str, int] = {}
    spellings: dict[str, str] = {}
    for path in candidates:
        try:
            folders = path.relative_to(root).parts[:-1]
        except ValueError:
            continue
        if any(folder in SKIP_DIRS or folder.startswith(".") for folder in folders):
            continue
        try:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        for tag in [*_property_tags(text), *extract_tags(text)]:
            key = tag.casefold()
            counts[key] = counts.get(key, 0) + 1
            spellings.setdefault(key, tag)
    order = sorted(counts, key=lambda key: (-counts[key], spellings[key].casefold()))
    return [spellings[key] for key in order[:limit]]


# ---------------------------------------------------------------- 编辑框支持
class TagTextSupport:
    """给一个 `tk.Text` 接上「`#标签` 高亮」与「打 `#` 弹已有标签」。

    高亮与补全都挂在同一个编辑框上，所以合成一个对象：只用一套按键绑定、
    一处刷新逻辑，拆成两个类只会互相打架。

    标签池用回调取（pool_provider）而不是拷一份列表进来：扫库是后台跑完才
    投事件回来的，扫完之后这个对象不用重建就能看到新标签。
    """

    def __init__(self, text: tk.Text, pool_provider, colors: dict, scale: float = 1.0):
        self.text = text
        self.pool_provider = pool_provider
        self.colors = colors
        self.scale = max(1.0, scale)
        self.tag_name = "nh-tag"
        self.popup: tk.Toplevel | None = None
        self.listbox: tk.Listbox | None = None
        self.matches: list[str] = []
        self.active = 0
        self.dismissed = False               # 按过 Esc，光标没离开这个词就不再弹

        text.tag_configure(self.tag_name, foreground=colors["accent"])
        text.bind("<KeyRelease>", self._on_key_release, add="+")
        text.bind("<<Modified>>", self._on_modified, add="+")
        text.bind("<Down>", lambda _event: self._move(1), add="+")
        text.bind("<Up>", lambda _event: self._move(-1), add="+")
        text.bind("<Return>", lambda _event: self._accept(), add="+")
        text.bind("<KP_Enter>", lambda _event: self._accept(), add="+")
        text.bind("<Tab>", lambda _event: self._accept(), add="+")
        text.bind("<Escape>", self._on_escape, add="+")
        text.bind("<ButtonRelease-1>", lambda _event: self._update_popup(), add="+")
        text.bind("<FocusOut>", lambda _event: self._hide(), add="+")
        text.bind("<MouseWheel>", lambda _event: self._hide(), add="+")
        text.bind("<Destroy>", lambda _event: self._hide(), add="+")
        text.after_idle(self._highlight)

    # -------------------------------------------------------- 高亮
    def _on_modified(self, _event=None) -> None:
        # <<Modified>> 每次文本变化都会来（打字、粘贴、撤销、程序插入都算）。
        # 复位标志本身还会再触发一次，所以用 _busy 挡住重入。
        if getattr(self, "_busy", False):
            return
        self._busy = True
        try:
            self.text.edit_modified(False)
            self._highlight()
        finally:
            self._busy = False

    def _highlight(self) -> None:
        if not self.text.winfo_exists():
            return
        self.text.tag_remove(self.tag_name, "1.0", "end")
        content = self.text.get("1.0", "end-1c")
        for start, end, _tag in scan_tags(content):
            self.text.tag_add(self.tag_name,
                              f"1.0 + {start} chars", f"1.0 + {end} chars")

    # -------------------------------------------------------- 补全
    def _caret_prefix(self) -> tuple[str, str] | None:
        """光标前若正好是一个 `#` 开头的词，返回 (那个 `#` 的位置, 已输入的部分)。

        返回的位置要立刻用 index() 定死，不能把 `insert - n chars` 这种表达式
        交出去：Tk 的索引是惰性求值的，而接受补全时先删后插，中间那一步会让
        `n chars` 落到别的字符上（行尾的标签会因此被插到行首）。
        """
        caret = self.text.index("insert")
        line_start = self.text.index(f"{caret} linestart")
        before = self.text.get(line_start, caret)
        match = _PREFIX.search(before)
        if not match:
            return None
        partial = match.group(1)
        if not all(is_tag_char(char) for char in partial):
            return None
        return self.text.index(f"{caret} - {len(partial) + 1} chars"), partial

    def _visible(self) -> bool:
        return bool(self.popup and self.popup.winfo_exists() and self.matches)

    def _on_key_release(self, event) -> None:
        if event.keysym == "Escape":
            return                           # 收起由 _on_escape 负责，这里别又弹开
        if event.keysym in {"Down", "Up"} and self._visible():
            return                           # 按下时 _move 已选中；抬起不能重置到首项
        self._update_popup()

    def _on_escape(self, _event=None):
        if not self._visible():
            return None
        self.dismissed = True
        self._hide()
        return "break"

    def _update_popup(self) -> None:
        located = self._caret_prefix()
        if not located:
            self.dismissed = False           # 光标离开这个词，下次打 # 正常弹
            self._hide()
            return
        if self.dismissed:
            return
        _start, partial = located
        needle = partial.casefold()
        pool = list(self.pool_provider() or ())
        # 写法完全一样的那个不列：标签池里有「正文里已经有的标签」，正在打的这个
        # 词就在正文里，不滤掉的话它会给自己当候选项。大小写不同要留着 —— 那种
        # 正是要提示的（打 `#pytorch` 提示库里的 `#Pytorch`）。
        self.matches = [
            tag for tag in pool
            if tag.casefold().startswith(needle) and tag != partial
        ][:MAX_SUGGESTIONS]
        if not self.matches:
            self._hide()
            return
        self.active = 0
        self._show()

    def _show(self) -> None:
        if not self.matches:
            self._hide()
            return
        if self.popup is None or not self.popup.winfo_exists():
            # 无边框 + 从不 focus_set：焦点留在编辑框，按键不会跑到列表上去
            self.popup = tk.Toplevel(self.text)
            self.popup.wm_overrideredirect(True)
            self.popup.attributes("-topmost", True)
            frame = tk.Frame(self.popup, background=self.colors["border"], bd=0)
            frame.pack()
            self.listbox = tk.Listbox(
                frame,
                font="TkDefaultFont",
                activestyle="none",
                exportselection=False,
                highlightthickness=0,
                borderwidth=0,
                background=self.colors["bg_primary"],
                foreground=self.colors["text_normal"],
                selectbackground=self.colors["accent_muted"],
                selectforeground=self.colors["accent"],
            )
            self.listbox.pack(padx=1, pady=1)
        self.listbox.delete(0, "end")
        for tag in self.matches:
            self.listbox.insert("end", f"#{tag}")
        self.listbox.configure(height=len(self.matches))
        self.listbox.selection_clear(0, "end")
        self.listbox.selection_set(self.active)

        box = self.text.bbox("insert")
        if not box:                          # 光标滚出可视区了，不摆
            self._hide()
            return
        self.popup.update_idletasks()
        x = self.text.winfo_rootx() + box[0]
        y = self.text.winfo_rooty() + box[1] + box[3]
        height = self.popup.winfo_reqheight()
        if y + height > self.text.winfo_screenheight():
            y = max(0, self.text.winfo_rooty() + box[1] - height)   # 放下方不够就翻上去
        self.popup.geometry(f"+{x}+{y}")

    def _hide(self) -> None:
        self.matches = []
        self.listbox = None
        if self.popup is not None:
            try:
                if self.popup.winfo_exists():
                    self.popup.destroy()
            except tk.TclError:
                pass
            self.popup = None

    def _move(self, delta: int):
        if not self._visible():
            return None                      # 没有补全时上下键就是正常移动光标
        self.active = (self.active + delta) % len(self.matches)
        self.listbox.selection_clear(0, "end")
        self.listbox.selection_set(self.active)
        self.listbox.see(self.active)
        return "break"

    def _accept(self):
        if not self._visible():
            return None                      # 没有补全时回车还是正常换行
        located = self._caret_prefix()
        if not located:
            self._hide()
            return None
        start, _partial = located
        tag = self.matches[self.active]
        # 换成池子里的规范写法，然后再补一个空格：光标停在词外，弹层不会立刻又开
        self.text.delete(start, "insert")
        self.text.insert(start, f"#{tag} ")
        self._hide()
        return "break"

    def destroy(self) -> None:
        self._hide()
