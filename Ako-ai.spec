# -*- mode: python ; coding: utf-8 -*-
from __future__ import annotations
import os
import sys
import PyInstaller.hooks
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

# ── 프로젝트 루트 ──────────────────────────────────────────────
ROOT = os.path.abspath(os.path.dirname(SPEC) if "SPEC" in dir() else os.getcwd())


# ── site-packages 탐색 (동적, 하드코딩 없음) ───────────────────
def find_site_packages() -> str:
    candidates = []

    env_site = os.environ.get("AKO_BUILD_SITE_PACKAGES", "")
    if env_site:
        candidates.append(env_site)

    candidates.extend([
        os.path.join(sys.prefix, "Lib", "site-packages"),
        os.path.join(ROOT, ".build_venv", "Lib", "site-packages"),
    ])

    for p in sys.path:
        if "site-packages" in p and os.path.isdir(p):
            candidates.append(p)

    candidates.extend([
        os.path.join(ROOT, ".venv",  "Lib", "site-packages"),
        os.path.join(ROOT, "venv",   "Lib", "site-packages"),
    ])

    seen = set()
    for c in candidates:
        c = os.path.abspath(c)
        key = os.path.normcase(c)
        if key in seen:
            continue
        seen.add(key)
        if os.path.isdir(c):
            return c
    return ""


SITE_PKG = find_site_packages()

# ── hidden imports ─────────────────────────────────────────────
hiddenimports = [
    # Ako 앱 모듈
    "ako_gui",
    "command_actions",
    "llm_agent",
    "ui_do",
    "ui_loop",
    "ui_tap",
    "ui_vision",
    "voice_loop",
    # vision 서브패키지
    "vision",
    "vision.highlight_overlay",
    "vision.screen_tools",
    # core 패키지 (AkoController 포함)
    *collect_submodules("core"),
    # 주요 외부 라이브러리
    "faster_whisper",
    "ctranslate2",
    "pyautogui",
    "pytesseract",
    "PIL",
    "PIL.Image",
    "PIL.ImageEnhance",
    "PIL.ImageFilter",
    "PIL.ImageOps",
    "mss",
    "numpy",
    "sounddevice",
    "requests",
    "ollama",
    # opencv (ui_vision 전처리용 — 필수)
    "cv2",
    # tkinter (highlight_overlay 사용)
    "tkinter",
    "tkinter.ttk",
    # 기타
    "difflib",
    "threading",
    "dataclasses",
]

# ── datas ──────────────────────────────────────────────────────
datas = []

def _add(src: str, dst: str) -> None:
    """존재하는 경우에만 datas에 추가."""
    if os.path.exists(src):
        datas.append((src, dst))
    else:
        print(f"[SPEC WARN] datas 경로 없음 (skip): {src}")

# 설정·명령 파일
_add(os.path.join(ROOT, "app_commands.json"),      ".")
_add(os.path.join(ROOT, "search_sites.json"),       ".")
_add(os.path.join(ROOT, "llm_agent.py"),            ".")

# 메모리 파일 (없으면 런타임에 자동 생성되지만 포함해두면 초기화 안전)
_add(os.path.join(ROOT, "assistant_memory.json"),   ".")
_add(os.path.join(ROOT, "assistant_prefs.json"),    ".")

# 아이콘
ico = os.path.join(ROOT, "assets", "ako.ico")
_add(ico, "assets")

# 로딩 애니메이션 프레임
loading_frames_dir = os.path.join(ROOT, "assets", "loading", "frames")
_add(loading_frames_dir, "assets/loading/frames")

# faster-whisper assets
if SITE_PKG:
    fw_assets = os.path.join(SITE_PKG, "faster_whisper", "assets")
    fw_vad    = os.path.join(SITE_PKG, "faster_whisper", "vad.py")
    _add(fw_assets, "faster_whisper/assets")
    _add(fw_vad,    "faster_whisper")

# 번들 Tesseract (tools/tesseract/ 전체)
tesseract_dir = os.path.join(ROOT, "tools", "tesseract")
_add(tesseract_dir, "tools/tesseract")

# ── Analysis ───────────────────────────────────────────────────
a = Analysis(
    [os.path.join(ROOT, "app.py")],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # torch/tf 등 대형 패키지 제외 (EasyOCR/PaddleOCR는 런타임 설치 권장)
    excludes=[
        "torch", "torchvision", "torchaudio",
        "tensorflow", "keras",
        "matplotlib", "pandas", "scipy",
        "IPython", "notebook",
        "easyocr", "paddleocr", "paddlepaddle",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Ako-ai",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,                  # GUI 모드 (디버그 필요 시 True로)
    disable_windowed_traceback=False,
    icon=ico if os.path.exists(ico) else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Ako-ai",
)
