from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Iterable, Optional


@dataclass(frozen=True)
class HighlightRect:
    x: int
    y: int
    w: int
    h: int
    label: str = ""


_overlay: Optional["HighlightOverlay"] = None
_overlay_lock = threading.Lock()


class HighlightOverlay:
    def __init__(self, rects: Iterable[HighlightRect], monitor_width: int, monitor_height: int):
        self.rects = list(rects)
        self.monitor_width = int(monitor_width)
        self.monitor_height = int(monitor_height)
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="AkoHighlightOverlay")

    def show(self) -> None:
        self._thread.start()
        self._ready.wait(timeout=1.5)

    def _run(self) -> None:
        import tkinter as tk

        self.root = tk.Tk()
        self.root.title("Ako Highlight")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        try:
            self.root.attributes("-transparentcolor", "white")
        except Exception:
            self.root.attributes("-alpha", 0.35)
        self.root.geometry(f"{self.monitor_width}x{self.monitor_height}+0+0")

        canvas = tk.Canvas(
            self.root,
            width=self.monitor_width,
            height=self.monitor_height,
            bg="white",
            highlightthickness=0,
        )
        canvas.pack(fill="both", expand=True)

        for rect in self.rects:
            pad = 8
            x1 = max(0, rect.x - pad)
            y1 = max(0, rect.y - pad)
            x2 = min(self.monitor_width, rect.x + rect.w + pad)
            y2 = min(self.monitor_height, rect.y + rect.h + pad)
            canvas.create_rectangle(x1, y1, x2, y2, fill="#8a2be2", stipple="gray50", outline="#b56cff", width=3)
            if rect.label:
                canvas.create_text(x1, max(12, y1 - 10), anchor="w", text=rect.label, fill="#b56cff", font=("Malgun Gothic", 14, "bold"))

        btn = tk.Button(self.root, text="강조 끄기", command=self.close, bg="#2b1742", fg="white", relief="flat")
        btn.place(x=16, y=16)
        self._ready.set()
        self.root.mainloop()

    def close(self) -> None:
        try:
            self.root.after(0, self.root.destroy)
        except Exception:
            try:
                self.root.destroy()
            except Exception:
                pass


def show_highlights(rects: Iterable[HighlightRect], monitor_width: int, monitor_height: int) -> None:
    global _overlay
    clear_highlights()
    overlay = HighlightOverlay(rects, monitor_width, monitor_height)
    with _overlay_lock:
        _overlay = overlay
    overlay.show()


def clear_highlights() -> bool:
    global _overlay
    with _overlay_lock:
        overlay = _overlay
        _overlay = None
    if overlay is None:
        return False
    overlay.close()
    return True
