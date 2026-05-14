from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import numpy as np
from PIL import Image

from ui_vision import Box, find_text_boxes, grab_screen, ocr_lines


@dataclass(frozen=True)
class ScreenTextHit:
    text: str
    x: int
    y: int
    w: int
    h: int
    conf: float

    @classmethod
    def from_box(cls, box: Box) -> "ScreenTextHit":
        return cls(box.text, box.x, box.y, box.w, box.h, box.conf)


def save_screenshot(monitor_index: int = 1, output_dir: Optional[str] = None) -> str:
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


def read_screen_text(monitor_index: int = 1, lang: str = "kor+eng") -> List[str]:
    return ocr_lines(grab_screen(monitor_index), lang=lang)


def find_text_on_screen(
    target: str,
    monitor_index: int = 1,
    lang: str = "kor+eng",
    conf_min: float = 35.0,
) -> List[ScreenTextHit]:
    boxes = find_text_boxes(
        grab_screen(monitor_index),
        target,
        lang=lang,
        conf_min=conf_min,
        allow_contains=True,
    )
    return [ScreenTextHit.from_box(box) for box in boxes]


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
