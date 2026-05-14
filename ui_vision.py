from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from typing import List, Optional, Tuple

import mss
import numpy as np
from PIL import Image, ImageEnhance, ImageFilter, ImageOps
import pytesseract


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _env_float(name: str, default: float, minimum: float | None = None, maximum: float | None = None) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except Exception:
        value = default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _set_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            import ctypes

            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def grab_screen(monitor_index: int = 1) -> np.ndarray:
    """BGRA uint8 이미지(HxWx4) 반환. monitor_index: 1=메인 모니터."""
    _set_dpi_awareness()
    with mss.mss() as sct:
        mon = sct.monitors[monitor_index]
        if _env_bool("AKO_OCR_DEBUG", False):
            print(f"[OCR DEBUG] capture monitor={monitor_index} bounds={mon}")
        return np.array(sct.grab(mon))


def monitor_bounds(monitor_index: int = 1) -> Tuple[int, int, int, int]:
    _set_dpi_awareness()
    with mss.mss() as sct:
        mon = sct.monitors[monitor_index]
        return int(mon["left"]), int(mon["top"]), int(mon["width"]), int(mon["height"])


def all_monitor_bounds() -> List[Tuple[int, int, int, int, int]]:
    _set_dpi_awareness()
    with mss.mss() as sct:
        return [
            (index, int(mon["left"]), int(mon["top"]), int(mon["width"]), int(mon["height"]))
            for index, mon in enumerate(sct.monitors)
        ]


def grab_screen_with_origin(monitor_index: int = 1) -> tuple[np.ndarray, int, int, int, int]:
    _set_dpi_awareness()
    with mss.mss() as sct:
        mon = sct.monitors[monitor_index]
        if _env_bool("AKO_OCR_DEBUG", False):
            print(f"[OCR DEBUG] capture monitor={monitor_index} bounds={mon}")
        bgra = np.array(sct.grab(mon))
        return bgra, int(mon["left"]), int(mon["top"]), int(mon["width"]), int(mon["height"])


def _resolve_tesseract() -> None:
    """동봉된 Tesseract 또는 시스템 PATH의 Tesseract를 사용한다."""
    cmd = os.environ.get("TESSERACT_CMD")
    if cmd and os.path.exists(cmd):
        pytesseract.pytesseract.tesseract_cmd = cmd
        return

    base = os.path.dirname(__file__)
    local_cmd = os.path.join(base, "tools", "tesseract", "tesseract.exe")
    if os.path.exists(local_cmd):
        pytesseract.pytesseract.tesseract_cmd = local_cmd
        local_tessdata = os.path.join(base, "tools", "tesseract", "tessdata")
        if os.path.isdir(local_tessdata):
            os.environ.setdefault("TESSDATA_PREFIX", local_tessdata)


def _ocr_config(psm: int = 6) -> str:
    return f"--oem 1 --psm {int(psm)}"


def _ocr_psms() -> List[int]:
    raw = os.getenv("AKO_OCR_PSMS", "6")
    out: List[int] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            out.append(int(item))
        except Exception:
            pass
    return out or [6]


def _bgra_to_rgb_image(bgra: np.ndarray) -> Image.Image:
    rgb = bgra[:, :, :3][:, :, ::-1]
    return Image.fromarray(rgb)


def _preprocess_image_for_ocr(img: Image.Image, invert: bool = False, scale_override: float | None = None) -> tuple[Image.Image, float]:
    scale = scale_override if scale_override is not None else _env_float("AKO_OCR_UPSCALE", 2.0, minimum=1.0, maximum=4.0)
    out = img.convert("L")
    if invert:
        out = ImageOps.invert(out)
    if scale != 1.0:
        resampling = getattr(Image, "Resampling", Image)
        out = out.resize((int(out.width * scale), int(out.height * scale)), resampling.LANCZOS)
    out = ImageOps.autocontrast(out)
    contrast = _env_float("AKO_OCR_CONTRAST", 1.6, minimum=0.1, maximum=5.0)
    sharpness = _env_float("AKO_OCR_SHARPNESS", 1.4, minimum=0.1, maximum=5.0)
    out = ImageEnhance.Contrast(out).enhance(contrast)
    out = ImageEnhance.Sharpness(out).enhance(sharpness)
    out = out.filter(ImageFilter.SHARPEN)
    if _env_bool("AKO_OCR_THRESHOLD", False):
        threshold = int(_env_float("AKO_OCR_THRESHOLD_VALUE", 180, minimum=0, maximum=255))
        out = out.point(lambda p: 255 if p >= threshold else 0)
    return out, scale


def _debug_dir() -> str:
    path = os.getenv("AKO_OCR_DEBUG_DIR", "debug_ocr").strip() or "debug_ocr"
    os.makedirs(path, exist_ok=True)
    return path


def _save_ocr_debug_images(raw_img: Image.Image, pre_img: Image.Image, debug_tag: str | None = None) -> None:
    if not _env_bool("AKO_OCR_SAVE_DEBUG_IMAGES", True):
        return
    folder = _debug_dir()
    suffix = ""
    if debug_tag:
        suffix = "_" + re.sub(r"[^0-9A-Za-z가-힣_.-]+", "_", debug_tag.strip())[:60]
    raw_path = os.path.abspath(os.path.join(folder, f"debug_ocr_input{suffix}_raw.png"))
    pre_path = os.path.abspath(os.path.join(folder, f"debug_ocr_input{suffix}_preprocessed.png"))
    raw_img.save(raw_path)
    pre_img.save(pre_path)
    print(f"[OCR DEBUG] raw image: {raw_path} size={raw_img.size}")
    print(f"[OCR DEBUG] preprocessed image: {pre_path} size={pre_img.size}")


def _save_ocr_debug_extra(name: str, img: Image.Image) -> None:
    if not _env_bool("AKO_OCR_SAVE_DEBUG_IMAGES", True):
        return
    safe = re.sub(r"[^0-9A-Za-z가-힣_.-]+", "_", name)[:80]
    path = os.path.abspath(os.path.join(_debug_dir(), f"debug_ocr_{safe}.png"))
    img.save(path)
    print(f"[OCR DEBUG] variant image: {path} size={img.size}")


def _parse_conf(raw: object) -> float:
    try:
        value = str(raw).strip()
        return float(value) if value != "-1" else -1.0
    except Exception:
        return -1.0


def _console_safe(text: object) -> str:
    raw = str(text)
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return raw.encode(encoding, errors="backslashreplace").decode(encoding, errors="replace")


def _image_to_boxes(
    img: Image.Image,
    lang: str,
    variant: str,
    scale: float = 1.0,
    psm: int = 6,
    offset_x: int = 0,
    offset_y: int = 0,
) -> List[Box]:
    data = pytesseract.image_to_data(
        img,
        lang=lang,
        output_type=pytesseract.Output.DICT,
        config=_ocr_config(psm=psm),
    )
    boxes: List[Box] = []
    count = len(data.get("text", []))
    for index in range(count):
        raw_text = (data["text"][index] or "").strip()
        if not raw_text:
            continue
        confidence = _parse_conf(data.get("conf", ["-1"] * count)[index])
        left = offset_x + int(float(data["left"][index]) / scale)
        top = offset_y + int(float(data["top"][index]) / scale)
        width = max(1, int(float(data["width"][index]) / scale))
        height = max(1, int(float(data["height"][index]) / scale))
        boxes.append(Box(raw_text, left, top, width, height, confidence, variant=variant))
    return boxes


def _dedupe_boxes(boxes: List["Box"]) -> List["Box"]:
    out: List[Box] = []
    seen: set[tuple[int, int, int, int, str, str]] = set()
    for box in boxes:
        key = (box.x, box.y, box.w, box.h, _norm(box.text), box.variant)
        if key in seen:
            continue
        seen.add(key)
        out.append(box)
    return out


def _merged_line_boxes(boxes: List["Box"]) -> List["Box"]:
    """Tesseract가 한국어를 글자별로 쪼갠 경우 인접 박스를 이어 붙인다."""
    tokens = [box for box in boxes if _norm(box.text)]
    if not tokens:
        return []
    tokens.sort(key=lambda box: (box.y, box.x))

    lines: List[List[Box]] = []
    for box in tokens:
        placed = False
        for line in lines:
            line_cy = sum(item.cy for item in line) / len(line)
            max_h = max(max(item.h for item in line), box.h)
            if abs(box.cy - line_cy) <= max(8.0, max_h * 0.75):
                line.append(box)
                placed = True
                break
        if not placed:
            lines.append([box])

    merged: List[Box] = []
    max_tokens = int(_env_float("AKO_OCR_MERGE_MAX_TOKENS", 8, minimum=2, maximum=20))
    for line in lines:
        line.sort(key=lambda box: box.x)
        parts: List[List[Box]] = []
        current: List[Box] = []
        for box in line:
            if not current:
                current = [box]
                continue
            prev = current[-1]
            gap = box.x - (prev.x + prev.w)
            max_h = max(prev.h, box.h)
            if gap <= max(35, max_h * 2.5):
                current.append(box)
            else:
                parts.append(current)
                current = [box]
        if current:
            parts.append(current)

        for part in parts:
            n = len(part)
            for start in range(n):
                end_limit = min(n, start + max_tokens)
                for end in range(start + 2, end_limit + 1):
                    chunk = part[start:end]
                    text = "".join(item.text for item in chunk)
                    norm = _norm(text)
                    if len(norm) < 2:
                        continue
                    x1 = min(item.x for item in chunk)
                    y1 = min(item.y for item in chunk)
                    x2 = max(item.x + item.w for item in chunk)
                    y2 = max(item.y + item.h for item in chunk)
                    confs = [item.conf for item in chunk if item.conf >= 0]
                    conf = sum(confs) / len(confs) if confs else -1.0
                    merged.append(Box(text, x1, y1, x2 - x1, y2 - y1, conf, variant=f"merged:{chunk[0].variant}"))
    return _dedupe_boxes(merged)


@dataclass(frozen=True)
class _OcrVariant:
    name: str
    image: Image.Image
    scale: float
    offset_x: int = 0
    offset_y: int = 0


def _ocr_variants(raw_img: Image.Image) -> List[_OcrVariant]:
    variants: List[_OcrVariant] = []
    pre_img, scale = _preprocess_image_for_ocr(raw_img)

    inv_img, inv_scale = _preprocess_image_for_ocr(raw_img, invert=True)
    if _env_bool("AKO_OCR_FULL_PAGE", False):
        variants.append(_OcrVariant("raw", raw_img, 1.0))
        variants.append(_OcrVariant("preprocessed", pre_img, scale))
        variants.append(_OcrVariant("inverted", inv_img, inv_scale))

    if _env_bool("AKO_OCR_TILE", True):
        w, h = raw_img.size
        tile_scale = _env_float("AKO_OCR_TILE_UPSCALE", 3.0, minimum=1.0, maximum=5.0)
        regions = [
            ("top", 0, 0, w, int(h * 0.28)),
            ("top_left", 0, 0, int(w * 0.45), int(h * 0.35)),
            ("left", 0, 0, int(w * 0.35), h),
            ("center", int(w * 0.15), int(h * 0.10), int(w * 0.70), int(h * 0.75)),
            ("right", int(w * 0.62), 0, int(w * 0.38), h),
            ("bottom", 0, int(h * 0.62), w, int(h * 0.38)),
        ]
        if _env_bool("AKO_OCR_GRID", False):
            cols = int(_env_float("AKO_OCR_GRID_COLS", 3, minimum=1, maximum=6))
            rows = int(_env_float("AKO_OCR_GRID_ROWS", 3, minimum=1, maximum=6))
            overlap = _env_float("AKO_OCR_GRID_OVERLAP", 0.08, minimum=0.0, maximum=0.4)
            cell_w = w / cols
            cell_h = h / rows
            for row in range(rows):
                for col in range(cols):
                    x = max(0, int((col - overlap) * cell_w))
                    y = max(0, int((row - overlap) * cell_h))
                    x2 = min(w, int((col + 1 + overlap) * cell_w))
                    y2 = min(h, int((row + 1 + overlap) * cell_h))
                    regions.append((f"grid_{row}_{col}", x, y, x2 - x, y2 - y))

        seen_regions: set[tuple[int, int, int, int]] = set()
        for name, x, y, tw, th in regions:
            if tw <= 10 or th <= 10:
                continue
            key = (x, y, tw, th)
            if key in seen_regions:
                continue
            seen_regions.add(key)
            crop = raw_img.crop((x, y, min(w, x + tw), min(h, y + th)))
            crop_pre, crop_scale = _preprocess_image_for_ocr(crop, scale_override=tile_scale)
            crop_inv, crop_inv_scale = _preprocess_image_for_ocr(crop, invert=True, scale_override=tile_scale)
            variants.append(_OcrVariant(f"tile_{name}", crop_pre, crop_scale, x, y))
            variants.append(_OcrVariant(f"tile_{name}_inverted", crop_inv, crop_inv_scale, x, y))

    if _env_bool("AKO_OCR_SAVE_DEBUG_VARIANTS", False):
        for variant in variants:
            if variant.name != "raw":
                _save_ocr_debug_extra(variant.name, variant.image)
    return variants


def _print_ocr_debug(target: str, lang: str, raw_boxes: List["Box"], pre_boxes: List["Box"]) -> None:
    if not _env_bool("AKO_OCR_DEBUG", True):
        return
    print(f"[OCR DEBUG] engine=tesseract cmd={pytesseract.pytesseract.tesseract_cmd} lang={lang} target={target!r}")
    try:
        langs = pytesseract.get_languages(config="")
        print(f"[OCR DEBUG] available_langs={langs}")
    except Exception as e:
        print(f"[OCR DEBUG] available_langs_error={type(e).__name__}: {e}")

    limit = int(_env_float("AKO_OCR_DEBUG_LIMIT", 0, minimum=0, maximum=100000))

    def emit(label: str, boxes: List[Box]) -> None:
        print(f"[OCR RAW] {label} count={len(boxes)}")
        shown = boxes if limit <= 0 else boxes[:limit]
        for box in shown:
            print(
                "[OCR RAW] "
                f"{label} "
                f"variant={box.variant} "
                f"text={_console_safe(repr(box.text))} "
                f"conf={box.conf:.1f} "
                f"x={box.x} y={box.y} w={box.w} h={box.h}"
            )
        if limit > 0 and len(boxes) > limit:
            print(f"[OCR RAW] {label} truncated={len(boxes) - limit}")

    emit("raw", raw_boxes)
    emit("preprocessed", pre_boxes)


def ocr_lines(bgra: np.ndarray, lang: str = "kor+eng") -> List[str]:
    """BGRA(HxWx4) 이미지를 OCR해 텍스트 라인 리스트로 반환한다."""
    _resolve_tesseract()
    img = _bgra_to_rgb_image(bgra)
    if _env_bool("AKO_OCR_PREPROCESS", True):
        img, _scale = _preprocess_image_for_ocr(img)
    text = pytesseract.image_to_string(img, lang=lang, config=_ocr_config(psm=6))
    return [line.strip() for line in text.splitlines() if line.strip()]


@dataclass
class Box:
    text: str
    x: int
    y: int
    w: int
    h: int
    conf: float
    variant: str = "raw"

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2


def _norm(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]", "", text.strip()).lower()


def _similarity(a: str, b: str) -> float:
    from difflib import SequenceMatcher

    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _match_sort_key(box: "Box", target: str) -> tuple[int, int, float, int]:
    box_n = _norm(box.text)
    target_n = _norm(target)
    if box_n == target_n:
        kind = 0
    elif target_n and target_n in box_n:
        kind = 1
    elif box_n and box_n in target_n:
        kind = 2
    else:
        kind = 3
    return (kind, abs(len(box_n) - len(target_n)), -box.conf, box.w * box.h)


def _overlap_fraction(a: "Box", b: "Box") -> float:
    ax2 = a.x + a.w
    ay2 = a.y + a.h
    bx2 = b.x + b.w
    by2 = b.y + b.h
    ix1 = max(a.x, b.x)
    iy1 = max(a.y, b.y)
    ix2 = min(ax2, bx2)
    iy2 = min(ay2, by2)
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    inter = (ix2 - ix1) * (iy2 - iy1)
    smaller = max(1, min(a.w * a.h, b.w * b.h))
    return inter / smaller


def _box_matches(box_text: str, target: str, allow_contains: bool) -> bool:
    box_n = _norm(box_text)
    target_n = _norm(target)
    if not box_n or not target_n:
        return False
    target_has_hangul = bool(re.search(r"[가-힣]", target_n))
    if target_has_hangul and not re.search(r"[가-힣]", box_n):
        return False
    if box_n == target_n:
        return True
    if allow_contains and target_n in box_n:
        return True
    if allow_contains and box_n in target_n and len(box_n) >= max(2, len(target_n) - 1):
        return True
    if not _env_bool("AKO_OCR_FUZZY", True):
        return False
    if target_has_hangul and len(target_n) <= 2:
        return False
    threshold = _env_float("AKO_OCR_FUZZY_THRESHOLD", 0.72, minimum=0.0, maximum=1.0)
    return _similarity(box_n, target_n) >= threshold


def find_text_boxes(
    bgra: np.ndarray,
    target: str,
    lang: str = "kor+eng",
    conf_min: float = 50.0,
    allow_contains: bool = True,
    debug_tag: str | None = None,
) -> List[Box]:
    """
    target 텍스트가 포함된 OCR 박스 리스트 반환 (좌표 포함).
    - bgra: HxWx4 (mss 캡처 그대로)
    - conf_min: 낮추면 더 많이 잡히지만 오탐 증가
    """
    _resolve_tesseract()

    target_n = _norm(target)
    if not target_n:
        return []

    raw_img = _bgra_to_rgb_image(bgra)
    pre_img, _scale = _preprocess_image_for_ocr(raw_img)
    _save_ocr_debug_images(raw_img, pre_img, debug_tag=debug_tag)

    raw_boxes: List[Box] = []
    processed_boxes: List[Box] = []
    for variant in _ocr_variants(raw_img):
        for psm in _ocr_psms():
            boxes_for_variant = _image_to_boxes(
                variant.image,
                lang=lang,
                variant=f"{variant.name}_psm{psm}",
                scale=variant.scale,
                psm=psm,
                offset_x=variant.offset_x,
                offset_y=variant.offset_y,
            )
            if variant.name == "raw":
                raw_boxes.extend(boxes_for_variant)
            else:
                processed_boxes.extend(boxes_for_variant)

    raw_boxes = _dedupe_boxes(raw_boxes)
    processed_boxes = _dedupe_boxes(processed_boxes)
    merged_boxes = _merged_line_boxes(raw_boxes + processed_boxes)
    _print_ocr_debug(target, lang, raw_boxes, processed_boxes + merged_boxes)

    boxes: List[Box] = []
    seen: set[tuple[int, int, int, int, str]] = set()
    for box in raw_boxes + processed_boxes + merged_boxes:
        if box.conf < conf_min:
            continue
        if not _box_matches(box.text, target_n, allow_contains):
            continue
        key = (box.x, box.y, box.w, box.h, _norm(box.text))
        if key in seen:
            continue
        seen.add(key)
        boxes.append(box)

    boxes.sort(key=lambda box: _match_sort_key(box, target_n))
    filtered_boxes: List[Box] = []
    for box in boxes:
        if any(_overlap_fraction(box, accepted) >= 0.6 for accepted in filtered_boxes):
            continue
        filtered_boxes.append(box)
    boxes = filtered_boxes

    if _env_bool("AKO_OCR_DEBUG", True):
        print(f"[OCR MATCH] target={target!r} conf_min={conf_min:.1f} matches={len(boxes)}")
        for box in boxes:
            print(
                "[OCR MATCH] "
                f"variant={box.variant} text={_console_safe(repr(box.text))} conf={box.conf:.1f} "
                f"x={box.x} y={box.y} w={box.w} h={box.h}"
            )
    return boxes


def pick_by_direction(boxes: List[Box], direction: Optional[str]) -> Optional[Box]:
    """
    boxes가 여러 개면 방향으로 하나 선택한다.
    direction 예:
      - 왼쪽/오른쪽/위/아래
      - 왼쪽위/오른쪽위/왼쪽아래/오른쪽아래
      - 좌상/우상/좌하/우하
    """
    if not boxes:
        return None

    if not direction:
        return max(boxes, key=lambda box: box.conf)

    aliases = {
        "좌": "left",
        "왼쪽": "left",
        "우": "right",
        "오른쪽": "right",
        "상": "up",
        "위": "up",
        "하": "down",
        "아래": "down",
        "좌상": "upleft",
        "좌상단": "upleft",
        "왼쪽위": "upleft",
        "우상": "upright",
        "우상단": "upright",
        "오른쪽위": "upright",
        "좌하": "downleft",
        "좌하단": "downleft",
        "왼쪽아래": "downleft",
        "우하": "downright",
        "우하단": "downright",
        "오른쪽아래": "downright",
    }
    key = aliases.get(direction.replace(" ", ""), direction)

    if key == "left":
        return min(boxes, key=lambda box: box.cx)
    if key == "right":
        return max(boxes, key=lambda box: box.cx)
    if key == "up":
        return min(boxes, key=lambda box: box.cy)
    if key == "down":
        return max(boxes, key=lambda box: box.cy)
    if key == "upleft":
        return min(boxes, key=lambda box: box.cx + box.cy)
    if key == "upright":
        return min(boxes, key=lambda box: (-box.cx) + box.cy)
    if key == "downleft":
        return min(boxes, key=lambda box: box.cx - box.cy)
    if key == "downright":
        return min(boxes, key=lambda box: (-box.cx) - box.cy)

    return max(boxes, key=lambda box: box.conf)
