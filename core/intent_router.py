from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class IntentType(str, Enum):
    CHAT = "chat"
    SMALLTALK = "smalltalk"
    COMMAND = "command"
    MEMORY_UPDATE = "memory_update"
    SCREEN_STATUS = "screen_status"
    VOICE_STATUS = "voice_status"
    CAPABILITY = "capability"
    META = "meta"


@dataclass(frozen=True)
class IntentResult:
    intent: IntentType
    confidence: float = 0.5
    reason: str = ""


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").strip().lower())


def _has_any(text: str, words: tuple[str, ...]) -> bool:
    return any(word in text for word in words)


def _looks_like_memory_update(text: str, compact: str) -> bool:
    memory_markers = (
        "기억해", "기억해둬", "잊지마", "뜻이야", "의미야", "라고 말한건",
        "라고 말하는건", "라고 부르는건", "이라고 말한건", "이라고 부르는건",
        "다음부터", "앞으로", "이렇게 이해해", "이렇게 받아들여",
    )
    if _has_any(text, memory_markers):
        return True

    if re.search(r"나는\s+.+?(좋아해|싫어해|선호해|별로야|불편해)$", text):
        return True

    return False


def _looks_like_screen_status(text: str, compact: str) -> bool:
    screen_words = ("화면", "스크린", "모니터", "창", "ocr", "인식")
    status_words = ("보여", "볼수", "볼 수", "인식", "가능", "켜져", "상태", "보고있", "보고 있")
    return _has_any(text, screen_words) and _has_any(text, status_words)


def _looks_like_voice_status(text: str, compact: str) -> bool:
    voice_words = ("마이크", "음성", "목소리", "듣고", "듣는", "stt", "녹음")
    status_words = ("가능", "켜져", "상태", "듣고있", "듣고 있", "인식", "되냐", "돼")
    return _has_any(text, voice_words) and _has_any(text, status_words)


def _looks_like_capability(text: str, compact: str) -> bool:
    capability_words = ("뭐할수", "뭐 할 수", "할수있", "할 수 있", "기능", "가능한", "할줄", "할 줄")
    if _has_any(compact, tuple(w.replace(" ", "") for w in capability_words)):
        return True
    return _has_any(text, capability_words)


def _looks_like_smalltalk(text: str, compact: str) -> bool:
    """도움 요청이 아니라 감정/잡담 흐름인 짧은 말."""
    exacts = {
        "뭐해", "뭐함", "머해", "심심해", "졸려", "피곤해", "보고싶어",
        "뭔생각해", "무슨생각해", "뭐생각해", "먼생각해",
        "야", "아코야", "아코", "굿", "좋아", "고마워", "ㄱㅅ", "ㅇㅋ", "ㅇㅎ",
        "안녕", "ㅎㅇ", "하이", "오랜만", "오랜만이네",
        "일어났어", "깼어", "잤어", "자고있어", "자고있었어",
        "뭐생각중", "무슨생각중", "뭔생각중", "먼생각중",
    }
    if compact in exacts:
        return True

    phrases = (
        "뭔 생각해", "무슨 생각해", "뭐 생각해", "먼 생각해",
        "너 뭐해", "너 뭐 함", "나 심심", "나 졸려", "나 피곤",
        "보고 싶", "놀자", "말 걸어", "대화하자",
        "일어났어", "깼어", "자고 있어", "자고있어",
        "무슨 생각 중", "뭔 생각 중", "뭐 생각 중",
    )
    return _has_any(text, phrases)


def _looks_like_real_command(text: str, compact: str) -> bool:
    chat_like = (
        "말해봐", "불러봐", "따라해", "따라 해", "주인님 해봐", "대답해봐",
        "생각해봐", "설명해봐", "어떻게 생각", "뭐라고 생각",
    )
    if _has_any(text, chat_like):
        return False

    if _has_any(text, ("화면", "여기", "현재", "지금")) and _has_any(text, ("단어", "텍스트", "글자", "문구")) and _has_any(text, ("있니", "있어", "찾아", "강조", "하이라이트", "어디")):
        return True

    if "창" in text and _has_any(text, ("다", "모두", "전부")) and _has_any(text, ("내려", "최소화")):
        return True

    if "창" in text and _has_any(text, ("꺼줘", "닫아", "종료", "닫")):
        return True

    app_targets = (
        "크롬", "chrome", "유튜브", "youtube", "디스코드", "discord", "카카오톡",
        "메모장", "계산기", "스포티파이", "spotify", "치지직", "브라우저",
        "탐색기", "파일", "폴더", "창", "탭", "버튼", "링크", "주소창",
        "화면", "스크린", "모니터", "영상", "음악", "노래", "미디어",
    )
    action_words = (
        "열어", "켜줘", "켜", "꺼줘", "꺼", "실행", "재생", "일시정지",
        "눌러", "클릭", "닫아", "입력", "검색", "삭제", "가줘", "이동",
        "앞으로", "포커스", "띄워", "최소화", "최대화", "캡처", "캡쳐",
        "스크린샷", "찾아", "강조", "하이라이트", "멈춰", "넘겨",
    )

    if _has_any(text, app_targets) and _has_any(text, action_words):
        return True

    positions = ("왼쪽", "오른쪽", "위", "아래", "중앙", "가운데", "상단", "하단")
    if _has_any(text, positions) and _has_any(text, ("눌러", "클릭", "터치")):
        return True

    if "검색" in text and len(text) >= 4:
        return True

    return False


def classify_intent(text: str) -> IntentResult:
    raw = (text or "").strip()
    compact = _compact(raw)

    if not raw:
        return IntentResult(IntentType.CHAT, 0.1, "empty")

    if _looks_like_memory_update(raw, compact):
        return IntentResult(IntentType.MEMORY_UPDATE, 0.95, "explicit memory/correction marker")

    if _looks_like_screen_status(raw, compact):
        return IntentResult(IntentType.SCREEN_STATUS, 0.88, "screen status/capability question")

    if _looks_like_voice_status(raw, compact):
        return IntentResult(IntentType.VOICE_STATUS, 0.88, "voice status/capability question")

    if _looks_like_capability(raw, compact):
        return IntentResult(IntentType.CAPABILITY, 0.78, "capability question")

    if _looks_like_real_command(raw, compact):
        return IntentResult(IntentType.COMMAND, 0.86, "clear app/screen action command")

    if _looks_like_smalltalk(raw, compact):
        return IntentResult(IntentType.SMALLTALK, 0.82, "friendly smalltalk/emotional chat")

    return IntentResult(IntentType.CHAT, 0.65, "default chat")
