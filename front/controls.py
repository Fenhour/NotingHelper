"""自绘控件：勾选框 / 单选钮 / 提示气泡。

ttk 在 Windows 上能改色的只有 clam 主题，而 clam 勾选框的勾是一条歪斜的细线，
`indicatorcolor` 只能改底色、改不了那个形状，单选钮同理。这两个控件按 Obsidian
的样子自绘，再用 ttk 的 image 接上去（配合 theme.py 里摘掉 indicator 的 Flat.* 布局）。

其余控件（输入框、下拉框、按钮、滚动条）改色就够，不必自绘。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageDraw, ImageTk


class ControlImages:
    """勾选框与单选钮的状态图，按尺寸和配色缓存。"""

    def __init__(self, icon_cache, scale=1.0, accent="#8a5cf5", border="#d4d4d4",
                 on_accent="#ffffff"):
        self.icons = icon_cache
        self.scale = max(1.0, scale)
        self.accent, self.border, self.on_accent = accent, border, on_accent
        self._cache: dict[tuple, ImageTk.PhotoImage] = {}

    def checkbox(self, checked: bool, size: int = 16):
        return self._get("cb", checked, size)

    def radio(self, checked: bool, size: int = 16):
        return self._get("rb", checked, size)

    # ---------------------------------------------------------------- 内部
    def _get(self, kind, checked, size):
        key = (kind, checked, size)
        img = self._cache.get(key)
        if img is None:
            img = ImageTk.PhotoImage(self._draw(kind, checked, size))
            self._cache[key] = img
        return img

    def _draw(self, kind, checked, size):
        ss = 4                                     # 超采样，圆角和勾的边缘才干净
        S = max(4, int(round(size * self.scale)))
        N = S * ss
        img = Image.new("RGBA", (N, N), (0, 0, 0, 0))
        dr = ImageDraw.Draw(img)
        w = max(2, int(round(1.5 * self.scale * ss)))   # 描边 1.5 逻辑像素
        pad = w / 2 + ss * 0.5

        if kind == "cb":
            dr.rounded_rectangle((pad, pad, N - pad, N - pad), radius=N * 0.22,
                                 fill=self.accent if checked else None,
                                 outline=self.accent if checked else self.border,
                                 width=w)
            if checked:
                # 勾直接用素材里的 check，形状与 Obsidian 一致
                g = max(4, int(N * 0.62))
                # pil() 内部会乘一次 scale，这里要的是超采样空间里的 g 像素
                glyph = self.icons.pil("check", g / self.scale, self.on_accent)
                img.alpha_composite(glyph.convert("RGBA"), ((N - g) // 2, (N - g) // 2))
        else:
            c = N / 2
            dr.ellipse((pad, pad, N - pad, N - pad),
                       fill=self.accent if checked else None,
                       outline=self.accent if checked else self.border, width=w)
            if checked:
                r = N * 0.20
                dr.ellipse((c - r, c - r, c + r, c + r), fill=self.on_accent)

        return img.resize((S, S), Image.LANCZOS)


def checkbox(parent, text="", images=None, checked=False, command=None, size=16,
             style="Flat.TCheckbutton", variable=None, **kw):
    """Obsidian 样式的勾选框。

    ttk 没有经典 Tk 的 -selectimage，所以监听变量变化自行换图；这样每个实例可以
    用不同尺寸，不受「样式 map 只能挂一份图」的限制。键盘空格切换同样会改变变量，
    图会跟着更新。

    传 variable 就接管调用方自己的变量（界面里那些要读回配置的开关都是这样），
    不传就内部建一个，通过返回值的 _var 取。
    """
    images = images or ControlImages(None)
    var = variable if variable is not None else tk.BooleanVar(value=checked)
    # 首图按变量当下的值取，不按 checked：调用方传进来的变量可能已经是 True，
    # 而 checked 还是默认的 False，差这一下会显示成没勾上。
    # 不写 takefocus：勾选框要能用 Tab 选中、空格切换（原来是 ttk 默认的可聚焦）。
    cb = ttk.Checkbutton(parent, text=text, variable=var, command=command,
                         style=style, compound="left",
                         image=images.checkbox(bool(var.get()), size), **kw)
    cb._images = images          # 保持引用，否则 Tk 会把图回收成空白

    def sync(*_):
        # 控件可能已经销毁：变量是调用方持有的，活得比控件久（设置窗关掉之后
        # 再打开别处会重新给这个变量赋值），那时 trace 还在，configure 会炸。
        if not cb.winfo_exists():
            return
        cb.configure(image=images.checkbox(bool(var.get()), size))

    trace_id = var.trace_add("write", sync)

    def drop_trace(_event=None):
        try:
            var.trace_remove("write", trace_id)
        except (tk.TclError, ValueError):
            pass                      # 变量自己已经销毁了，trace 自然也没了

    cb.bind("<Destroy>", drop_trace, add="+")
    cb._var = var
    return cb


def tooltip(widget, text, colors, scale=1.0, font=None):
    """轻量提示气泡，跟随控件下方居中。"""
    state = {"win": None}

    def show(_=None):
        if state["win"]:
            return
        x = widget.winfo_rootx() + widget.winfo_width() // 2
        y = widget.winfo_rooty() + widget.winfo_height() + int(6 * scale)
        win = tk.Toplevel(widget)
        win.wm_overrideredirect(True)
        win.attributes("-topmost", True)
        # 气泡用「反色」：底色取 text_normal、字取 bg_primary，两个主题下都是
        # 深底白字或浅底深字，永远读得清。原来字写死 #ffffff，深色主题下
        # text_normal 本身是浅色，会变成浅底白字。
        tk.Label(win, text=text, background=colors["text_normal"],
                 foreground=colors["bg_primary"],
                 font=font or ("Microsoft YaHei UI", 9),
                 padx=int(9 * scale), pady=int(4 * scale)).pack()
        win.update_idletasks()
        win.geometry(f"+{x - win.winfo_width() // 2}+{y}")
        state["win"] = win

    def hide(_=None):
        if state["win"]:
            state["win"].destroy()
            state["win"] = None

    widget.bind("<Enter>", show, add="+")
    widget.bind("<Leave>", hide, add="+")
    widget.bind("<Destroy>", hide, add="+")
    return widget


def icon_button(parent, icons, name, size=18, color=None, text=None,
                tooltip_text=None, colors=None, scale=1.0, style="Toolbar.TButton",
                command=None, **kw):
    """图标按钮：自动持有图片引用，可选提示气泡。"""
    img = icons.get(name, size, color) if (icons and color) else None
    if text:
        kw.setdefault("compound", "left")
    btn = ttk.Button(parent, image=img, text=text or "", style=style,
                     command=command, takefocus=False, **kw)
    btn.image = img                       # 关键：必须持有引用
    if tooltip_text and colors:
        tooltip(btn, tooltip_text, colors, scale)
    return btn
