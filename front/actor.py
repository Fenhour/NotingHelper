"""角色层：与页面内容完全分开的一层。

为什么必须分开
--------------
当前实现用 `canvas.pack(side="bottom", fill="x")` 把人物塞进弹窗的内容布局里，
pack 会占用布局空间，于是「显示人物」这件事本身改变了页面的构图。要做成
Obsidian 那种干净的静态版式，人物就不能参与排版。

这里的做法
----------
用 `place()` 把人物挂上去。place 完全脱离布局流：既不占空间也不影响任何兄弟
控件的尺寸和位置，等价于窗口内的一层浮层（z 轴上一层），而不是布局里的一个格子。

几何不写死
----------
素材的透明包围盒各不相同（实测 Toix 是 4,20,166,167，Yinyao 是 16,9,155,164），
所以锚点、阴影、画布尺寸全部从素材量出来，换素材不需要改代码。

阴影在载入时用 Pillow 烘焙成带渐变的软阴影。原来在画布上画两个扁平椭圆的做法
依赖固定坐标，换素材会错位，而且边缘是硬的。
"""
from __future__ import annotations

import json
import time
import tkinter as tk
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageTk


# 每烘一帧都要走一次 ImageTk.PhotoImage：它把整张 RGBA 以 base64 文本命令灌进
# Tcl 解释器，实测约 8 ms/帧。90 帧一口气做完就是一秒出头的界面冻结，而弹窗窗口
# 在那之前就已经映射到屏幕上了——用户看到的是「窗口出来了，然后整个界面卡住」。
# 所以同步部分只烘第 0 帧，剩下的按时间预算分摊到事件循环里，每轮只占几毫秒。
BAKE_BUDGET_MS = 6
BAKE_GAP_MS = 6

# 烘焙结果缓存：同一份素材在同一个 Tk 解释器里只烘一次。
# 代价要说清楚——waiting 90 帧在 200% 缩放下约 36 MB，walking 52 帧约 21 MB。这份
# 内存在弹窗开着的时候本来就要占，缓存只是让开关弹窗不再反复分配又丢弃。如果你更
# 在意关窗后的常驻内存、而不是再次开窗的速度，把这里改成 False 即可：会退化成
# 「每次开窗重新烘焙」，但因为有上面那套分摊，界面仍然不会卡死。
FRAME_CACHE_ENABLED = True
FRAME_CACHE_LIMIT = 2
_FRAME_CACHE: "dict[tuple, dict]" = {}


class CharacterActor:
    """一个自带动画时钟的角色。挂到 host 上，不参与 host 的布局。"""

    def __init__(self, host: tk.Misc, asset_dir, background: str,
                 scale: float = 1.0, shadow_alpha: int = 90,
                 display_width: int | None = None):
        """display_width 是角色在界面上的逻辑宽度，默认按素材原始尺寸。

        素材是 172×172，90 帧在 200% 缩放下要占约 53 MB；角色只是装饰，缩小到
        100 逻辑像素左右即可，内存能降到约三分之一。
        """
        self.host = host
        self.asset_dir = Path(asset_dir)
        self.background = background
        self.scale = max(1.0, scale)
        self.shadow_alpha = shadow_alpha
        self.display_width = display_width

        self.frames: list[ImageTk.PhotoImage] = []
        self.frame_delay_ms = 33
        self.repeat_mode = "pause_then_replay"
        self.replay_delay_ms = 10_000
        self._after_id: str | None = None
        self._index = 0
        self._place_kw: dict | None = None

        # 磁盘上一共多少帧。动画的「放完了吗」要拿它比，不能拿 len(self.frames)——
        # 后者在烘焙还没收尾时只是个中间值，会把动画提前当成放完。
        self._total_frames = 0
        self._bake_ctx: dict | None = None      # 非空表示还有帧没烘完
        self._pump_after: str | None = None
        self._destroyed = False

        self.canvas: tk.Canvas | None = None
        self._image_item: int | None = None
        self._geometry: dict | None = None
        self.size = (0, 0)

        if self._load_manifest() and self._bake_frames():
            self._build_canvas()

    # ------------------------------------------------------------ 载入
    def _load_manifest(self) -> bool:
        try:
            m = json.loads((self.asset_dir / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        self.frame_delay_ms = max(16, int(m.get("frame_delay_ms", 33)))
        mode = str(m.get("repeat_mode", "pause_then_replay"))
        self.repeat_mode = mode if mode in {"pause_then_replay", "continuous_loop"} \
            else "pause_then_replay"
        self.replay_delay_ms = max(0, int(m.get("replay_delay_ms", 10_000)))
        return True

    def _frames_on_disk(self) -> list[Path]:
        paths = sorted(self.asset_dir.glob("frame_*.png"))
        # 中间缺帧会让动画跳变，只有尾部截短可以接受
        if [p.name for p in paths] != [f"frame_{i:03d}.png" for i in range(len(paths))]:
            return []
        return paths

    def _measure(self, paths: list[Path]) -> tuple[int, int, tuple[int, int, int, int]]:
        """量出全片段的联合不透明包围盒，作为锚点和阴影的依据。"""
        sample = paths[:: max(1, len(paths) // 12)]
        box = None
        size = (0, 0)
        for p in sample:
            im = Image.open(p).convert("RGBA")
            size = im.size
            b = im.getchannel("A").getbbox()
            if b:
                box = b if box is None else (min(box[0], b[0]), min(box[1], b[1]),
                                             max(box[2], b[2]), max(box[3], b[3]))
        return size[0], size[1], (box or (0, 0, size[0], size[1]))

    def _cache_key(self) -> tuple:
        return (str(self.asset_dir), round(self.scale, 4),
                self.display_width, self.shadow_alpha)

    def _bake_frames(self) -> bool:
        """同步只烘第 0 帧，其余交给 _pump 分摊；同一份素材第二次直接命中缓存。

        原来这里是 for p in paths 一整个循环，90 帧全在主线程上烘完才返回，而弹窗
        的 Toplevel 建得比这早（word_picker.py 先建窗、后挂角色），于是每次开窗都
        有 1.1~1.2 秒的界面冻结。现在同步部分只剩「量尺寸 + 做阴影 + 一帧」，约
        80 ms，弹窗一出来就是能用的。
        """
        key = self._cache_key()
        cached = _FRAME_CACHE.get(key)
        if cached is not None:
            # 缓存里的 PhotoImage 属于它被烘出来的那个 Tk 解释器，解释器换过了
            # （测试里每个用例一个新 root）就不能再用，得丢弃重烘。
            if cached["interp"] is self.host.tk:
                self.frames = cached["frames"]
                self.size = cached["size"]
                self._geometry = cached["geometry"]
                self._total_frames = len(self.frames)
                return True
            _FRAME_CACHE.pop(key, None)

        paths = self._frames_on_disk()
        if not paths:
            return False
        w, h, box = self._measure(paths)
        if w <= 0 or h <= 0:
            return False

        # 逻辑像素 -> 物理像素：先按 display_width 缩放素材，再乘 DPI 系数
        s = self.scale * (self.display_width / w if self.display_width else 1.0)
        pad = int(round(10 * s))                     # 阴影需要的外扩空间
        W, H = int(round(w * s)) + pad * 2, int(round(h * s)) + pad * 2

        # 阴影画在包围盒底边中心，宽度取包围盒宽度的八成，看起来像脚下的一块地影
        cx = pad + int(round((box[0] + box[2]) / 2 * s))
        foot = pad + int(round(box[3] * s))
        rx = max(4, int(round((box[2] - box[0]) * s * 0.40)))
        ry = max(2, int(round(5 * s)))

        small = 4                                    # 先在小图上模糊再放大，省时间
        sm = Image.new("L", (W // small + 1, H // small + 1), 0)
        ImageDraw.Draw(sm).ellipse(
            (cx // small - rx // small, foot // small - ry // small,
             cx // small + rx // small, foot // small + ry // small), fill=255)
        blur = sm.filter(ImageFilter.GaussianBlur(radius=max(1.0, 3.0 * s / small)))
        blur = blur.resize((W, H), Image.BILINEAR)

        shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        shadow.putalpha(blur.point(lambda v: v * self.shadow_alpha // 255))

        self.size = (W, H)
        self._geometry = {"sprite": (w, h), "bbox": box, "pad": pad, "canvas": (W, H)}
        self._total_frames = len(paths)
        self._bake_ctx = {"paths": paths, "shadow": shadow, "s": s,
                          "w": w, "h": h, "pad": pad}
        self.frames = []
        self._append_frame(0)
        if self._total_frames > 1:
            self._schedule_pump()
        else:
            self._store_cache()
        return True

    def _append_frame(self, index: int) -> None:
        ctx = self._bake_ctx
        frame = Image.open(ctx["paths"][index]).convert("RGBA")
        if ctx["s"] != 1.0:
            frame = frame.resize((int(round(ctx["w"] * ctx["s"])),
                                  int(round(ctx["h"] * ctx["s"]))), Image.LANCZOS)
        base = ctx["shadow"].copy()
        base.alpha_composite(frame, (ctx["pad"], ctx["pad"]))
        self.frames.append(ImageTk.PhotoImage(base, master=self.host))

    def _schedule_pump(self) -> None:
        try:
            if self._destroyed or not self.host.winfo_exists():
                return
            self._pump_after = self.host.after(BAKE_GAP_MS, self._pump)
        except tk.TclError:
            self._pump_after = None

    def _pump(self) -> None:
        """按时间预算接着烘。弹窗关掉了就立刻收手，别对着已销毁的控件白烧 CPU。"""
        self._pump_after = None
        ctx = self._bake_ctx
        if self._destroyed or not ctx:
            return
        deadline = time.perf_counter() + BAKE_BUDGET_MS / 1000
        while len(self.frames) < self._total_frames and time.perf_counter() < deadline:
            self._append_frame(len(self.frames))
        if len(self.frames) >= self._total_frames:
            self._bake_ctx = None
            self._store_cache()
            return
        self._schedule_pump()

    def _store_cache(self) -> None:
        if not FRAME_CACHE_ENABLED or not self.frames:
            return
        key = self._cache_key()
        _FRAME_CACHE[key] = {"interp": self.host.tk, "frames": self.frames,
                             "size": self.size, "geometry": self._geometry}
        while len(_FRAME_CACHE) > FRAME_CACHE_LIMIT:
            _FRAME_CACHE.pop(next(iter(_FRAME_CACHE)))

    def bake_progress(self) -> tuple[int, int]:
        """(已烘好的帧数, 总帧数)。给测试和探针看烘焙是不是真的在分摊。"""
        return len(self.frames), self._total_frames or len(self.frames)

    def _build_canvas(self) -> None:
        """place 挂载：不占布局空间，纯粹浮在内容之上。"""
        self.canvas = tk.Canvas(self.host, width=self.size[0], height=self.size[1],
                                background=self.background, highlightthickness=0, bd=0)
        self._image_item = self.canvas.create_image(
            self.size[0] // 2, self.size[1] // 2, anchor="center")
        self.canvas.itemconfigure(self._image_item,
                                  image=self.frames[0] if self.frames else None)

    def attach(self, corner: str = "se", margin=(10, 6), relx=None, rely=None):
        """放到 host 的某个角。也可用 relx/rely 指定相对位置（0~1）。

        corner 里的 e/s 决定靠哪边，margin 相应取正负；给了 relx/rely 时按
        相对位置摆，margin 这时是相对那个点的偏移。位置参数存下来，
        place_forget() 之后能原样恢复。
        """
        if not self.canvas:
            return
        if relx is not None:
            self._place_kw = {"relx": relx, "rely": rely, "anchor": corner}
            if margin != (0, 0):
                self._place_kw["x"] = int(round(margin[0] * self.scale))
                self._place_kw["y"] = int(round(margin[1] * self.scale))
        else:
            east, south = "e" in corner, "s" in corner
            self._place_kw = {
                "relx": 1.0 if east else 0.0,
                "rely": 1.0 if south else 0.0,
                "anchor": corner,
                "x": -int(round(margin[0] * self.scale)) if east
                     else int(round(margin[0] * self.scale)),
                "y": -int(round(margin[1] * self.scale)) if south
                     else int(round(margin[1] * self.scale)),
            }
        self.canvas.place(**self._place_kw)

    # ------------------------------------------------------------ 动画
    def play(self):
        if not self.frames or not self.canvas:
            return
        self._cancel()
        self._index = 0
        self._tick()

    def _tick(self):
        if not self.frames or not self.canvas or not self.canvas.winfo_exists():
            return
        if self._index >= len(self.frames):
            # 这一帧还没烘出来：原地等一轮，不要跳过中间的帧——烘焙通常比播放快
            # （约 20 ms/帧 对 33 ms/帧），只有机器很忙时才会走到这里。
            self._after_id = self.canvas.after(self.frame_delay_ms, self._tick)
            return
        self.canvas.itemconfigure(self._image_item, image=self.frames[self._index])
        # 这里比的是磁盘上的总帧数，不是已经烘好的帧数——后者在烘焙收尾前偏小，
        # 会让动画提前走进「放完了」的分支。
        if self._index >= self._total_frames - 1:
            if self.repeat_mode == "continuous_loop":
                self._index = 0
                self._after_id = self.canvas.after(self.frame_delay_ms, self._tick)
            else:
                self._after_id = self.canvas.after(self.replay_delay_ms, self.play)
            return
        self._index += 1
        self._after_id = self.canvas.after(self.frame_delay_ms, self._tick)

    def _cancel(self):
        if self._after_id:
            try:
                self.canvas.after_cancel(self._after_id)
            except (tk.TclError, AttributeError):
                pass
            self._after_id = None

    def set_visible(self, visible: bool):
        """显隐人物。因为走的是 place，隐藏它不会让页面内容移动一个像素。"""
        if not self.canvas:
            return
        if visible:
            if self._place_kw:
                self.canvas.place(**self._place_kw)
            # Canvas 把 lift / tkraise 都重载成了 tag_raise（作用于画布内元素），
            # 想抬升控件本身只能直接调 Tcl 的 raise。
            self.canvas.tk.call("raise", self.canvas._w)
        else:
            self.canvas.place_forget()

    def destroy(self):
        self._cancel()
        if self._pump_after:
            try:
                self.host.after_cancel(self._pump_after)
            except (tk.TclError, AttributeError):
                pass
            self._pump_after = None
        self._destroyed = True
        self._bake_ctx = None
        # 重新绑定而不是 clear()：缓存里存的可能就是这同一个列表对象，clear() 会把
        # 它一并清空，下一个命中缓存的角色就无帧可播了。
        self.frames = []
        if self.canvas and self.canvas.winfo_exists():
            self.canvas.destroy()
        self.canvas = None
