from __future__ import annotations

import ctypes
import sys


VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_PLAY_PAUSE = 0xB3
VK_MEDIA_STOP = 0xB2
KEYEVENTF_KEYUP = 0x0002


def _send_virtual_key(vk: int) -> bool:
    if sys.platform != "win32":
        return False
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.keybd_event(int(vk), 0, 0, 0)
    user32.keybd_event(int(vk), 0, KEYEVENTF_KEYUP, 0)
    return True


def play_pause() -> bool:
    return _send_virtual_key(VK_MEDIA_PLAY_PAUSE)


def next_track() -> bool:
    return _send_virtual_key(VK_MEDIA_NEXT_TRACK)


def previous_track() -> bool:
    return _send_virtual_key(VK_MEDIA_PREV_TRACK)


def stop() -> bool:
    return _send_virtual_key(VK_MEDIA_STOP)
