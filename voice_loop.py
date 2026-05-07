# voice_loop.py
# -----------------------------------------------------------------------------
# 마이크 입력을 받아 STT(음성->텍스트) 후 app.run_actions()로 넘기는 루프.
# - faster-whisper(오프라인) 사용
# - WhisperModel을 싱글턴으로 캐싱 → 첫 호출 이후 빠른 응답
# -----------------------------------------------------------------------------
from __future__ import annotations

import os
import time
import threading
from dataclasses import dataclass, field
from typing import Optional, Callable

LogFn = Callable[[str], None]


@dataclass
class VoiceConfig:
    device: Optional[int] = None
    samplerate: int = 16000
    min_record_sec: float = 0.6
    max_record_sec: float = 10.0
    silence_sec: float = 0.9
    silence_threshold: float = 0.012
    model: str = field(default_factory=lambda: os.getenv("AKO_WHISPER_MODEL", "base"))
    language: str = "ko"
    wake_word: str = field(default_factory=lambda: os.getenv("AKO_WAKE_WORD", ""))
    print_heard_audio_stats: bool = False


# -----------------------------------------------------------------------------
# 모델 준비/다운로드
# -----------------------------------------------------------------------------
def get_user_data_dir() -> str:
    local = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or os.path.expanduser("~")
    path = os.path.join(local, "Ako")
    os.makedirs(path, exist_ok=True)
    return path


def get_models_dir(models_root: str | None = None) -> str:
    root = (models_root or "").strip() if models_root is not None else ""
    path = root if root else os.path.join(get_user_data_dir(), "models")
    os.makedirs(path, exist_ok=True)
    return path


@dataclass
class BootstrapStatus:
    ready: bool = False
    downloading: bool = False
    last_error: str = ""


def ensure_whisper_model(
    model: str,
    log: Optional[LogFn] = None,
    force: bool = False,
    models_root: str | None = None,
) -> str:
    """
    faster-whisper CTranslate2 모델을 사용자 데이터 폴더에 준비한다.
    - 이미 존재하면 스킵
    - 없으면 HuggingFace에서 다운로드
    반환: 로컬 모델 경로
    """
    model = (model or "small").strip()
    models_dir = get_models_dir(models_root)
    output_dir = os.path.join(models_dir, model.replace("/", "_"))
    model_bin = os.path.join(output_dir, "model.bin")

    if not force and os.path.isfile(model_bin):
        if log:
            log(f"(부트스트랩) 모델 이미 존재: {output_dir}")
        return output_dir

    if log:
        log(f"(부트스트랩) 모델 다운로드 준비: {model} -> {output_dir}")

    try:
        from faster_whisper.utils import download_model
    except Exception as e:
        raise RuntimeError(f"faster-whisper를 불러올 수 없어요: {e}")

    try:
        download_model(model, output_dir=output_dir, local_files_only=False)
    except Exception as e:
        raise RuntimeError(f"모델 다운로드 실패: {e}")

    if not os.path.isfile(model_bin):
        raise RuntimeError("다운로드가 끝났는데 model.bin을 찾지 못했어요.")

    if log:
        log(f"(부트스트랩) 모델 준비 완료: {output_dir}")
    return output_dir


def ensure_whisper_model_async(
    model: str,
    log: Optional[LogFn],
    status: BootstrapStatus,
    on_done: Optional[Callable[[Optional[str]], None]] = None,
    models_root: str | None = None,
) -> None:
    """GUI가 멈추지 않도록 백그라운드 스레드에서 모델을 준비한다."""

    def _task():
        status.downloading = True
        status.last_error = ""
        try:
            path = ensure_whisper_model(model, log=log, models_root=models_root)
            status.ready = True
            if on_done:
                on_done(path)
        except Exception as e:
            status.ready = False
            status.last_error = str(e)
            if log:
                log(f"(부트스트랩) 오류: {e}")
            if on_done:
                on_done(None)
        finally:
            status.downloading = False

    threading.Thread(target=_task, daemon=True, name="AkoBootstrap").start()


# -----------------------------------------------------------------------------
# WhisperModel 싱글턴 캐시
# - 모델명이 바뀔 때만 재로드, 그 외엔 재사용 → 응답 속도 대폭 개선
# -----------------------------------------------------------------------------
_whisper_cache: dict = {}
_whisper_lock = threading.Lock()


def _get_whisper_model(model_name: str):
    """WhisperModel을 캐싱해서 반환. 같은 모델명이면 재사용."""
    with _whisper_lock:
        if model_name not in _whisper_cache:
            try:
                from faster_whisper import WhisperModel
                _whisper_cache[model_name] = WhisperModel(
                    model_name, device="cpu", compute_type="int8"
                )
            except ImportError:
                raise RuntimeError(
                    "faster-whisper가 설치되어 있지 않아요.\n"
                    "pip install faster-whisper"
                )
            except Exception as e:
                raise RuntimeError(f"Whisper 모델 로드 실패({model_name}): {e}")
        return _whisper_cache[model_name]


def clear_whisper_cache():
    """모델 캐시를 비웁니다. 모델을 교체할 때 호출."""
    with _whisper_lock:
        _whisper_cache.clear()


# -----------------------------------------------------------------------------
# 녹음
# -----------------------------------------------------------------------------
def _rms(x) -> float:
    import numpy as np
    if x is None:
        return 0.0
    x = np.asarray(x, dtype=np.float32)
    if x.size == 0:
        return 0.0
    return float((np.mean(x * x) + 1e-12) ** 0.5)


def _record_until_silence(cfg: VoiceConfig):
    """sounddevice로 녹음. 무음이 일정 시간 지속되면 종료."""
    import numpy as np
    import sounddevice as sd

    sr = int(cfg.samplerate)
    block = int(sr * 0.2)  # 200ms 단위로 처리

    frames = []
    started = False
    silent_for = 0.0
    total = 0.0

    def _cb(indata, _frames, _time, status):
        nonlocal started, silent_for, total
        mono = indata[:, 0].copy()
        frames.append(mono)
        total += _frames / sr

        level = _rms(mono)
        if cfg.print_heard_audio_stats:
            print(f"[VOICE] rms={level:.4f} total={total:.1f}s")

        if not started:
            if total >= cfg.min_record_sec:
                started = True
            return

        if level < cfg.silence_threshold:
            silent_for += _frames / sr
        else:
            silent_for = 0.0

    try:
        with sd.InputStream(
            samplerate=sr,
            device=cfg.device,
            channels=1,
            dtype="float32",
            blocksize=block,
            callback=_cb,
        ):
            t0 = time.time()
            while True:
                time.sleep(0.05)
                if total >= cfg.max_record_sec:
                    break
                if started and silent_for >= cfg.silence_sec:
                    break
                if time.time() - t0 > cfg.max_record_sec + 2.0:
                    break
    except Exception as e:
        raise RuntimeError(
            f"마이크 녹음 실패: {e}\n"
            "- pip install sounddevice numpy\n"
            "- 마이크 연결 및 기본 장치 설정을 확인하세요."
        )

    if not frames:
        import numpy as np
        return np.zeros((0,), dtype=np.float32)

    import numpy as np
    return np.concatenate(frames, axis=0)


# -----------------------------------------------------------------------------
# STT
# -----------------------------------------------------------------------------
def _transcribe(audio, cfg: VoiceConfig) -> str:
    """캐싱된 WhisperModel로 STT 수행."""
    import numpy as np

    if np.asarray(audio).size == 0:
        return ""

    try:
        model = _get_whisper_model(cfg.model)
    except RuntimeError as e:
        raise RuntimeError(str(e))

    try:
        segments, _ = model.transcribe(
            audio,
            language=(cfg.language or None),
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": int(cfg.silence_sec * 1000)},
        )
        return " ".join((seg.text or "").strip() for seg in segments if seg.text and seg.text.strip())
    except Exception as e:
        raise RuntimeError(f"STT 변환 실패: {e}")


# -----------------------------------------------------------------------------
# 공개 API
# -----------------------------------------------------------------------------
def listen_once(cfg: VoiceConfig) -> str:
    """한 번 녹음해서 텍스트로 반환."""
    audio = _record_until_silence(cfg)

    import numpy as np
    if np.asarray(audio).size < int(cfg.samplerate * cfg.min_record_sec):
        return ""

    return _transcribe(audio, cfg)


def _passes_wakeword(text: str, wake: str) -> bool:
    wake = (wake or "").strip().lower()
    if not wake:
        return True
    t = (text or "").strip().lower()
    return t.startswith(wake) or t.startswith(wake + "야") or (wake in t.split())


def _strip_wakeword(text: str, wake_word: str) -> str:
    """웨이크워드를 텍스트 앞에서 제거."""
    if not wake_word:
        return text
    low = text.lower()
    w = wake_word.lower()
    if low.startswith(w):
        text = text[len(wake_word):].lstrip(" ,.!?\t")
    if text.startswith("야"):
        text = text[1:].lstrip()
    return text.strip()


# -----------------------------------------------------------------------------
# CLI 루프
# -----------------------------------------------------------------------------
def voice_actions_loop(cfg: VoiceConfig):
    """무한 루프: 음성 → 텍스트 → actions 실행 (CLI용)"""
    from app import run_actions

    print("[VOICE] 시작: 말하고 잠깐 멈추면 인식합니다. (Ctrl+C 종료)")
    if cfg.wake_word:
        print(f"[VOICE] 웨이크워드: '{cfg.wake_word}'가 포함/시작할 때만 실행")

    # 첫 루프 전에 모델을 미리 로드해서 워밍업
    print(f"[VOICE] 모델 로드 중: {cfg.model} ...")
    try:
        _get_whisper_model(cfg.model)
        print("[VOICE] 모델 준비 완료. 말씀하세요.")
    except RuntimeError as e:
        print(f"[VOICE] 모델 로드 실패: {e}")
        return

    while True:
        try:
            text = listen_once(cfg)
        except RuntimeError as e:
            print(f"[VOICE] 오류: {e}")
            # 폴백: 키보드 입력
            try:
                s = input("[VOICE] (폴백) 텍스트로 명령 입력: ").strip()
                if s:
                    print(run_actions(s))
            except (EOFError, KeyboardInterrupt):
                break
            continue

        text = (text or "").strip()
        if not text:
            continue

        print(f"[VOICE] 인식: {text}")
        if not _passes_wakeword(text, cfg.wake_word):
            continue

        text = _strip_wakeword(text, cfg.wake_word)
        if not text:
            continue

        try:
            result = run_actions(text)
            print(f"[VOICE] 결과: {result}")
        except Exception as e:
            print(f"[VOICE] 명령 실행 오류: {e}")


# -----------------------------------------------------------------------------
# GUI용: stop_event로 중단 가능한 음성 루프
# -----------------------------------------------------------------------------
def gui_voice_loop(
    cfg: VoiceConfig,
    stop_event: threading.Event,
    on_text: Callable[[str], None],
    on_error: Optional[Callable[[str], None]] = None,
):
    """
    GUI에서 별도 스레드로 돌릴 수 있는 음성 루프.
    - stop_event가 set 되면 즉시 종료
    - 텍스트가 인식되면 on_text(text) 호출
    - 오류는 on_error(msg)로 전달
    """
    def _err(msg: str):
        if on_error:
            try:
                on_error(msg)
            except Exception:
                pass

    # 모델 미리 로드 (GUI 스레드를 블로킹하지 않고 여기서 처리)
    try:
        _get_whisper_model(cfg.model)
    except RuntimeError as e:
        _err(str(e))
        return

    while not stop_event.is_set():
        try:
            text = listen_once(cfg)
        except RuntimeError as e:
            _err(str(e))
            time.sleep(0.6)
            continue

        if stop_event.is_set():
            break

        text = (text or "").strip()
        if not text:
            continue

        if not _passes_wakeword(text, cfg.wake_word):
            continue

        text = _strip_wakeword(text, cfg.wake_word)
        if not text:
            continue

        try:
            on_text(text)
        except Exception as e:
            _err(f"콜백 오류: {e}")
            time.sleep(0.2)
