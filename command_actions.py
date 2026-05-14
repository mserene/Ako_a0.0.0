# command_actions.py
# -----------------------------------------------------------------------------
# 이 파일의 역할
# - 사용자의 텍스트 명령을 "실제 OS 동작"으로 변환한다.
#   예) "크롬 켜줘" → exe 실행 / "디스코드 앞으로" → 포커스 가져오기 / "구글에서 ~ 검색" → 브라우저 검색
#
# 어디와 연결되나
# - local_assistant.py : llm_worker()가 handle_open_app / handle_search_command 를 호출한다.
# - bot.py(디스코드)   : 디스코드 메시지에서 같은 핸들러를 호출할 수 있다.
#
# 어떤 데이터 파일을 쓰나
# - app_commands.json  : 앱 별칭/프로세스명/실행 후보 경로/실행 인자/대체 URI
# - search_sites.json  : 검색 엔진/사이트 정의(별칭, url/uri 템플릿)
# -----------------------------------------------------------------------------

from __future__ import annotations

import csv
import ctypes
import ctypes.wintypes as wt
import glob
import io
import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# -------------------------------------------------------------------------
# 배포/패키징(Python 실행 vs PyInstaller)에서 데이터 파일 경로 처리
# - app_commands.json, search_sites.json 같은 파일은 exe 옆에 둔다.
# - 현재 작업 디렉터리가 달라도 항상 올바르게 로드되도록 base dir로 보정한다.
# -------------------------------------------------------------------------
def _data_path(rel: str) -> str:
    rel = (rel or "").strip()
    if not rel:
        return rel
    if os.path.isabs(rel):
        return rel
    try:
        # PyInstaller로 빌드된 경우(sys.frozen==True) exe가 있는 폴더를 기준으로 함
        base_dir = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(__file__)
    except Exception:
        base_dir = os.getcwd()
    return os.path.join(base_dir, rel)




# -----------------------------------------------------------------------------
# 공통 유틸
# -----------------------------------------------------------------------------
def _expand_env(path: str) -> str:
    return os.path.expandvars(path or "")


def _glob_paths(pattern: str) -> List[str]:
    pat = _expand_env(pattern)
    if "*" in pat or "?" in pat:
        return [p for p in glob.glob(pat) if os.path.isfile(p)]
    return [pat] if os.path.isfile(pat) else []


def _file_exists(path_or_pattern: str) -> bool:
    try:
        if not path_or_pattern:
            return False
        pat = _expand_env(path_or_pattern)
        if "*" in pat or "?" in pat:
            return any(os.path.isfile(p) for p in glob.glob(pat))
        return os.path.isfile(pat)
    except Exception:
        return False


def _run_hidden(cmd: List[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            cmd,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="ignore",
        )
    except Exception as exc:
        return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr=str(exc))


def _which(exe_name: str) -> Optional[str]:
    exe_name = (exe_name or "").strip()
    if not exe_name:
        return None
    cp = _run_hidden(["where", exe_name])
    if cp.returncode == 0:
        first = (cp.stdout or "").splitlines()[0].strip()
        return first if first else None
    return None


def _launch_exe(exe_path_or_name: str, args: Optional[List[str]] = None) -> bool:
    args = args or []
    target = exe_path_or_name

    expanded = _expand_env(target)
    if os.path.isfile(expanded):
        target = expanded
    else:
        found = _which(target)
        if found:
            target = found

    try:
        subprocess.Popen([target] + list(args), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def _start_uri(uri_or_url: str) -> bool:
    u = (uri_or_url or "").strip()
    if not u:
        return False
    try:
        _run_hidden(["cmd", "/c", "start", "", u])
        return True
    except Exception:
        return False


# -----------------------------------------------------------------------------
# 프로세스 조회/포커스(Windows 전용)
# -----------------------------------------------------------------------------
def _is_process_running_exact(image_name: str) -> bool:
    name = (image_name or "").strip()
    if not name:
        return False
    try:
        cp = _run_hidden(["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH"])
        out = (cp.stdout or "").strip()
        if not out:
            return False
        low = out.lower()
        if "no tasks" in low or "정보" in out:
            return False
        return name.lower() in low
    except Exception:
        return False


def _get_pids_exact(image_name: str) -> List[int]:
    name = (image_name or "").strip()
    if not name:
        return []
    try:
        cp = _run_hidden(["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH"])
        out = (cp.stdout or "").strip()
        if not out:
            return []
        low = out.lower()
        if "no tasks" in low or "정보" in out:
            return []
        pids: List[int] = []
        for row in csv.reader(io.StringIO(out)):
            if len(row) >= 2 and (row[0] or "").lower() == name.lower():
                try:
                    pids.append(int(row[1]))
                except Exception:
                    pass
        return pids
    except Exception:
        return []


def _app_activate_by_pid(pid: int) -> bool:
    try:
        ps = (
            "$s=New-Object -ComObject WScript.Shell; "
            f"if($s.AppActivate({int(pid)})){{ exit 0 }} else {{ exit 1 }}"
        )
        cp = _run_hidden(["powershell", "-NoProfile", "-Command", ps])
        return cp.returncode == 0
    except Exception:
        return False


try:
    _user32 = ctypes.WinDLL("user32", use_last_error=True) if sys.platform == "win32" else None
except OSError:
    _user32 = None

SW_RESTORE = 9
SW_SHOW = 5
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_SHOWWINDOW = 0x0040


def _hwnd_title(hwnd) -> str:
    if _user32 is None:
        return ""
    buf = ctypes.create_unicode_buffer(512)
    _user32.GetWindowTextW(wt.HWND(hwnd), buf, 512)
    return buf.value or ""


def _enum_hwnds_for_pid(pid: int) -> List[int]:
    if _user32 is None:
        return []
    results: List[int] = []
    pid_c = wt.DWORD()

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def _cb(hwnd, lparam):
        try:
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_c))
            if pid_c.value == int(pid):
                title = _hwnd_title(hwnd).strip()
                if title:
                    results.append(int(hwnd))
        except Exception:
            pass
        return True

    _user32.EnumWindows(_cb, 0)
    return results


def _force_foreground(hwnd: int) -> bool:
    if _user32 is None:
        return False
    try:
        h = wt.HWND(hwnd)
        _user32.ShowWindowAsync(h, SW_RESTORE)
        _user32.ShowWindowAsync(h, SW_SHOW)
        _user32.SetWindowPos(h, wt.HWND(HWND_TOPMOST), 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
        _user32.SetWindowPos(h, wt.HWND(HWND_NOTOPMOST), 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
        ok = bool(_user32.SetForegroundWindow(h))
        _user32.BringWindowToTop(h)
        return ok
    except Exception:
        return False


def _bring_to_front_process(process_name: str, title_hints: Optional[List[str]] = None) -> bool:
    pids = _get_pids_exact(process_name)
    if not pids:
        return False

    hints = [h.lower() for h in (title_hints or []) if h]
    for pid in pids:
        hwnds = _enum_hwnds_for_pid(pid)

        # 창 핸들을 못 찾으면 AppActivate로 시도
        if not hwnds:
            if _app_activate_by_pid(pid):
                return True
            continue

        # 힌트(제목/별칭)가 포함된 창을 우선으로 점수화
        scored = []
        for hwnd in hwnds:
            title = _hwnd_title(hwnd).strip()
            t_low = title.lower()
            score = 1
            if hints and any(h in t_low for h in hints):
                score += 10
            score += min(len(title), 100) / 100.0
            scored.append((score, hwnd))

        scored.sort(reverse=True, key=lambda x: x[0])
        best = scored[0][1]

        if _force_foreground(best):
            return True
        if _app_activate_by_pid(pid):
            return True

    return False


def _app_activate_by_title(title: str) -> bool:
    t = (title or "").strip()
    if not t:
        return False
    safe_title = t.replace("'", "''")
    ps = (
        "$s=New-Object -ComObject WScript.Shell; "
        f"if($s.AppActivate('{safe_title}')){{ exit 0 }} else {{ exit 1 }}"
    )
    cp = _run_hidden(["powershell", "-NoProfile", "-Command", ps])
    return cp.returncode == 0


# -----------------------------------------------------------------------------
# 앱 사전(app_commands.json)
# -----------------------------------------------------------------------------
@dataclass
class AppSpec:
    key: str
    aliases: List[str]
    process_name: str
    window_title: str = ""
    candidates: List[str] = None
    args: List[str] = None
    fallback_uri: Optional[str] = None

    def __post_init__(self):
        if self.candidates is None:
            self.candidates = []
        if self.args is None:
            self.args = []
        if self.aliases is None:
            self.aliases = []


_APP_CACHE: Optional[Dict[str, AppSpec]] = None
_APP_CACHE_MTIME: float = 0.0


def load_app_specs(path: str = "app_commands.json") -> Dict[str, AppSpec]:
    # 앱 사전은 여러 곳에서 공유되니 수정 시간이 같을 때만 캐시를 재사용한다.
    global _APP_CACHE, _APP_CACHE_MTIME
    full = _data_path(path)
    try:
        mtime = os.path.getmtime(full)
    except Exception:
        mtime = 0.0
    if _APP_CACHE is not None and mtime == _APP_CACHE_MTIME:
        return _APP_CACHE

    try:
        with open(full, "r", encoding="utf-8") as f:
            raw = json.load(f)
        apps = raw.get("apps", raw)
    except Exception:
        logging.exception("load_app_specs failed")
        apps = {}

    out: Dict[str, AppSpec] = {}
    for key, v in (apps or {}).items():
        if not isinstance(v, dict):
            continue
        out[key] = AppSpec(
            key=key,
            aliases=list(v.get("aliases", [])),
            process_name=str(v.get("process_name", "")),
            window_title=str(v.get("window_title", "")),
            candidates=list(v.get("candidates", [])),
            args=list(v.get("args", [])),
            fallback_uri=v.get("fallback_uri"),
        )

    _APP_CACHE = out
    _APP_CACHE_MTIME = mtime
    return out


def _resolve_candidate_list(spec: AppSpec) -> List[Tuple[str, List[str]]]:
    # app_commands.json의 candidates + 일부 앱(디스코드/카톡/스포티파이) 추가 후보를 합친다.
    out: List[Tuple[str, List[str]]] = []

    for cand in (spec.candidates or []):
        for p in _glob_paths(cand):
            out.append((p, list(spec.args or [])))

    if spec.process_name.lower() == "discord.exe":
        extra = [
            r"%LOCALAPPDATA%\Discord\app-*\Discord.exe",
            r"%LOCALAPPDATA%\DiscordCanary\app-*\Discord.exe",
            r"%LOCALAPPDATA%\DiscordPTB\app-*\Discord.exe",
        ]
        for pat in extra:
            for p in _glob_paths(pat):
                out.append((p, []))

    if spec.process_name.lower() == "kakaotalk.exe":
        extra = [
            r"%LOCALAPPDATA%\Kakao\KakaoTalk\KakaoTalk.exe",
            r"%LOCALAPPDATA%\KakaoTalk\KakaoTalk.exe",
        ]
        for pat in extra:
            for p in _glob_paths(pat):
                out.append((p, []))

    if spec.process_name.lower() == "spotify.exe":
        extra = [
            r"%APPDATA%\Spotify\Spotify.exe",
            r"%LOCALAPPDATA%\Spotify\Spotify.exe",
        ]
        for pat in extra:
            for p in _glob_paths(pat):
                out.append((p, []))

    # 경로 중복 제거
    seen = set()
    uniq: List[Tuple[str, List[str]]] = []
    for p, a in out:
        k = os.path.normcase(p)
        if k in seen:
            continue
        seen.add(k)
        uniq.append((p, a))
    return uniq


def match_app(text: str, specs: Dict[str, AppSpec]) -> Optional[AppSpec]:
    t = (text or "").strip()
    if not t:
        return None
    for spec in specs.values():
        for a in (spec.aliases or []):
            if a and a in t:
                return spec
    return None


# -----------------------------------------------------------------------------
# 명령 판별(앱 열기/포커스)
# -----------------------------------------------------------------------------
def is_open_intent(text: str) -> bool:
    return bool(re.search(r"(열어|켜|실행|띄워)\s*(줘|줘요|라|줘라)?", text or ""))


def is_focus_intent(text: str) -> bool:
    return bool(re.search(r"(앞으로|포커스|전면|맨\s*앞|앞에\s*가져와)", text or ""))


def is_open_or_focus_intent(text: str) -> bool:
    return is_open_intent(text) or is_focus_intent(text)


# -----------------------------------------------------------------------------
# 1) 앱 실행/포커스 처리
# -----------------------------------------------------------------------------
def handle_open_app(user_text: str, app_specs: Optional[Dict[str, AppSpec]] = None) -> Optional[str]:
    txt = (user_text or "").strip()
    if not txt or not is_open_or_focus_intent(txt):
        return None

    specs = app_specs or load_app_specs()
    spec = match_app(txt, specs)
    if not spec:
        return None

    app_name = spec.aliases[0] if spec.aliases else spec.key

    # 1) 이미 켜져 있으면 "앞으로 띄우기" 시도
    if _is_process_running_exact(spec.process_name):
        hints: List[str] = []
        if spec.window_title:
            hints.append(spec.window_title)
        hints.extend([a for a in (spec.aliases or []) if a])

        ok = _bring_to_front_process(spec.process_name, hints)
        if not ok:
            for h in hints:
                if _app_activate_by_title(h):
                    ok = True
                    break

        if ok:
            return f"{app_name} 켜져 있어요. 앞으로 띄웠어요."

        # focus 의도가 강하면 재실행 후 포커스 재시도
        if is_focus_intent(txt):
            try:
                for exe_path, args in _resolve_candidate_list(spec):
                    _launch_exe(exe_path, args)
                    break
                time.sleep(0.6)
                ok2 = _bring_to_front_process(spec.process_name, hints)
                if not ok2:
                    for h in hints:
                        if _app_activate_by_title(h):
                            ok2 = True
                            break
                if ok2:
                    return f"{app_name} 켜져 있어요. 앞으로 띄웠어요."
            except Exception:
                pass

        return f"{app_name} 켜져 있어요. 근데 창을 앞으로 가져오지는 못했어요."

    # 2) 꺼져 있으면 candidates 우선 실행
    for exe_path, args in _resolve_candidate_list(spec):
        if _launch_exe(exe_path, args):
            return f"{app_name} 켰어요."

    # 3) 대체 URI
    if spec.fallback_uri:
        if _start_uri(spec.fallback_uri):
            return f"{app_name} 켰어요."

    # 4) 마지막으로 process_name 자체를 exe로 가정하고 실행
    if spec.process_name and _launch_exe(spec.process_name, spec.args):
        return f"{app_name} 켰어요."

    return f"{app_name} 실행을 못 했어요. 설치 경로나 후보 경로를 app_commands.json에 추가해줘요."


# -----------------------------------------------------------------------------
# 검색 사전(search_sites.json)
# -----------------------------------------------------------------------------
@dataclass
class SearchSite:
    key: str
    aliases: List[str]
    type: str
    url: str = ""
    uri: str = ""
    browser_app: str = ""

    def __post_init__(self):
        if self.aliases is None:
            self.aliases = []


_SEARCH_CACHE: Optional[Tuple[str, Dict[str, SearchSite]]] = None
_SEARCH_CACHE_MTIME: float = 0.0


def load_search_sites(path: str = "search_sites.json") -> Tuple[str, Dict[str, SearchSite]]:
    global _SEARCH_CACHE, _SEARCH_CACHE_MTIME
    full = _data_path(path)
    try:
        mtime = os.path.getmtime(full)
    except Exception:
        mtime = 0.0
    if _SEARCH_CACHE is not None and mtime == _SEARCH_CACHE_MTIME:
        return _SEARCH_CACHE

    default_key = "google"
    sites: Dict[str, SearchSite] = {}
    try:
        with open(full, "r", encoding="utf-8") as f:
            raw = json.load(f)
        default_key = raw.get("default", default_key)
        raw_sites = raw.get("sites", {})
        for key, v in (raw_sites or {}).items():
            if not isinstance(v, dict):
                continue
            sites[key] = SearchSite(
                key=key,
                aliases=list(v.get("aliases", [])),
                type=str(v.get("type", "web")),
                url=str(v.get("url", "")),
                uri=str(v.get("uri", "")),
                browser_app=str(v.get("browser_app", "")),
            )
    except Exception:
        logging.exception("load_search_sites failed")

    _SEARCH_CACHE = (default_key, sites)
    _SEARCH_CACHE_MTIME = mtime
    return _SEARCH_CACHE


def _match_site(site_text: str, sites: Dict[str, SearchSite]) -> Optional[SearchSite]:
    t = (site_text or "").strip()
    if not t:
        return None
    for s in sites.values():
        for a in s.aliases:
            if a and a in t:
                return s
    if t in sites:
        return sites.get(t)
    return None


_SEARCH_VERB = r"(?:검색\s*해\s*줘(?:요|라)?|검색해(?:요|라)?|검색|찾아\s*줘(?:요|라)?|찾아(?:요|라)?|서치\s*해\s*줘(?:요|라)?|서치해(?:요|라)?|서치)"
_SEARCH_PATTERNS = [
    re.compile(fr"(?P<site>.+?)(?:에서|에)\s*(?P<q>.+?)\s*{_SEARCH_VERB}\s*$"),
    re.compile(fr"(?P<q>.+?)\s*{_SEARCH_VERB}\s*$"),
]


# -----------------------------------------------------------------------------
# 2) 검색 처리
# -----------------------------------------------------------------------------
def handle_search_command(
    user_text: str,
    app_specs: Optional[Dict[str, AppSpec]] = None,
    sites_path: str = "search_sites.json",
) -> Optional[str]:
    txt = (user_text or "").strip()
    if not txt:
        return None

    default_key, sites = load_search_sites(sites_path)

    m = None
    for pat in _SEARCH_PATTERNS:
        m = pat.match(txt)
        if m:
            break
    if not m:
        return None

    q = (m.groupdict().get("q") or "").strip()
    if not q:
        return None

    site_text = (m.groupdict().get("site") or "").strip()
    site = _match_site(site_text, sites) if site_text else sites.get(default_key)
    if not site:
        site = sites.get(default_key)

    q_url = urllib.parse.quote(q, safe="")
    q_uri = urllib.parse.quote(q, safe="")

    # uri 타입(앱 내부 검색 등)
    if site.type == "uri" and site.uri:
        uri = site.uri.replace("{q}", q_uri)
        _start_uri(uri)
        name = site.aliases[0] if site.aliases else site.key
        return f"{name}에서 검색했어요."

    # 웹 검색
    url = (site.url or "").replace("{q}", q_url)
    if not url:
        url = f"https://www.google.com/search?q={q_url}"

    _start_uri(url)
    name = site.aliases[0] if site.aliases else site.key
    return f"{name}에서 검색했어요."

# -----------------------------------------------------------------------------
# app.py에서 분리: 로컬 명령 파서 (app.py는 진입점만 담당하도록)
# -----------------------------------------------------------------------------
_DIR_PAT = r"(왼쪽\s*위|오른쪽\s*위|왼쪽\s*아래|오른쪽\s*아래|왼쪽|오른쪽|위|아래|좌상|우상|좌하|우하)"


def handle_window_control(text: str, app_specs: Optional[Dict[str, AppSpec]] = None) -> Optional[str]:
    """창 닫기/최소화 명령 처리."""
    s = (text or "").strip()
    if not s:
        return None

    try:
        from automation.window_control import close_foreground_window, close_window_by_app, minimize_all_windows
    except Exception as e:
        return f"창 제어 모듈 로드 실패: {e}"

    compact = re.sub(r"\s+", "", s)
    if re.search(r"(창|윈도우).*(다|전부|모두).*(내려|최소화)", s) or re.search(r"(다|전부|모두).*창.*(내려|최소화)", s):
        return "창을 모두 최소화했어요." if minimize_all_windows() else "창 최소화에 실패했어요."

    if re.search(r"(맨\s*앞|현재|앞에\s*있는|포커스된).*(창|윈도우).*(꺼|닫|종료)", s) or compact in {"창꺼줘", "창닫아줘", "현재창꺼줘", "현재창닫아줘"}:
        return "맨 앞 창을 닫았어요." if close_foreground_window() else "맨 앞 창을 닫지 못했어요."

    if not re.search(r"(꺼\s*줘|꺼줘|닫아\s*줘|닫아줘|종료\s*해\s*줘|종료해줘|끄기|꺼)", s):
        return None

    specs = app_specs or load_app_specs()
    spec = match_app(s, specs)
    if not spec:
        return None

    hints = []
    if spec.window_title:
        hints.append(spec.window_title)
    hints.extend(spec.aliases or [])
    ok = close_window_by_app(spec.process_name, hints)
    app_name = spec.aliases[0] if spec.aliases else spec.key
    return f"{app_name} 창을 닫았어요." if ok else f"{app_name} 창을 찾지 못했어요."


def handle_media_control(text: str) -> Optional[str]:
    """Windows media key 기반 재생 제어."""
    s = (text or "").strip()
    if not s:
        return None

    media_context = re.search(r"(영상|음악|노래|미디어|재생|일시\s*정지|일시정지|다음\s*곡|이전\s*곡)", s)
    if not media_context:
        return None

    try:
        from automation import media_control
    except Exception as e:
        return f"미디어 제어 모듈 로드 실패: {e}"

    if re.search(r"(다음\s*곡|다음\s*노래|넘겨\s*줘|넘겨줘|next)", s, re.IGNORECASE):
        return "다음 트랙으로 넘겼어요." if media_control.next_track() else "다음 트랙 제어에 실패했어요."
    if re.search(r"(이전\s*곡|이전\s*노래|뒤로|previous|prev)", s, re.IGNORECASE):
        return "이전 트랙으로 넘겼어요." if media_control.previous_track() else "이전 트랙 제어에 실패했어요."
    if re.search(r"(멈춰\s*줘|멈춰줘|정지|일시\s*정지|일시정지|재생\s*해\s*줘|재생해줘|틀어\s*줘|틀어줘|pause|play)", s, re.IGNORECASE):
        return "미디어 재생/일시정지를 전환했어요." if media_control.play_pause() else "미디어 제어에 실패했어요."

    return None


def handle_screen_capture(text: str) -> Optional[str]:
    """현재 화면 캡처 저장."""
    s = (text or "").strip()
    if not s or not re.search(r"(화면|스크린|모니터).*(캡처|캡쳐|스크린샷|저장)", s):
        return None
    try:
        from vision.screen_tools import save_screenshot
        path = save_screenshot(monitor_index=1)
        return f"화면을 캡처했어요: {path}"
    except Exception as e:
        return f"화면 캡처 실패: {e}"


def handle_screen_highlight(text: str) -> Optional[str]:
    """화면 내 텍스트 OCR 검색 후 overlay 강조."""
    s = (text or "").strip()
    if not s:
        return None

    if re.search(r"(강조|하이라이트).*(꺼|끄|제거|닫|없애)|이제\s*됐어", s):
        try:
            from vision.highlight_overlay import clear_highlights
            return "강조를 껐어요." if clear_highlights() else "꺼진 강조가 없어요."
        except Exception as e:
            return f"강조 제거 실패: {e}"

    if not re.search(r"(있니|있어|찾아\s*줘|찾아줘|강조|하이라이트|어디)", s):
        return None
    if not re.search(r"(화면|여기|현재|지금|단어|텍스트|글자|문구|찾아|강조|하이라이트)", s):
        return None

    try:
        from vision.highlight_overlay import HighlightRect, show_highlights
        from vision.screen_tools import extract_quoted_or_target, find_text_on_screen
    except Exception as e:
        return f"화면 강조 모듈 로드 실패: {e}"

    target = extract_quoted_or_target(s)
    if not target:
        return None

    try:
        hits = find_text_on_screen(target, monitor_index=1)
        if not hits:
            return f"화면에서 '{target}'를 찾지 못했어요."

        import pyautogui as pag

        width, height = pag.size()
        rects = [HighlightRect(hit.x, hit.y, hit.w, hit.h, hit.text) for hit in hits[:12]]
        show_highlights(rects, int(width), int(height))
        return f"화면에서 '{target}'를 {len(hits)}개 찾았어요. 보라색으로 강조했어요."
    except Exception as e:
        return f"화면 텍스트 검색 실패: {e}"


def handle_youtube_toggle(text: str) -> Optional[str]:
    """'유튜브 재생 눌러줘', '유튜브 일시정지 눌러줘' 등을 처리."""
    s = (text or "").strip()
    if not s or "유튜브" not in s:
        return None

    dm = re.search(_DIR_PAT, s)
    direction = dm.group(0) if dm else None

    if re.search(r"(재생|일시\s*정지|일시정지|멈춰|정지|토글)", s):
        try:
            from ui_tap import youtube_toggle_click_only
            youtube_toggle_click_only(direction=direction)
            return "유튜브 토글 완료"
        except Exception as e:
            return f"유튜브 토글 실패: {e}"
    return None


def handle_ui_click(text: str) -> Optional[str]:
    """'닫기 눌러줘', '오른쪽 위에 있는 닫기 눌러줘' 등 UI 클릭 명령 처리."""
    s = (text or "").strip()
    if not s:
        return None

    m = re.search(
        rf"(?:(?P<dir>{_DIR_PAT})\s*(?:에\s*있는|쪽|쪽에\s*있는)?\s*)?(?P<label>.+?)\s*(?:버튼)?\s*(?:눌러\s*줘|눌러줘|클릭\s*해\s*줘|클릭해줘)$",
        s,
    )
    if not m:
        return None

    direction = m.group("dir")
    label = m.group("label").strip().strip('"').strip("'")
    if not label:
        return None

    try:
        from ui_do import do_click_text
        ok = do_click_text(target_text=label, direction=direction or None, monitor_index=1)
        return f"'{label}' 클릭 완료" if ok else f"'{label}'를 화면에서 찾지 못했어요."
    except Exception as e:
        return f"UI 클릭 오류: {e}"


def run_text(user_text: str) -> str:
    """
    [Deprecated] app.py의 run_actions() 또는 llm_agent.py의 run_agent()를 사용하세요.
    하위 호환을 위해 남겨두지만 LLM 에이전트로 위임합니다.
    """
    try:
        from llm_agent import run_agent
        return run_agent(user_text)
    except Exception:
        pass

    # fallback
    txt = (user_text or "").strip()
    if not txt:
        return "명령이 비어 있어요."
    try:
        msg = handle_open_app(txt)
        if msg:
            return msg
    except Exception:
        pass
    try:
        msg = handle_search_command(txt)
        if msg:
            return msg
    except Exception:
        pass
    return "아직 그 명령은 못 해요."
