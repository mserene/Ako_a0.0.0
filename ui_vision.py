from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from typing import List, Optional

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
        return np.array(sct.grab(mon))


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
    raw = os.getenv("AKO_OCR_PSMS", "6,11")
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


def _preprocess_image_for_ocr(img: Image.Image) -> tuple[Image.Image, float]:
    scale = _env_float("AKO_OCR_UPSCALE", 2.0, minimum=1.0, maximum=4.0)
    out = img.convert("L")
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


def _save_ocr_debug_images(raw_img: Image.Image, pre_img: Image.Image) -> None:
    if not _env_bool("AKO_OCR_SAVE_DEBUG_IMAGES", True):
        return
    folder = _debug_dir()
    raw_path = os.path.abspath(os.path.join(folder, "debug_ocr_input_raw.png"))
    pre_path = os.path.abspath(os.path.join(folder, "debug_ocr_input_preprocessed.png"))
    raw_img.save(raw_path)
    pre_img.save(pre_path)
    print(f"[OCR DEBUG] raw image: {raw_path} size={raw_img.size}")
    print(f"[OCR DEBUG] preprocessed image: {pre_path} size={pre_img.size}")


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
        left = int(float(data["left"][index]) / scale)
        top = int(float(data["top"][index]) / scale)
        width = max(1, int(float(data["width"][index]) / scale))
        height = max(1, int(float(data["height"][index]) / scale))
        boxes.append(Box(raw_text, left, top, width, height, confidence, variant=variant))
    return boxes


def _print_ocr_debug(target: str, lang: str, raw_boxes: List["Box"], pre_boxes: List["Box"]) -> None:
    if not _env_bool("AKO_OCR_DEBUG", True):
        return
    print(f"[OCR DEBUG] engine=tesseract cmd={pytesseract.pytesseract.tesseract_cmd} lang={lang} target={target!r}")
    try:
        langs = pytesseract.get_languages(config="")
        print(f"[OCR DEBUG] available_langs={langs}")
    except Exception as e:
        print(f"[OCR DEBUG] available_langs_error={type(e).__name__}: {e}")

    def emit(label: str, boxes: List[Box]) -> None:
        print(f"[OCR RAW] {label} count={len(boxes)}")
        for box in boxes:
            print(
                "[OCR RAW] "
                f"{label} "
                f"text={_console_safe(repr(box.text))} "
                f"conf={box.conf:.1f} "
                f"x={box.x} y={box.y} w={box.w} h={box.h}"
            )

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


def _box_matches(box_text: str, target: str, allow_contains: bool) -> bool:
    box_n = _norm(box_text)
    target_n = _norm(target)
    if not box_n or not target_n:
        return False
    if box_n == target_n:
        return True
    if allow_contains and target_n in box_n:
        return True
    if allow_contains and box_n in target_n and len(box_n) >= max(2, len(target_n) - 1):
        return True
    if not _env_bool("AKO_OCR_FUZZY", True):
        return False
    threshold = _env_float("AKO_OCR_FUZZY_THRESHOLD", 0.72, minimum=0.0, maximum=1.0)
    return _similarity(box_n, target_n) >= threshold


def find_text_boxes(
    bgra: np.ndarray,
    target: str,
    lang: str = "kor+eng",
    conf_min: float = 50.0,
    allow_contains: bool = True,
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
    pre_img, scale = _preprocess_image_for_ocr(raw_img)
    _save_ocr_debug_images(raw_img, pre_img)

    raw_boxes: List[Box] = []
    pre_boxes: List[Box] = []
    for psm in _ocr_psms():
        raw_boxes.extend(_image_to_boxes(raw_img, lang=lang, variant=f"raw_psm{psm}", scale=1.0, psm=psm))
        pre_boxes.extend(_image_to_boxes(pre_img, lang=lang, variant=f"preprocessed_psm{psm}", scale=scale, psm=psm))
    _print_ocr_debug(target, lang, raw_boxes, pre_boxes)

    boxes: List[Box] = []
    seen: set[tuple[int, int, int, int, str]] = set()
    for box in raw_boxes + pre_boxes:
        if box.conf < conf_min:
            continue
        if not _box_matches(box.text, target_n, allow_contains):
            continue
        key = (box.x, box.y, box.w, box.h, _norm(box.text))
        if key in seen:
            continue
        seen.add(key)
        boxes.append(box)

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
