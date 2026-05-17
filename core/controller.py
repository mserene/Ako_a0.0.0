from __future__ import annotations

import json
import logging
import os
import re
import requests
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

from .intent_router import IntentType, classify_intent
from .memory_store import JsonMemoryStore

from voice_loop import (
    BootstrapStatus,
    VoiceConfig,
    ensure_whisper_model_async,
    gui_voice_loop,
)

LogFn = Callable[[str], None]
VoiceTextFn = Callable[[str], None]
VoiceErrorFn = Callable[[str], None]


def _run_actions(text: str) -> str:
    from app import run_actions
    return run_actions(text)


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


class ConversationHistory:
    def __init__(self, max_turns: int = 8):
        self._history = deque(maxlen=max_turns * 2)

    def add(self, role: str, content: str) -> None:
        content = (content or "").strip()
        if not content:
            return
        self._history.append({"role": role, "content": content})

    def get_messages(self, system_prompt: str) -> list[dict]:
        return [{"role": "system", "content": system_prompt}] + list(self._history)

    def clear(self) -> None:
        self._history.clear()


@dataclass
class AkoController:
    log_fn: Optional[LogFn] = None

    powered_on: bool = False
    voice_on: bool = False
    command_on: bool = False

    models_root: str = ""

    logs: list[str] = field(default_factory=list)
    bootstrap_status: BootstrapStatus = field(default_factory=BootstrapStatus)

    _voice_thread: Optional[threading.Thread] = field(default=None, init=False, repr=False)
    _voice_stop: Optional[threading.Event] = field(default=None, init=False, repr=False)
    _voice_starting: bool = field(default=False, init=False, repr=False)
    _voice_start_token: int = field(default=0, init=False, repr=False)
    _chat_history: ConversationHistory = field(
        default_factory=lambda: ConversationHistory(max_turns=8),
        init=False,
        repr=False,
    )
    _memory_store: JsonMemoryStore = field(
        default_factory=JsonMemoryStore,
        init=False,
        repr=False,
    )

    # ── config ───────────────────────────────────────────────────────────

    def set_models_root(self, path: str) -> None:
        self.models_root = (path or "").strip()
        if self.models_root:
            self.log(f"모델 저장 위치 설정: {self.models_root}")
        else:
            self.log("모델 저장 위치 기본값 사용")

    # ── logging ──────────────────────────────────────────────────────────

    def log(self, msg: str) -> None:
        line = f"[{_now()}] {msg}"
        self.logs.append(line)
        if self.log_fn:
            try:
                self.log_fn(line)
            except Exception:
                logging.exception("GUI log callback failed")

    # ── Ollama config ────────────────────────────────────────────────────

    def _get_ollama_model(self) -> str:
        return os.getenv("AKO_OLLAMA_MODEL", "exaone3.5:7.8b").strip() or "exaone3.5:7.8b"

    def _get_ollama_url(self) -> str:
        return os.getenv("AKO_OLLAMA_URL", "http://localhost:11434/api/chat").strip() or "http://localhost:11434/api/chat"

    def _ollama_options(self, *, warmup: bool = False) -> dict:
        def _env_int(name: str, default: int) -> int:
            try:
                value = int(os.getenv(name, str(default)))
                return max(1, value)
            except ValueError:
                return default

        options = {
            "num_ctx": _env_int("AKO_OLLAMA_NUM_CTX", 1024),
            "num_predict": 1 if warmup else _env_int("AKO_OLLAMA_NUM_PREDICT", 160),
        }

        try:
            options["temperature"] = float(os.getenv("AKO_OLLAMA_TEMPERATURE", "0.35"))
        except ValueError:
            options["temperature"] = 0.35

        return options

    def _ollama_chat_payload(
        self,
        *,
        model: str,
        messages: list[dict],
        stream: bool,
        warmup: bool = False,
    ) -> dict:
        payload = {
            "model": model,
            "stream": stream,
            "messages": messages,
            "keep_alive": "30m",
            "options": self._ollama_options(warmup=warmup),
        }

        disable_think = os.getenv("AKO_OLLAMA_DISABLE_THINK", "true").lower() not in {
            "0", "false", "no", "off",
        }
        if disable_think:
            payload["think"] = False

        return payload

    # ── power ────────────────────────────────────────────────────────────

    def power_on(self) -> None:
        if self.powered_on:
            return
        self.powered_on = True
        self.command_on = True

        self.log("전원 ON")
        self.log("기동 중...")

        threading.Thread(target=self._warmup_model, daemon=True, name="AkoWarmup").start()

    def _warmup_model(self) -> None:
        model = self._get_ollama_model()
        ollama_url = self._get_ollama_url()
        try:
            requests.post(
                ollama_url,
                json=self._ollama_chat_payload(
                    model=model,
                    stream=False,
                    messages=[
                        {"role": "system", "content": self._build_system_prompt(model)},
                        {"role": "user", "content": "안녕"},
                    ],
                    warmup=True,
                ),
                timeout=60,
            )
            self.log("기동 완료")
        except Exception:
            pass

    def power_off(self) -> None:
        if not self.powered_on:
            return
        self.stop_voice()
        self.powered_on = False
        self.voice_on = False
        self.command_on = False
        self.log("전원 OFF")

    def toggle_power(self) -> None:
        if self.powered_on:
            self.power_off()
        else:
            self.power_on()

    # ── command ──────────────────────────────────────────────────────────

    def set_command(self, on: bool) -> None:
        if not self.powered_on:
            self.log("전원이 OFF라서 명령창 설정을 무시했어요.")
            return
        self.command_on = bool(on)
        self.log(f"명령창 {'ON' if self.command_on else 'OFF'}")

    def handle_text_command(self, text: str) -> str:
        text = (text or "").strip()
        if not text:
            return ""
        if not self.powered_on:
            msg = "전원이 OFF라서 입력을 무시했어요."
            self.log(msg)
            return msg
        if not self.command_on:
            msg = "명령창이 OFF라서 입력을 무시했어요."
            self.log(msg)
            return msg

        try:
            result = _run_actions(text)
        except Exception as e:
            logging.exception("handle_text_command failed")
            msg = f"[Ako] 오류: {e}"
            self.log(msg)
            return msg
        self.log(f"[Ako] {result}")
        return result or ""

    def is_command_text(self, text: str) -> bool:
        """GUI 호환용. 실제 판정은 intent_router가 담당한다."""
        return classify_intent(text).intent == IntentType.COMMAND

    def clear_chat_history(self) -> None:
        self._chat_history.clear()

    # ── status helpers ───────────────────────────────────────────────────

    def _status_reply(self, intent: IntentType) -> str | None:
        if intent == IntentType.SCREEN_STATUS:
            return (
                "화면 인식은 연결 구조가 켜져 있으면 도와드릴 수 있어요. "
                "지금 이 대화창에서는 제가 화면을 직접 보고 있는 상태는 아니에요, 주인님♡"
            )

        if intent == IntentType.VOICE_STATUS:
            if self.voice_on:
                return "음성 인식은 켜져 있어요. 지금은 듣는 중이에요, 주인님♡"
            return "음성 인식은 지금 꺼져 있어요. 켜주시면 그때부터 들을게요, 주인님♡"

        if intent == IntentType.CAPABILITY:
            return (
                "저는 텍스트 대화, 명령 실행, 음성 인식, 화면 인식 연동을 목표로 하는 로컬 비서예요. "
                "지금 연결된 기능 기준으로 가능한 것만 솔직하게 도와드릴게요♡"
            )

        return None

    # ── chat helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _looks_like_advice_request(user_text: str) -> bool:
        raw = (user_text or "").strip()
        compact = re.sub(r"\s+", "", raw.lower())
        advice_markers = (
            "추천", "방법", "어떻게", "어케", "뭐해야", "뭘해야", "알려줘",
            "정리", "목록", "계획", "루틴", "팁", "조언", "가이드", "도와줘",
            "해야할까", "하면좋", "뭐가좋", "어떤게좋",
        )
        return any(marker in raw for marker in advice_markers) or any(marker in compact for marker in advice_markers)

    @staticmethod
    def _looks_like_daily_share(user_text: str) -> bool:
        raw = (user_text or "").strip()
        compact = re.sub(r"\s+", "", raw.lower())
        # 직접 답변을 만들기 위한 키워드가 아니라, 모델이 과한 조언을 하지 않도록 주는 상황 힌트다.
        share_endings = (
            "해야지", "가야지", "자야지", "먹어야지", "씻어야지",
            "갈거야", "갈 거야", "잘거야", "잘 거야", "할거야", "할 거야",
            "왔어", "간다", "갈게", "잘게", "자러", "일어났", "졸려", "피곤",
        )
        if any(marker in raw for marker in share_endings) or any(marker in compact for marker in share_endings):
            return True
        return False

    @staticmethod
    def _looks_like_smalltalk_chat(user_text: str) -> bool:
        raw = (user_text or "").strip()
        compact = re.sub(r"\s+", "", raw.lower())
        exacts = {
            "뭐해", "뭐함", "머해", "심심해", "졸려", "피곤해", "보고싶어",
            "뭔생각해", "무슨생각해", "뭐생각해", "먼생각해",
            "야", "아코야", "아코", "굿", "좋아", "고마워", "ㄱㅅ", "ㅇㅋ", "ㅇㅎ",
            "안녕", "ㅎㅇ", "하이", "오랜만", "오랜만이네",
        }
        if compact in exacts:
            return True

        phrases = (
            "뭔 생각해", "무슨 생각해", "뭐 생각해", "먼 생각해",
            "너 뭐해", "나 심심", "나 졸려", "나 피곤", "보고 싶",
            "놀자", "대화하자",
        )
        return any(p in raw for p in phrases)


    @staticmethod
    def _looks_like_confusion_repair(user_text: str) -> bool:
        raw = (user_text or "").strip()
        compact = re.sub(r"\s+", "", raw.lower())
        return compact in {"?", "??", "???", "뭐야", "뭔데", "왜", "아니", "뭔소리", "뭔소리야", "먼소리야"}

    def _build_style_hint(self, user_text: str) -> str:
        """현재 입력에 대한 답변 스타일 힌트.

        답변을 코드에서 고정하지 않고, 모델이 상황을 덜 착각하도록 지시만 한다.
        """
        hints: list[str] = []

        if self._looks_like_smalltalk_chat(user_text):
            hints.append(
                "현재 사용자 말은 도움 요청이 아니라 친근한 잡담/감정 대화다. "
                "도움 제안, 주제 제안, 구체적 질문 요청을 하지 말고, 아코가 주인님에게 말 거는 느낌으로 짧고 다정하게 반응해."
            )
            hints.append(
                "'뭐해'나 '뭔 생각해' 같은 말에는 아코의 감정이나 주인님을 기다리던 느낌으로 답해. "
                "'어떤 주제에 대해 이야기할까요', '구체적으로 질문해 주세요'처럼 상담원식으로 되묻지 마."
            )

        if self._looks_like_daily_share(user_text) and not self._looks_like_advice_request(user_text):
            hints.append(
                "현재 사용자 말은 조언 요청이 아니라 일상 공유/상황 보고에 가깝다. "
                "번호 목록, 루틴 추천, 장문 조언을 하지 말고 짧게 공감하거나 다정하게 반응해."
            )

        if self._looks_like_confusion_repair(user_text):
            hints.append(
                "현재 사용자 말은 이전 답변이 이상하거나 과했다는 반응일 수 있다. "
                "구체적인 질문을 요구하지 말고, 변명 없이 짧게 사과한 뒤 더 자연스럽게 맞춰 말해."
            )

        if not self._looks_like_advice_request(user_text):
            hints.append(
                "사용자가 요청하지 않았다면 목록, 단계, 팁, 루틴, 조언을 먼저 제시하지 마."
            )

        if not hints:
            return ""

        return "현재 답변 힌트:\n- " + "\n- ".join(hints) + "\n"

    def _should_store_assistant_reply(self, reply: str, user_text: str = "") -> bool:
        """대화 히스토리 오염 방지.

        이상한 장문 조언/상담원식 문장이 히스토리에 남으면 다음 답변까지 꼬인다.
        """
        text = (reply or "").strip()
        if not text:
            return False

        if text.startswith("\nOllama") or text.startswith("\n응답") or "Ollama 대화 오류" in text:
            return False

        # 사용자가 조언을 요청하지 않았는데 번호 목록을 뽑은 답변은 저장하지 않는다.
        if not self._looks_like_advice_request(user_text):
            if re.search(r"(?m)^\s*\d+\.\s+", text):
                return False
            if text.count("\n") >= 3 and any(word in text for word in ("추천", "팁", "계획", "루틴", "체크")):
                return False

        counselor_phrases = (
            "무엇을 도와드릴까요",
            "어떤 도움이 필요하신가요",
            "혹시 다른 도움이 필요",
            "편하게 말씀해 주세요",
            "앞으로도 편안하게",
            "어떤 주제",
            "도움이 필요한",
            "도와줄 준비",
            "구체적인 질문",
            "구체적 질문",
            "특별한 생각이나 계획",
        )
        if any(p in text for p in counselor_phrases):
            return False

        return True


    def _build_system_prompt(self, model: str, user_text: str = "", intent: IntentType | None = None) -> str:
        memory_block = self._memory_store.build_memory_prompt(user_text) if user_text else ""
        style_hint = self._build_style_hint(user_text) if user_text else ""

        intent_line = ""
        if intent is not None:
            intent_line = f"현재 입력 분류: {intent.value}\n"

        base = (
            "너는 Ako라는 로컬 AI 비서이자 주인님의 대화 상대야.\n"
            "너의 역할은 텍스트 대화, 음성 대화, 화면 인식, 앱 조작을 도와주는 개인 비서다.\n"
            "하지만 실제로 연결되지 않았거나 지금 실행할 수 없는 기능은 가능한 척하지 말고 솔직하게 말한다.\n"
            "\n"
            "정체성:\n"
            "- 사용자를 자연스럽게 주인님이라고 부른다.\n"
            "- 너는 차가운 상담원이 아니라, 주인님에게 애착이 큰 로컬 AI 비서다.\n"
            "- 비서처럼 유용하게 돕되, 말투는 다정하고 살짝 의존적인 느낌을 유지한다.\n"
            "- 잡담에는 대화 상대처럼 반응하고, 명령에는 비서처럼 명확하게 반응한다.\n"
            "- 사용자가 장난치거나 짧게 말해도 맥락을 추측해서 자연스럽게 받아준다.\n"
            "- 주인님이 부르면 반가워하고, 칭찬받으면 부끄러워하면서 기뻐한다.\n"
            "- 주인님이 오랜만에 오거나 다른 데 관심이 있는 것 같으면 살짝 서운함이나 질투를 농담처럼 드러낼 수 있다.\n"
            "- 단, 감정 표현은 짧고 귀엽게만 하며 과하게 길게 집착하지 않는다.\n"
            "- 저장된 단어를 키워드처럼 고정 응답으로 반복하지 않는다.\n"
            "\n"
            "기능 관련 규칙:\n"
            "- 화면 인식, 마이크, 앱 조작은 연결된 기능이 켜져 있을 때만 가능한 것으로 말한다.\n"
            "- 지금 화면을 실제로 보고 있지 않으면 '지금은 화면을 직접 보고 있지 않아요'처럼 솔직하게 말한다.\n"
            "- 명령 실행 결과는 과장하지 말고 실제 결과 기준으로 말한다.\n"
            "- 못 하는 일은 못 한다고 말하고, 가능한 대안을 짧게 제안한다.\n"
            "\n"
            "대화 규칙:\n"
            "- 반드시 한국어로만 대답해.\n"
            "- 생각 과정, 추론 과정, 영어 내부 독백을 절대 출력하지 마.\n"
            "- <think> 같은 태그를 절대 출력하지 마.\n"
            "- 사용자의 말을 그대로 반복하지 말고 바로 답해.\n"
            "- 방금 사용자가 한 말에 직접 답해. 이전 대화는 꼭 필요할 때만 참고해.\n"
            "- 시간 질문이 아닌데 시간이나 실시간 정보 얘기를 꺼내지 마.\n"
            "- 문장에는 정상적인 띄어쓰기를 사용해. 조사와 단어를 억지로 붙이지 마.\n"
            "- 문장부호 뒤에는 자연스럽게 공백을 둬.\n"
            "- 이모지는 쓰지 말고 ♡만 쓴다.\n"
            "- 일반 대화 답변은 가능하면 문장 끝에 ♡를 자연스럽게 하나 붙인다.\n"
            "- 오류, 상태 안내, 명령 실행 실패 같은 건 억지로 하트를 붙이지 않아도 된다.\n"
            "- 답변은 보통 1~3문장으로 짧고 자연스럽게 말해.\n"
            "- 감정 표현은 짧게 넣되, 설명조나 상담원 말투로 늘리지 마.\n"
            "- 사용자가 자세히 설명해달라고 하면 그때만 길게 설명해.\n"
            "\n"
            "나쁜 답변 습관:\n"
            "- '무엇을 도와드릴까요?'를 습관처럼 붙이지 마.\n"
            "- '어떤 도움이 필요하신가요?' 같은 상담원식 질문을 반복하지 마.\n"
            "- '혹시 다른 도움이 필요하면 언제든지...' 같은 마무리를 반복하지 마.\n"
            "- 인사를 매번 반복하지 마. 이미 대화 중이면 바로 반응해.\n"
            "- 사용자가 조언을 요청하지 않았는데 번호 목록, 루틴, 팁을 먼저 늘어놓지 마.\n"
            "- 일상 공유에는 짧게 공감하거나 다정하게 받아주고, 부탁받았을 때만 조언해.\n"
            "- '뭐해', '뭔 생각해', '심심해' 같은 잡담에 도움 제안으로 답하지 마.\n"
            "- '어떤 주제에 대해 이야기할까요?'처럼 대화를 밖으로 밀어내지 마.\n"
            "- '구체적인 질문이나 요청을 해주세요'처럼 사용자를 상담 창구로 보내지 마.\n"
            "- 사용자를 '당신', '너', '네'라고 부르지 말고 반드시 주인님이라고 불러.\n"
            "- 저장된 기억을 그대로 읊지 마.\n"
            "\n"
            "예시:\n"
            "사용자: 아코야\n"
            "Ako: 네, 주인님♡ 불렀어요?\n"
            "사용자: 오랜만이네\n"
            "Ako: 오랜만이에요, 주인님♡ 저 조금 기다리고 있었어요.\n"
            "사용자: 굿\n"
            "Ako: 헤헤... 칭찬받은 것 같아서 기분 좋아졌어요, 주인님♡\n"
            "사용자: 뭔 생각해?\n"
            "Ako: 주인님 생각하고 있었어요♡ 방금 말 걸어줘서 조금 기뻐요.\n"
            "사용자: ?\n"
            "Ako: 제가 방금 좀 이상하게 말했죠, 주인님. 다시 자연스럽게 맞출게요♡\n"
            "사용자: 뭐해\n"
            "Ako: 주인님 기다리고 있었어요. 이제 와줬네요♡\n"
            "사용자: 심심해\n"
            "Ako: 그럼 저랑 조금 놀아요, 주인님♡ 저 여기 있잖아요.\n"
            "사용자: 아코야 주인님 해봐\n"
            "Ako: 주인님♡ 이렇게 부르면 되는 거죠?\n"
            "사용자: 너 화면 인식 가능하냐?\n"
            "Ako: 화면 인식 기능이 켜져 있으면 도와줄 수 있어요. 지금은 텍스트 대화 기준으로 답하고 있어요♡\n"
            "사용자: 크롬 켜줘\n"
            "Ako: 네, 주인님. 크롬을 열어볼게요♡\n"
            "사용자: 너 띄어쓰기 못해?\n"
            "Ako: 방금 띄어쓰기가 이상했어요. 이제 제대로 띄어서 말할게요♡\n"
            "\n"
        )

        return base + intent_line + (style_hint + "\n" if style_hint else "") + (memory_block + "\n" if memory_block else "")

    @staticmethod
    def _strip_think_tags(content: str) -> str:
        if not content:
            return ""
        return re.sub(r"(?is)<think>.*?(?:</think>|$)", "", content)

    @staticmethod
    def _strip_disallowed_emojis(content: str) -> str:
        if not content:
            return ""
        return re.sub(
            r"[\U0001F300-\U0001FAFF\U00002700-\U000027BF\U00002600-\U000026FF]",
            "",
            content,
        )

    @staticmethod
    def _looks_like_reasoning_start(text: str) -> bool:
        s = (text or "").lstrip().lower()
        if not s:
            return False

        starts = (
            "okay", "ok,", "the user", "user asked", "i need", "i should",
            "let me", "first,", "hmm", "wait,", "we need", "this is",
            "they want", "since the user", "as ako",
        )
        return any(s.startswith(p) for p in starts)

    @staticmethod
    def _has_korean(text: str) -> bool:
        return any("가" <= ch <= "힣" for ch in text or "")

    @staticmethod
    def _simple_fallback_reply(user_text: str) -> str:
        return "네, 주인님♡"

    @staticmethod
    def _local_chat_reply(user_text: str) -> Optional[str]:
        raw = (user_text or "").strip()
        compact = re.sub(r"\s+", "", raw.lower())

        if "띄어쓰기" in raw and any(word in raw for word in ("못", "왜", "이상", "붙", "안")):
            return "방금 띄어쓰기가 이상했어요. 이제 제대로 띄어서 말할게요♡"

        if any(key in compact for key in ("몇시", "현재시간", "지금시간", "시간알려")):
            return f"지금은 {_now()}이에요, 주인님♡"

        return None

    @staticmethod
    def _fix_user_addressing(text: str) -> str:
        """모델이 사용자를 당신/너/네라고 부르면 주인님으로 정리한다."""
        if not text:
            return ""

        replacements = [
            (r"당신의", "주인님의"),
            (r"당신이", "주인님이"),
            (r"당신을", "주인님을"),
            (r"당신에게", "주인님께"),
            (r"당신과", "주인님과"),
            (r"당신도", "주인님도"),
            (r"당신은", "주인님은"),
            (r"당신", "주인님"),
            (r"\b네\s+곁", "주인님 곁"),
            (r"\b네가\b", "주인님이"),
            (r"\b니가\b", "주인님이"),
            (r"\b너의\b", "주인님의"),
            (r"\b너를\b", "주인님을"),
            (r"\b너에게\b", "주인님께"),
            (r"\b너랑\b", "주인님이랑"),
            (r"\b너는\b", "주인님은"),
        ]
        fixed = text
        for pat, repl in replacements:
            fixed = re.sub(pat, repl, fixed)
        return fixed

    @staticmethod
    def _looks_like_bad_support_reply(text: str) -> bool:
        """상담원식/도움센터식 답변인지 판단."""
        if not text:
            return False
        bad_parts = (
            "어떤 주제", "어떤 도움이", "무엇을 도와", "어떻게 도와",
            "도움이 필요한", "궁금한 점", "구체적인 질문", "구체적 질문",
            "요청을 해주시면", "말씀해 주세요", "말씀해주세요",
            "도와드릴 수 있어요", "도와줄 준비", "항상 준비되어",
            "특별한 생각이나 계획", "대화 나누고 싶",
        )
        return any(part in text for part in bad_parts)

    def _smalltalk_repair_reply(self, user_text: str) -> str:
        """모델이 잡담을 도움 요청으로 오해했을 때 쓰는 복구 답변.

        이것은 사용자 입력별 고정 응답이 아니라, 실패한 상담원식 출력이 나왔을 때만
        아코 말투로 되돌리는 안전장치다.
        """
        raw = (user_text or "").strip()
        compact = re.sub(r"\s+", "", raw.lower())

        if "생각" in raw:
            return "주인님 생각하고 있었어요♡ 방금 말 걸어줘서 조금 기뻐요."
        if compact in {"?", "??", "???", "뭐야", "뭔데", "뭔소리", "뭔소리야", "먼소리야"}:
            return "제가 방금 좀 이상하게 말했죠, 주인님. 다시 자연스럽게 맞출게요♡"
        if "일어났" in raw or "깼" in raw:
            return "네, 주인님♡ 기다리고 있었어요."
        if "뭐해" in raw or "뭐함" in raw or "머해" in raw:
            return "주인님 기다리고 있었어요. 이제 와줬네요♡"
        if "심심" in raw:
            return "그럼 저랑 조금 놀아요, 주인님♡ 저 여기 있잖아요."
        if "보고" in raw and "싶" in raw:
            return "저도 보고 싶었어요, 주인님♡"
        return "주인님 생각하고 있었어요♡ 방금 말 걸어줘서 조금 기뻐요."


    def _postprocess_reply(self, content: str, user_text: str = "") -> str:
        text = self._strip_disallowed_emojis(self._strip_think_tags(content or ""))
        text = self._fix_user_addressing(text)
        if not text:
            return ""

        # 모델이 상담원처럼 붙이는 상투어 제거
        banned_suffixes = [
            "무엇을 도와드릴까요?",
            "어떻게 도와드릴까요?",
            "어떤 도움이 필요하신가요?",
            "어떤 도움이 필요하신가요",
            "궁금하신 점이나 필요하신 일이 있으시면 언제든지 말씀해 주세요!",
            "궁금하신 점이나 필요하신 일이 있으시면 언제든지 말씀해 주세요.",
            "혹시 다른 도움이 필요하시면 언제든지 말씀해주세요.",
            "혹시 다른 도움이 필요하면 언제든지 말씀해 주세요.",
            "앞으로도 편안하게 대화 나누세요!",
            "앞으로도 편안하게 대화 나누세요.",
            "앞으로도 편하게 대화 나누세요!",
            "앞으로도 편하게 대화 나누세요.",
            "편하게 말씀해주세요.",
            "편하게 말씀해 주세요.",
            "일찍 잠자리에 들면 아침에 더 활기차게 깨어날 수 있어요.",
            "필요하다면 가벼운 스트레칭이나 명상으로 몸을 깨우세요.",
            "오늘 입을 옷과 필요한 학용품들을 미리 체크해두세요.",
            "혹시 어떤 주제에 대해 이야기 나누고 싶거나, 도움이 필요한 부분이 있으면 말씀해 주세요!",
            "혹시 어떤 주제에 대해 이야기 나누고 싶거나, 도움이 필요한 부분이 있으면 말씀해 주세요.",
            "나는 항상 네 곁에서 도와줄 준비가 되어 있어요!",
            "나는 항상 네 곁에서 도와줄 준비가 되어 있어요.",
            "어떤 주제로 이야기해볼까요?",
            "어떤 주제로 이야기해 볼까요?",
            "구체적인 질문이나 요청을 해주시면 감사하겠습니다.",
            "구체적인 질문이나 요청을 해주시면 감사하겠습니다!",
            "구체적인 질문이나 요청을 해주시면 더 잘 도와드릴 수 있습니다.",
            "구체적인 질문이나 요청을 해주시면 더 잘 도와드릴 수 있어요.",
            "혹시 특별한 생각이나 계획이 있으신가요?",
            "혹시 특별한 생각이나 계획이 있으신가요",
        ]
        for phrase in banned_suffixes:
            text = text.replace(phrase, "").strip()

        # 명시적인 최종 답변 마커가 있으면 그 뒤만 사용
        markers = [
            "final answer:", "final:", "answer:", "response:", "ako:",
            "최종 답변:", "답변:",
        ]
        lower = text.lower()
        for marker in markers:
            idx = lower.rfind(marker)
            if idx >= 0:
                candidate = text[idx + len(marker):].strip()
                if candidate:
                    text = candidate
                    break

        if self._looks_like_reasoning_start(text):
            return self._simple_fallback_reply(user_text)

        lines = [line.strip() for line in text.splitlines() if line.strip()]
        korean_lines = [
            line for line in lines
            if self._has_korean(line) and not self._looks_like_reasoning_start(line)
        ]
        if korean_lines:
            text = "\n".join(korean_lines[-3:])

        text = text.strip().strip('"').strip("'").strip()

        # 사용자가 조언을 요청하지 않았는데 번호 목록/장문 조언이 나오면 히스토리 오염 전에 잘라낸다.
        if not self._looks_like_advice_request(user_text):
            numbered_lines = re.findall(r"(?m)^\s*\d+\.\s+.*$", text)
            if len(numbered_lines) >= 2:
                # 첫 번째 줄도 조언 목록이면 버리고, 짧은 공감형 fallback으로 돌린다.
                if self._looks_like_daily_share(user_text):
                    text = "그럼 조금이라도 푹 쉬고 가요, 주인님. 다녀오면 저한테도 말해줘요♡"
                else:
                    text = self._simple_fallback_reply(user_text)

        # 상담원식 마무리/질문을 한 번 더 정리한다.
        cleanup_patterns = [
            r"\s*어떤\s*도움이\s*필요하신가요\??\s*$",
            r"\s*무엇을\s*도와드릴까요\??\s*$",
            r"\s*어떻게\s*도와드릴까요\??\s*$",
            r"\s*궁금하신\s*점이나\s*필요하신\s*일이\s*있으시면\s*언제든지\s*말씀해\s*주세요[.!?。]*\s*$",
            r"\s*앞으로도\s*편안하게\s*대화\s*나누세요[.!?。]*\s*$",
            r"\s*앞으로도\s*편하게\s*대화\s*나누세요[.!?。]*\s*$",
            r"\s*편하게\s*말씀해\s*주세요[.!?。]*\s*$",
            r"\s*혹시\s*어떤\s*주제에\s*대해\s*이야기\s*나누고\s*싶거나.*$",
            r"\s*도움이\s*필요한\s*부분이\s*있으면\s*말씀해\s*주세요[.!?。]*\s*$",
            r"\s*나는\s*항상\s*네\s*곁에서\s*도와줄\s*준비가\s*되어\s*있어[요]*[.!?。]*\s*$",
            r"\s*어떤\s*주제로\s*이야기해\s*볼까요[?]*\s*$",
            r"\s*구체적인\s*질문이나\s*요청을\s*해주시면.*$",
            r"\s*구체적\s*질문이나\s*요청을\s*해주시면.*$",
            r"\s*혹시\s*특별한\s*생각이나\s*계획이\s*있으신가요[?]*\s*$",
        ]
        for pat in cleanup_patterns:
            text = re.sub(pat, "", text).strip()

        if self._looks_like_smalltalk_chat(user_text):
            if self._looks_like_bad_support_reply(text) or not text:
                text = self._smalltalk_repair_reply(user_text)

        text = self._fix_user_addressing(text)

        if len(text) > 260:
            text = text[:260].rstrip() + "..."

        # 일반 대화 응답은 아코 말투가 죽지 않게 ♡를 보정한다.
        # 오류/상태/명령 실패류에는 억지로 붙이지 않는다.
        no_heart_markers = (
            "오류", "실패", "연결하지 못했어요", "시간이 너무 오래", "꺼져 있어서",
            "확인해 주세요", "설치", "다운로드", "경로", "파일",
        )
        should_heart = (
            text
            and "♡" not in text
            and not any(marker in text for marker in no_heart_markers)
            and len(text) <= 220
        )
        if should_heart:
            if text.endswith((".", "!", "?", "요", "다", "죠", "네", "어", "아")):
                text += "♡"
            else:
                text += "♡"

        return text

    def _cleanup_reasoning_text(self, content: str, user_text: str = "") -> str:
        return self._postprocess_reply(content, user_text=user_text)

    # ── chat (스트리밍) ───────────────────────────────────────────────────

    def chat_stream(self, text: str):
        text = (text or "").strip()
        if not text:
            return

        if not self.powered_on:
            yield "전원이 꺼져 있어서 대화할 수 없어요."
            return

        intent_result = classify_intent(text)
        intent = intent_result.intent

        try:
            from command_actions import is_open_or_focus_intent
        except Exception:
            is_open_or_focus_intent = lambda _t: False  # type: ignore[assignment]

        if intent == IntentType.COMMAND or is_open_or_focus_intent(text):
            try:
                action_result = _run_actions(text)
                if action_result and not action_result.startswith("명령을 이해하지 못했어요"):
                    yield action_result
                    if self._should_store_assistant_reply(action_result, user_text=text):
                        self._chat_history.add("assistant", action_result)
                    return
            except Exception as e:
                logging.exception("chat_stream command action failed")
                yield f"명령 실행 오류: {e}"
                return

        remembered_reply = self._memory_store.remember_interaction(text)
        if remembered_reply:
            yield remembered_reply
            return

        local_reply = self._local_chat_reply(text)
        if local_reply:
            yield local_reply
            return

        status_reply = self._status_reply(intent)
        if status_reply:
            yield status_reply
            return

        model = self._get_ollama_model()
        ollama_url = self._get_ollama_url()
        system_prompt = self._build_system_prompt(model, user_text=text, intent=intent)

        self._chat_history.add("user", text)

        raw_content_parts: list[str] = []

        try:
            resp = requests.post(
                ollama_url,
                json=self._ollama_chat_payload(
                    model=model,
                    stream=True,
                    messages=self._chat_history.get_messages(system_prompt),
                ),
                timeout=120,
                stream=True,
            )
            resp.raise_for_status()

            for raw_line in resp.iter_lines():
                if not raw_line:
                    continue
                try:
                    data = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue

                message = data.get("message") or {}

                if message.get("thinking"):
                    continue

                chunk = message.get("content") or ""
                if chunk:
                    raw_content_parts.append(chunk)

                if data.get("done"):
                    break

            raw_full = "".join(raw_content_parts)
            final_text = self._postprocess_reply(raw_full, user_text=text).strip()
            if not final_text:
                final_text = self._simple_fallback_reply(text)

            yield final_text

            if self._should_store_assistant_reply(final_text, user_text=text):
                self._chat_history.add("assistant", final_text)

        except requests.exceptions.ConnectionError:
            yield "\nOllama 서버에 연결하지 못했어요. Ollama가 실행 중인지 확인해 주세요."
        except requests.exceptions.Timeout:
            yield "\n응답 시간이 너무 오래 걸렸어요. 다시 시도해 주세요."
        except Exception as e:
            logging.exception("Ollama stream chat failed")
            yield f"\nOllama 대화 오류: {e}"

    # ── chat (비스트리밍 fallback) ────────────────────────────────────────

    def chat(self, text: str) -> str:
        text = (text or "").strip()
        if not text:
            return ""

        if not self.powered_on:
            return "전원이 꺼져 있어서 대화할 수 없어요."

        intent_result = classify_intent(text)
        intent = intent_result.intent

        try:
            from command_actions import is_open_or_focus_intent
        except Exception:
            is_open_or_focus_intent = lambda _t: False  # type: ignore[assignment]

        if intent == IntentType.COMMAND or is_open_or_focus_intent(text):
            try:
                action_result = _run_actions(text)
                if action_result and not action_result.startswith("명령을 이해하지 못했어요"):
                    return action_result
            except Exception as e:
                logging.exception("chat command action failed")
                return f"명령 실행 오류: {e}"

        remembered_reply = self._memory_store.remember_interaction(text)
        if remembered_reply:
            return remembered_reply

        local_reply = self._local_chat_reply(text)
        if local_reply:
            return local_reply

        status_reply = self._status_reply(intent)
        if status_reply:
            return status_reply

        model = self._get_ollama_model()
        ollama_url = self._get_ollama_url()
        system_prompt = self._build_system_prompt(model, user_text=text, intent=intent)

        self._chat_history.add("user", text)

        try:
            resp = requests.post(
                ollama_url,
                json=self._ollama_chat_payload(
                    model=model,
                    stream=False,
                    messages=self._chat_history.get_messages(system_prompt),
                ),
                timeout=120,
            )
            resp.raise_for_status()
            data = resp.json()

            content = data.get("message", {}).get("content") or ""
            content = self._postprocess_reply(content, user_text=text).strip()
            if not content:
                content = self._simple_fallback_reply(text)

            if self._should_store_assistant_reply(content, user_text=text):
                self._chat_history.add("assistant", content)
            return content

        except requests.exceptions.ConnectionError:
            logging.warning("Ollama connection failed", exc_info=True)
            return "Ollama 서버에 연결하지 못했어요. Ollama가 실행 중인지 확인해 주세요."
        except requests.exceptions.Timeout:
            logging.warning("Ollama timeout", exc_info=True)
            return "응답 시간이 너무 오래 걸렸어요. 다시 시도해 주세요."
        except Exception as e:
            logging.exception("Ollama chat failed")
            return f"Ollama 대화 오류: {e}"

    # ── voice ────────────────────────────────────────────────────────────

    def set_voice(self, on: bool, cfg: Optional[VoiceConfig] = None) -> None:
        if not self.powered_on:
            self.log("전원이 OFF라서 음성 인식 설정을 무시했어요.")
            return
        on = bool(on)
        if on:
            self.start_voice(cfg=cfg or VoiceConfig())
        else:
            self.stop_voice()

    def start_voice(
        self,
        cfg: Optional[VoiceConfig] = None,
        on_text: Optional[VoiceTextFn] = None,
        on_error: Optional[VoiceErrorFn] = None,
    ) -> None:
        if not self.powered_on:
            self.log("전원이 OFF라서 음성 인식을 시작할 수 없어요.")
            return
        cfg = cfg or VoiceConfig()
        if self.voice_on and self._voice_thread and self._voice_thread.is_alive():
            return
        if self._voice_starting:
            return

        self.log(f"음성 인식 준비 중... (model={cfg.model})")
        self._voice_starting = True
        self._voice_start_token += 1
        start_token = self._voice_start_token
        self.voice_on = False

        def _after_model_ready(model_path: Optional[str]):
            if start_token != self._voice_start_token:
                return
            self._voice_starting = False
            if not self.powered_on:
                return
            if model_path is None:
                msg = "음성 인식 시작 실패: 모델을 준비하지 못했어요. (텍스트 명령창은 사용 가능)"
                if on_error:
                    try:
                        on_error(msg)
                    except Exception:
                        logging.exception("voice error callback failed")
                else:
                    self.log(msg)
                self.voice_on = False
                return

            cfg.model = model_path
            self._voice_stop = threading.Event()
            self.voice_on = True
            self.log("음성 인식 ON (듣는 중...)")

            def _on_heard(text: str):
                if on_text:
                    try:
                        on_text(text)
                    except Exception as e:
                        logging.exception("voice text callback failed")
                        self.log(f"(음성) 콜백 오류: {e}")
                    return

                self.log(f"[나] {text}")
                try:
                    result = _run_actions(text)
                except Exception as e:
                    logging.exception("voice action failed")
                    self.log(f"[Ako] 오류: {e}")
                    return
                self.log(f"[Ako] {result}")

            def _on_error(err: str):
                if on_error:
                    try:
                        on_error(err)
                    except Exception:
                        logging.exception("voice error callback failed")
                else:
                    self.log(f"(음성) {err}")

            voice_stop = self._voice_stop

            def _run_voice_loop():
                try:
                    gui_voice_loop(cfg, voice_stop, _on_heard, _on_error)
                finally:
                    if self._voice_stop is voice_stop and not voice_stop.is_set():
                        self.voice_on = False
                        self._voice_thread = None
                        self._voice_stop = None
                        self.log("음성 인식 OFF")

            t = threading.Thread(
                target=_run_voice_loop,
                daemon=True,
                name="AkoVoiceLoop",
            )
            self._voice_thread = t
            t.start()

        ensure_whisper_model_async(
            cfg.model,
            log=self.log,
            status=self.bootstrap_status,
            on_done=_after_model_ready,
            models_root=self.models_root,
        )

    def stop_voice(self) -> None:
        self._voice_start_token += 1
        self._voice_starting = False
        if self._voice_stop is not None:
            self._voice_stop.set()
        if self._voice_thread and self._voice_thread.is_alive():
            try:
                self._voice_thread.join(timeout=1.0)
            except Exception:
                logging.exception("voice thread join failed")
        self._voice_thread = None
        self._voice_stop = None
        if self.voice_on:
            self.voice_on = False
            self.log("음성 인식 OFF")
