from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys
from typing import Iterable, List, Optional

import pyautogui as pag


WM_CLOSE = 0x0010
SW_RESTORE = 9

try:
    _user32 = ctypes.WinDLL("user32", use_last_error=True) if sys.platform == "win32" else None
except OSError:
    _user32 = None


def _window_title(hwnd: int) -> str:
    if _user32 is None:
        return ""
    buf = ctypes.create_unicode_buffer(512)
    _user32.GetWindowTextW(wt.HWND(hwnd), buf, 512)
    return buf.value or ""


def _is_visible(hwnd: int) -> bool:
    if _user32 is None:
        return False
    return bool(_user32.IsWindowVisible(wt.HWND(hwnd)))


def _enum_windows() -> List[int]:
    if _user32 is None:
        return []
    hwnds: List[int] = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def _cb(hwnd, _lparam):
        try:
            if _is_visible(int(hwnd)) and _window_title(int(hwnd)).strip():
                hwnds.append(int(hwnd))
        except Exception:
            pass
        return True

    _user32.EnumWindows(_cb, 0)
    return hwnds


def _pid_for_hwnd(hwnd: int) -> int:
    if _user32 is None:
        return 0
    pid = wt.DWORD()
    _user32.GetWindowThreadProcessId(wt.HWND(hwnd), ctypes.byref(pid))
    return int(pid.value)


def _process_image_name(pid: int) -> str:
    if not pid:
        return ""
    try:
        import subprocess

        cp = subprocess.run(
            ["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="ignore",
            check=False,
        )
        line = (cp.stdout or "").strip().splitlines()
        if not line:
            return ""
        import csv
        import io

        row = next(csv.reader(io.StringIO(line[0])), [])
        return row[0] if row else ""
    except Exception:
        return ""


def _post_close(hwnd: int) -> bool:
    if _user32 is None or not hwnd:
        return False
    _user32.ShowWindow(wt.HWND(hwnd), SW_RESTORE)
    return bool(_user32.PostMessageW(wt.HWND(hwnd), WM_CLOSE, 0, 0))


def close_foreground_window() -> bool:
    if _user32 is None:
        return False
    hwnd = int(_user32.GetForegroundWindow())
    return _post_close(hwnd)


def minimize_all_windows() -> bool:
    try:
        pag.hotkey("win", "m")
        return True
    except Exception:
        return False


def close_window_by_app(
    process_name: str = "",
    title_hints: Optional[Iterable[str]] = None,
) -> bool:
    wanted_process = (process_name or "").strip().lower()
    hints = [h.strip().lower() for h in (title_hints or []) if h and h.strip()]
    candidates: list[tuple[int, int]] = []

    for hwnd in _enum_windows():
        title = _window_title(hwnd).strip()
        title_l = title.lower()
        image_name = _process_image_name(_pid_for_hwnd(hwnd)).lower()
        score = 0
        if wanted_process and image_name == wanted_process:
            score += 20
        if hints and any(h in title_l for h in hints):
            score += 10
        if score:
            score += min(len(title), 80)
            candidates.append((score, hwnd))

    if not candidates:
        return False
    candidates.sort(reverse=True, key=lambda item: item[0])
    return _post_close(candidates[0][1])
