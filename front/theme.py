"""Obsidian 风格的 Tk 主题。

令牌全部取自 Obsidian 自身（obsidian.asar 里的 CSS 变量），不是凭印象调的近似色：
亮色/暗色两套基色阶梯、强调色 hsl(258,88%,66%)、语义色、图标描边宽度都保持一致。
两套阶梯都在这儿，但界面没有主题开关，产品只装暗色那一套（见 default_palette），
亮色留作备用。

样式表只写**界面真的在用的**名字，两类，别在两者之间和稀泥：

- **变体**（`Xxx.TLabel` / `Xxx.TFrame` / `Xxx.TButton` …）—— 只给明确带了
  `style="Xxx.…"` 的控件用。没人引用的变体一律删掉，不预留给将来：留着的话，
  读代码的人会以为某个地方在用它，而且真去用了别的名字也不会报错，只会静默变回
  clam 的默认长相。想知道现在有哪些，grep 界面代码里的 `style=` 即可。
  `front/controls.py` 里那些工具函数要求的样式也算「有人用」：`icon_button` 的默认值
  `Toolbar.TButton`、`ControlImages.radio()` 配套的 `Flat.TRadiobutton`。界面当下
  可能一处都没调用它们（现在就没有单选钮），但那是这个前端工具箱对外承诺的能力，
  样式缺了将来一用就露馅——自绘的图配着 clam 自带的指示器一起出现。
- **基础名**（`TFrame` / `TLabel` / `TEntry` / `TCombobox` / `TButton` / `TCheckbutton`
  / `TRadiobutton` / `Treeview` / `TNotebook` / `TSeparator` / `TProgressbar` /
  滚动条）—— 兜底层：不带 `style=` 的控件、以及 `front/` 缺失时退回的经典界面，
  全靠它们取色，因此全部保留，哪怕界面里没有一处显式写这些名字。

`Card.TLabelframe.Label` 属于变体，但它是 `Card.TLabelframe` 的标题子样式，由 ttk
自动取用（控件自己的 `style` 只会显示成 `Card.TLabelframe`），grep 时容易误判成没人用。

两个前提，缺一个观感就不成立：

1. Windows 上只有 clam 主题能被完整改色。vista 主题的控件由系统绘制，背景、
   边框、选中色都改不动，所以整套样式建立在 clam 之上。

2. 进程必须声明 DPI 感知，否则在缩放显示器上 Windows 会把窗口位图整体拉伸
   （200% 显示就放大到 2 倍再插值），细描边图标会糊掉。声明之后 Tk 才报告
   真实 DPI，图标也能按物理像素渲染。字体用 point 交给 tk scaling 自动缩放，
   像素尺寸（内边距、图标、行高）由这里的 scale 手动乘。
"""
from __future__ import annotations

import colorsys
import tkinter as tk
from tkinter import ttk

# ---------------------------------------------------------------- 设计令牌
BASE_LIGHT = {                                  # --color-base-*
    "00": "#ffffff", "05": "#fcfcfc", "10": "#fafafa", "20": "#f6f6f6",
    "25": "#e3e3e3", "30": "#e0e0e0", "35": "#d4d4d4", "40": "#bdbdbd",
    "50": "#ababab", "60": "#707070", "70": "#5c5c5c", "100": "#222222",
}
BASE_DARK = {
    "00": "#1e1e1e", "05": "#212121", "10": "#242424", "20": "#262626",
    "25": "#2a2a2a", "30": "#363636", "35": "#3f3f3f", "40": "#555555",
    "50": "#666666", "60": "#999999", "70": "#b3b3b3", "100": "#dadada",
}
ACCENT_HSL = (258, 0.88, 0.66)                  # --accent-h/s/l
SEMANTIC = {
    "red": "#e93147", "orange": "#ec7500", "yellow": "#e0ac00", "green": "#08b94e",
    "cyan": "#00bfbc", "blue": "#086ddd", "purple": "#7852ee", "pink": "#d53984",
}

FONT_UI = "Microsoft YaHei UI"
FONT_MONO = "Cascadia Mono"


def _hsl_to_hex(h, s, l):
    r, g, b = colorsys.hls_to_rgb(h / 360.0, l, s)
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


def _shift(hex_color, amount):
    """朝白（amount>0）或黑（amount<0）推，用于派生 hover / active。"""
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    f = (lambda c: round(c + (255 - c) * amount)) if amount > 0 \
        else (lambda c: round(c * (1 + amount)))
    return "#%02x%02x%02x" % (f(r), f(g), f(b))


def _blend(top, bottom, alpha):
    """把 top 以 alpha 透明度压在 bottom 上，得到等效实色（Tk 不支持透明）。"""
    c1 = [int(top[i:i + 2], 16) for i in (1, 3, 5)]
    c2 = [int(bottom[i:i + 2], 16) for i in (1, 3, 5)]
    a = 1.0 - alpha
    return "#%02x%02x%02x" % tuple(round(x * alpha + y * a) for x, y in zip(c1, c2))


class Palette:
    def __init__(self, dark=False):
        b = BASE_DARK if dark else BASE_LIGHT
        self.dark = dark
        self.bg_primary = b["00"]           # --background-primary：面板 / 编辑区
        self.bg_secondary = b["20"]         # --background-secondary：侧栏 / 工具条
        # 页面底色。亮色是「灰底页面 + 白卡片」的抬升层次；暗色里没有比卡片更浅的
        # 那一层（深灰压在近黑上会显得卡片是凹进去的），整页就取卡片色，
        # 靠间距和加粗标题分块，和暗色 Obsidian 一样。
        self.bg_page = b["00"] if dark else b["20"]
        self.bg_hover = _shift(b["00"], 0.06 if dark else -0.04)
        self.bg_active = _shift(b["00"], 0.12 if dark else -0.08)
        # 输入框比所在卡片略深一点，白底卡上才看得出「这里能填」；
        # 深色主题反过来，要比卡片略浅
        self.bg_field = _shift(b["00"], 0.03 if dark else -0.03)
        self.border = b["30"]               # --background-modifier-border
        self.border_strong = b["40"]

        self.text_normal = b["100"]         # --text-normal
        self.text_muted = b["70"]           # --text-muted
        self.text_faint = b["50"]           # --text-faint
        self.text_on_accent = "#ffffff"

        self.accent = _hsl_to_hex(*ACCENT_HSL)
        self.accent_hover = _shift(self.accent, 0.12)
        self.accent_active = _shift(self.accent, -0.10)
        self.accent_muted = _blend(self.accent, self.bg_primary, 0.82)

        for name, hex_v in SEMANTIC.items():
            setattr(self, name, hex_v)

        self.highlight = _blend("#ffd000", self.bg_primary, 0.40)   # --text-highlight-bg
        self.icon = self.text_muted                                 # --icon-color


def default_palette() -> Palette:
    """产品出厂外观：暗色，与用户的暗色 Obsidian 一致。

    界面没有主题开关，这就是唯一的一套观感。亮色仍完整保留在 BASE_LIGHT 里，
    想换回亮色只需把这里的 dark 改成 False（Palette 的构造默认值保持亮色不动，
    免得「产品选了暗色」被误当成「类默认就是暗色」）。
    """
    return Palette(dark=True)


def ui_font(size=10, weight="normal"):
    """字号以 point 计，交给 tk scaling 按 DPI 自动换算成像素。"""
    return (FONT_UI, size, weight)


def enable_dpi_awareness() -> bool:
    """声明进程 DPI 感知。必须在创建 Tk 根窗口之前调用。

    不声明的话，Windows 会把窗口按缩放比例位图放大（200% 显示即 2 倍拉伸），
    细描边图标会糊掉；声明之后 Tk 报真实 DPI，图标按物理像素渲染，线条锐利。
    """
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)      # PER_MONITOR_AWARE
        return True
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
            return True
        except Exception:
            return False


def detect_scale(root: tk.Misc | None = None) -> float:
    """真实 DPI 相对 96 的倍数，用于换算界面里写死的像素尺寸。

    优先问 Windows，而不是 Tk 的 winfo_fpixels：Tk 8.6 在 Windows 上对每台显示器
    的 DPI 支持不可靠，进程里一旦先创建过 DPI 不感知的 Tk root，它就一直返回 96。

    两种情形都自洽：
      - 进程已声明感知：GetDpiForWindow/GetDpiForSystem 返回真实值（如 192）→ 2.0，
        界面自己按物理像素排布。
      - 进程未声明感知：这些调用返回 96 → 1.0，此时由 Windows 做位图缩放，
        界面也不该再手动乘倍数。
    """
    dpi = 0
    try:
        import ctypes
        user32 = ctypes.windll.user32
        if root is not None:
            try:
                dpi = int(user32.GetDpiForWindow(root.winfo_id()))
            except Exception:
                dpi = 0
        if not dpi:
            dpi = int(user32.GetDpiForSystem())
    except Exception:
        dpi = 0
    if dpi > 0:
        return max(1.0, round(dpi / 96.0, 2))
    try:
        return max(1.0, round(root.winfo_fpixels("1i") / 96.0, 2))
    except Exception:
        return 1.0


def apply_dark_titlebar(root: tk.Misc, caption: str | None = None) -> bool:
    """把窗口标题栏也改成深色，并尽量让它和界面底色是同一个颜色。

    不做的话，暗色界面顶上会压一条亮标题栏，在暗色桌面上格外显眼。
    用 DWMWA_USE_IMMERSIVE_DARK_MODE：Windows 10 1809+ 是属性号 20，更早的
    预览版是 19，系统不认就返回非 0，静默放弃、标题栏维持系统外观。

    只设深色模式时，DWM 给的是它自己那套灰（还会跟着壁纸染一点色），和界面的
    #1e1e1e 摆一起仍看得出是两条颜色；所以传 caption 时再用 DWMWA_CAPTION_COLOR
    直接把标题栏调成同一个色（Windows 11 才有，老系统不认就退回上面那套灰）。
    """
    import ctypes
    try:
        hwnd = root.winfo_id()
        # Tk 在 Windows 上的 winfo_id() 是子窗口，真正带标题栏的是它的顶层祖先
        top = ctypes.c_void_p(ctypes.windll.user32.GetAncestor(hwnd, 2) or hwnd)  # GA_ROOT
        dwm = ctypes.windll.dwmapi
        flag = ctypes.c_int(1)
        done = False
        for attr in (20, 19):
            if dwm.DwmSetWindowAttribute(top, attr, ctypes.byref(flag),
                                         ctypes.sizeof(flag)) == 0:
                done = True
                break
        if caption:
            # COLORREF 是 0x00BBGGRR，跟 #RRGGBB 反着来
            r, g, b = (int(caption[i:i + 2], 16) for i in (1, 3, 5))
            color = ctypes.c_int(r | (g << 8) | (b << 16))
            done = dwm.DwmSetWindowAttribute(top, 35, ctypes.byref(color),
                                             ctypes.sizeof(color)) == 0 or done
        return done
    except Exception:
        return False


def _arrow_layout(s: ttk.Style, icons, p: Palette, scale: float) -> None:
    """把下拉框和微调框的箭头换成 icons 里的 chevron。

    clam 自带的箭头是个细窄三角形，200% 缩放下只剩一条竖缝，看不出能点。换成图标后
    粗细和界面里其他图标一致。element_create 重复调用会报错，所以先查一遍。
    """
    def chevron(name):
        return icons.get(name, 14, p.text_muted)

    existing = set(s.element_names())
    if "Chevron.Down" not in existing:
        s.element_create("Chevron.Down", "image", chevron("chevron-down"), sticky="")
    if "Chevron.Up" not in existing:
        s.element_create("Chevron.Up", "image", chevron("chevron-up"), sticky="")

    s.layout("TCombobox", [
        ("Chevron.Down", {"side": "right", "sticky": ""}),
        ("Combobox.field", {"sticky": "nswe", "children": [
            ("Combobox.padding", {"sticky": "nswe", "children": [
                ("Combobox.textarea", {"sticky": "nswe"})]})]}),
    ])
    s.layout("TSpinbox", [
        ("Spinbox.field", {"side": "top", "sticky": "we", "children": [
            ("null", {"side": "right", "sticky": "", "children": [
                ("Chevron.Up", {"side": "top", "sticky": ""}),
                ("Chevron.Down", {"side": "bottom", "sticky": ""})]}),
            ("Spinbox.padding", {"sticky": "nswe", "children": [
                ("Spinbox.textarea", {"sticky": "nswe"})]})]}),
    ])


def apply(root: tk.Misc, palette: Palette | None = None, scale: float = 1.0,
          icons=None) -> dict:
    """装好整套样式，返回 {名字: 颜色} 供 tk.Text 等非 ttk 控件取用。

    命名对齐 Obsidian 语义：Accent 是主操作，Ghost 是无边框次级操作，
    Card 是抬高一级的容器，Muted / Faint 是依次更淡的文字。

    传入 icons（front.icons.IconCache）时，下拉框和微调框的箭头会换成图集里的
    chevron；不传就沿用 clam 自带的箭头。
    """
    p = palette or Palette()
    s = ttk.Style(root)
    s.theme_use("clam")                     # 唯一能完整改色的内置主题

    try:                                    # 字体按真实 DPI 缩放
        root.tk.call("tk", "scaling", root.winfo_fpixels("1i") / 72.0)
    except Exception:
        pass

    # tk.Text / tk.Frame 这些经典控件不走 ttk 样式，靠命名字体定字号。
    # 用命名字体而不是 root.option_add("*Font", ...)：后者是写进选项库的，
    # 连 ttk 控件也一起覆盖，样式里设的字号（Title 16 / Heading 12 / 正文 10）
    # 会全部被压平成同一个大小，界面的层级就没了。
    try:
        import tkinter.font as tkfont
        tkfont.nametofont("TkDefaultFont").configure(family=FONT_UI, size=10)
        tkfont.nametofont("TkTextFont").configure(family=FONT_UI, size=10)
        tkfont.nametofont("TkFixedFont").configure(family=FONT_MONO, size=10)
    except Exception:
        pass

    def P(v):
        """像素尺寸 -> 物理像素。"""
        return max(1, int(round(v * scale)))

    def pad(a, b=None):
        return (P(a), P(b if b is not None else a))

    # ttk 默认元素配色，全部显式指定，避免 clam 的灰色底漏出来
    root.configure(background=p.bg_primary)
    s.configure(".", background=p.bg_primary, foreground=p.text_normal,
                fieldbackground=p.bg_field, bordercolor=p.border,
                lightcolor=p.bg_primary, darkcolor=p.bg_primary,
                troughcolor=p.bg_secondary, focuscolor=p.accent,
                font=ui_font(10))
    s.configure("TFrame", background=p.bg_primary)
    # 版面分层，和 Obsidian 一样：页面底 -> 卡片 -> 卡内控件。亮色是「灰底页面 +
    # 白卡片」；暗色页面取卡片色（见 Palette.bg_page），分块靠间距不靠底色差。
    # 侧栏和工具条用的灰是另一档（--background-secondary），不跟着页面走。
    # 这样卡里的 Label 用默认的 TLabel（卡片底）就对了，不必逐个改样式。
    s.configure("Page.TFrame", background=p.bg_page)
    s.configure("Sidebar.TFrame", background=p.bg_secondary)
    s.configure("Card.TFrame", background=p.bg_primary)
    s.configure("Hairline.TFrame", background=p.border)   # 1px 分隔线

    # 字号只留用得上的三档：标题、正文（默认 TLabel）、次要说明。小一号的灰色文字
    # 是 MutedCard（卡内）和 SideMuted（侧栏），别的字号目前没人要。
    s.configure("TLabel", background=p.bg_primary, foreground=p.text_normal)
    s.configure("Title.TLabel", font=ui_font(16, "bold"))
    s.configure("Muted.TLabel", foreground=p.text_muted)
    s.configure("MutedCard.TLabel", background=p.bg_primary, foreground=p.text_muted)
    # 就地报错用（输入框下面的校验提示）。不能拿 Muted 顶替：那和说明文字同一个灰，
    # 用户看不出这句话是「错了」。红色取自 SEMANTIC，两套主题共用。
    s.configure("Danger.TLabel", foreground=p.red)

    # 侧栏上的文字：底色必须跟侧栏一致，否则 TLabel 会在灰底上留白块
    s.configure("SideTitle.TLabel", background=p.bg_secondary,
                foreground=p.text_normal, font=ui_font(15, "bold"))
    s.configure("SideMuted.TLabel", background=p.bg_secondary,
                foreground=p.text_muted, font=ui_font(9))
    s.configure("Word.TLabel", font=ui_font(11, "bold"))

    s.configure("TSeparator", background=p.border)

    # 按钮：默认是安静的次级按钮（浅底 + 细边），Accent 主操作
    s.configure("TButton", background=p.bg_secondary, foreground=p.text_normal,
                bordercolor=p.border, relief="flat", padding=pad(12, 6),
                font=ui_font(10), anchor="center")
    s.map("TButton",
          background=[("disabled", p.bg_secondary), ("pressed", p.bg_active), ("active", p.bg_hover)],
          foreground=[("disabled", p.text_faint)],
          bordercolor=[("focus", p.accent), ("active", p.border_strong)])

    # 灰底页面上用的按钮：白底 + 细边。默认 TButton 是灰底，那是给白卡片配的，
    # 放到灰底上会跟背景糊成一片，看不出是个按钮。
    s.configure("Page.TButton", background=p.bg_primary, foreground=p.text_normal,
                bordercolor=p.border)
    s.map("Page.TButton",
          background=[("disabled", p.bg_secondary), ("pressed", p.bg_active),
                      ("active", p.bg_hover)],
          foreground=[("disabled", p.text_faint)],
          bordercolor=[("focus", p.accent), ("active", p.border_strong)])

    s.configure("Accent.TButton", background=p.accent, foreground=p.text_on_accent,
                bordercolor=p.accent)
    s.map("Accent.TButton",
          background=[("disabled", p.accent_muted), ("pressed", p.accent_active),
                      ("active", p.accent_hover)],
          foreground=[("disabled", p.text_faint)],
          bordercolor=[("disabled", p.accent_muted)])

    s.configure("Toolbar.TButton", background=p.bg_secondary, foreground=p.text_muted,
                bordercolor=p.bg_secondary, padding=pad(5, 4))
    s.map("Toolbar.TButton",
          background=[("pressed", p.bg_active), ("active", p.bg_hover)],
          foreground=[("active", p.text_normal)])

    # 输入控件：白底细边，聚焦时描边转强调色
    for style, extra in (("TEntry", {}), ("TSpinbox", {"arrowcolor": p.text_muted})):
        s.configure(style, background=p.bg_field, fieldbackground=p.bg_field,
                    foreground=p.text_normal, bordercolor=p.border,
                    lightcolor=p.border, darkcolor=p.border,
                    insertcolor=p.text_normal, padding=P(4), relief="flat", **extra)
        s.map(style,
              bordercolor=[("focus", p.accent), ("hover", p.border_strong)],
              lightcolor=[("focus", p.accent), ("hover", p.border_strong)],
              darkcolor=[("focus", p.accent), ("hover", p.border_strong)],
              foreground=[("disabled", p.text_faint)],
              fieldbackground=[("disabled", p.bg_secondary)],
              arrowcolor=[("disabled", p.text_faint)])

    s.configure("TCombobox", background=p.bg_secondary, fieldbackground=p.bg_field,
                foreground=p.text_normal, bordercolor=p.border,
                lightcolor=p.border, darkcolor=p.border,
                arrowcolor=p.text_muted, padding=P(4), relief="flat")
    s.map("TCombobox",
          fieldbackground=[("readonly", p.bg_field), ("disabled", p.bg_secondary)],
          bordercolor=[("focus", p.accent), ("hover", p.border_strong)],
          arrowcolor=[("active", p.accent)])
    # 下拉列表是独立 Tk 控件，不受 ttk 样式管辖，只能走 option database
    root.option_add("*TCombobox*Listbox.background", p.bg_primary)
    root.option_add("*TCombobox*Listbox.foreground", p.text_normal)
    root.option_add("*TCombobox*Listbox.selectBackground", p.accent)
    root.option_add("*TCombobox*Listbox.selectForeground", p.text_on_accent)
    root.option_add("*TCombobox*Listbox.borderWidth", 0)

    # 下拉菜单也一样：tk.Menu 由 Tk 自己绘制（不是系统原生菜单），颜色能改，
    # 但只能走 option database。不设这段，深色主题下弹出来的是白菜单。
    # 用 *Menu 通配，后面新加的菜单不用再逐个接线。
    root.option_add("*Menu.background", p.bg_primary)
    root.option_add("*Menu.foreground", p.text_normal)
    root.option_add("*Menu.activeBackground", p.bg_hover)
    root.option_add("*Menu.activeForeground", p.text_normal)
    root.option_add("*Menu.disabledForeground", p.text_faint)
    root.option_add("*Menu.borderWidth", 0)
    root.option_add("*Menu.activeBorderWidth", 0)
    root.option_add("*Menu.relief", "flat")

    s.configure("TCheckbutton", background=p.bg_primary, foreground=p.text_normal,
                focuscolor=p.bg_primary, padding=P(2))
    s.map("TCheckbutton",
          background=[("active", p.bg_primary)],
          foreground=[("disabled", p.text_faint)],
          indicatorcolor=[("selected", p.accent), ("pressed", p.accent_active),
                          ("!selected", p.bg_field)])
    # clam 自带的勾选框指示器很难看，且 `indicatorcolor` 只改底色、改不了那个歪斜
    # 的勾。这里把指示器从布局里摘掉，改用 icons 自绘的图（见 controls.py）。
    s.layout("Flat.TCheckbutton", [
        ("Checkbutton.padding", {"children": [
            ("Checkbutton.focus", {"children": [
                ("Checkbutton.label", {"sticky": "nswe"})], "sticky": "nswe"})],
            "sticky": "nswe"})])
    s.layout("Flat.TRadiobutton", [
        ("Radiobutton.padding", {"children": [
            ("Radiobutton.focus", {"children": [
                ("Radiobutton.label", {"sticky": "nswe"})], "sticky": "nswe"})],
            "sticky": "nswe"})])
    s.configure("Flat.TCheckbutton", background=p.bg_primary,
                foreground=p.text_normal, focuscolor=p.bg_primary, padding=P(2))
    s.map("Flat.TCheckbutton",
          background=[("active", p.bg_primary)],
          foreground=[("disabled", p.text_faint)])
    s.configure("Flat.TRadiobutton", background=p.bg_primary,
                foreground=p.text_normal, focuscolor=p.bg_primary, padding=P(2))
    s.map("Flat.TRadiobutton",
          background=[("active", p.bg_primary)],
          foreground=[("disabled", p.text_faint)])

    s.configure("TRadiobutton", background=p.bg_primary, foreground=p.text_normal,
                focuscolor=p.bg_primary, padding=P(2))
    s.map("TRadiobutton",
          background=[("active", p.bg_primary)],
          indicatorcolor=[("selected", p.accent), ("!selected", p.bg_field)])

    # 侧栏导航：Obsidian 的选中态是「中性 hover 底 + 强调色文字」，不是强调色浅底
    # （浅紫底配浅紫字对比度不够，读起来发灰）。这里保持同样的做法。
    s.configure("Nav.TButton", background=p.bg_secondary, foreground=p.text_muted,
                bordercolor=p.bg_secondary, relief="flat",
                padding=(P(10), P(9)), anchor="w", font=ui_font(11), space=P(9))
    s.map("Nav.TButton",
          background=[("pressed", p.bg_active), ("active", p.bg_active)],
          foreground=[("active", p.text_normal)])

    s.configure("NavActive.TButton", background=p.bg_active, foreground=p.accent,
                bordercolor=p.bg_active, relief="flat",
                padding=(P(10), P(9)), anchor="w", font=ui_font(11, "bold"), space=P(9))
    s.map("NavActive.TButton",
          background=[("active", p.bg_hover), ("pressed", p.bg_hover)],
          foreground=[("active", p.accent)])

    # 滚动条：细、无箭头、悬停加深
    for name in ("Vertical.TScrollbar", "Horizontal.TScrollbar"):
        s.configure(name, background=p.border_strong, troughcolor=p.bg_primary,
                    bordercolor=p.bg_primary, arrowcolor=p.bg_primary,
                    relief="flat", width=P(10), arrowsize=0)
        s.map(name,
              background=[("pressed", p.accent), ("active", p.text_faint), ("!active", p.border)])

    s.configure("TNotebook", background=p.bg_secondary, bordercolor=p.border,
                tabmargins=(P(2), P(4), P(2), 0))
    s.configure("TNotebook.Tab", background=p.bg_secondary, foreground=p.text_muted,
                padding=pad(14, 7), font=ui_font(10))
    s.map("TNotebook.Tab",
          background=[("selected", p.bg_primary)],
          foreground=[("selected", p.text_normal)],
          expand=[("selected", (0, 0, 0, 0))])

    s.configure("TProgressbar", background=p.accent, troughcolor=p.bg_secondary,
                bordercolor=p.bg_secondary, lightcolor=p.accent, darkcolor=p.accent)
    s.configure("TLabelframe", background=p.bg_primary, bordercolor=p.border)
    s.configure("TLabelframe.Label", background=p.bg_primary, foreground=p.text_muted,
                font=ui_font(10, "bold"))

    # 卡片：白底、无边框，标题在卡内左上。放在灰底页面上就成了一块「浮起来」的白卡。
    # 不用描边是因为 Obsidian 的卡片就靠底色差分层，加边框反而显脏（clam 画不出圆角）。
    s.configure("Card.TLabelframe", background=p.bg_primary, borderwidth=0,
                relief="flat", bordercolor=p.bg_primary,
                lightcolor=p.bg_primary, darkcolor=p.bg_primary)
    # 标题左内缩，和卡片里第一列文字对齐（LabelFrame 的标签默认贴在框边上）
    s.configure("Card.TLabelframe.Label", background=p.bg_primary,
                foreground=p.text_normal, font=ui_font(11, "bold"),
                padding=(P(13), 0, 0, P(3)))

    s.configure("Treeview", background=p.bg_primary, fieldbackground=p.bg_primary,
                foreground=p.text_normal, bordercolor=p.border,
                lightcolor=p.border, darkcolor=p.border, rowheight=P(24))
    s.configure("Treeview.Heading", background=p.bg_secondary, foreground=p.text_muted,
                relief="flat", padding=pad(8, 5), font=ui_font(9))
    s.map("Treeview", background=[("selected", p.accent_muted)],
          foreground=[("selected", p.accent)])
    s.map("Treeview.Heading", background=[("active", p.bg_hover)])

    if icons is not None:
        _arrow_layout(s, icons, p, scale)

    return {
        "bg_primary": p.bg_primary, "bg_secondary": p.bg_secondary,
        "bg_hover": p.bg_hover, "bg_active": p.bg_active, "bg_field": p.bg_field,
        "border": p.border, "border_strong": p.border_strong,
        "text_normal": p.text_normal, "text_muted": p.text_muted,
        "text_faint": p.text_faint, "text_on_accent": p.text_on_accent,
        "accent": p.accent, "accent_hover": p.accent_hover,
        "accent_muted": p.accent_muted,
        "red": p.red, "orange": p.orange, "yellow": p.yellow,
        "green": p.green, "blue": p.blue, "purple": p.purple,
        "highlight": p.highlight, "icon": p.icon, "scale": scale,
    }
