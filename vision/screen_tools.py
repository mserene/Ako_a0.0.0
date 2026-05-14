from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import numpy as np
from PIL import Image

from ui_vision import Box, all_monitor_bounds, find_text_boxes, grab_screen, grab_screen_with_origin, monitor_bounds, ocr_lines


@dataclass(frozen=True)
class ScreenTextHit:
    text: str
    x: int
    y: int
    w: int
    h: int
    conf: float
    variant: str = "raw"

    @classmethod
    def from_box(cls, box: Box, offset_x: int = 0, offset_y: int = 0) -> "ScreenTextHit":
        return cls(box.text, box.x + offset_x, box.y + offset_y, box.w, box.h, box.conf, getattr(box, "variant", "raw"))


def default_monitor_index() -> int:
    try:
        return int(os.getenv("AKO_SCREEN_MONITOR_INDEX", "0"))
    except Exception:
        return 0


def save_screenshot(monitor_index: int | None = None, output_dir: Optional[str] = None) -> str:
    if monitor_index is None:
        monitor_index = default_monitor_index()
    bgra = grab_screen(monitor_index)
    rgb = bgra[:, :, :3][:, :, ::-1]
    img = Image.fromarray(rgb)
    if output_dir:
        folder = Path(output_dir)
    else:
        folder = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Pictures" / "Ako"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    path = folder / f"Ako_{stamp}.png"
    img.save(path)
    return str(path)


def read_screen_text(monitor_index: int | None = None, lang: str = "kor+eng") -> List[str]:
    if monitor_index is None:
        monitor_index = default_monitor_index()
    return ocr_lines(grab_screen(monitor_index), lang=lang)


def find_text_on_screen(
    target: str,
    monitor_index: int | None = None,
    lang: str = "kor+eng",
    conf_min: float = 35.0,
) -> List[ScreenTextHit]:
    if monitor_index is None:
        monitor_index = default_monitor_index()
    if monitor_index == 0:
        hits: List[ScreenTextHit] = []
        for index, _left, _top, _width, _height in all_monitor_bounds():
            if index == 0:
                continue
            hits.extend(find_text_on_screen(target, monitor_index=index, lang=lang, conf_min=conf_min))
            if hits and os.getenv("AKO_OCR_STOP_AFTER_FIRST_MONITOR", "1").strip().lower() not in {"0", "false", "no", "off"}:
                break
        return hits

    bgra, left, top, _width, _height = grab_screen_with_origin(monitor_index)
    boxes = find_text_boxes(
        bgra,
        target,
        lang=lang,
        conf_min=conf_min,
        allow_contains=True,
        debug_tag=f"monitor{monitor_index}",
    )
    return [ScreenTextHit.from_box(box, offset_x=left, offset_y=top) for box in boxes]


def get_screen_bounds(monitor_index: int | None = None) -> tuple[int, int, int, int]:
    if monitor_index is None:
        monitor_index = default_monitor_index()
    return monitor_bounds(monitor_index)


def extract_quoted_or_target(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    quoted = re.search(r"[\"'“”‘’'](?P<target>.+?)[\"'“”‘’']", raw)
    if quoted:
        return quoted.group("target").strip()
    patterns = [
        r"(?P<target>.+?)(?:라는|이라는)\s*(?:단어|글자|텍스트)",
        r"(?P<target>.+?)\s*(?:찾아\s*줘|찾아줘|검색\s*해\s*줘|검색해줘|강조\s*해\s*줘|강조해줘)",
    ]
    for pattern in patterns:
        match = re.search(pattern, raw)
        if match:
            target = match.group("target").strip()
            target = re.sub(
                r"^(야\s*)?(아코야|아코)?\s*(여기에|여기에서|여기|화면에서|화면에|화면|지금|현재)\s*",
                "",
                target,
            ).strip()
            if target:
                return target
    return ""
