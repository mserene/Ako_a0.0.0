# OCR 엔진 업그레이드 가이드

## 변경 파일

| 파일 | 변경 내용 |
|---|---|
| `ui_vision.py` | **엔진 교체** (pytesseract → PaddleOCR) |
| `screen_tools.py` | **변경 없음** — 기존 그대로 사용 |
| `vision/highlight_overlay.py` | **변경 없음** |

`ocr_engine.py` / `ui_vision_patch.py` 파일이 있다면 삭제해도 됩니다.

---

## 설치 (한 번만)

```bash
# PaddleOCR (권장)
pip install paddlepaddle paddleocr

# GPU 버전 (선택)
pip install paddlepaddle-gpu paddleocr

# 없을 경우 자동 fallback → EasyOCR
pip install easyocr

# opencv (전처리용, 이미 있으면 skip)
pip install opencv-python
```

---

## 적용

```
프로젝트 루트/
├── ui_vision.py        ← 이 파일만 교체
├── screen_tools.py     ← 그대로
└── vision/
    └── highlight_overlay.py  ← 그대로
```

`ui_vision.py`를 출력된 파일로 교체하면 끝입니다.

---

## 엔진 우선순위 (자동)

```
PaddleOCR  →  EasyOCR  →  pytesseract
```

시작 시 콘솔에 아래 중 하나가 출력됩니다:

```
[OCR-Engine] PaddleOCR 로드 완료 ✓
[OCR-Engine] EasyOCR 로드 완료 ✓
[OCR-Engine] pytesseract fallback 사용
```

---

## 변경된 함수 (2개만)

### `_image_to_boxes()`
- 기존: `pytesseract.image_to_data()` 직접 호출
- 변경: `_get_engine()` → PaddleOCR / EasyOCR / pytesseract 자동 선택
- **시그니처·반환 타입 동일** — 나머지 코드 수정 불필요

### `ocr_lines()`
- 기존: `pytesseract.image_to_string()` 직접 호출
- 변경: 동일하게 엔진 자동 선택
- **시그니처·반환 타입 동일**

---

## 기존 로직 유지 항목 (변경 없음)

- tiling (`AKO_OCR_TILE=True`)
- grid scan (`AKO_OCR_GRID=True`)
- inverted variant
- merged line boxes (한국어 글자 분리 대응)
- deduplication
- fuzzy matching (`AKO_OCR_FUZZY`, `AKO_OCR_FUZZY_THRESHOLD`)
- debug image 저장 (`debug_ocr/`)
- 모든 환경변수 (`AKO_OCR_*`)

---

## 환경변수 튜닝 (선택)

| 변수 | 기본값 | 설명 |
|---|---|---|
| `AKO_OCR_TILE` | `1` | 타일 분할 OCR 활성화 |
| `AKO_OCR_TILE_UPSCALE` | `3.0` | 타일 업스케일 배율 |
| `AKO_OCR_UPSCALE` | `2.0` | 전체 이미지 업스케일 배율 |
| `AKO_OCR_FUZZY` | `1` | fuzzy matching 활성화 |
| `AKO_OCR_FUZZY_THRESHOLD` | `0.72` | 유사도 하한 (0~1) |
| `AKO_OCR_DEBUG` | `1` | 콘솔 raw dump 출력 |
| `AKO_OCR_SAVE_DEBUG_IMAGES` | `1` | debug_ocr/ 이미지 저장 |
| `AKO_OCR_GRID` | `0` | 그리드 분할 활성화 |

---

## 테스트

```bash
# Python에서 직접 테스트
python - <<'EOF'
from ui_vision import find_text_boxes, grab_screen
bgra = grab_screen(1)
boxes = find_text_boxes(bgra, "평달", conf_min=40)
print(f"발견: {len(boxes)}개")
for b in boxes:
    print(f"  '{b.text}'  conf={b.conf:.1f}  ({b.x},{b.y})")
EOF
```
