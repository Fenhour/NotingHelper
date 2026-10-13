"""划词后浮在选区结尾的模式图标入口。

展示层与业务层分离：图标/色块的悬停和点击由 word_picker.py 注入回调。
同一个无边框 Toplevel 承载图标及展开区，避免跨窗口指针间隙；离开时
复查物理指针位置，不能靠猜测性计时器。高光模式的圆形色块依次弹出；
记录模式上方为较大的文字按钮，左键进入记录、右键只切换模式。屏幕下沿时
色块改向上展开，仍维持图标原位。动画定时器只驱动视觉呈现，
退出/重开必须取消，绝不拿它判断是否还在悬停。
"""
from __future__ import annotations

import tkinter as tk

# 图标本体与内边距（逻辑像素），以及相对指针的偏移
DEFAULT_ICON_SIZE = 22
DEFAULT_PAD = 5
DEFAULT_OFFSET = (10, 12)
# 图标与展开内容之间的间隔
GAP = 6
# 动作按钮里的小图标边长，以及按钮的水平内边距
ACTION_ICON_SIZE = 16
ACTION_PAD = 9
# 主按钮（记录问题 / 记录单词）与模式分段条里的图标边长
PRIMARY_ICON_SIZE = 20
MODE_ICON_SIZE = 18
# 模式条里药丸的内边距与间隔。药丸的内边距只能走 pack 的 ipadx/ipady：Label 的
# padx/pady 在只显示图片时会被 Tk 忽略。下面两处估尺寸的地方也照这几个数算，
# 不然窗口贴屏幕边缘时翻向会算错。
MODE_PILL_PAD_X = 11
MODE_PILL_PAD_Y = 5
MODE_CELL_GAP_X = 2
MODE_CELL_GAP_Y = 3
MODE_BAR_HEIGHT = (MODE_ICON_SIZE + MODE_PILL_PAD_Y * 2 + MODE_CELL_GAP_Y * 2 + 1)
# flash() 那一项的 key。它只用来占位，不会从 on_pick 回来（提示项点不动）
FLASH_KEY = "__flash__"


class FloatingEntry:
    """划词的快捷入口：无边框、置顶、不抢焦点。

    回调（都由 `word_picker.py` 注入）：

    - ``on_hover()``        指针进入入口
    - ``on_context_menu(x, y)``  在物理屏幕坐标处弹右键菜单
    - ``on_pick(key)``      点了某个选项（颜色块或动作按钮）
    """

    def __init__(self, root, icons, *, scale=1.0, colors,
                 icon_name="pen-line", icon_size=DEFAULT_ICON_SIZE,
                 icon_color="#5c5c5c", offset=DEFAULT_OFFSET,
                 on_hover=None, on_context_menu=None,
                 on_pick=None, on_click=None, mode_options=(),
                 active_mode=None, on_mode_select=None):
        self.root = root
        self.icons = icons
        self.scale = max(1.0, scale)
        self.colors = colors
        self.icon_name = icon_name
        self.icon_size = icon_size
        self.icon_color = icon_color
        self.offset = offset
        self.on_hover = on_hover
        self.on_context_menu = on_context_menu
        self.on_pick = on_pick
        self.on_click = on_click
        self.mode_options = tuple(mode_options)
        self.active_mode = active_mode
        self.on_mode_select = on_mode_select
        self._mode_photos: list = []
        self._mode_cells: dict = {}
        self.mode_bar: tk.Frame | None = None
        # 主按钮上那个模式图标：必须留引用，否则 Tk 会把图丢掉
        self._primary_photo = None

        # 图标锚点（物理像素）：期望的左上角，展开时图标停在这里不动
        self._anchor: tuple[int, int] | None = None
        self._pointer: tuple[int, int] = (0, 0)
        self._hovered = False
        self._expanded_left = False
        self._item_photos: list = []
        # 当前展开内容的内容指纹，用来判断展开时要不要重建（见 expand_items）
        self._item_spec: list = []
        # 展开区里「底色跟着悬停状态走」的控件：键是选项容器，值是它连同内部
        # 那几件一起变底色的控件。色块本体不在此列——它有自己的颜色。
        self._bg_followers: dict = {}
        # flash() 的自动收起定时器
        self._flash_after: str | None = None
        # 图标与选项当前的排布 (是否含选项, 是否左展开)，None 表示还没排过
        self._row_state: tuple[bool, bool] | None = None
        # 展开内容的宽度缓存，None 表示还没量过（见 _strip_span）
        self._strip_span_cache: int | None = None
        self._vertical = False
        self._vertical_up = False
        self._swatch_cells: list = []
        self._reveal_after: list = []

        self.window: tk.Toplevel | None = None
        self._border: tk.Frame | None = None
        self._row: tk.Frame | None = None
        self.icon_label: tk.Label | None = None
        self.strip: tk.Frame | None = None

    # ------------------------------------------------------------ 尺寸换算
    def _px(self, value: float) -> int:
        return max(1, int(round(value * self.scale)))

    @property
    def icon_span(self) -> int:
        """图标窗口的边长（物理像素，含内边距）。"""
        if self.active_mode in {"question", "translate"}:
            return self._px(48)
        return self._px(self.icon_size) + self._px(DEFAULT_PAD) * 2

    @property
    def entry_width(self) -> int:
        return self._px(180) if self.active_mode in {"question", "translate"} else self.icon_span

    # ------------------------------------------------------------ 生命周期
    def _build(self) -> None:
        if self.window is not None and self.window.winfo_exists():
            return
        window = tk.Toplevel(self.root)
        self.window = window
        # 无边框 + 置顶 + 从不 focus_set：入口不该抢走用户正在编辑的焦点
        window.wm_overrideredirect(True)
        window.attributes("-topmost", True)
        window.configure(background=self.colors["border"])

        border = tk.Frame(window, background=self.colors["border"], bd=0)
        border.pack(fill="both", expand=True)
        self._border = border
        row = tk.Frame(border, background=self.colors["bg_primary"], bd=0)
        row.pack(padx=1, pady=1)          # 1px 描边，照 front/tags.py 的补全弹层
        self._row = row
        if self.mode_options:
            # 主按钮和模式条底色相同，中间拉一条 1px 分隔线，两块才有层次，
            # 不然看起来像一整块卡片上飘着两组图标。
            separator = tk.Frame(border, background=self.colors["border"], bd=0,
                                 height=self._px(1))
            separator.pack(fill="x", padx=1)
            bar = tk.Frame(border, background=self.colors["bg_primary"], bd=0)
            bar.pack(fill="x", padx=1, pady=(0, 1))
            self.mode_bar = bar
            self._build_mode_bar()

        pad = self._px(DEFAULT_PAD)
        photo = None
        if self.icons is not None:
            photo = self.icons.get(self.icon_name, self.icon_size, self.icon_color)
        self._icon_photo = photo            # 必须留引用，否则 Tk 会把图丢掉

        label_kw = {"background": self.colors["bg_primary"], "bd": 0,
                    "highlightthickness": 0, "padx": pad, "pady": pad}
        if photo is not None:
            label_kw["image"] = photo
        else:                               # 素材缺失时不留空位，退成文字
            label_kw["text"] = "✎"
            label_kw["foreground"] = self.icon_color
        self.icon_label = tk.Button(row, **label_kw, relief="flat", cursor="hand2",
                                    takefocus=False, activebackground=self.colors.get("bg_active", self.colors["bg_primary"]))
        self._configure_primary_action()

        strip = tk.Frame(row, background=self.colors["bg_primary"], bd=0)
        self.strip = strip
        strip.pack_forget()                 # 默认是收起的
        self._item_spec = []                # 新窗口里没有选项
        self._bg_followers = {}
        self._row_state = None              # 新控件还没排过
        self._strip_span_cache = None

        for widget in (window, border, row, self.icon_label):
            widget.bind("<Enter>", self._on_enter, add="+")
            widget.bind("<Leave>", self._on_leave, add="+")
        # Tk bindtags 会把 Label 的事件继续派发到 Toplevel。只绑定一次右键；
        # 同时绑 window 与 icon_label 会触发两遍，先开记录再弹切换卡。
        window.bind("<Button-3>", self._on_right_click, add="+")
        self.icon_label.bind("<Button-1>", self._on_icon_click, add="+")
        window.bind("<Destroy>", self._on_destroy, add="+")

    def destroy(self) -> None:
        self._cancel_reveal()
        self._cancel_flash()
        window = self.window
        self.window = None
        self._border = self._row = self.icon_label = self.strip = None
        self.mode_bar = None
        self._mode_photos = []
        self._mode_cells = {}
        self._primary_photo = None
        self._icon_photo = None
        self._item_photos = []
        self._item_spec = []
        self._bg_followers = {}
        self._row_state = None
        self._strip_span_cache = None
        self._anchor = None
        self._hovered = False
        if window is not None:
            try:
                if window.winfo_exists():
                    window.destroy()
            except tk.TclError:
                pass

    def _on_destroy(self, event=None) -> None:
        if event is not None and event.widget is not self.window:
            return
        self._flash_after = None            # 窗口没了，那个定时器也无所谓了
        self.window = None
        self._border = self._row = self.icon_label = self.strip = None
        self.mode_bar = None
        self._mode_photos = []
        self._mode_cells = {}
        self._primary_photo = None
        self._item_spec = []                # 窗口没了，选项也就没了
        self._bg_followers = {}
        self._row_state = None
        self._strip_span_cache = None

    # ------------------------------------------------------------ 显示 / 收起
    def show(self, pointer_x: int, pointer_y: int) -> None:
        """在选区结尾浮现。参数是**物理像素**的屏幕坐标（鼠标抬起点）。"""
        self._build()
        self._pointer = (pointer_x, pointer_y)
        self._expanded_left = False
        self.collapse()
        self._anchor = self._icon_anchor(pointer_x, pointer_y)
        self._apply_geometry()
        self.window.deiconify()
        self.window.lift()
        # 映射时指针应当在窗口外。真在里面（极端的多显示器坐标）就当场认下悬停，
        # 免得用户必须先把指针移出去再移回来才能触发。
        if self.contains(pointer_x, pointer_y):
            self._hovered = True
            self._notify_hover()

    def hide(self) -> None:
        self._cancel_reveal()
        self._cancel_flash()
        self._hovered = False
        self.collapse()
        window = self.window
        if window is None:
            return
        try:
            if window.winfo_exists():
                window.withdraw()
        except tk.TclError:
            pass

    def is_visible(self) -> bool:
        if self.window is None:
            return False
        try:
            return bool(self.window.winfo_exists()) and self.window.state() != "withdrawn"
        except tk.TclError:
            return False

    def _icon_anchor(self, pointer_x: int, pointer_y: int) -> tuple[int, int]:
        """算出图标左上角。容不下就翻到指针的另一侧，保证指针落在窗口外。"""
        # 没有主按钮时卡片就是模式条的宽度（高光模式下就是这样），按药丸尺寸估：
        # 每格 = 图标 + 左右内边距 + 间隔，再留一点描边余量。
        pill = self._px(MODE_ICON_SIZE + MODE_PILL_PAD_X * 2 + MODE_CELL_GAP_X * 2)
        width = max(self.entry_width, pill * len(self.mode_options) + self._px(4))
        height = self.icon_span + (self._px(MODE_BAR_HEIGHT) if self.mode_options else 0)
        off_x, off_y = self.offset
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()

        x = pointer_x + off_x
        if x + width > screen_w - 2:                 # 右边放不下 → 翻到指针左侧
            x = pointer_x - width - off_x
        x = max(2, min(x, screen_w - width - 2))

        y = pointer_y + off_y
        if y + height > screen_h - 2:                 # 下边放不下 → 翻到指针上方
            y = pointer_y - height - off_y
        y = max(2, min(y, screen_h - height - 2))
        return x, y

    def _apply_geometry(self) -> None:
        """按当前是否展开摆好窗口。展开时图标本体的屏幕位置不动。

        这里**不**调 update_idletasks：它只在布局脏的时候有用，而每次展开/收起
        都会把布局弄脏，于是每次指针扫过入口都要多花 ~15 ms 去同步刷一遍（实测
        一次进出 58 ms，几乎全在这上面）。摆放只用到 icon_span、_strip_span 和屏
        幕尺寸，都不依赖那次刷新——窗口尺寸交给 Tk 在空闲时自己算。
        """
        window = self.window
        if window is None or self._anchor is None:
            return
        icon_ax, ay = self._anchor
        if self._vertical:
            base_height = self.icon_span + (self._px(MODE_BAR_HEIGHT) if self.mode_options else 0)
            height = base_height
            if self.strip is not None and self.strip.winfo_manager():
                self.strip.update_idletasks()
                height += self._px(GAP) + self.strip.winfo_reqheight()
            top = max(2, ay + base_height - height) if self._vertical_up else ay
            window.geometry(f"+{icon_ax}+{top}")
            return
        extra = 0
        if self._expanded_left or (self.strip is not None and self.strip.winfo_manager()):
            extra = self._strip_span()
        total = self.entry_width + (extra + self._px(GAP) if extra else 0)
        screen_w = self.root.winfo_screenwidth()

        # 右边放不下展开内容 → 往左展开，图标仍然停在锚点
        wants_left = bool(extra) and icon_ax + total > screen_w - 2 and icon_ax - extra >= 2
        if wants_left != self._expanded_left:
            self._expanded_left = wants_left
            # 展开方向变了就必须重排：选项在左展开时要排在图标前面，否则窗口整体
            # 左移之后图标会跟着跑到左边，不再停在锚点上。
            self._rebuild_row(with_strip=bool(extra))
        if wants_left:
            window.geometry(f"+{icon_ax + self.entry_width - total}+{ay}")
        else:
            window.geometry(f"+{icon_ax}+{ay}")

    def _strip_span(self) -> int:
        """展开内容的宽度（物理像素）。

        量一次就记住：选项内容不变时宽度也不变（内容变时 expand_items 会把
        缓存清掉）。winfo_reqwidth 要先把布局刷干净才准，而刷一次十几毫秒，不能
        每次指针扫过都付。
        """
        if self.strip is None:
            return 0
        if self._strip_span_cache is None:
            self.strip.update_idletasks()
            self._strip_span_cache = int(self.strip.winfo_reqwidth())
        return self._strip_span_cache

    # ------------------------------------------------------------ 悬停展开
    def _rebuild_row(self, with_strip: bool) -> None:
        """把图标和展开内容按顺序重新 pack。左展开时选项排在图标前面。

        顺序没变就直接返回。pack_forget + pack 会把布局弄脏，紧接着
        _apply_geometry 里的 update_idletasks 就得真算一遍——实测一次要点掉十几
        毫秒，而指针每次扫过入口都会走这条路。
        """
        if self._row is None or self.icon_label is None or self.strip is None:
            return
        state = (bool(with_strip), bool(self._expanded_left), self._vertical, self._vertical_up)
        if state == self._row_state:
            return
        self._row_state = state
        for widget in (self.icon_label, self.strip):
            widget.pack_forget()
        gap = self._px(GAP)
        if self._vertical:
            if with_strip and self._vertical_up:
                self.strip.pack(side="top", pady=(0, self._px(GAP)))
            self.icon_label.pack(side="top")
            if with_strip and not self._vertical_up:
                self.strip.pack(side="top", pady=(self._px(GAP), 0))
        elif with_strip and self._expanded_left:
            self.strip.pack(side="left", padx=(gap, 0))
            self.icon_label.pack(side="left")
        elif with_strip:
            self.icon_label.pack(side="left")
            self.strip.pack(side="left", padx=(gap, 0))
        else:
            self.icon_label.pack(side="left")

    def expand_items(self, items) -> None:
        """展开一组可点击选项。每项是一个 dict，两种外观：

        - ``{"key": …, "kind": "swatch", "color": "#rrggbb"}`` —— 颜色块
        - ``{"key": …, "kind": "action", "label": "记录问题", "icon": "…"}``
          —— 「图标 + 文字」的动作按钮，``icon`` 可以省略

        ``key`` 原样交给 ``on_pick(key)``，这里不解释它的含义（和 `front/actor.py`
        一样，展示层不含业务判断）。

        选项只在内容真的变了的时候才重建。原来每次进入入口都先销毁、再新建这六
        个 Frame，而 Tk 的 Frame 在 Windows 上各是一个 HWND——建窗口本身很贵，
        实测一次进出要 56 ms（p50），指针扫过入口就能感到界面顿一下。内容没变
        时只做 pack_forget/pack，实测约 2 ms。

        指纹里**必须**带上 label 和 kind：只比 key 的话，「同一个 key 换了文案」
        不会重建，界面上就留着上一次的旧文字。
        """
        if self.window is None or self.strip is None:
            return
        spec = [self._item_fingerprint(item) for item in items]
        vertical = bool(items) and all(str(item.get("kind")) == "swatch" for item in items)
        changed = spec != self._item_spec or vertical != self._vertical
        if changed:
            self._cancel_reveal()
            self._vertical = vertical
            self._vertical_up = bool(vertical and self._anchor and
                self._anchor[1] + self.icon_span + (self._px(34) if self.mode_options else 0) + len(items) * self._px(50)
                    > self.root.winfo_screenheight() - 2)
            self._row_state = None
            self._swatch_cells = []
            for child in self.strip.winfo_children():
                child.destroy()
            self._item_photos = []
            self._bg_followers = {}
            for item in items:
                if str(item.get("kind", "action")) == "swatch":
                    self._build_swatch(item)
                else:
                    self._build_action(item)
            self._item_spec = spec
            self._strip_span_cache = None       # 内容变了，宽度要重新量
            self._apply_entry_background()      # 新控件要先穿上当前底色
        self._rebuild_row(with_strip=True)
        reveal = vertical and (changed or not any(cell.winfo_manager()
            for cell in self._swatch_cells))
        if reveal and self.window is not None:
            # 复用上一次的 Canvas；仅逐个 pack，避免每次悬停重建 HWND。
            for index, cell in enumerate(self._swatch_cells):
                self._reveal_after.append(self.window.after(index * 38,
                    lambda c=cell: self._reveal_swatch(c)))
        self._apply_geometry()

    def _cancel_reveal(self) -> None:
        window = self.window
        if window is not None:
            for token in self._reveal_after:
                try: window.after_cancel(token)
                except tk.TclError: pass
        self._reveal_after.clear()

    def _reveal_swatch(self, cell) -> None:
        if self.strip is None or not self.strip.winfo_manager():
            return
        cell.pack(side="top", pady=self._px(3), padx=self._px(4))
        self._apply_geometry()

    def _build_mode_bar(self) -> None:
        """模式分段条：三段等分，选中项是药丸 + 强调色图标。

        原来选中态只是给单元格加 1px 描边，而单元格是 fill="x" 撑满的：描边绕着
        图标拉成一个很宽的方框，看着像没对齐的输入框。改成药丸之后，选中项一眼
        就能认出来，三段之间也不再需要那圈突兀的框。
        """
        bar = self.mode_bar
        if bar is None:
            return
        for child in bar.winfo_children():
            child.destroy()
        self._mode_photos.clear()
        self._mode_cells.clear()
        accent = self.colors.get("accent", "#8b5cf6")
        active_bg = self.colors.get("bg_active", self.colors["bg_primary"])
        for mode, label, icon_name in self.mode_options:
            active = mode == self.active_mode
            # 选中态用药丸底 + 强调色图标（悬停、选中、未选中是同一档中性色的三级）；
            # 不填实心强调色——上面那个主按钮已经整块是强调色，两块同色挨在一起会
            # 看成一块拼坏的板子。
            cell = tk.Label(bar, background=active_bg if active else self.colors["bg_primary"],
                            cursor="hand2", bd=0, highlightthickness=0)
            photo = (self.icons.get(
                icon_name, MODE_ICON_SIZE,
                accent if active else self.colors.get("text_muted", "#aaa"),
            ) if self.icons else None)
            if photo is not None:
                cell.configure(image=photo)
                self._mode_photos.append(photo)
            else:                           # 素材缺失时不留空位，退成文字
                cell.configure(text=label[:1],
                               foreground=accent if active
                               else self.colors.get("text_normal", "#ddd"))
            # 不 fill：三段各自居中，选中的药丸只包住图标，不会拉成一整条。
            cell.pack(side="left", expand=True, ipadx=self._px(MODE_PILL_PAD_X),
                      ipady=self._px(MODE_PILL_PAD_Y),
                      padx=self._px(MODE_CELL_GAP_X), pady=self._px(MODE_CELL_GAP_Y))
            cell.bind("<Button-1>", lambda _event, value=mode: self._pick_mode(value), add="+")
            cell.bind("<Enter>", lambda _event, value=mode: self._hover_mode(value, True), add="+")
            cell.bind("<Leave>", lambda _event, value=mode: self._hover_mode(value, False), add="+")
            cell.bind("<Enter>", self._on_enter, add="+")
            cell.bind("<Leave>", self._on_leave, add="+")
            self._mode_cells[mode] = cell

    def _hover_mode(self, mode, hovered: bool) -> None:
        """未选中项悬停时浅一档；选中的药丸要保持住，不能跟着指针变深浅。"""
        cell = self._mode_cells.get(mode)
        if cell is None:
            return
        if mode == self.active_mode:
            cell.configure(background=self.colors.get("bg_active", self.colors["bg_primary"]))
            return
        cell.configure(background=(self.colors.get("bg_hover", self.colors["bg_primary"])
                                   if hovered else self.colors["bg_primary"]))

    def _pick_mode(self, mode) -> None:
        if mode == self.active_mode or self.on_mode_select is None:
            return
        self.on_mode_select(mode)

    def set_active_mode(self, mode) -> None:
        if mode == self.active_mode:
            return
        self.active_mode = mode
        self._configure_primary_action()
        if self.mode_bar is not None:
            self._build_mode_bar()

    def _record_label(self) -> str:
        """主按钮的文案：记录模式是「记录问题 / 记录单词」，高光模式没有按钮。

        按钮的底色、尺寸和是不是强调色按钮都取决于它，所以只留这一份判断。
        """
        return {"question": "记录问题", "translate": "记录单词"}.get(self.active_mode, "")

    def _configure_primary_action(self) -> None:
        """主按钮：记录模式是「强调色底 + 图标 + 文字」，高光模式只有图标。

        记录模式下原来只写一行加粗文字，按钮本身就是那行字，和下面的分段条摆
        在一起像两块拼偏的板子。改成填满强调色的按钮后，它是整块面板唯一的实心
        色块——「要点这里」不用再靠猜。

        宽度这里有个 Tk 的坑：`width` / `height` 只有**纯文字**时才算字符数，
        一旦设了 image 就变成像素。所以有图标时按像素给——卡片宽度照旧对齐
        entry_width（窗口摆位就是照它算的），高度交给自然值，否则图标会被裁掉。
        """
        if self.icon_label is None:
            return
        label = self._record_label()
        if label:
            accent = self.colors.get("accent", "#8b5cf6")
            on_accent = self.colors.get("text_on_accent", "#ffffff")
            icon_name = next((icon for mode, _label, icon in self.mode_options
                              if mode == self.active_mode and icon), "")
            photo = (self.icons.get(icon_name, PRIMARY_ICON_SIZE, on_accent)
                     if self.icons is not None and icon_name else None)
            self._primary_photo = photo
            if photo is not None:
                frame = self._px(14) * 2 + self._px(2) * 2 + 2   # 按钮左右内边距 + 行的 + 描边
                size = {"width": max(self._px(96), self.entry_width - frame), "height": 0}
            else:
                size = {"width": 12, "height": 1}
            self.icon_label.configure(image=photo or "", text=label,
                                      compound="left" if photo is not None else "none",
                                      font=("Microsoft YaHei UI", 12, "bold"),
                                      padx=self._px(14), pady=self._px(9),
                                      background=accent, activebackground=self.colors.get("accent_active", accent),
                                      foreground=on_accent, activeforeground=on_accent,
                                      **size)
        else:
            self._primary_photo = None
            # 回到高光模式：底色也要一起还原，否则图标底下还留着上一模式的强调色块。
            self.icon_label.configure(image=self._icon_photo or "", text="" if self._icon_photo else "✎",
                                      compound="none", width=0, height=0,
                                      background=self.colors["bg_primary"],
                                      activebackground=self.colors.get("bg_active", self.colors["bg_primary"]),
                                      foreground=self.icon_color,
                                      padx=self._px(DEFAULT_PAD), pady=self._px(DEFAULT_PAD))

    def set_icon(self, name: str) -> None:
        if self.icon_name == name:
            return
        self.icon_name = name
        if self.icon_label is not None and self.icons is not None:
            self._icon_photo = self.icons.get(name, self.icon_size, self.icon_color)
            self._configure_primary_action()

    def _on_icon_click(self, event=None) -> None:
        if self.on_click is not None:
            x, y = (event.x_root, event.y_root) if event is not None else self._pointer
            self.on_click(x, y)
        # The record action may return focus to the source application before
        # copying. Do not let Tk's Button class reclaim it after this handler.
        return "break"

    @staticmethod
    def _item_fingerprint(item) -> tuple:
        return (
            str(item.get("key", "")),
            str(item.get("kind", "action")),
            str(item.get("label", "")),
            str(item.get("color", "")),
            str(item.get("icon", "")),
        )

    def _build_swatch(self, item) -> None:
        """纵向有间距的圆形色点；Canvas 绘制抗锯齿边框和色心。"""
        key = item.get("key")
        diameter = self._px(30)
        background = self._current_background()
        cell = tk.Frame(self.strip, background=background, bd=0, cursor="hand2")
        canvas = tk.Canvas(cell, width=diameter, height=diameter, bd=0,
                           highlightthickness=0, background=background, cursor="hand2")
        canvas.pack(padx=self._px(3), pady=self._px(2))
        canvas.create_oval(self._px(2), self._px(2), diameter-self._px(2),
                           diameter-self._px(2), fill=str(item.get("color", "#888888")),
                           outline=self.colors.get("border", "#555555"), width=self._px(2))
        canvas.create_oval(self._px(6), self._px(6), diameter-self._px(6),
                           diameter-self._px(6), outline="#ffffff", width=1)
        canvas.bind("<Button-1>", lambda _e, k=key: self._on_item(k), add="+")
        for widget in (cell, canvas):
            widget.bind("<Enter>", self._on_enter, add="+")
            widget.bind("<Leave>", self._on_leave, add="+")
        self._bg_followers[cell] = [cell, canvas]
        self._swatch_cells.append(cell)

    def _build_action(self, item) -> None:
        """动作按钮：图标（可省）+ 文字，整块可点。"""
        key = item.get("key")
        background = self._current_background()
        pad = self._px(ACTION_PAD)
        cell = tk.Frame(self.strip, background=background, bd=0, cursor="hand2")
        cell.pack(side="left", padx=self._px(2), pady=self._px(2))
        widgets = [cell]
        icon_name = str(item.get("icon", "") or "")
        if icon_name and self.icons is not None:
            photo = self.icons.get(
                icon_name, ACTION_ICON_SIZE,
                self.colors.get("text_normal", self.icon_color),
            )
            if photo is not None:
                self._item_photos.append(photo)     # 必须留引用，否则 Tk 会把图丢掉
                icon = tk.Label(cell, image=photo, background=background, bd=0,
                                highlightthickness=0, cursor="hand2")
                icon.pack(side="left", padx=(pad, self._px(2)))
                widgets.append(icon)
        text = tk.Label(cell, text=str(item.get("label", "")), background=background,
                        foreground=self.colors.get("text_normal", "#dcddde"),
                        bd=0, highlightthickness=0, cursor="hand2")
        text.pack(side="left", padx=(self._px(2) if len(widgets) > 1 else pad,
                                     pad))
        widgets.append(text)
        for widget in widgets:
            widget.bind("<Button-1>", lambda _e, k=key: self._on_item(k), add="+")
            widget.bind("<Enter>", lambda _e, c=cell: self._hover_cell(c, True), add="+")
            widget.bind("<Leave>", lambda _e, c=cell: self._hover_cell(c, False), add="+")
            widget.bind("<Enter>", self._on_enter, add="+")
            widget.bind("<Leave>", self._on_leave, add="+")
        self._bg_followers[cell] = widgets

    def flash(self, text, icon: str = "triangle-alert", duration_ms: int = 3600) -> None:
        """在展开区显示一行提示，隔一会儿自己收起。

        复用同一个 Toplevel 与同一套几何逻辑：不新开窗口，也就不需要第二份摆放、
        销毁和指针进出处理。内容用 `expand_items` 铺，所以「内容没变不重建」那套
        优化照样管用。

        这是本项目里少见的**主动定时器**，理由写在这里免得后人当成违规：它管的是
        一次性提示的寿命，**不参与悬停判定**。之前否掉定时器，否的是「靠定时器猜
        指针还在不在入口里」那种用法——那种会在冷启动或机器忙时赌错；这个赌错了
        也只是提示多留一会儿。
        """
        if self.window is None or self.strip is None:
            return
        self.expand_items(
            [{"key": FLASH_KEY, "kind": "action", "label": str(text), "icon": icon}]
        )
        self._cancel_flash()
        try:
            self._flash_after = self.window.after(duration_ms, self._end_flash)
        except tk.TclError:
            self._flash_after = None

    def _end_flash(self) -> None:
        self._flash_after = None
        self.collapse()

    def _cancel_flash(self) -> None:
        if self._flash_after is None or self.window is None:
            self._flash_after = None
            return
        try:
            self.window.after_cancel(self._flash_after)
        except tk.TclError:
            pass
        self._flash_after = None

    def collapse(self) -> None:
        self._cancel_reveal()
        if self._vertical:
            for cell in self._swatch_cells: cell.pack_forget()
        if self.strip is not None:
            self.strip.pack_forget()
        self._expanded_left = False
        self._rebuild_row(with_strip=False)
        if self.window is not None and self._anchor is not None:
            self._apply_geometry()

    # ------------------------------------------------------------ 指针事件
    def _on_enter(self, _event=None) -> None:
        if self._hovered:
            return
        self._hovered = True
        self._apply_entry_background()
        self._notify_hover()

    def _on_leave(self, _event=None) -> None:
        # 指针可能只是移到了同一个窗口内的另一个子控件上，Tk 照样发 Leave。
        # 复查**当前**指针位置，而不是等一个定时器——定时器会赌错时序。
        if self.window is None or self._anchor is None:
            return
        try:
            x, y = self.window.winfo_pointerxy()
        except tk.TclError:
            return
        if self.contains(x, y):
            return
        self._hovered = False
        self._apply_entry_background()
        self.collapse()

    def _notify_hover(self) -> None:
        if self.on_hover is not None:
            self.on_hover()

    def _current_background(self) -> str:
        """整条入口当前的底色：指针在里面就用悬停色，否则用常态色。"""
        if self._hovered:
            return self.colors.get("bg_hover", self.colors["bg_primary"])
        return self.colors["bg_primary"]

    def _apply_entry_background(self) -> None:
        """把当前底色刷到图标、行、展开区和各选项容器上。

        展开区里的容器（色块格子、动作按钮）必须一起刷——它们的父控件是 strip，
        Tk 的 Frame 不透明，漏掉谁谁就是一块不改色的补丁。色块**本体**不在
        `_bg_followers` 里，它有自己的颜色，不该跟着变。
        """
        color = self._current_background()
        widgets = [self._row, self.strip]
        # 记录模式下的主按钮是整块强调色，不能跟着悬停刷成灰的；高光模式下它就是
        # 那个图标本体，得跟着变，否则悬停时图标底下一块圆形的暗斑。
        if not self._record_label():
            widgets.append(self.icon_label)
        for group in self._bg_followers.values():
            widgets.extend(group)
        for widget in widgets:
            if widget is None:
                continue
            try:
                widget.configure(background=color)
            except tk.TclError:
                pass

    def _hover_cell(self, cell, active: bool) -> None:
        """指针落在某个选项上时把它单独提亮一档，好知道点的是哪一个。"""
        if cell is None:
            return
        color = (self.colors.get("bg_active", self._current_background())
                 if active else self._current_background())
        for widget in self._bg_followers.get(cell, [cell]):
            try:
                widget.configure(background=color)
            except tk.TclError:
                pass

    def _on_right_click(self, event=None) -> None:
        if self.on_context_menu is None:
            return
        x, y = (event.x_root, event.y_root) if event is not None else self._pointer
        self.on_context_menu(x, y)

    def _on_item(self, key) -> None:
        if self.on_pick is not None:
            self.on_pick(key)

    # ------------------------------------------------------------ 查询
    def contains(self, x: int, y: int) -> bool:
        """物理屏幕坐标点是否落在入口窗口内。供「点别处消失」判断。"""
        window = self.window
        if window is None:
            return False
        try:
            if not window.winfo_exists() or window.state() == "withdrawn":
                return False
            left = window.winfo_rootx()
            top = window.winfo_rooty()
            return (left <= x < left + window.winfo_width()
                    and top <= y < top + window.winfo_height())
        except tk.TclError:
            return False


class ModePopup:
    """划词入口右键的暗色模式卡；不使用系统模态菜单。"""

    def __init__(self, root, colors, options, selected, on_select, on_close=None, *,
                 icons=None, scale=1.0):
        self.root = root
        self.colors = colors
        self.options = tuple(options)
        self.selected = selected
        self.on_select = on_select
        self.on_close = on_close
        self.scale = max(1.0, scale)
        self.window = tk.Toplevel(root)
        self.window.wm_overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.window.configure(background=colors["border"])
        self._photos = []
        surface = tk.Frame(self.window, background=colors["bg_primary"], bd=0)
        surface.pack(padx=1, pady=1)
        for key, label, icon_name in self.options:
            active = key == selected
            cell = tk.Frame(surface, background=colors["bg_primary"], cursor="hand2")
            cell.pack(fill="x", padx=4, pady=2)
            indicator = tk.Frame(cell, width=3, background=(colors.get("accent", "#8b5cf6")
                                  if active else colors["bg_primary"]))
            indicator.pack(side="left", fill="y", padx=(0, 8))
            indicator.pack_propagate(False)
            widgets = [cell, indicator]
            photo = icons.get(icon_name, 16, colors.get("text_muted", "#aaa")) if icons and icon_name else None
            if photo is not None:
                self._photos.append(photo)
                icon = tk.Label(cell, image=photo, background=colors["bg_primary"], cursor="hand2")
                icon.pack(side="left", padx=(0, 8))
                widgets.append(icon)
            text = tk.Label(cell, text=label, background=colors["bg_primary"],
                            foreground=(colors.get("accent", "#8b5cf6") if active
                                        else colors.get("text_normal", "#ddd")),
                            anchor="w", cursor="hand2",
                            padx=max(1, round(5 * self.scale)), pady=max(1, round(6 * self.scale)))
            text.pack(side="left", fill="x", expand=True)
            widgets.append(text)
            for widget in widgets:
                widget.bind("<Button-1>", lambda _event, value=key: self._choose(value))
                widget.bind("<Enter>", lambda _event, group=widgets: self._paint(group, True))
                widget.bind("<Leave>", lambda _event, group=widgets: self._paint(group, False))
        self.window.withdraw()

    def _paint(self, widgets, active):
        background = self.colors.get("bg_active", self.colors["bg_primary"]) if active else self.colors["bg_primary"]
        for widget in widgets:
            if widget is widgets[1]:  # 选中条不是按钮底色
                continue
            try:
                widget.configure(background=background)
            except tk.TclError:
                pass

    def _choose(self, key):
        self.on_select(key)
        self.close()

    def show(self, x, y):
        self.window.update_idletasks()
        width, height = self.window.winfo_reqwidth(), self.window.winfo_reqheight()
        x = max(2, min(x, self.root.winfo_screenwidth() - width - 2))
        y = max(2, min(y, self.root.winfo_screenheight() - height - 2))
        self.window.geometry(f"+{x}+{y}")
        self.window.deiconify()

    def contains(self, x, y):
        if not self.winfo_exists():
            return False
        w = self.window
        return w.winfo_rootx() <= x < w.winfo_rootx() + w.winfo_width() and \
            w.winfo_rooty() <= y < w.winfo_rooty() + w.winfo_height()

    def winfo_exists(self):
        return bool(self.window and self.window.winfo_exists())

    # 兼容旧的测试/调用方查询菜单项的最小接口；实际渲染已是主题化按钮卡片。
    def index(self, value):
        return len(self.options) - 1 if value == "end" else int(value)

    def entrycget(self, index, option):
        item = self.options[int(index)]
        if option == "label": return item[1]
        if option == "value": return item[0]
        raise ValueError(option)

    def destroy(self):
        self.close()

    def close(self):
        if self.winfo_exists():
            self.window.destroy()
        if self.on_close:
            callback, self.on_close = self.on_close, None
            callback()
