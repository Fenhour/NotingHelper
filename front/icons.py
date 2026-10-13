"""图标：载入、着色、缓存。

素材是 `assets/ui/icons/<名字>.png`，48×48 的**黑色描边 + 透明底主图**。
不直接存成品图（固定颜色、固定尺寸）是因为那样会把颜色和 DPI 都锁死：
界面需要灰色/深灰/强调紫/白四种图标色，200% 缩放的显示器又需要两倍物理尺寸，
成品图就得为每种组合各存一份，换主题还得重新导出。

这里运行时只做两件事——把主图的 alpha 拿过来、填上目标颜色，再缩放到需要的
物理尺寸。颜色和尺寸都不受素材限制。

主图为什么是 48px：图标原始设计 24 格、实际显示 16~18 逻辑像素，48 留出向下缩放
的余量；200% 缩放下需要 36 物理像素，也够用。
"""
from __future__ import annotations

import os

from PIL import Image, ImageColor, ImageTk

DEFAULT_COLOR = "#5c5c5c"


class IconCache:
    """按 (名字, 物理尺寸, 颜色) 缓存 PhotoImage。

    Tk 的 PhotoImage 一旦被回收，界面上就变成空白，所以缓存同时承担「保持引用」
    的职责。
    """

    def __init__(self, icons_dir: str, scale: float = 1.0):
        self.dir = icons_dir
        self.scale = max(1.0, scale)
        self._masters: dict[str, Image.Image] = {}
        self._photos: dict[tuple, ImageTk.PhotoImage] = {}
        self._warned: set[str] = set()

    # ---------------------------------------------------------------- 素材
    def has(self, name: str) -> bool:
        return os.path.isfile(self._path(name))

    def names(self) -> list[str]:
        if not os.path.isdir(self.dir):
            return []
        return sorted(f[:-4] for f in os.listdir(self.dir) if f.endswith(".png"))

    def _path(self, name: str) -> str:
        return os.path.join(self.dir, f"{name}.png")

    def _master(self, name: str) -> Image.Image | None:
        """载入主图，只保留 alpha 通道。

        只取 alpha 是刻意的：主图 RGB 全是黑，缩放时若连 RGB 一起插值，边缘会渗出
        黑色，着色后就是一圈脏边。只要 alpha，颜色由外面填。
        """
        m = self._masters.get(name)
        if m is None:
            try:
                with Image.open(self._path(name)) as raw:
                    m = raw.convert("RGBA").getchannel("A")
            except (OSError, ValueError):
                if name not in self._warned:
                    self._warned.add(name)
                    print(f"[icons] 找不到图标素材: {name}.png")
                return None
            self._masters[name] = m
        return m

    # ---------------------------------------------------------------- 输出
    def pil(self, name: str, size: int = 18, color: str = DEFAULT_COLOR) -> Image.Image:
        """size 是逻辑尺寸，输出按其 × scale 的物理尺寸。"""
        px = max(1, int(round(size * self.scale)))
        master = self._master(name)
        if master is None:
            return Image.new("RGBA", (px, px), (0, 0, 0, 0))
        alpha = master.resize((px, px), Image.LANCZOS) if master.size != (px, px) else master
        out = Image.new("RGBA", (px, px), ImageColor.getrgb(color) + (255,))
        out.putalpha(alpha)
        return out

    def get(self, name: str, size: int = 18,
            color: str = DEFAULT_COLOR) -> ImageTk.PhotoImage:
        px = max(1, int(round(size * self.scale)))
        key = (name, px, color)
        photo = self._photos.get(key)
        if photo is None:
            photo = ImageTk.PhotoImage(self.pil(name, size, color))
            self._photos[key] = photo
        return photo

    def clear(self):
        self._photos.clear()
        self._masters.clear()
        self._warned.clear()
