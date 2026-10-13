"""Lightweight vertical splitter: coalesced live resizing without native sash proxies."""
from __future__ import annotations

import tkinter as tk


class SmoothVerticalPanedWindow(tk.PanedWindow):
    """Keep Tk's pane sizing, but perform at most one drag resize per frame.

    The classic Windows sash handler runs layout for every mouse motion. Text
    widgets (especially embedded images) then repaint faster than the event loop
    can settle. Bypass that handler and retain only the most recent pointer target.
    """

    FRAME_MS = 16

    def __init__(self, master, *, grip_color, active_color, scale=1.0, **kwargs):
        kwargs.update(orient="vertical", opaqueresize=True, showhandle=False,
                      relief="flat", borderwidth=0, sashrelief="flat")
        super().__init__(master, **kwargs)
        self._grip_color = grip_color
        self._active_color = active_color
        self._drag_anchor = None
        self._target_y = None
        self._frame_job = None
        self._grip_job = None
        self._previous_grab = None
        self._grip = tk.Frame(self, width=max(20, round(32 * scale)),
                              height=max(2, round(2 * scale)), borderwidth=0,
                              background=grip_color, cursor="sb_v_double_arrow")
        # Never invoke the native drag/proxy handlers alongside our own handler.
        self.bindtags(tuple(tag for tag in self.bindtags() if tag != self.winfo_class()))
        for widget in (self, self._grip):
            widget.bind("<ButtonPress-1>", self._start_drag)
            widget.bind("<B1-Motion>", self._drag)
            widget.bind("<ButtonRelease-1>", self._finish_drag)
            widget.bind("<Motion>", self._hover)
            widget.bind("<Leave>", self._leave)
        self.bind("<Configure>", self._schedule_grip)
        self.bind("<Destroy>", self._on_destroy)

    def _on_sash(self, event):
        if event.widget is self._grip:
            return True
        hit = self.identify(event.x, event.y)
        return bool(hit and str(hit[-1]) in {"sash", "handle"})

    def _start_drag(self, event):
        if not self._on_sash(event) or len(self.panes()) != 2:
            return
        self._drag_anchor = (event.y_root, self.sash_coord(0)[1])
        self._previous_grab = self.grab_current()
        # A parent grab still dispatches to its Text descendants. Capture on
        # the childless handle so motion/release stay here outside the sash.
        self._grip.grab_set()
        self._grip.configure(background=self._active_color)
        self.configure(cursor="sb_v_double_arrow")
        return "break"

    def _drag(self, event):
        if self._drag_anchor is None:
            return
        pointer_y, sash_y = self._drag_anchor
        self._target_y = sash_y + event.y_root - pointer_y
        if self._frame_job is None:
            self._frame_job = self.after(self.FRAME_MS, self._flush_drag)
        return "break"

    def _flush_drag(self):
        self._frame_job = None
        target, self._target_y = self._target_y, None
        if target is not None and self.winfo_exists():
            self.sash_place(0, 0, target)

    def _finish_drag(self, event):
        if self._drag_anchor is None:
            return
        pointer_y, sash_y = self._drag_anchor
        self._target_y = sash_y + event.y_root - pointer_y
        if self._frame_job is not None:
            self.after_cancel(self._frame_job)
            self._frame_job = None
        self._flush_drag()  # Release commits the exact final pointer, no lagging frame.
        self._drag_anchor = None
        self._grip.configure(background=self._grip_color)
        self.configure(cursor="")
        self._grip.grab_release()
        previous, self._previous_grab = self._previous_grab, None
        if previous is not None and previous is not self and previous.winfo_exists():
            previous.grab_set()
        return "break"

    def sash_place(self, index, x, y):
        result = super().sash_place(index, x, y)
        self._schedule_grip()
        return result

    def _schedule_grip(self, _event=None):
        if self._grip_job is None:
            self._grip_job = self.after_idle(self._place_grip)

    def _place_grip(self):
        self._grip_job = None
        if len(self.panes()) != 2:
            self._grip.place_forget()
            return
        _, y = self.sash_coord(0)
        self._grip.place(relx=0.5, y=y + int(self.cget("sashwidth")) // 2,
                         anchor="center")
        self._grip.lift()

    def _hover(self, event):
        if self._drag_anchor is None:
            on_sash = self._on_sash(event)
            self.configure(cursor="sb_v_double_arrow" if on_sash else "")
            self._grip.configure(background=self._active_color if on_sash else self._grip_color)

    def _leave(self, _event):
        if self._drag_anchor is None:
            self.configure(cursor="")
            self._grip.configure(background=self._grip_color)

    def _on_destroy(self, event):
        if event.widget is not self:
            return
        for job in (self._frame_job, self._grip_job):
            if job is not None:
                self.after_cancel(job)
        self._frame_job = self._grip_job = None
        self._target_y = self._drag_anchor = None
