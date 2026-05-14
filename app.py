# app.py
# 통합 엔트리: ako_ai
from __future__ import annotations

import argparse
import logging
import os
import sys


def _set_workdir_to_appdir() -> str:
    """배포(PyInstaller one-folder)에서 상대경로가 항상 exe 폴더 기준이 되도록 고정"""
    try:
        app_dir = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(__file__)
    except Exception:
        app_dir = os.getcwd()
    if app_dir and os.path.isdir(app_dir):
        os.chdir(app_dir)
    return app_dir


_APP_DIR = _set_workdir_to_appdir()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("ako.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)


def run_actions(text: str) -> str:
    txt = (text or "").strip()
    if not txt:
        return "명령이 비어 있어요."

    try:
        from command_actions import (
            handle_youtube_toggle,
            handle_ui_click,
            handle_open_app,
            handle_search_command,
            load_app_specs,
        )
    except ImportError as e:
        logging.exception("command_actions import failed")
        return f"명령 모듈 로드 실패: {e}"

    # 0) 유튜브 토글
    try:
        r = handle_youtube_toggle(txt)
        if r:
            return r
    except Exception as e:
        logging.exception("youtube toggle failed")
        return f"유튜브 토글 오류: {e}"

    # 1) UI 클릭
    try:
        r = handle_ui_click(txt)
        if r:
            return r
    except Exception as e:
        logging.exception("ui click failed")
        return f"UI 클릭 오류: {e}"

    # 2) 앱 실행/포커스
    try:
        specs = load_app_specs()
        r = handle_open_app(txt, specs)
        if r:
            return r
    except Exception as e:
        logging.exception("open app failed")
        return f"앱 실행 오류: {e}"

    # 3) 검색
    try:
        r = handle_search_command(txt)
        if r:
            return r
    except Exception as e:
        logging.exception("search failed")
        return f"검색 오류: {e}"

    # 4) 그래도 못 잡으면 LLM 에이전트
    try:
        from llm_agent import run_agent
        return run_agent(txt)
    except Exception as e:
        logging.warning("[LLM fallback failed] %s", e, exc_info=True)

    return "명령을 이해하지 못했어요. 예: '크롬 켜줘', '디스코드 앞으로', '유튜브에서 고양이 검색해줘'"

def run_ui() -> str:
    try:
        from ui_loop import ui_mvp_loop
        ui_mvp_loop()
        return "UI 루프 종료"
    except Exception as e:
        logging.exception("UI loop failed")
        return f"UI 루프 오류: {e}"


def run_do(press: str = "", direction: str = "", timeout_sec: float = 8.0, tap: str = "") -> str:
    if tap:
        try:
            from ui_tap import youtube_toggle_click_only
            if tap in ("youtube", "youtube_toggle", "yt"):
                youtube_toggle_click_only(direction=(direction or None))
                return "[DO] ok"
            return f"[DO] 알 수 없는 tap 액션: {tap}"
        except Exception as e:
            logging.exception("tap action failed")
            return f"[DO] tap 오류: {e}"

    try:
        from ui_do import do_click_text
        ok = do_click_text(target_text=press, direction=(direction or None), monitor_index=1, timeout_sec=timeout_sec)
        return "[DO] ok" if ok else f"[DO] '{press}'를 화면에서 찾지 못했어요."
    except Exception as e:
        logging.exception("do_click_text failed")
        return f"[DO] 오류: {e}"


def main():
    p = argparse.ArgumentParser(prog="ako_ai")
    p.add_argument("--mode", choices=["gui", "actions", "ui", "do", "voice", "voice-test"], default="gui")
    p.add_argument("--text", default="", help="actions 모드에서 사용할 텍스트 명령")
    p.add_argument("--press", default="", help="do 모드: 클릭할 텍스트 (예: 닫기/취소/확인)")
    p.add_argument("--tap", default="", help="do 모드: 탭/토글 액션 (예: youtube_toggle)")
    p.add_argument("--dir", default="", help="do 모드: 방향(예: 왼쪽/오른쪽/위/아래)")
    p.add_argument("--timeout", type=float, default=8.0, help="do 모드: 찾기 제한 시간(초)")
    p.add_argument("--wake", default="", help="voice 모드: 웨이크워드(예: 아코). 비우면 항상 실행")
    p.add_argument("--device", type=int, default=-1, help="voice 모드: 입력 장치 인덱스. -1이면 기본")
    p.add_argument("--sr", type=int, default=16000, help="voice 모드: 샘플레이트")
    p.add_argument("--model", default="small", help="voice 모드: faster-whisper 모델명 (tiny/base/small/medium/large-v3)")
    p.add_argument("--lang", default="ko", help="voice 모드: 인식 언어(ko/en 등)")
    p.add_argument("--silence", type=float, default=0.8, help="voice 모드: 종료 판정 무음 지속시간(초)")
    p.add_argument("--start-thresh", type=float, default=None, help="voice 모드: 음성 시작 RMS 임계값")
    p.add_argument("--end-thresh", type=float, default=None, help="voice 모드: 음성 종료 RMS 임계값")
    p.add_argument("--thresh", type=float, default=None, help="voice 모드: 종료 RMS 임계값(이전 옵션, --end-thresh와 같음)")
    p.add_argument("--max-record", type=float, default=None, help="voice 모드: 한 번의 녹음 최대 길이(초)")

    args = p.parse_args()

    def _make_voice_config():
        from voice_loop import VoiceConfig

        threshold_kwargs = {}
        end_threshold = args.end_thresh if args.end_thresh is not None else args.thresh
        if args.start_thresh is not None:
            threshold_kwargs["speech_start_threshold"] = float(args.start_thresh)
        if end_threshold is not None:
            threshold_kwargs["speech_end_threshold"] = float(end_threshold)
            threshold_kwargs["silence_threshold"] = float(end_threshold)
        if args.max_record is not None:
            threshold_kwargs["max_record_sec"] = float(args.max_record)
        return VoiceConfig(
            device=(None if args.device == -1 else args.device),
            samplerate=int(args.sr),
            model=str(args.model),
            language=str(args.lang),
            wake_word=str(args.wake),
            silence_sec=float(args.silence),
            **threshold_kwargs,
        )

    if args.mode == "gui":
        try:
            from ako_gui import main as gui_main
            gui_main()
        except Exception as e:
            logging.exception("GUI launch failed")
            print(f"[GUI] 실행 오류: {e}")
        return

    if args.mode == "actions":
        if not args.text.strip():
            print('예: python app.py --mode=actions --text "크롬 켜줘"')
            print('예: python app.py --mode=actions --text "오른쪽에 있는 닫기 눌러줘"')
            print('예: python app.py --mode=actions --text "유튜브 재생 눌러줘"')
            return
        print(run_actions(args.text))

    elif args.mode == "do":
        if not args.press.strip() and not args.tap.strip():
            print('예: python app.py --mode=do --press "닫기"')
            print('예: python app.py --mode=do --press "닫기" --dir "왼쪽"')
            print('예: python app.py --mode=do --tap "youtube_toggle"')
            return
        print(run_do(args.press, direction=args.dir, timeout_sec=args.timeout, tap=args.tap))

    elif args.mode == "ui":
        print(run_ui())

    elif args.mode == "voice":
        try:
            from voice_loop import voice_actions_loop
            voice_actions_loop(_make_voice_config())
        except Exception as e:
            logging.exception("VOICE launch failed")
            print(f"[VOICE] 실행 오류: {e}")

    elif args.mode == "voice-test":
        try:
            from voice_loop import listen_once
            cfg = _make_voice_config()
            print("[VOICE TEST] STT만 출력합니다. LLM/controller로 전달하지 않습니다. (Ctrl+C 종료)")
            while True:
                text = listen_once(cfg)
                print(f"[VOICE TEST] {text}")
        except KeyboardInterrupt:
            print("\n[VOICE TEST] 종료")
        except Exception as e:
            logging.exception("VOICE TEST launch failed")
            print(f"[VOICE TEST] 실행 오류: {e}")


if __name__ == "__main__":
    main()
