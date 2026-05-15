"""
ocr_engine.py  –  Ako-ai Vision OCR 고도화 모듈
--------------------------------------------------
기존 screen_tools.py 의 OCR 부분을 이 모듈로 교체하세요.

우선순위 반영:
  1. OCR raw dump + debug image 저장
  2. OpenCV 전처리 (grayscale → upscale → threshold → sharpen)
  3. PaddleOCR 기반 (없으면 EasyOCR → pytesseract 순 fallback)
  4. scan tiling (화면 4분할 → 결과 병합·중복제거)
  5. rapidfuzz fuzzy matching
  6. DPI awareness
  7. detect / match / overlay count 검증 로그

의존 패키지 설치 (한 번만):
  pip install paddlepaddle paddleocr
  pip install easyocr                   # fallback
  pip install rapidfuzz
  pip install opencv-python pillow mss pyautogui
"""

from __future__ import annotations

import ctypes
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

# ──────────────────────────────────────────────
# 0. DPI Awareness  (Windows 전용, 필수)
# ──────────────────────────────────────────────
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)   # PROCESS_PER_MONITOR_DPI_AWARE
    print("[DPI] SetProcessDpiAwareness(2) 적용 완료")
except Exception as e:
    print(f"[DPI] 설정 실패(비-Windows 환경이거나 권한 부족): {e}")


# ──────────────────────────────────────────────
# 디버그 저장 폴더
# ──────────────────────────────────────────────
DEBUG_DIR = Path("debug_ocr")
DEBUG_DIR.mkdir(exist_ok=True)


# ──────────────────────────────────────────────
# 1. OCR 엔진 로드  (PaddleOCR → EasyOCR → Tesseract)
# ──────────────────────────────────────────────
class _EngineLoader:
    """지연 로딩 + 자동 fallback"""

    def __init__(self):
        self._paddle = None
        self._easy   = None
        self._mode   = None

    def get(self):
        if self._mode:
            return self._mode, self._paddle or self._easy or None

        # ① PaddleOCR
        try:
            from paddleocr import PaddleOCR
            self._paddle = PaddleOCR(use_angle_cls=True, lang="korean", show_log=False)
            self._mode = "paddle"
            print("[OCR-Engine] PaddleOCR 로드 성공 ✓")
            return self._mode, self._paddle
        except ImportError:
            print("[OCR-Engine] PaddleOCR 없음 → EasyOCR 시도")
        except Exception as e:
            print(f"[OCR-Engine] PaddleOCR 로드 실패: {e} → EasyOCR 시도")

        # ② EasyOCR
        try:
            import easyocr
            self._easy = easyocr.Reader(["ko", "en"], gpu=False, verbose=False)
            self._mode = "easy"
            print("[OCR-Engine] EasyOCR 로드 성공 ✓")
            return self._mode, self._easy
        except ImportError:
            print("[OCR-Engine] EasyOCR 없음 → pytesseract fallback")
        except Exception as e:
            print(f"[OCR-Engine] EasyOCR 로드 실패: {e} → pytesseract fallback")

        # ③ Tesseract (비추천이지만 fallback)
        self._mode = "tesseract"
        print("[OCR-Engine] pytesseract fallback 사용 (PaddleOCR 설치 권장)")
        return self._mode, None


_loader = _EngineLoader()


# ──────────────────────────────────────────────
# 2. 결과 데이터클래스
# ──────────────────────────────────────────────
@dataclass
class OCRResult:
    text:       str
    confidence: float
    bbox:       tuple   # (x1, y1, x2, y2) – 절대 화면 좌표

    def __repr__(self):
        return f'OCRResult("{self.text}", conf={self.confidence:.2f}, bbox={self.bbox})'


@dataclass
class MatchResult:
    text:  str
    score: float   # 유사도 0~100
    bbox:  tuple   # (x1, y1, x2, y2)
    cx:    int = field(init=False)
    cy:    int = field(init=False)

    def __post_init__(self):
        x1, y1, x2, y2 = self.bbox
        self.cx = (x1 + x2) // 2
        self.cy = (y1 + y2) // 2

    def __repr__(self):
        return f'Match("{self.text}", score={self.score:.1f}, center=({self.cx},{self.cy}))'


# ──────────────────────────────────────────────
# 3. 이미지 전처리
# ──────────────────────────────────────────────
def preprocess(img_bgr: np.ndarray, scale: float = 2.0) -> np.ndarray:
    """
    grayscale → CLAHE 대비 향상 → upscale → adaptive-threshold → sharpen
    한국어 소획 손실 최소화를 위해 업스케일이 핵심.
    """
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)

    # 업스케일: 작은 UI 글씨 인식률 핵심
    h, w = gray.shape
    gray = cv2.resize(
        gray,
        (int(w * scale), int(h * scale)),
        interpolation=cv2.INTER_CUBIC,
    )

    # CLAHE: 어두운 배경 / 반투명 UI 대비 향상
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    gray = clahe.apply(gray)

    # Adaptive threshold: 밝기 불균일 화면 대응
    binary = cv2.adaptiveThreshold(
        gray, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        blockSize=15, C=8,
    )

    # Sharpen
    kernel = np.array([[0, -1, 0],
                        [-1, 5, -1],
                        [0, -1, 0]])
    return cv2.filter2D(binary, -1, kernel)


# ──────────────────────────────────────────────
# 4. 단일 이미지 → OCRResult 리스트
# ──────────────────────────────────────────────
def _ocr_single(
    img_bgr:         np.ndarray,
    offset_x:        int = 0,
    offset_y:        int = 0,
    scale:           float = 2.0,
    debug_save_path: Optional[str] = None,
) -> list[OCRResult]:
    """
    img_bgr   : BGR numpy array (화면 전체 또는 타일)
    offset_x/y: 타일 분할 시 원본 좌표 보정값
    scale     : 전처리 업스케일 배율
    """
    proc = preprocess(img_bgr, scale=scale)

    if debug_save_path:
        cv2.imwrite(debug_save_path, proc)
        print(f"[OCR-Debug] 전처리 이미지 저장 → {debug_save_path}")

    mode, engine = _loader.get()
    results: list[OCRResult] = []

    # ── PaddleOCR ──────────────────────────────
    if mode == "paddle":
        raw = engine.ocr(proc, cls=True)
        if raw and raw[0]:
            for line in raw[0]:
                pts, (txt, conf) = line
                xs = [p[0] for p in pts]
                ys = [p[1] for p in pts]
                x1 = int(min(xs) / scale) + offset_x
                y1 = int(min(ys) / scale) + offset_y
                x2 = int(max(xs) / scale) + offset_x
                y2 = int(max(ys) / scale) + offset_y
                results.append(OCRResult(txt, float(conf), (x1, y1, x2, y2)))

    # ── EasyOCR ────────────────────────────────
    elif mode == "easy":
        raw = engine.readtext(proc, detail=1)
        for (pts, txt, conf) in raw:
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            x1 = int(min(xs) / scale) + offset_x
            y1 = int(min(ys) / scale) + offset_y
            x2 = int(max(xs) / scale) + offset_x
            y2 = int(max(ys) / scale) + offset_y
            results.append(OCRResult(txt, float(conf), (x1, y1, x2, y2)))

    # ── Tesseract (fallback) ───────────────────
    else:
        import pytesseract
        data = pytesseract.image_to_data(
            proc,
            lang="kor+eng",
            output_type=pytesseract.Output.DICT,
            config="--psm 6",
        )
        for i, txt in enumerate(data["text"]):
            txt = txt.strip()
            if not txt:
                continue
            conf = float(data["conf"][i])
            if conf < 0:
                continue
            x1 = int(data["left"][i]    / scale) + offset_x
            y1 = int(data["top"][i]     / scale) + offset_y
            x2 = x1 + int(data["width"][i]  / scale)
            y2 = y1 + int(data["height"][i] / scale)
            results.append(OCRResult(txt, conf / 100.0, (x1, y1, x2, y2)))

    return results


# ──────────────────────────────────────────────
# 5. 전체 화면 캡처
# ──────────────────────────────────────────────
def capture_screen(region: Optional[tuple] = None) -> tuple[np.ndarray, int, int]:
    """
    DPI-aware 화면 캡처.
    Returns (img_bgr, offset_x, offset_y)
    region: (x1, y1, x2, y2) or None for full screen
    """
    try:
        import mss
        with mss.mss() as sct:
            if region:
                x1, y1, x2, y2 = region
                mon = {"top": y1, "left": x1, "width": x2 - x1, "height": y2 - y1}
                ox, oy = x1, y1
            else:
                mon = sct.monitors[1]
                ox, oy = 0, 0
            shot = sct.grab(mon)
            img = cv2.cvtColor(np.array(shot), cv2.COLOR_BGRA2BGR)
            return img, ox, oy
    except ImportError:
        from PIL import ImageGrab
        if region:
            pil = ImageGrab.grab(bbox=region)
            ox, oy = region[0], region[1]
        else:
            pil = ImageGrab.grab()
            ox, oy = 0, 0
        img = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
        return img, ox, oy


# ──────────────────────────────────────────────
# 6. 타일 중복 제거
# ──────────────────────────────────────────────
def _deduplicate(results: list[OCRResult], iou_thresh: float = 0.5) -> list[OCRResult]:
    """타일 경계 중복 bbox 제거 (IoU 기반, confidence 높은 쪽 우선)"""

    def iou(a: OCRResult, b: OCRResult) -> float:
        ax1, ay1, ax2, ay2 = a.bbox
        bx1, by1, bx2, by2 = b.bbox
        ix1 = max(ax1, bx1); iy1 = max(ay1, by1)
        ix2 = min(ax2, bx2); iy2 = min(ay2, by2)
        inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
        if inter == 0:
            return 0.0
        area_a = max(1, (ax2 - ax1) * (ay2 - ay1))
        area_b = max(1, (bx2 - bx1) * (by2 - by1))
        return inter / (area_a + area_b - inter + 1e-6)

    kept: list[OCRResult] = []
    for r in sorted(results, key=lambda x: -x.confidence):
        if all(iou(r, k) < iou_thresh for k in kept):
            kept.append(r)
    return kept


# ──────────────────────────────────────────────
# 7. 메인 OCR 함수
# ──────────────────────────────────────────────
def run_ocr(
    region:    Optional[tuple] = None,
    scale:     float = 2.0,
    tile:      bool  = True,
    tile_rows: int   = 2,
    tile_cols: int   = 2,
    debug:     bool  = True,
) -> list[OCRResult]:
    """
    메인 OCR 함수.

    Parameters
    ----------
    region    : (x1, y1, x2, y2) 캡처 영역. None 이면 전체 화면.
    scale     : 전처리 업스케일 배율 (기본 2x)
    tile      : True = 화면 분할 OCR (고해상도 권장)
    tile_rows : 세로 분할 수
    tile_cols : 가로 분할 수
    debug     : True = 전처리 이미지를 debug_ocr/ 에 저장

    Returns
    -------
    list[OCRResult] – 화면 절대 좌표 포함
    """
    full, ox, oy = capture_screen(region)
    h, w = full.shape[:2]
    ts = int(time.time())

    print(f"[OCR] 캡처 크기: {w}x{h}  |  엔진: {_loader._mode}  |  "
          f"tile: {tile} ({tile_rows}x{tile_cols})")

    # 원본 캡처 저장
    if debug:
        raw_path = str(DEBUG_DIR / f"raw_{ts}.png")
        cv2.imwrite(raw_path, full)
        print(f"[OCR-Debug] 원본 캡처 저장 → {raw_path}")

    all_results: list[OCRResult] = []

    if not tile:
        dbg = str(DEBUG_DIR / f"proc_{ts}.png") if debug else None
        all_results = _ocr_single(full, ox, oy, scale=scale, debug_save_path=dbg)
    else:
        th = h // tile_rows
        tw = w // tile_cols
        for row in range(tile_rows):
            for col in range(tile_cols):
                ty1 = row * th
                ty2 = ty1 + th if row < tile_rows - 1 else h
                tx1 = col * tw
                tx2 = tx1 + tw if col < tile_cols - 1 else w

                tile_img = full[ty1:ty2, tx1:tx2]
                dbg = str(DEBUG_DIR / f"tile_{row}{col}_{ts}.png") if debug else None
                results = _ocr_single(
                    tile_img,
                    offset_x=ox + tx1,
                    offset_y=oy + ty1,
                    scale=scale,
                    debug_save_path=dbg,
                )
                all_results.extend(results)

        all_results = _deduplicate(all_results)

    # Raw dump 출력
    print(f"\n[OCR-RAW] 총 {len(all_results)}개 감지:")
    for r in all_results:
        print(f"  [OCR] '{r.text}'  conf={r.confidence:.2f}  bbox={r.bbox}")
    print()

    return all_results


# ──────────────────────────────────────────────
# 8. Fuzzy 검색
# ──────────────────────────────────────────────
def find_text(
    query:       str,
    ocr_results: Optional[list[OCRResult]] = None,
    threshold:   float = 80.0,
    region:      Optional[tuple] = None,
    scale:       float = 2.0,
    tile:        bool  = True,
    debug:       bool  = True,
) -> list[MatchResult]:
    """
    화면(또는 사전 OCR 결과)에서 query 텍스트를 fuzzy 검색.

    Parameters
    ----------
    query       : 검색할 텍스트 (예: "평달")
    ocr_results : 이미 실행한 OCR 결과. None 이면 run_ocr() 재실행.
    threshold   : rapidfuzz 유사도 하한선 (기본 80)

    Returns
    -------
    list[MatchResult] – 발견된 모든 위치 (overlay 에 바로 전달 가능)
    """
    try:
        from rapidfuzz import fuzz
        _fuzzy = True
    except ImportError:
        print("[Fuzzy] rapidfuzz 없음 → exact/partial match fallback")
        _fuzzy = False

    if ocr_results is None:
        ocr_results = run_ocr(region=region, scale=scale, tile=tile, debug=debug)

    matches: list[MatchResult] = []
    for r in ocr_results:
        if _fuzzy:
            # partial_ratio: 긴 문자열 안에 query가 포함되는 경우도 매칭
            score = fuzz.partial_ratio(query, r.text)
        else:
            score = 100.0 if query in r.text else 0.0

        if score >= threshold:
            matches.append(MatchResult(r.text, float(score), r.bbox))

    # 단계별 카운트 로그 (overlay count 검증)
    print(f"[FIND] 검색어: '{query}'  |  threshold: {threshold}")
    print(f"[FIND] OCR 감지: {len(ocr_results)}개  →  매칭: {len(matches)}개  "
          f"(overlay 표시 예정: {len(matches)}개)")
    for m in matches:
        print(f"  [MATCH] {m}")

    return matches


# ──────────────────────────────────────────────
# 9. screen_tools.py 교체 방법 (주석)
# ──────────────────────────────────────────────
"""
# ── 기존 screen_tools.py 에서 교체하는 방법 ──────────────────────

# Before (기존):
#   screenshot = capture()
#   text = pytesseract.image_to_string(screenshot)
#   if query in text:
#       ...

# After (교체):
from ocr_engine import find_text, run_ocr

# 방법 A) 검색까지 한번에 (가장 간단)
matches = find_text("평달", threshold=80)
print(f"발견: {len(matches)}개")
for m in matches:
    draw_overlay(m.bbox)      # 기존 overlay 함수에 bbox 전달
    print(f"위치: ({m.cx}, {m.cy})")

# 방법 B) OCR 결과를 캐시해서 여러 단어 검색
ocr = run_ocr(tile=True, debug=True)
matches_a = find_text("평달",   ocr_results=ocr)
matches_b = find_text("홍길동", ocr_results=ocr)

# overlay count 검증은 자동 출력됨:
# [FIND] OCR 감지: 47개  →  매칭: 5개  (overlay 표시 예정: 5개)
"""

# ──────────────────────────────────────────────
# 10. 단독 실행 시 디버그 테스트
# ──────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    query = sys.argv[1] if len(sys.argv) > 1 else "평달"
    print(f"\n=== OCR 디버그 테스트: '{query}' ===\n")
    matches = find_text(query, threshold=75, tile=True, debug=True)
    print(f"\n=== 최종 결과: {len(matches)}개 ===")
    for m in matches:
        print(f"  위치 ({m.cx}, {m.cy})  유사도 {m.score:.1f}  텍스트: '{m.text}'")
