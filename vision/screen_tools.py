"""
screen_tools.py  –  Ako-ai Vision 텍스트 탐색 (OCR 고도화 버전)
----------------------------------------------------------------
변경 사항:
  - find_text_on_screen(): ui_vision 대신 ocr_engine 사용
  - read_screen_text()   : ocr_engine.run_ocr() 기반
  - save_screenshot()    : 그대로 유지 (mss 직접 캡처)
  - ScreenTextHit        : 기존 인터페이스 완전 호환 유지
  - highlight_overlay.py : 변경 없음 (HighlightRect 변환 헬퍼 추가)

ui_vision 모듈이 없는 환경에서도 동작합니다.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import numpy as np
from PIL import Image

# ── ocr_engine (이 패키지 내 고도화 모듈) ──
from ocr_engine import MatchResult, OCRResult, capture_screen, find_text, run_ocr

# ── ui_vision은 grab_screen / monitor_bounds 용으로만 optional import ──
try:
    from ui_vision import all_monitor_bounds, grab_screen, grab_screen_with_origin, monitor_bounds as _monitor_bounds
    _HAS_UI_VISION = True
except ImportError:
    _HAS_UI_VISION = False


# ──────────────────────────────────────────────
# 데이터클래스 (기존 인터페이스 완전 호환)
# ──────────────────────────────────────────────
@dataclass(frozen=True)
class ScreenTextHit:
    text:    str
    x:       int
    y:       int
    w:       int
    h:       int
    conf:    float
    variant: str = "raw"

    @classmethod
    def from_match(cls, m: MatchResult) -> "ScreenTextHit":
        x1, y1, x2, y2 = m.bbox
        return cls(
            text=m.text,
            x=x1, y=y1,
            w=max(1, x2 - x1),
            h=max(1, y2 - y1),
            conf=m.score / 100.0,
            variant="ocr_engine",
        )

    @classmethod
    def from_ocr(cls, r: OCRResult) -> "ScreenTextHit":
        x1, y1, x2, y2 = r.bbox
        return cls(
            text=r.text,
            x=x1, y=y1,
            w=max(1, x2 - x1),
            h=max(1, y2 - y1),
            conf=r.confidence,
            variant="raw",
        )


# ──────────────────────────────────────────────
# 내부 유틸
# ──────────────────────────────────────────────
def default_monitor_index() -> int:
    try:
        return int(os.getenv("AKO_SCREEN_MONITOR_INDEX", "0"))
    except Exception:
        return 0


def _get_monitor_region(monitor_index: int) -> tuple[int, int, int, int] | None:
    """(left, top, width, height) 반환. ui_vision 없으면 None."""
    if not _HAS_UI_VISION:
        return None
    try:
        left, top, width, height = _monitor_bounds(monitor_index)
        return left, top, width, height
    except Exception:
        return None


def _region_from_monitor(monitor_index: int) -> tuple[int, int, int, int] | None:
    """ocr_engine.capture_screen 에 넘길 (x1,y1,x2,y2) 형태로 변환."""
    info = _get_monitor_region(monitor_index)
    if info is None:
        return None
    left, top, width, height = info
    return left, top, left + width, top + height


# ──────────────────────────────────────────────
# 스크린샷 저장 (기존 동작 유지)
# ──────────────────────────────────────────────
def save_screenshot(
    monitor_index: int | None = None,
    output_dir: Optional[str] = None,
) -> str:
    if monitor_index is None:
        monitor_index = default_monitor_index()

    region = _region_from_monitor(monitor_index)
    img_bgr, _, _ = capture_screen(region)                # BGR numpy

    # BGR → RGB PIL
    import cv2
    rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
    pil = Image.fromarray(rgb)

    if output_dir:
        folder = Path(output_dir)
    else:
        folder = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Pictures" / "Ako"
    folder.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    path = folder / f"Ako_{stamp}.png"
    pil.save(path)
    return str(path)


# ──────────────────────────────────────────────
# 화면 텍스트 전체 읽기
# ──────────────────────────────────────────────
def read_screen_text(
    monitor_index: int | None = None,
    lang: str = "kor+eng",          # 하위 호환용, ocr_engine은 내부적으로 처리
) -> List[str]:
    """화면에서 인식된 모든 텍스트 라인 반환."""
    if monitor_index is None:
        monitor_index = default_monitor_index()

    region = _region_from_monitor(monitor_index)
    results = run_ocr(region=region, tile=True, debug=False)
    return [r.text for r in results if r.text.strip()]


# ──────────────────────────────────────────────
# 텍스트 위치 탐색  ← 핵심 함수
# ──────────────────────────────────────────────
def find_text_on_screen(
    target: str,
    monitor_index: int | None = None,
    lang: str = "kor+eng",          # 하위 호환용
    conf_min: float = 35.0,
    fuzzy_threshold: float = 80.0,
    debug: bool = True,
) -> List[ScreenTextHit]:
    """
    화면에서 target 텍스트를 fuzzy 검색해 ScreenTextHit 리스트로 반환.

    Parameters
    ----------
    target          : 검색할 텍스트 (예: "평달")
    monitor_index   : 0=전체, 1~N=특정 모니터
    conf_min        : OCR confidence 하한 (0~100)
    fuzzy_threshold : rapidfuzz 유사도 하한 (기본 80)
    debug           : True = debug_ocr/ 에 이미지 저장

    Returns
    -------
    List[ScreenTextHit]  –  highlight_overlay.py 에 바로 전달 가능
    """
    if monitor_index is None:
        monitor_index = default_monitor_index()

    # ── 전체 모니터 순회 모드 (index == 0) ──
    if monitor_index == 0 and _HAS_UI_VISION:
        hits: List[ScreenTextHit] = []
        for index, _left, _top, _width, _height in all_monitor_bounds():
            if index == 0:
                continue
            hits.extend(
                find_text_on_screen(
                    target,
                    monitor_index=index,
                    lang=lang,
                    conf_min=conf_min,
                    fuzzy_threshold=fuzzy_threshold,
                    debug=debug,
                )
            )
            if hits and os.getenv(
                "AKO_OCR_STOP_AFTER_FIRST_MONITOR", "1"
            ).strip().lower() not in {"0", "false", "no", "off"}:
                break
        return hits

    # ── 단일 모니터 OCR ──
    region = _region_from_monitor(monitor_index) if monitor_index != 0 else None

    # OCR 실행
    ocr_results = run_ocr(region=region, tile=True, debug=debug)

    # confidence 필터
    filtered = [r for r in ocr_results if r.confidence * 100 >= conf_min]
    print(f"[SCREEN] conf_min={conf_min} 필터 후: {len(filtered)}/{len(ocr_results)}개")

    # fuzzy 매칭
    matches = find_text(
        target,
        ocr_results=filtered,
        threshold=fuzzy_threshold,
        debug=False,   # run_ocr 에서 이미 debug 출력됨
    )

    hits = [ScreenTextHit.from_match(m) for m in matches]

    # ── overlay count 검증 로그 ──
    print(
        f"[SCREEN] '{target}' 탐색 완료: "
        f"OCR {len(ocr_results)}개 → 필터 {len(filtered)}개 → "
        f"매칭 {len(matches)}개 → overlay 표시 {len(hits)}개"
    )

    return hits


# ──────────────────────────────────────────────
# 모니터 bounds (기존 인터페이스 유지)
# ──────────────────────────────────────────────
def get_screen_bounds(monitor_index: int | None = None) -> tuple[int, int, int, int]:
    if monitor_index is None:
        monitor_index = default_monitor_index()
    if _HAS_UI_VISION:
        return _monitor_bounds(monitor_index)
    # fallback: mss 로 직접 조회
    try:
        import mss
        with mss.mss() as sct:
            m = sct.monitors[monitor_index] if monitor_index < len(sct.monitors) else sct.monitors[1]
            return m["left"], m["top"], m["width"], m["height"]
    except Exception:
        return 0, 0, 1920, 1080


# ──────────────────────────────────────────────
# highlight_overlay.py 연결 헬퍼
# ──────────────────────────────────────────────
def hits_to_highlight_rects(hits: List[ScreenTextHit]):
    """
    ScreenTextHit 리스트 → HighlightRect 리스트 변환.

    사용 예:
        from vision.highlight_overlay import show_highlights
        from vision.screen_tools import find_text_on_screen, hits_to_highlight_rects

        hits  = find_text_on_screen("평달")
        rects = hits_to_highlight_rects(hits)
        left, top, w, h = get_screen_bounds()
        show_highlights(rects, w, h, monitor_left=left, monitor_top=top)
    """
    from vision.highlight_overlay import HighlightRect
    return [
        HighlightRect(
            x=hit.x, y=hit.y,
            w=hit.w, h=hit.h,
            label=f"{hit.text} ({hit.conf*100:.0f}%)",
        )
        for hit in hits
    ]


# ──────────────────────────────────────────────
# 기존 extract_quoted_or_target 유지
# ──────────────────────────────────────────────
def extract_quoted_or_target(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    quoted = re.search(r"[\"'""'''](?P<target>.+?)[\"'""''']", raw)
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
