from __future__ import annotations

import os
import re
from typing import List, Tuple


def _env_bool(name: str, default: bool = True) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off", "n"}


def _clean_spaces(text: str) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    text = re.sub(r"\s+([?.!,])", r"\1", text)
    return text.strip()


_OPEN_FOCUS_WORDS = (
    "열어", "켜", "실행", "띄워", "띄어", "뛰워", "뛰어", "튀워", "튀어",
    "앞으로", "포커스", "전면", "맨 앞", "맨앞",
)

_COMMAND_WORDS = _OPEN_FOCUS_WORDS + (
    "검색", "찾아", "눌러", "클릭", "재생", "정지", "일시정지",
)


def _looks_like_command(text: str) -> bool:
    return any(word in text for word in _COMMAND_WORDS)


_CHROME_CONFUSIONS = (
    "그럼", "그롬", "그룸", "그름", "구름", "크럼", "크름", "크론", "크롬이", "크로미",
)

_CHROME_PATTERNS = [
    re.compile(
        rf"^\s*(?P<wrong>{'|'.join(map(re.escape, _CHROME_CONFUSIONS))})\s*"
        rf"(?=(?:좀\s*)?(?:{'|'.join(map(re.escape, _OPEN_FOCUS_WORDS))}))"
    ),
    re.compile(
        rf"^\s*(?P<wrong>{'|'.join(map(re.escape, _CHROME_CONFUSIONS))})\s*"
        rf"(?=(?:좀\s*)?(?:검색|찾아))"
    ),
]

_VERB_FIXES: Tuple[Tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"띄어\s*봐"), "띄워봐"),
    (re.compile(r"띄어\s*줘"), "띄워줘"),
    (re.compile(r"뛰어\s*봐|뛰워\s*봐|튀어\s*봐|튀워\s*봐"), "띄워봐"),
    (re.compile(r"뛰어\s*줘|뛰워\s*줘|튀어\s*줘|튀워\s*줘"), "띄워줘"),
)


def correct_stt_text(text: str, *, return_reasons: bool = False):
    """음성 인식 결과를 명령 라우터에 넣기 전에 아주 보수적으로 보정한다.

    - 텍스트 채팅에는 쓰지 않고 음성 STT 결과에만 사용한다.
    - 앱 실행/포커스/검색처럼 명령 동사가 보일 때만 앱명 보정을 한다.
    - "그럼"은 실제 한국어 단어라서, 명령 동사가 바로 따라올 때만 "크롬"으로 본다.
    """
    original = _clean_spaces(text)
    if not original or not _env_bool("AKO_STT_AUTOCORRECT", True):
        return (original, []) if return_reasons else original

    s = original
    reasons: List[str] = []

    if _looks_like_command(s):
        for pat in _CHROME_PATTERNS:
            new_s = pat.sub("크롬 ", s, count=1)
            if new_s != s:
                reasons.append("app-name:chrome")
                s = new_s
                break

    for pat, repl in _VERB_FIXES:
        new_s = pat.sub(repl, s)
        if new_s != s:
            reasons.append("verb:띄워")
            s = new_s

    s = _clean_spaces(s)
    return (s, reasons) if return_reasons else s
