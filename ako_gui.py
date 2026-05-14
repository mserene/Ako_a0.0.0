from __future__ import annotations

import os
import sys
import tkinter as tk
import threading
from pathlib import Path

from loading_overlay import LoadingOverlay
from core.controller import AkoController
from voice_loop import VoiceConfig


def resource_path(rel_path: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.abspath("."))
    return os.path.join(base, rel_path)


def _rounded_rect(canvas: tk.Canvas, x1, y1, x2, y2, radius=18, **kwargs):
    """Canvas에 둥근 사각형을 그리는 helper."""
    radius = max(0, min(radius, int((x2 - x1) / 2), int((y2 - y1) / 2)))
    points = [
        x1 + radius, y1,
        x2 - radius, y1,
        x2, y1,
        x2, y1 + radius,
        x2, y2 - radius,
        x2, y2,
        x2 - radius, y2,
        x1 + radius, y2,
        x1, y2,
        x1, y2 - radius,
        x1, y1 + radius,
        x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, **kwargs)


class RoundIconButton(tk.Canvas):
    def __init__(
        self,
        master,
        command,
        size=42,
        bg="#f2f4ff",
        fg="#0c0f1a",
        hover_bg="#ffffff",
        disabled_bg="#555a70",
    ):
        super().__init__(
            master,
            width=size,
            height=size,
            bg=master.cget("bg"),
            highlightthickness=0,
            bd=0,
        )
        self.command = command
        self.size = size
        self.bg_color = bg
        self.fg_color = fg
        self.hover_bg = hover_bg
        self.disabled_bg = disabled_bg
        self.enabled = True
        self._pressed = False

        self._draw(bg)

        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)

    def _draw(self, fill):
        self.delete("all")
        pad = 2
        self.create_oval(
            pad,
            pad,
            self.size - pad,
            self.size - pad,
            fill=fill,
            outline="",
        )
        cx = self.size / 2
        cy = self.size / 2
        self.create_polygon(
            cx - 5, cy - 7,
            cx + 8, cy,
            cx - 5, cy + 7,
            fill=self.fg_color,
            outline="",
        )

    def configure_state(self, enabled: bool):
        self.enabled = enabled
        self._draw(self.bg_color if enabled else self.disabled_bg)

    def _on_enter(self, _event):
        if self.enabled and not self._pressed:
            self._draw(self.hover_bg)

    def _on_leave(self, _event):
        if self.enabled and not self._pressed:
            self._draw(self.bg_color)

    def _on_press(self, _event):
        if self.enabled:
            self._pressed = True
            self._draw(self.hover_bg)

    def _on_release(self, event):
        if not self.enabled:
            return

        inside = 0 <= event.x <= self.size and 0 <= event.y <= self.size
        self._pressed = False
        self._draw(self.hover_bg if inside else self.bg_color)

        if inside and self.command:
            self.command()


class ChatBubble(tk.Frame):
    """인스타 DM 느낌의 좌우 말풍선."""

    def __init__(
        self,
        master,
        role: str,
        text: str,
        colors: dict[str, str],
        max_width: int,
        font=None,
        name_font=None,
    ):
        super().__init__(master, bg=colors["chat_bg"])

        self.role = role
        self.colors = colors
        self.max_width = max_width
        self.font = font or ("Malgun Gothic", 11)
        self.name_font = name_font or ("Malgun Gothic", 9, "bold")

        if role == "user":
            self.bubble_bg = colors["bubble_user"]
            self.text_fg = colors["bubble_user_text"]
            self.anchor = "e"
            self.side = "right"
            self.name = ""
        elif role == "assistant":
            self.bubble_bg = colors["bubble_assistant"]
            self.text_fg = colors["bubble_assistant_text"]
            self.anchor = "w"
            self.side = "left"
            self.name = "아코"
        else:
            self.bubble_bg = colors["bubble_system"]
            self.text_fg = colors["bubble_system_text"]
            self.anchor = "center"
            self.side = "top"
            self.name = ""

        self.canvas = tk.Canvas(
            self,
            bg=colors["chat_bg"],
            bd=0,
            highlightthickness=0,
            relief="flat",
        )
        self.canvas.pack(anchor=self.anchor if role != "system" else "center")

        self.text_id: int | None = None
        self.bg_id: int | None = None
        self.name_id: int | None = None
        self._text = ""

        self.set_text(text)

    def set_text(self, text: str):
        self._text = text or " "
        self._redraw()

    def set_max_width(self, max_width: int):
        self.max_width = max(220, max_width)
        self._redraw()

    def _redraw(self):
        self.canvas.delete("all")

        padding_x = 15
        padding_y = 10
        name_gap = 4
        radius = 18

        wrap_width = max(220, self.max_width - padding_x * 2)

        y = padding_y

        if self.name:
            self.name_id = self.canvas.create_text(
                padding_x,
                y,
                text=self.name,
                fill=self.colors["muted"],
                font=self.name_font,
                anchor="nw",
                width=wrap_width,
            )
            name_bbox = self.canvas.bbox(self.name_id) or (0, 0, 0, 0)
            y = name_bbox[3] + name_gap

        self.text_id = self.canvas.create_text(
            padding_x,
            y,
            text=self._text,
            fill=self.text_fg,
            font=self.font,
            anchor="nw",
            width=wrap_width,
        )

        text_bbox = self.canvas.bbox(self.text_id) or (0, 0, 80, 24)
        content_w = text_bbox[2] - text_bbox[0]
        content_h = text_bbox[3] - padding_y

        bubble_w = min(self.max_width, max(54, content_w + padding_x * 2))
        bubble_h = max(38, content_h + padding_y)

        # 이름이 있으면 말풍선 안 상단에 같이 들어가게 여유 계산
        if self.name:
            bubble_h = max(54, text_bbox[3] + padding_y)

        self.bg_id = _rounded_rect(
            self.canvas,
            0,
            0,
            bubble_w,
            bubble_h,
            radius=radius,
            fill=self.bubble_bg,
            outline="",
        )
        self.canvas.tag_lower(self.bg_id)

        self.canvas.configure(width=bubble_w, height=bubble_h)


class AkoGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.withdraw()

        try:
            self.iconbitmap(resource_path(os.path.join("assets", "ako.ico")))
        except Exception:
            pass

        self.title("Ako")
        self.geometry("940x740")
        self.minsize(860, 680)
        self.configure(bg="#090b14")
        self._ui_thread_id = threading.get_ident()

        self.colors = {
            "bg": "#090b14",
            "panel": "#101321",
            "panel_2": "#151a2b",
            "border": "#2a2f4b",
            "text": "#eceefe",
            "muted": "#a7adc9",
            "accent": "#ab8dff",
            "accent_soft": "#241f45",
            "entry_bg": "#23252d",
            "entry_fg": "#f3f4ff",
            "chat_bg": "#0c0f1b",
            "bubble_user": "#ab8dff",
            "bubble_user_text": "#0b0b14",
            "bubble_assistant": "#1b2035",
            "bubble_assistant_text": "#f3f4ff",
            "bubble_system": "#111728",
            "bubble_system_text": "#a7adc9",
            "chip_on_bg": "#241f45",
            "chip_on_fg": "#ab8dff",
            "chip_off_bg": "#151a2b",
            "chip_off_fg": "#a7adc9",
        }
        # Windows 기본 한글 폰트 중 가장 안정적인 쪽으로 통일.
        # Pretendard를 모든 사용자 PC에 강제 포함하지 않는 한, 배포용은 Malgun Gothic이 안전합니다.
        self.font_family = "Malgun Gothic"

        self.fonts = {
            "logo": (self.font_family, 12, "bold"),
            "status": (self.font_family, 10, "bold"),
            "button": (self.font_family, 10, "bold"),
            "bubble": (self.font_family, 11, "bold"),
            "bubble_name": (self.font_family, 9, "bold"),
            "input": (self.font_family, 12, "bold"),
        }

        self.controller = AkoController(log_fn=self._append_log)
        self.loading_overlay: LoadingOverlay | None = None

        self._placeholder_text = "메시지 입력..."
        self._placeholder_active = True

        self._stream_bubble: ChatBubble | None = None
        self._stream_text = ""
        self._bubble_widgets: list[ChatBubble] = []

        self._build_ui()
        self._refresh_ui()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.deiconify()
        self.update_idletasks()
        self._start_loading_overlay()

    def _start_loading_overlay(self):
        self.loading_overlay = LoadingOverlay(
            self,
            on_done=self._finish_loading_overlay,
            frames_dir=str(Path("assets") / "loading" / "frames"),
            fps=24,
            max_duration_ms=5000,
            max_frames=180,
        )

    def _finish_loading_overlay(self):
        self.loading_overlay = None

    def _build_ui(self):
        root = tk.Frame(self, bg=self.colors["bg"], padx=18, pady=16)
        root.pack(fill="both", expand=True)

        root.grid_columnconfigure(0, weight=1)
        root.grid_rowconfigure(0, weight=0)
        root.grid_rowconfigure(1, weight=1)
        root.grid_rowconfigure(2, weight=0)

        header = tk.Frame(root, bg=self.colors["bg"])
        header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        header.grid_columnconfigure(0, weight=1)

        header_left = tk.Frame(header, bg=self.colors["bg"])
        header_left.grid(row=0, column=0, sticky="w")

        tk.Label(
            header_left,
            text="AKO",
            bg=self.colors["bg"],
            fg=self.colors["accent"],
            font=self.fonts["logo"],
        ).pack(anchor="w")

        right = tk.Frame(header, bg=self.colors["bg"])
        right.grid(row=0, column=1, sticky="e")

        self.power_btn = tk.Button(
            right,
            text="전원 켜기",
            command=self._toggle_power,
            bg="#ab8dff",
            fg="#0b0b14",
            activebackground="#bea7ff",
            activeforeground="#0b0b14",
            relief="flat",
            bd=0,
            padx=16,
            pady=9,
            font=self.fonts["button"],
            cursor="hand2",
            highlightthickness=0,
        )
        self.power_btn.pack(side="left", padx=(0, 10))

        self.mic_btn = tk.Button(
            right,
            text="마이크 켜기",
            command=self._toggle_mic,
            bg="#1b1f34",
            fg="#f3f4ff",
            activebackground="#2a3050",
            activeforeground="#f3f4ff",
            disabledforeground="#5f657e",
            relief="flat",
            bd=0,
            padx=16,
            pady=9,
            font=self.fonts["button"],
            cursor="hand2",
            highlightthickness=0,
        )
        self.mic_btn.pack(side="left", padx=(0, 10))

        self.mic_chip = tk.Label(
            right,
            text="마이크 꺼짐",
            bg=self.colors["chip_off_bg"],
            fg=self.colors["chip_off_fg"],
            font=self.fonts["button"],
            padx=16,
            pady=8,
        )
        self.mic_chip.pack(side="left", padx=(0, 10))

        self.power_chip = tk.Label(
            right,
            text="꺼짐",
            bg=self.colors["chip_off_bg"],
            fg=self.colors["chip_off_fg"],
            font=self.fonts["button"],
            padx=16,
            pady=8,
        )
        self.power_chip.pack(side="left")

        main_panel = tk.Frame(
            root,
            bg=self.colors["panel"],
            highlightbackground=self.colors["border"],
            highlightthickness=1,
            bd=0,
            padx=14,
            pady=14,
        )
        main_panel.grid(row=1, column=0, sticky="nsew")
        main_panel.grid_columnconfigure(0, weight=1)
        main_panel.grid_rowconfigure(0, weight=0)
        main_panel.grid_rowconfigure(1, weight=1)

        top_info = tk.Frame(main_panel, bg=self.colors["panel"])
        top_info.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        top_info.grid_columnconfigure(0, weight=1)

        self.status_line = tk.Label(
            top_info,
            text="전원 꺼짐",
            bg=self.colors["panel"],
            fg=self.colors["muted"],
            font=self.fonts["status"],
            anchor="w",
        )
        self.status_line.grid(row=0, column=0, sticky="w")

        self.clear_btn = tk.Button(
            top_info,
            text="대화 지우기",
            command=self._clear_log,
            bg="#1b1f34",
            fg="#f3f4ff",
            activebackground="#2a3050",
            activeforeground="#f3f4ff",
            relief="flat",
            bd=0,
            padx=14,
            pady=8,
            font=self.fonts["button"],
            cursor="hand2",
            highlightthickness=0,
        )
        self.clear_btn.grid(row=0, column=1, sticky="e")

        chat_wrap = tk.Frame(main_panel, bg=self.colors["chat_bg"], bd=0)
        chat_wrap.grid(row=1, column=0, sticky="nsew")
        chat_wrap.grid_columnconfigure(0, weight=1)
        chat_wrap.grid_rowconfigure(0, weight=1)

        self.chat_canvas = tk.Canvas(
            chat_wrap,
            bg=self.colors["chat_bg"],
            bd=0,
            highlightthickness=0,
        )
        self.chat_canvas.grid(row=0, column=0, sticky="nsew")

        self.chat_scrollbar = tk.Scrollbar(
            chat_wrap,
            orient="vertical",
            command=self.chat_canvas.yview,
        )
        self.chat_scrollbar.grid(row=0, column=1, sticky="ns")
        self.chat_canvas.configure(yscrollcommand=self.chat_scrollbar.set)

        self.chat_frame = tk.Frame(self.chat_canvas, bg=self.colors["chat_bg"])
        self.chat_window = self.chat_canvas.create_window(
            (0, 0),
            window=self.chat_frame,
            anchor="nw",
        )

        self.chat_frame.bind("<Configure>", self._on_chat_frame_configure)
        self.chat_canvas.bind("<Configure>", self._on_chat_canvas_configure)
        self.chat_canvas.bind("<MouseWheel>", self._on_mousewheel)
        self.chat_frame.bind("<MouseWheel>", self._on_mousewheel)

        bottom = tk.Frame(root, bg=self.colors["bg"])
        bottom.grid(row=2, column=0, sticky="ew", pady=(14, 0))
        bottom.grid_columnconfigure(0, weight=1)

        input_shell = tk.Frame(
            bottom,
            bg=self.colors["entry_bg"],
            highlightbackground="#3a3d49",
            highlightthickness=1,
            bd=0,
            padx=14,
            pady=8,
        )
        input_shell.grid(row=0, column=0, sticky="ew")
        input_shell.grid_columnconfigure(0, weight=1)

        self.msg_entry = tk.Entry(
            input_shell,
            bg=self.colors["entry_bg"],
            fg=self.colors["muted"],
            insertbackground=self.colors["text"],
            relief="flat",
            bd=0,
            font=self.fonts["input"],
        )
        self.msg_entry.grid(row=0, column=0, sticky="ew", padx=(2, 10), ipady=8)
        self.msg_entry.bind("<Return>", lambda e: self._send_message())
        self.msg_entry.bind("<FocusIn>", self._on_entry_focus_in)
        self.msg_entry.bind("<FocusOut>", self._on_entry_focus_out)

        self._set_placeholder()

        self.send_btn = RoundIconButton(
            input_shell,
            command=self._send_message,
            size=42,
            bg="#f2f4ff",
            fg="#0c0f1a",
            hover_bg="#ffffff",
            disabled_bg="#666b88",
        )
        self.send_btn.grid(row=0, column=1, sticky="e")

    # ── DM 채팅 UI ────────────────────────────────────────────────────────

    def _on_chat_frame_configure(self, _event=None):
        self.chat_canvas.configure(scrollregion=self.chat_canvas.bbox("all"))

    def _on_chat_canvas_configure(self, event):
        self.chat_canvas.itemconfigure(self.chat_window, width=event.width)
        self._update_bubble_widths()

    def _on_mousewheel(self, event):
        self.chat_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _scroll_to_bottom(self):
        self.update_idletasks()
        self.chat_canvas.configure(scrollregion=self.chat_canvas.bbox("all"))
        self.chat_canvas.yview_moveto(1.0)

    def _bubble_max_width(self) -> int:
        width = max(480, self.chat_canvas.winfo_width())
        return max(260, int(width * 0.62))

    def _update_bubble_widths(self):
        max_width = self._bubble_max_width()
        for bubble in self._bubble_widgets:
            try:
                bubble.set_max_width(max_width)
            except Exception:
                pass

    def _add_message(self, role: str, text: str) -> ChatBubble:
        row = tk.Frame(self.chat_frame, bg=self.colors["chat_bg"])
        row.pack(fill="x", padx=14, pady=(8, 3))

        bubble = ChatBubble(
            row,
            role=role,
            text=text,
            colors=self.colors,
            max_width=self._bubble_max_width(),
            font=self.fonts["bubble"],
            name_font=self.fonts["bubble_name"],
        )
        self._bubble_widgets.append(bubble)

        if role == "user":
            bubble.pack(anchor="e")
        elif role == "assistant":
            bubble.pack(anchor="w")
        else:
            bubble.pack(anchor="center", pady=(2, 2))

        self._scroll_to_bottom()
        return bubble

    def _append_log(self, line: str):
        """컨트롤러 로그를 DM 스타일 메시지로 변환."""
        if threading.get_ident() != self._ui_thread_id:
            self.after(0, self._append_log, line)
            return

        raw = (line or "").strip()
        if not raw:
            return

        content = raw
        if content.startswith("[") and "] " in content:
            content = content.split("] ", 1)[1].strip()

        if content.startswith("[나]"):
            self._add_message("user", content.replace("[나]", "", 1).strip())
        elif content.startswith("[Ako]"):
            self._add_message("assistant", content.replace("[Ako]", "", 1).strip())
        elif content.startswith("[아코]"):
            self._add_message("assistant", content.replace("[아코]", "", 1).strip())
        else:
            self._add_message("system", content)

        if "음성 인식 ON" in content or "음성 인식 OFF" in content or "음성 인식 시작 실패" in content:
            self._refresh_ui()

    def _clear_log(self):
        for child in self.chat_frame.winfo_children():
            child.destroy()
        self._bubble_widgets.clear()
        self._stream_bubble = None
        self._stream_text = ""
        if hasattr(self.controller, "clear_chat_history"):
            self.controller.clear_chat_history()

    # ── 입력창 ───────────────────────────────────────────────────────────

    def _set_placeholder(self):
        self.msg_entry.delete(0, "end")
        self.msg_entry.insert(0, self._placeholder_text)
        self.msg_entry.configure(fg=self.colors["muted"])
        self._placeholder_active = True

    def _clear_placeholder(self):
        if self._placeholder_active:
            self.msg_entry.delete(0, "end")
            self.msg_entry.configure(fg=self.colors["entry_fg"])
            self._placeholder_active = False

    def _on_entry_focus_in(self, _event):
        self._clear_placeholder()

    def _on_entry_focus_out(self, _event):
        if not self.msg_entry.get().strip():
            self._set_placeholder()

    def _toggle_power(self):
        self.controller.toggle_power()
        self._refresh_ui()

    def _make_voice_config(self) -> VoiceConfig:
        return VoiceConfig(
            device=None,
            samplerate=16000,
            model=os.getenv("AKO_WHISPER_MODEL", "small"),
            language="ko",
            wake_word=os.getenv("AKO_WAKE_WORD", ""),
        )

    def _toggle_mic(self):
        if not self.controller.powered_on:
            self.status_line.configure(text="전원을 먼저 켜주세요")
            return

        if self.controller.voice_on:
            self.controller.stop_voice()
            self.status_line.configure(text="마이크 꺼짐")
            self._refresh_ui()
            return

        self.status_line.configure(text="마이크 준비 중...")
        self.mic_btn.configure(state="disabled", cursor="arrow")
        self.controller.start_voice(
            cfg=self._make_voice_config(),
            on_text=self._on_voice_text_from_thread,
            on_error=self._on_voice_error_from_thread,
        )
        self._refresh_ui()

    def _on_voice_text_from_thread(self, text: str):
        self.after(0, self._handle_voice_text, text)

    def _handle_voice_text(self, text: str):
        text = (text or "").strip()
        if not text or not self.controller.powered_on:
            return

        self._add_message("user", text)
        self.status_line.configure(text="음성 입력 처리 중...")
        self._handle_message(text)

    def _on_voice_error_from_thread(self, error: str):
        self.after(0, self._handle_voice_error, error)

    def _handle_voice_error(self, error: str):
        msg = (error or "알 수 없는 마이크 오류").strip()
        self._add_message("system", f"마이크 오류: {msg}")
        self.status_line.configure(text=f"마이크 오류: {msg}")
        self._refresh_ui()

    def _send_message(self):
        text = self.msg_entry.get().strip()
        if self._placeholder_active or not text:
            return

        self.msg_entry.delete(0, "end")
        self.msg_entry.configure(fg=self.colors["entry_fg"])
        self._placeholder_active = False

        self._add_message("user", text)
        self.status_line.configure(text="메시지 처리 중...")

        self._handle_message(text)
        self.msg_entry.focus_set()

    def _handle_message(self, text: str):
        """명령어는 즉시 처리, 일반 대화는 논블로킹 스트리밍으로 처리."""
        try:
            handled = False
            if hasattr(self.controller, "is_command_text"):
                try:
                    handled = bool(self.controller.is_command_text(text))
                except Exception:
                    handled = False
            else:
                command_hints = [
                    "열어", "켜", "꺼", "실행", "재생", "눌러", "클릭", "검색",
                    "닫아", "삭제", "입력", "가줘", "가자", "해줘",
                ]
                handled = any(k in text for k in command_hints)

            if handled:
                self.controller.handle_text_command(text)
                self.status_line.configure(text="명령 실행 완료")
                return

            self._start_chat_async(text)

        except Exception as e:
            self._add_message("system", f"오류: {e}")
            self.status_line.configure(text="오류 발생")
            self._set_input_enabled(True)

    # ── 스트리밍 대화 관련 메서드 ──────────────────────────────────────────

    def _set_input_enabled(self, enabled: bool):
        """입력창·전송 버튼 활성/비활성 토글."""
        if enabled and self.controller.powered_on:
            self.msg_entry.configure(state="normal")
            self.send_btn.configure_state(True)
            if self._placeholder_active:
                self.msg_entry.configure(fg=self.colors["muted"])
            else:
                self.msg_entry.configure(fg=self.colors["entry_fg"])
        else:
            self.msg_entry.configure(state="disabled")
            self.send_btn.configure_state(False)

    def _start_chat_async(self, text: str):
        """별도 스레드에서 스트리밍 chat을 시작하고 GUI는 즉시 반환."""
        self.status_line.configure(text="[아코] 생각 중...")
        self._set_input_enabled(False)

        self._stream_text = ""
        self._stream_bubble = self._add_message("assistant", "")

        threading.Thread(
            target=self._chat_stream_worker,
            args=(text,),
            daemon=True,
            name="AkoChatStream",
        ).start()

    def _chat_stream_worker(self, text: str):
        """백그라운드 스레드: 토큰을 받을 때마다 GUI 업데이트 예약."""
        try:
            for chunk in self.controller.chat_stream(text):
                self.after(0, self._on_stream_chunk, chunk)
        except Exception as e:
            self.after(0, self._on_stream_error, str(e))
        finally:
            self.after(0, self._on_stream_done)

    def _on_stream_chunk(self, chunk: str):
        """메인 스레드: 아코 말풍선을 실시간 업데이트."""
        if self._stream_bubble is None:
            self._stream_bubble = self._add_message("assistant", "")

        self._stream_text += chunk
        self._stream_bubble.set_text(self._stream_text)
        self._scroll_to_bottom()

    def _on_stream_done(self):
        """메인 스레드: 스트리밍 완료 후 정리."""
        if self._stream_bubble is not None and not self._stream_text.strip():
            self._stream_bubble.set_text("응답이 비어 있어요.")

        self._stream_bubble = None
        self._stream_text = ""

        self.status_line.configure(text="대기 중")
        self._set_input_enabled(True)
        self.msg_entry.focus_set()

    def _on_stream_error(self, error: str):
        """메인 스레드: 스트림 중 에러 처리."""
        self._add_message("system", f"오류: {error}")
        self.status_line.configure(text="오류 발생")
        self._set_input_enabled(True)

    def _refresh_ui(self):
        on = self.controller.powered_on
        mic_on = self.controller.voice_on
        mic_starting = bool(getattr(self.controller, "_voice_starting", False))

        self.power_btn.configure(text="전원 끄기" if on else "전원 켜기")
        self.mic_btn.configure(
            text="마이크 끄기" if mic_on else "마이크 켜기",
            state=("normal" if on and not mic_starting else "disabled"),
            cursor=("hand2" if on and not mic_starting else "arrow"),
        )

        if not on:
            self.power_chip.configure(
                text="꺼짐",
                bg=self.colors["chip_off_bg"],
                fg=self.colors["chip_off_fg"],
            )
            self.mic_chip.configure(
                text="마이크 꺼짐",
                bg=self.colors["chip_off_bg"],
                fg=self.colors["chip_off_fg"],
            )
            self.msg_entry.configure(state="disabled")
            self.send_btn.configure_state(False)
            self.status_line.configure(text="전원 꺼짐")
        else:
            self.power_chip.configure(
                text="켜짐",
                bg=self.colors["chip_on_bg"],
                fg=self.colors["chip_on_fg"],
            )
            if mic_on:
                self.mic_chip.configure(
                    text="마이크 켜짐",
                    bg=self.colors["chip_on_bg"],
                    fg=self.colors["chip_on_fg"],
                )
            elif mic_starting:
                self.mic_chip.configure(
                    text="마이크 준비 중",
                    bg=self.colors["accent_soft"],
                    fg=self.colors["accent"],
                )
            else:
                self.mic_chip.configure(
                    text="마이크 꺼짐",
                    bg=self.colors["chip_off_bg"],
                    fg=self.colors["chip_off_fg"],
                )
            self.msg_entry.configure(state="normal")
            self.send_btn.configure_state(True)

            if self._placeholder_active:
                self.msg_entry.configure(fg=self.colors["muted"])
            else:
                self.msg_entry.configure(fg=self.colors["entry_fg"])

            if self.status_line.cget("text") in ("전원 꺼짐", "대기 중"):
                self.status_line.configure(text="대기 중")

    def _on_close(self):
        try:
            self.controller.stop_voice()
            self.controller.power_off()
        except Exception:
            pass
        self.destroy()


def main():
    app = AkoGUI()
    app.mainloop()


if __name__ == "__main__":
    main()

