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
from collections import deque
from dataclasses import dataclass, field
from typing import Optional, Callable

LogFn = Callable[[str], None]


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _env_float(
    name: str,
    default: float,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except Exception:
        value = default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


@dataclass
class VoiceConfig:
    device: Optional[int] = None
    samplerate: int = 16000
    min_record_sec: float = 0.45
    max_record_sec: float = field(default_factory=lambda: _env_float("AKO_STT_MAX_RECORD_SEC", 8.0, minimum=0.5))
    silence_sec: float = field(default_factory=lambda: _env_float("AKO_STT_SILENCE_SEC", 0.80, minimum=0.1))
    silence_threshold: float = 0.010
    speech_start_threshold: float = field(default_factory=lambda: _env_float("AKO_STT_SPEECH_START_THRESHOLD", 0.020, minimum=0.0))
    speech_end_threshold: float = field(default_factory=lambda: _env_float("AKO_STT_SPEECH_END_THRESHOLD", 0.009, minimum=0.0))
    candidate_sec: float = field(default_factory=lambda: _env_float("AKO_STT_CANDIDATE_SEC", 0.30, minimum=0.0))
    min_speech_sec: float = field(default_factory=lambda: _env_float("AKO_STT_MIN_SPEECH_SEC", 0.40, minimum=0.0))
    pre_roll_sec: float = field(default_factory=lambda: _env_float("AKO_STT_PRE_ROLL_SEC", 0.30, minimum=0.0))
    rms_smoothing: float = field(default_factory=lambda: _env_float("AKO_STT_RMS_SMOOTHING", 0.8, minimum=0.0, maximum=0.98))
    rms_release_smoothing: float = field(default_factory=lambda: _env_float("AKO_STT_RMS_RELEASE_SMOOTHING", 0.4, minimum=0.0, maximum=0.98))
    debug_audio: bool = field(default_factory=lambda: _env_bool("AKO_STT_DEBUG_AUDIO", False))
    model: str = field(default_factory=lambda: os.getenv("AKO_WHISPER_MODEL", "small"))
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
    """WhisperModel을 캐싱해서 반환. 같은 모델/장치/연산 타입이면 재사용."""
    with _whisper_lock:
        model_name = (model_name or "small").strip() or "small"
        device = os.getenv("AKO_WHISPER_DEVICE", "cpu").strip().lower() or "cpu"
        compute_type = os.getenv("AKO_WHISPER_COMPUTE_TYPE", "").strip()
        if not compute_type:
            compute_type = "float16" if device == "cuda" else "int8"

        cache_key = (model_name, device, compute_type)
        if cache_key not in _whisper_cache:
            try:
                from faster_whisper import WhisperModel
                _whisper_cache[cache_key] = WhisperModel(
                    model_name,
                    device=device,
                    compute_type=compute_type,
                )
            except ImportError:
                raise RuntimeError(
                    "faster-whisper가 설치되어 있지 않아요."
                    "pip install faster-whisper"
                )
            except Exception as e:
                # CUDA 설정이 꼬인 PC에서도 프로그램이 죽지 않게 CPU로 한 번 더 시도한다.
                if device == "cuda" and os.getenv("AKO_WHISPER_CUDA_FALLBACK", "1").lower() not in {"0", "false", "no", "off"}:
                    try:
                        from faster_whisper import WhisperModel
                        fallback_key = (model_name, "cpu", "int8")
                        _whisper_cache[fallback_key] = WhisperModel(
                            model_name,
                            device="cpu",
                            compute_type="int8",
                        )
                        print(f"[VOICE] CUDA Whisper 로드 실패, CPU로 폴백합니다: {e}")
                        return _whisper_cache[fallback_key]
                    except Exception as e2:
                        raise RuntimeError(f"Whisper 모델 로드 실패({model_name}): {e}; CPU 폴백 실패: {e2}")
                raise RuntimeError(f"Whisper 모델 로드 실패({model_name}): {e}")
        return _whisper_cache[cache_key]


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
    """sounddevice로 녹음. 음성 후보/녹음/무음 상태를 분리해 종료를 판정한다."""
    import numpy as np
    import sounddevice as sd

    sr = int(cfg.samplerate)
    block_sec = _env_float("AKO_STT_BLOCK_SEC", 0.05, minimum=0.02, maximum=0.20)
    block = max(1, int(sr * block_sec))

    speech_end_threshold = cfg.speech_end_threshold
    if speech_end_threshold is None or speech_end_threshold <= 0:
        speech_end_threshold = cfg.silence_threshold
    speech_end_threshold = max(0.0, float(speech_end_threshold))
    speech_start_threshold = max(float(cfg.speech_start_threshold), speech_end_threshold)
    candidate_sec = max(0.0, float(cfg.candidate_sec))
    silence_sec = max(0.0, float(cfg.silence_sec))
    min_speech_sec = max(0.0, float(cfg.min_speech_sec))
    max_record_sec = max(0.5, float(cfg.max_record_sec))
    attack_smoothing = min(0.98, max(0.0, float(cfg.rms_smoothing)))
    release_smoothing = min(0.98, max(0.0, float(cfg.rms_release_smoothing)))
    debug_audio = bool(cfg.debug_audio or cfg.print_heard_audio_stats)

    waiting = "WAITING"
    candidate = "SPEECH_CANDIDATE"
    recording = "RECORDING"

    frames = []
    pre_roll = deque(maxlen=max(1, int(max(0.0, cfg.pre_roll_sec) / block_sec) + 1))
    candidate_frames = []

    state = waiting
    smooth_rms: float | None = None
    stream_elapsed = 0.0
    candidate_elapsed = 0.0
    active_elapsed = 0.0
    recorded_elapsed = 0.0
    silence_elapsed = 0.0
    finished = False
    finish_reason = ""
    last_debug_at = -1.0

    if debug_audio:
        print(
            "[AUDIO] vad "
            f"start_threshold={speech_start_threshold:.5f} "
            f"end_threshold={speech_end_threshold:.5f} "
            f"candidate_sec={candidate_sec:.2f} "
            f"silence_sec={silence_sec:.2f} "
            f"min_speech_sec={min_speech_sec:.2f} "
            f"max_record_sec={max_record_sec:.2f} "
            f"attack_smoothing={attack_smoothing:.2f} "
            f"release_smoothing={release_smoothing:.2f}"
        )

    def _cb(indata, _frames, _time, status):
        nonlocal state, smooth_rms, stream_elapsed, candidate_elapsed
        nonlocal active_elapsed, recorded_elapsed, silence_elapsed
        nonlocal finished, finish_reason, last_debug_at

        if finished:
            return

        mono = indata[:, 0].copy()
        chunk_sec = _frames / sr
        stream_elapsed += chunk_sec

        rms = _rms(mono)
        if smooth_rms is None:
            smooth_rms = rms
        elif rms > smooth_rms:
            smooth_rms = (smooth_rms * attack_smoothing) + (rms * (1.0 - attack_smoothing))
        else:
            smooth_rms = (smooth_rms * release_smoothing) + (rms * (1.0 - release_smoothing))

        if state == waiting:
            if rms >= speech_start_threshold:
                state = candidate
                candidate_frames.clear()
                candidate_frames.append(mono)
                candidate_elapsed = chunk_sec
                active_elapsed = chunk_sec
                silence_elapsed = 0.0
            else:
                pre_roll.append(mono)

        elif state == candidate:
            candidate_frames.append(mono)
            if rms >= speech_start_threshold:
                candidate_elapsed += chunk_sec
                active_elapsed += chunk_sec
            else:
                state = waiting
                candidate_frames.clear()
                candidate_elapsed = 0.0
                active_elapsed = 0.0
                silence_elapsed = 0.0
            if state == candidate and candidate_elapsed >= candidate_sec:
                frames.extend(pre_roll)
                frames.extend(candidate_frames)
                pre_roll.clear()
                candidate_frames.clear()
                state = recording
                recorded_elapsed = sum(len(frame) for frame in frames) / sr
                silence_elapsed = 0.0

        elif state == recording:
            frames.append(mono)
            recorded_elapsed += chunk_sec
            if smooth_rms < speech_end_threshold:
                silence_elapsed += chunk_sec
            else:
                silence_elapsed = 0.0
                active_elapsed += chunk_sec

            if silence_elapsed >= silence_sec:
                finished = True
                finish_reason = "silence"
            elif recorded_elapsed >= max_record_sec:
                finished = True
                finish_reason = "max_record"

        if state != recording and stream_elapsed >= max_record_sec:
            finished = True
            finish_reason = "no_speech"

        if debug_audio and (last_debug_at < 0 or stream_elapsed - last_debug_at >= 0.20 or finished):
            print(
                "[AUDIO] "
                f"state={state} "
                f"rms={rms:.5f} "
                f"smooth={smooth_rms:.5f} "
                f"started={state == recording} "
                f"raw_start={rms >= speech_start_threshold} "
                f"candidate={candidate_elapsed:.2f} "
                f"active={active_elapsed:.2f} "
                f"silence={silence_elapsed:.2f} "
                f"record={recorded_elapsed:.2f} "
                f"reason={finish_reason}"
            )
            last_debug_at = stream_elapsed

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
                if finished:
                    break
                if time.time() - t0 > max_record_sec + 2.0:
                    finish_reason = finish_reason or "timeout"
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

    if active_elapsed < min_speech_sec:
        if debug_audio:
            print(
                "[AUDIO] discard "
                f"active={active_elapsed:.2f} "
                f"min_speech={min_speech_sec:.2f} "
                f"reason={finish_reason or 'short_speech'}"
            )
        import numpy as np
        return np.zeros((0,), dtype=np.float32)

    import numpy as np
    return np.concatenate(frames, axis=0)

def _build_stt_initial_prompt() -> str:
    """짧은 한국어 PC 명령에서 앱 이름이 뭉개지지 않게 Whisper에 힌트를 준다."""
    aliases = [
        "크롬", "구글 크롬", "디스코드", "디코", "카카오톡", "카톡",
        "스팀", "스포티파이", "메모장", "계산기", "그림판",
        "라이엇", "롤", "리그 오브 레전드", "발로란트", "오비에스",
    ]
    try:
        from command_actions import load_app_specs
        specs = load_app_specs()
        for spec in specs.values():
            for alias in getattr(spec, "aliases", []) or []:
                alias = str(alias).strip()
                if alias and alias not in aliases and len(alias) <= 20:
                    aliases.append(alias)
    except Exception:
        pass

    # 너무 길면 오히려 느려져서 앞부분만 사용한다.
    aliases = aliases[:60]
    return "한국어 PC 음성 명령입니다. 앱 이름 후보: " + ", ".join(aliases) + ". 명령 예시: 크롬 띄워봐, 디스코드 켜줘."


def _apply_stt_correction(text: str) -> str:
    """STT 결과 후처리. 실패해도 원문을 그대로 돌려준다."""
    raw = (text or "").strip()
    if not raw:
        return raw
    try:
        from core.stt_corrections import correct_stt_text
        corrected, reasons = correct_stt_text(raw, return_reasons=True)
        if corrected != raw and os.getenv("AKO_STT_DEBUG_CORRECTION", "").lower() in {"1", "true", "yes", "on"}:
            print(f"[VOICE] STT 보정: {raw} -> {corrected} ({', '.join(reasons)})")
        return corrected
    except Exception:
        return raw

def _env_int(name: str, default: int, minimum: int = 1, maximum: int = 999) -> int:
    try:
        value = int(os.getenv(name, str(default)))
        return max(minimum, min(maximum, value))
    except Exception:
        return default


def _build_initial_prompt() -> str:
    """Whisper에게 앱/명령 어휘를 미리 알려준다. 결과 치환이 아니라 디코딩 힌트다."""
    extra = os.getenv("AKO_WHISPER_INITIAL_PROMPT", "").strip()
    base = (
        "다음은 한국어 PC 음성 명령입니다. "
        "앱 이름으로 크롬, 구글 크롬, 디스코드, 디코, 카카오톡, 카톡, 스팀, "
        "스포티파이, 메모장, 계산기, 유튜브, 롤, 리그 오브 레전드, 발로란트, OBS, "
        "VS Code, 코드, 그림판, 탐색기가 나올 수 있습니다. "
        "명령 표현으로 켜줘, 열어줘, 띄워봐, 실행해줘, 앞으로 가져와, 눌러줘, 클릭해줘, 검색해줘가 나올 수 있습니다."
    )
    return f"{base} {extra}".strip() if extra else base


def _prepare_audio_for_stt(audio):
    """마이크 입력을 Whisper가 듣기 쉬운 크기로 정리한다. 특정 단어 보정은 하지 않는다."""
    import numpy as np

    x = np.asarray(audio, dtype=np.float32)
    if x.size == 0:
        return x

    x = np.nan_to_num(x)
    x = x - float(np.mean(x))

    rms = float((np.mean(x * x) + 1e-12) ** 0.5)
    target_rms = float(os.getenv("AKO_WHISPER_TARGET_RMS", "0.08"))
    max_gain = float(os.getenv("AKO_WHISPER_MAX_GAIN", "6.0"))

    # 너무 작은 음성만 제한적으로 키운다. 이미 큰 음성은 건드리지 않는다.
    gain = 1.0
    if rms > 0 and rms < target_rms:
        gain = min(max_gain, target_rms / max(rms, 1e-6))
        x = x * gain

    peak = float(np.max(np.abs(x))) if x.size else 0.0
    if peak > 0.98:
        x = x * (0.98 / peak)

    if _env_bool("AKO_STT_DEBUG_AUDIO", False):
        new_rms = float((np.mean(x * x) + 1e-12) ** 0.5)
        new_peak = float(np.max(np.abs(x))) if x.size else 0.0
        print(f"[VOICE] audio rms={rms:.4f}->{new_rms:.4f} peak={peak:.4f}->{new_peak:.4f} gain={gain:.2f}")

    return x.astype(np.float32)


# -----------------------------------------------------------------------------
# STT
# -----------------------------------------------------------------------------
def _transcribe(audio, cfg: VoiceConfig) -> str:
    """캐싱된 WhisperModel로 STT 수행. 보정 치환 없이 인식 품질 설정만 강화한다."""
    import numpy as np

    audio = _prepare_audio_for_stt(audio)
    if np.asarray(audio).size == 0:
        return ""

    try:
        model = _get_whisper_model(cfg.model)
    except RuntimeError as e:
        raise RuntimeError(str(e))

    beam_size = _env_int("AKO_WHISPER_BEAM_SIZE", 5, minimum=1, maximum=10)
    best_of = _env_int("AKO_WHISPER_BEST_OF", 5, minimum=1, maximum=10)
    use_vad = _env_bool("AKO_WHISPER_VAD_FILTER", False)
    use_prompt = _env_bool("AKO_WHISPER_USE_INITIAL_PROMPT", True)

    kwargs = {
        "language": (cfg.language or None),
        "vad_filter": use_vad,
        "beam_size": beam_size,
        "best_of": best_of,
        "temperature": 0.0,
        "condition_on_previous_text": False,
        "no_speech_threshold": float(os.getenv("AKO_WHISPER_NO_SPEECH_THRESHOLD", "0.35")),
        "compression_ratio_threshold": float(os.getenv("AKO_WHISPER_COMPRESSION_RATIO_THRESHOLD", "2.4")),
        "log_prob_threshold": float(os.getenv("AKO_WHISPER_LOG_PROB_THRESHOLD", "-1.0")),
    }

    if use_vad:
        kwargs["vad_parameters"] = {
            "min_silence_duration_ms": int(cfg.silence_sec * 1000),
            "speech_pad_ms": int(os.getenv("AKO_WHISPER_VAD_SPEECH_PAD_MS", "250")),
        }

    if use_prompt:
        kwargs["initial_prompt"] = _build_initial_prompt()

    try:
        segments, info = model.transcribe(audio, **kwargs)
        text = " ".join((seg.text or "").strip() for seg in segments if seg.text and seg.text.strip())
        text = " ".join(text.split())

        if _env_bool("AKO_STT_PRINT_RAW", True):
            print(f"[STT RAW] {text}")

        if _env_bool("AKO_STT_DEBUG_TRANSCRIBE", False):
            lang = getattr(info, "language", "")
            prob = getattr(info, "language_probability", 0.0)
            print(f"[VOICE] stt model={cfg.model} beam={beam_size} vad={use_vad} lang={lang} prob={prob:.2f} text={text}")

        return text
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

        text = _apply_stt_correction(text)
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

        text = _apply_stt_correction(text)
        if not text:
            continue

        try:
            on_text(text)
        except Exception as e:
            _err(f"콜백 오류: {e}")
            time.sleep(0.2)

