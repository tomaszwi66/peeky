#!/usr/bin/env python3
"""
Peeky. See. Think. Help.

A privacy-first desktop sidekick that watches what you point it at,
listens to what you ask, and answers out loud.

Modes: voice, screen capture, camera, clipboard, text input, Video Coach.
Works fully offline once the local models are installed.
"""

import threading, wave, tempfile, os, sys, asyncio, time, socket
import base64, io, re, subprocess, unicodedata, logging, ctypes, json

# === DPI awareness ===========================================================
# MUST run BEFORE tkinter is imported, otherwise screen-capture coordinates
# get mismatched on monitors with non-100% display scaling. tkinter would
# report logical coords while ImageGrab expects physical coords, so the
# captured region drifts away from the rectangle the user actually drew.
def _enable_dpi_awareness():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # per-monitor v2
        return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()        # system DPI aware
    except (AttributeError, OSError):
        pass
_enable_dpi_awareness()

import tkinter as tk
from tkinter import scrolledtext

# === Logging ===
HERE        = os.path.dirname(os.path.abspath(__file__))
LOG_FILE    = os.path.join(HERE, "peeky.log")
MEMORY_FILE = os.path.join(HERE, "memory.json")
COACH_FILE  = os.path.join(HERE, "coach_state.json")
ICON_FILE   = os.path.join(HERE, "peeky_icon.ico")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("peeky")

# === Optional dependencies ===
try:
    from imageio_ffmpeg import get_ffmpeg_exe
    FFMPEG = get_ffmpeg_exe()
except Exception as e:
    log.error("imageio-ffmpeg missing: %s", e); sys.exit(1)
try:
    import speech_recognition as sr
except Exception as e:
    log.error("speech_recognition missing: %s", e); sys.exit(1)
try:
    import ollama
except Exception as e:
    log.error("ollama missing: %s", e); sys.exit(1)
try:
    import edge_tts
except Exception as e:
    log.error("edge_tts missing: %s", e); sys.exit(1)
try:
    import pygame; pygame.mixer.init()
except Exception as e:
    log.error("pygame missing: %s", e); sys.exit(1)
try:
    from PIL import ImageGrab, Image, ImageTk
except Exception as e:
    log.error("Pillow missing: %s", e); sys.exit(1)
try:
    import cv2; HAS_CV2 = True
except Exception:
    HAS_CV2 = False
try:
    import pyperclip; HAS_PYPERCLIP = True
except Exception:
    HAS_PYPERCLIP = False
try:
    from faster_whisper import WhisperModel as FasterWhisper
    HAS_FASTER_WHISPER = True
except Exception:
    HAS_FASTER_WHISPER = False

# === Theme ===
BG       = "#f0f4f8"
SURFACE  = "#ffffff"
BORDER   = "#d1dce8"
TEXT_HI  = "#1a202c"
TEXT_LO  = "#718096"
BUBBLE_W = 420

ACCENT = {
    "idle":      "#3b82f6",
    "recording": "#ef4444",
    "selecting": "#8b5cf6",
    "thinking":  "#f59e0b",
    "speaking":  "#10b981",
    "coaching":  "#7c3aed",
    "error":     "#9ca3af",
}
EMOJI_MAP = {
    "idle":      "🤖",
    "recording": "🎤",
    "selecting": "✂️",
    "thinking":  "🤔",
    "speaking":  "💬",
    "coaching":  "🎯",
    "error":     "😞",
}

# === Configuration ===
# Peeky needs a multimodal Ollama model (text + vision).
# Recommended:
#     ollama pull gemma4:e4b      (Gemma multimodal, 4B effective)
# Other compatible options if you prefer:
#     ollama pull qwen2.5vl:3b    (Qwen vision, smaller and faster)
#     ollama pull llava           (LLaVA, classic vision model)
#
# At startup Peeky picks the first one that is actually installed locally.
# Override the choice with the PEEKY_MODEL environment variable.
PREFERRED_MODELS = [
    "gemma4:e4b",
    "qwen2.5vl:7b",
    "qwen2.5vl:3b",
    "llava:latest",
    "llava",
    "bakllava",
]

def _resolve_model() -> str:
    """Pick the configured model, or auto-detect a multimodal model."""
    env = os.environ.get("PEEKY_MODEL")
    if env:
        return env
    try:
        installed = [m.get("model", m.get("name", ""))
                     for m in ollama.list().get("models", [])]
        for name in PREFERRED_MODELS:
            if name in installed:
                return name
        for name in PREFERRED_MODELS:
            short = name.split(":")[0]
            for m in installed:
                if m.startswith(short + ":"):
                    return m
    except Exception as e:
        log.warning("Could not list Ollama models: %s", e)
    return PREFERRED_MODELS[0]

OLLAMA_MODEL = _resolve_model()

TTS_VOICE_ON  = "en-US-AriaNeural"   # edge-tts (online)
TTS_VOICE_OFF = "Zira"               # Windows SAPI fallback (offline)
STT_LANGUAGE  = "en-US"

MAX_HISTORY        = 8
MAX_MEMORY         = 200
COACH_DONE_KEYWORD = "TASK_COMPLETE"
COACH_MIN_STEPS    = 2
IMG_MAX_PX         = 768             # downscale before sending to model
OLLAMA_TIMEOUT     = 180              # seconds before giving up on a response

SYSTEM_PROMPT = (
    "You are Peeky, a small desktop sidekick that watches the screen or camera and answers out loud. "
    "Reply in clear, concise English. Speak naturally without greetings or sign-offs. "
    "Never use emoji or special symbols. Plain text only."
)

COACH_SYSTEM = (
    "You are a precise visual coaching assistant. "
    "Reply in clear, concise English. No greetings, no sign-offs, no emoji. "
    f"Signal completion ONLY with the single word: {COACH_DONE_KEYWORD}. "
    "Use it only when there is clear, unambiguous visual evidence the task is fully done."
)


# === Helpers ===
def strip_emoji(text: str) -> str:
    return "".join(
        ch for ch in text
        if unicodedata.category(ch).startswith(("L", "N", "P", "Z", "M"))
        or ch in "\n\r\t "
    ).strip()


def has_internet(timeout: float = 2.0) -> bool:
    """Quick connectivity probe. Completes in at most `timeout` seconds."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(("8.8.8.8", 53))
        s.close()
        return True
    except OSError:
        return False


def round_corners(hwnd):
    """Apply Windows 11 rounded corners via DWM."""
    try:
        dwm = ctypes.windll.dwmapi
        ATTR_CORNER_PREFERENCE, ROUND = 33, 2
        dwm.DwmSetWindowAttribute(
            hwnd, ATTR_CORNER_PREFERENCE,
            ctypes.byref(ctypes.c_int(ROUND)), ctypes.sizeof(ctypes.c_int))
    except Exception:
        pass


# ======================================================================
class BubbleWindow:
    """Floating answer bubble shown next to the agent."""

    def __init__(self, root):
        self.root = root
        self.win = None
        self._hide_job = None
        self._last_answer = ""

    def show(self, text, pet_x, pet_y, pet_w, pet_h, accent=None):
        self._cancel_hide()
        self._last_answer = text
        if self.win:
            try: self.win.destroy()
            except: pass

        bar_color = accent or ACCENT["speaking"]
        self.win  = tk.Toplevel(self.root)
        self.win.overrideredirect(True)
        self.win.wm_attributes("-topmost", True)
        self.win.wm_attributes("-alpha", 0.97)
        self.win.configure(bg=SURFACE)

        tk.Frame(self.win, bg=bar_color, height=4).pack(fill="x")

        inner = tk.Frame(self.win, bg=SURFACE)
        inner.pack(fill="both", expand=True)

        hdr = tk.Frame(inner, bg=SURFACE)
        hdr.pack(fill="x", padx=12, pady=(8, 0))
        tk.Label(hdr, text="Peeky", font=("Segoe UI", 8, "bold"),
                 bg=SURFACE, fg=TEXT_LO).pack(side="left")
        bf = tk.Frame(hdr, bg=SURFACE); bf.pack(side="right")

        def hbtn(t, cmd):
            b = tk.Label(bf, text=t, font=("Segoe UI", 11),
                         bg=SURFACE, fg=TEXT_LO, cursor="hand2", padx=5)
            b.bind("<Button-1>", lambda _: cmd())
            b.bind("<Enter>",    lambda _: b.config(fg=TEXT_HI))
            b.bind("<Leave>",    lambda _: b.config(fg=TEXT_LO))
            b.pack(side="left")
        hbtn("⎘", self._copy)
        hbtn("✕", self.hide)

        tk.Frame(inner, bg=BORDER, height=1).pack(fill="x", pady=(6, 0))

        lines = min(max(3, len(text)//55 + text.count("\n") + 1), 15)
        txt = scrolledtext.ScrolledText(
            inner, font=("Segoe UI", 10), bg=SURFACE, fg=TEXT_HI,
            wrap="word", relief="flat", bd=0,
            padx=14, pady=10, height=lines, width=46,
        )
        txt.pack(fill="both", expand=True)
        txt.insert("end", text)
        txt.config(state="disabled")

        self.win.update_idletasks()
        bw, bh = BUBBLE_W, self.win.winfo_reqheight()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()

        x = pet_x - bw - 14
        if x < 10:           x = pet_x + pet_w + 14
        if x + bw > sw - 10: x = sw - bw - 10
        y = pet_y
        if y + bh > sh - 10: y = sh - bh - 10
        if y < 10:           y = 10

        self.win.geometry(f"{bw}x{bh}+{x}+{y}")
        self.win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(self.win.winfo_id())
        round_corners(hwnd)

    def tts_done(self):
        self._hide_job = self.root.after(10_000, self.hide)

    def hide(self):
        self._cancel_hide()
        if self.win:
            try: self.win.destroy()
            except: pass
            self.win = None

    def _cancel_hide(self):
        if self._hide_job:
            try: self.root.after_cancel(self._hide_job)
            except: pass
            self._hide_job = None

    def _copy(self):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(self._last_answer)
        except: pass


# ======================================================================
class TextInputDialog:
    """Modal dialog for typing a question."""

    @staticmethod
    def ask(root):
        result = {"text": None}
        dlg = tk.Toplevel(root)
        dlg.title("Type your question")
        dlg.configure(bg=BG)
        dlg.wm_attributes("-topmost", True)
        dlg.wm_attributes("-alpha", 0.97)
        dlg.grab_set(); dlg.resizable(False, False)

        tk.Label(dlg, text="Type your question:", font=("Segoe UI", 10),
                 bg=BG, fg=TEXT_LO).pack(anchor="w", padx=14, pady=(12, 3))
        entry = tk.Text(dlg, font=("Segoe UI", 10), width=44, height=4,
                        bg=SURFACE, fg=TEXT_HI, insertbackground=ACCENT["idle"],
                        wrap="word", relief="flat", bd=0, padx=10, pady=8)
        entry.pack(padx=12, pady=(0, 4)); entry.focus_set()

        def ok(_=None):
            result["text"] = entry.get("1.0", "end").strip() or None
            dlg.destroy()
        def cancel(_=None): dlg.destroy()
        entry.bind("<Control-Return>", ok)
        entry.bind("<Escape>", cancel)

        bf = tk.Frame(dlg, bg=BG); bf.pack(pady=(4, 12))

        def btn(txt, cmd, primary=False):
            bg = ACCENT["idle"] if primary else SURFACE
            fg = "white" if primary else TEXT_HI
            b = tk.Button(bf, text=txt, command=cmd, font=("Segoe UI", 9),
                          padx=12, pady=5, bg=bg, fg=fg, relief="flat",
                          activebackground=BORDER, cursor="hand2", bd=0)
            b.pack(side="left", padx=4)

        btn("Send  (Ctrl+Enter)", ok, True)
        btn("Cancel", cancel)

        w, h = 400, 188
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        dlg.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")
        root.wait_window(dlg)
        return result["text"]


# ======================================================================
class ScreenSelector:
    """Fullscreen overlay for selecting a rectangular region."""

    def __init__(self, root, on_done):
        self.on_done = on_done
        self.sx = self.sy = 0
        self.rect_id = self.dim_id = None

        self.win = tk.Toplevel(root)
        self.win.attributes("-fullscreen", True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-alpha", 0.35)
        self.win.configure(bg="#000814")

        cv = tk.Canvas(self.win, cursor="cross", bg="#000814", highlightthickness=0)
        cv.pack(fill="both", expand=True)
        cv.create_text(self.win.winfo_screenwidth() // 2, 34,
                       text="Drag to select an area.  ESC to cancel.",
                       font=("Segoe UI", 14, "bold"), fill="#7d8590")
        self.cv = cv
        cv.bind("<ButtonPress-1>",   self._press)
        cv.bind("<B1-Motion>",        self._drag)
        cv.bind("<ButtonRelease-1>",  self._release)
        self.win.bind("<Escape>", lambda _: self._cancel())

    def _press(self, e):
        self.sx, self.sy = e.x, e.y
        for t in (self.rect_id, self.dim_id):
            if t: self.cv.delete(t)

    def _drag(self, e):
        for t in (self.rect_id, self.dim_id):
            if t: self.cv.delete(t)
        self.rect_id = self.cv.create_rectangle(
            self.sx, self.sy, e.x, e.y,
            outline="#388bfd", width=2, dash=(6, 3))
        w, h = abs(e.x - self.sx), abs(e.y - self.sy)
        self.dim_id = self.cv.create_text(
            (self.sx + e.x) // 2, min(self.sy, e.y) - 14,
            text=f"{w} x {h} px",
            font=("Segoe UI", 9, "bold"), fill="#388bfd")

    def _release(self, e):
        x1, x2 = sorted([self.sx, e.x])
        y1, y2 = sorted([self.sy, e.y])
        self.win.destroy()
        if x2 - x1 > 10 and y2 - y1 > 10:
            self.on_done(x1, y1, x2, y2)
        else:
            self.on_done(None, None, None, None)

    def _cancel(self):
        self.win.destroy()
        self.on_done(None, None, None, None)


# ======================================================================
def _system_dpi_scale() -> float:
    """Display scale factor (1.0 = 100%, 1.5 = 150%, etc.)."""
    try:
        hdc = ctypes.windll.user32.GetDC(0)
        dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)  # LOGPIXELSX
        ctypes.windll.user32.ReleaseDC(0, hdc)
        return max(1.0, dpi / 96.0)
    except Exception:
        return 1.0


class PeekyAgent:
    """Main desktop agent window."""

    BASE_W = 160
    BASE_H = 258
    BASE_BUBBLE_W = 420

    def __init__(self):
        # Compute DPI scale once and apply it consistently across the UI.
        # tkinter without explicit scaling renders at physical pixels when
        # the process is DPI-aware, which makes everything tiny on hi-res
        # displays. We multiply window/bubble sizes and ask tk to scale
        # font point sizes accordingly.
        self._scale = _system_dpi_scale()
        self.WIN_W = int(self.BASE_W * self._scale)
        self.WIN_H = int(self.BASE_H * self._scale)

        global BUBBLE_W
        BUBBLE_W = int(self.BASE_BUBBLE_W * self._scale)

        self.root = tk.Tk()
        # tk's default scaling is 1.333 for 96 DPI. Adjust proportionally.
        self.root.tk.call("tk", "scaling", self._scale * 1.333)
        self.root.title("Peeky")
        if os.path.exists(ICON_FILE):
            try:
                self.root.iconbitmap(default=ICON_FILE)
            except Exception as e:
                log.warning("iconbitmap: %s", e)
        self.root.overrideredirect(True)
        self.root.wm_attributes("-topmost", True)
        self.root.wm_attributes("-alpha", 0.96)
        self.root.configure(bg=BG)

        self.state = "idle"
        self.history = []

        # Recording / capture
        self.ffmpeg_proc      = None
        self.tmp_wav          = None
        self._pending_img_b64 = None
        self._pending_mode    = "voice"

        # Drag detection
        self._drag_x = self._drag_y = 0
        self._drag_moved = False

        # Video Coach
        self._coaching            = False
        self._coaching_task       = ""
        self._coaching_step       = 0
        self._coaching_history    = []
        self._coaching_img_b64    = None
        self._coaching_completion = ""

        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.root.geometry(
            f"{self.WIN_W}x{self.WIN_H}+{sw - self.WIN_W - 16}+{sh - self.WIN_H - 48}")

        self.recognizer = sr.Recognizer()
        self.local_stt  = None
        self.audio_device_name = self._find_audio_device()

        self._build_ui()
        self.bubble = BubbleWindow(self.root)
        self._build_menu()

        self.root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
        self.root.after(80, lambda: round_corners(hwnd))

        self.root.bind("<Escape>", self._on_escape)

        threading.Thread(target=self._load_local_stt, daemon=True).start()
        threading.Thread(target=self._test_ollama,    daemon=True).start()

        self.root.protocol("WM_DELETE_WINDOW", self._quit)
        log.info("Peeky started. Model: %s", OLLAMA_MODEL)
        self.root.mainloop()

    # === UI ===

    def _build_ui(self):
        self._card = tk.Frame(self.root, bg=SURFACE, bd=0,
                              highlightbackground=BORDER, highlightthickness=1)
        self._card.pack(fill="both", expand=True, padx=6, pady=6)

        self._accent_bar = tk.Frame(self._card, bg=ACCENT["idle"], height=4)
        self._accent_bar.pack(fill="x")

        self._emoji_lbl = tk.Label(
            self._card, text=EMOJI_MAP["idle"],
            font=("Segoe UI Emoji", 54), bg=SURFACE, cursor="hand2")
        self._emoji_lbl.pack(pady=(10, 2))

        tk.Frame(self._card, bg=BORDER, height=1).pack(fill="x", padx=12, pady=(6, 0))

        modes = [
            ("🔍", "Screen",    self._screen_click),
            ("📷", "Camera",    self._camera_click),
            ("📋", "Clipboard", self._clipboard_click),
            ("⌨️", "Type",      self._type_click),
        ]
        btn_row = tk.Frame(self._card, bg=SURFACE)
        btn_row.pack(fill="x", padx=4, pady=(8, 4))

        for em, lbl, cmd in modes:
            f = tk.Frame(btn_row, bg=SURFACE, cursor="hand2")
            tk.Label(f, text=em,  font=("Segoe UI Emoji", 15), bg=SURFACE).pack()
            tk.Label(f, text=lbl, font=("Segoe UI", 7), bg=SURFACE, fg=TEXT_LO).pack()
            f.pack(side="left", expand=True)
            for w in [f] + list(f.winfo_children()):
                w.bind("<ButtonPress-1>",   self._on_press)
                w.bind("<B1-Motion>",        self._on_drag)
                w.bind("<ButtonRelease-1>",  lambda e, c=cmd: self._sub_release(e, c))
                w.bind("<Enter>",            lambda e, fr=f: self._btn_hover(fr, True))
                w.bind("<Leave>",            lambda e, fr=f: self._btn_hover(fr, False))

        # Video Coach (full-width row)
        tk.Frame(self._card, bg=BORDER, height=1).pack(fill="x", padx=12, pady=(0, 4))

        self._coach_frame = tk.Frame(self._card, bg=SURFACE, cursor="hand2")
        self._coach_frame.pack(fill="x", padx=4, pady=(0, 2))

        self._coach_icon = tk.Label(
            self._coach_frame, text="🎯", font=("Segoe UI Emoji", 13), bg=SURFACE)
        self._coach_icon.pack(side="left", padx=(6, 4))
        self._coach_lbl = tk.Label(
            self._coach_frame, text="Video Coach",
            font=("Segoe UI", 8, "bold"), bg=SURFACE, fg=ACCENT["coaching"])
        self._coach_lbl.pack(side="left")
        self._coach_stop_lbl = tk.Label(
            self._coach_frame, text="● STOP", font=("Segoe UI", 7),
            bg=SURFACE, fg=ACCENT["error"], cursor="hand2")

        for w in [self._coach_frame, self._coach_icon, self._coach_lbl]:
            w.bind("<ButtonPress-1>",   self._on_press)
            w.bind("<B1-Motion>",        self._on_drag)
            w.bind("<ButtonRelease-1>",  lambda e: self._sub_release(e, self._coach_click))
            w.bind("<Enter>",            lambda e: self._coach_hover(True))
            w.bind("<Leave>",            lambda e: self._coach_hover(False))
        self._coach_stop_lbl.bind("<Button-1>", lambda _: self._coach_stop())

        tk.Frame(self._card, bg=BORDER, height=1).pack(fill="x", padx=12, pady=(4, 0))

        self._status_var = tk.StringVar(value="Ready")
        self._status_lbl = tk.Label(
            self._card, textvariable=self._status_var,
            font=("Segoe UI", 9), bg=SURFACE, fg=TEXT_LO,
            wraplength=self.WIN_W - 20, justify="center")
        self._status_lbl.pack(pady=(6, 10))

        for w in (self._card, self._emoji_lbl, self._status_lbl):
            w.bind("<ButtonPress-1>",   self._on_press)
            w.bind("<B1-Motion>",        self._on_drag)
            w.bind("<ButtonRelease-1>",  self._on_main_release)
            w.bind("<Button-3>",         self._show_menu)

    def _btn_hover(self, frame, on: bool):
        bg = BG if on else SURFACE
        frame.config(bg=bg)
        for ch in frame.winfo_children():
            ch.config(bg=bg)

    def _coach_hover(self, on: bool):
        if not self._coaching:
            bg = BG if on else SURFACE
            self._coach_frame.config(bg=bg)
            self._coach_icon.config(bg=bg)
            self._coach_lbl.config(bg=bg)

    def _update_coach_btn(self):
        if self._coaching:
            bg = "#f3e8ff"
            self._coach_frame.config(bg=bg)
            self._coach_icon.config(bg=bg)
            self._coach_lbl.config(bg=bg, text="Video Coach (active)")
            self._coach_stop_lbl.pack(side="right", padx=6)
        else:
            self._coach_frame.config(bg=SURFACE)
            self._coach_icon.config(bg=SURFACE)
            self._coach_lbl.config(bg=SURFACE, text="Video Coach")
            self._coach_stop_lbl.pack_forget()

    def _set_state(self, name: str, status: str = ""):
        labels = {
            "idle":      "Ready",
            "recording": "Listening. Click to stop.",
            "selecting": "Select an area",
            "thinking":  "Thinking",
            "speaking":  "Speaking",
            "coaching":  "Video Coach active",
            "error":     "Error",
        }
        txt = status or labels.get(name, name)
        acc = ACCENT.get(name, ACCENT["idle"])

        def upd():
            self.state = name
            self._emoji_lbl.config(text=EMOJI_MAP.get(name, "🤖"))
            self._accent_bar.config(bg=acc)
            self._status_var.set(txt)
            self._status_lbl.config(fg=acc if name != "idle" else TEXT_LO)
            self._update_coach_btn()

        self.root.after(0, upd)

    def _set_status(self, text: str):
        self.root.after(0, lambda: self._status_var.set(text))

    # === Drag and click ===

    def _on_press(self, event):
        self._drag_x = event.x_root
        self._drag_y = event.y_root
        self._drag_moved = False

    def _on_drag(self, event):
        dx = event.x_root - self._drag_x
        dy = event.y_root - self._drag_y
        if abs(dx) > 4 or abs(dy) > 4:
            self._drag_moved = True
        self.root.geometry(
            f"+{self.root.winfo_x() + dx}+{self.root.winfo_y() + dy}")
        self._drag_x = event.x_root
        self._drag_y = event.y_root

    def _on_main_release(self, _):
        if self._drag_moved: return
        if self.state == "idle":
            self._pending_img_b64 = None
            self._pending_mode    = "voice"
            self._set_state("recording")
            self._start_ffmpeg()
        elif self.state == "recording":
            self._stop_recording()
        elif self.state == "error":
            self._set_state("idle")

    def _sub_release(self, _, cmd):
        if not self._drag_moved:
            cmd()

    def _on_escape(self, _=None):
        if self._coaching:
            self._coach_stop()
        elif self.state == "recording":
            self._stop_recording()

    # === Right-click menu ===

    def _build_menu(self):
        self.menu = tk.Menu(self.root, tearoff=0, font=("Segoe UI", 9),
                            bg=SURFACE, fg=TEXT_HI,
                            activebackground=BG, activeforeground=TEXT_HI,
                            relief="flat", bd=1)
        self.menu.add_command(label="⎘  Copy reply",    command=self._copy_last)
        self.menu.add_command(label="📖  History",       command=self._show_history)
        self.menu.add_command(label="🗑  Clear context", command=self._clear_history)
        self.menu.add_separator()
        self.menu.add_command(label="📄  Open log",      command=lambda: os.startfile(LOG_FILE))
        self.menu.add_separator()
        self.menu.add_command(label="✕  Quit",           command=self._quit)

    def _show_menu(self, e):
        self.menu.tk_popup(e.x_root, e.y_root)

    def _copy_last(self):
        if self.bubble._last_answer:
            try:
                self.root.clipboard_clear()
                self.root.clipboard_append(self.bubble._last_answer)
                self._set_status("Copied")
                self.root.after(2000, lambda: self._set_state("idle"))
            except: pass

    def _clear_history(self):
        self.history.clear()
        self._set_status("Context cleared")
        self.root.after(2000, lambda: self._set_state("idle"))

    def _quit(self):
        try:
            self._coaching = False
            for path in (COACH_FILE,):
                if os.path.exists(path):
                    os.unlink(path)
            if self.ffmpeg_proc and self.ffmpeg_proc.poll() is None:
                self.ffmpeg_proc.kill()
            pygame.mixer.quit()
        except: pass
        self.root.destroy()

    # === Memory timeline ===

    def _memory_log(self, mode: str, user_text: str, answer: str):
        entry = {
            "ts":      time.strftime("%Y-%m-%d %H:%M"),
            "mode":    mode,
            "user":    user_text[:120],
            "summary": answer[:160],
        }
        try:
            data = []
            if os.path.exists(MEMORY_FILE):
                with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
        except Exception:
            data = []
        data.append(entry)
        data = data[-MAX_MEMORY:]
        try:
            with open(MEMORY_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.warning("memory_log: %s", e)

    def _show_history(self):
        data = []
        try:
            if os.path.exists(MEMORY_FILE):
                with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
        except Exception:
            pass

        win = tk.Toplevel(self.root)
        win.title("History")
        win.configure(bg=BG)
        win.wm_attributes("-topmost", True)
        win.wm_attributes("-alpha", 0.97)
        win.resizable(True, True)

        tk.Frame(win, bg=ACCENT["idle"], height=4).pack(fill="x")

        hdr = tk.Frame(win, bg=BG)
        hdr.pack(fill="x", padx=16, pady=(12, 4))
        tk.Label(hdr, text="Interaction history",
                 font=("Segoe UI", 12, "bold"), bg=BG, fg=TEXT_HI).pack(side="left")
        tk.Label(hdr, text=f"{len(data)} entries",
                 font=("Segoe UI", 9), bg=BG, fg=TEXT_LO).pack(side="right", pady=2)

        outer = tk.Frame(win, bg=BG)
        outer.pack(fill="both", expand=True, padx=12, pady=(0, 4))

        canvas = tk.Canvas(outer, bg=BG, highlightthickness=0)
        scroll = tk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        inner  = tk.Frame(canvas, bg=BG)

        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        win_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)

        def on_resize(e):
            canvas.itemconfig(win_id, width=e.width)
        canvas.bind("<Configure>", on_resize)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        def wheel(e):
            canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", wheel)
        win.bind("<Destroy>", lambda _: canvas.unbind_all("<MouseWheel>"))

        ICONS = {
            "voice": "🎤", "screen": "🔍", "camera": "📷",
            "clipboard": "📋", "text": "⌨️", "coaching": "🎯",
        }

        if not data:
            tk.Label(inner, text="No history yet. Start a conversation.",
                     font=("Segoe UI", 10), bg=BG, fg=TEXT_LO,
                     pady=24).pack()
        else:
            for entry in reversed(data):
                card = tk.Frame(inner, bg=SURFACE, bd=0,
                                highlightbackground=BORDER, highlightthickness=1)
                card.pack(fill="x", pady=3, padx=2)

                icon = ICONS.get(entry.get("mode", ""), "💬")
                ts   = entry.get("ts", "")
                user = entry.get("user", "")
                smry = entry.get("summary", "")

                top = tk.Frame(card, bg=SURFACE)
                top.pack(fill="x", padx=10, pady=(6, 2))
                tk.Label(top, text=f"{icon}  {ts}",
                         font=("Segoe UI", 8), bg=SURFACE, fg=TEXT_LO).pack(side="left")

                if user:
                    u = user[:72] + ("..." if len(user) > 72 else "")
                    tk.Label(card, text=f"> {u}",
                             font=("Segoe UI", 9, "bold"), bg=SURFACE, fg=TEXT_HI,
                             anchor="w", wraplength=370, justify="left",
                             padx=10).pack(fill="x")
                if smry:
                    s = smry[:140] + ("..." if len(smry) > 140 else "")
                    tk.Label(card, text=s,
                             font=("Segoe UI", 9), bg=SURFACE, fg=TEXT_LO,
                             anchor="w", wraplength=370, justify="left",
                             padx=10, pady=(0, 6)).pack(fill="x")

        tf = tk.Frame(win, bg=BG)
        tf.pack(pady=(4, 12))

        def clear_mem():
            try:
                if os.path.exists(MEMORY_FILE):
                    os.unlink(MEMORY_FILE)
            except: pass
            win.destroy()
            self._set_status("Memory cleared")
            self.root.after(2000, lambda: self._set_state("idle"))

        tk.Button(tf, text="Close", command=win.destroy,
                  font=("Segoe UI", 9), bg=ACCENT["idle"], fg="white",
                  relief="flat", padx=14, pady=5, cursor="hand2", bd=0).pack(side="left", padx=4)
        tk.Button(tf, text="Clear memory", command=clear_mem,
                  font=("Segoe UI", 9), bg=SURFACE, fg=ACCENT["error"],
                  relief="flat", padx=14, pady=5, cursor="hand2", bd=0,
                  highlightbackground=BORDER, highlightthickness=1).pack(side="left", padx=4)

        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        w, h = 440, 520
        win.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        round_corners(hwnd)

    # === Mode handlers ===

    def _screen_click(self):
        if self.state == "recording":
            self._stop_recording()
        elif self.state == "idle":
            self._set_state("selecting")
            self.root.after(150, lambda: ScreenSelector(self.root, self._on_region))

    def _camera_click(self):
        if self.state != "idle": return
        if not HAS_CV2:
            self._handle_error("opencv-python is not installed"); return
        threading.Thread(target=self._capture_webcam, daemon=True).start()

    def _clipboard_click(self):
        if self.state != "idle": return
        clip = ""
        try: clip = self.root.clipboard_get().strip()
        except: pass
        if HAS_PYPERCLIP and not clip:
            try: clip = pyperclip.paste().strip()
            except: pass
        if not clip:
            self._set_status("Clipboard is empty")
            self.root.after(2000, lambda: self._set_state("idle"))
            return
        prompt = f"Analyze or explain the following text from the clipboard:\n\n{clip}"
        threading.Thread(
            target=self._ask_ollama,
            args=(prompt, None, "clipboard"), daemon=True).start()

    def _type_click(self):
        if self.state != "idle": return
        text = TextInputDialog.ask(self.root)
        if text:
            threading.Thread(
                target=self._ask_ollama,
                args=(text, None, "text"), daemon=True).start()

    def _on_region(self, x1, y1, x2, y2):
        if x1 is None:
            self._set_state("idle"); return
        try:
            img = ImageGrab.grab(bbox=(x1, y1, x2, y2))
            buf = io.BytesIO(); img.save(buf, format="PNG")
            self._pending_img_b64 = base64.b64encode(buf.getvalue()).decode()
            self._pending_mode    = "screen"
        except Exception as e:
            self._handle_error(f"Screenshot: {e}"); return
        self._set_state("recording", "Ask about the captured area")
        self._start_ffmpeg()

    # === Camera ===

    def _capture_frame(self):
        """Returns (b64_str, pil_image) or (None, None) on failure."""
        try:
            cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
            if not cap.isOpened():
                cap = cv2.VideoCapture(0)
            if not cap.isOpened():
                return None, None
            for _ in range(5):
                cap.read()
            ret, frame = cap.read()
            cap.release()
            if not ret:
                return None, None
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            pil = Image.fromarray(rgb)
            buf = io.BytesIO(); pil.save(buf, format="PNG")
            return base64.b64encode(buf.getvalue()).decode(), pil
        except Exception as e:
            log.exception("capture_frame: %s", e)
            return None, None

    def _capture_webcam(self):
        self._set_state("thinking", "Taking a photo")
        b64, pil = self._capture_frame()
        if not b64:
            self._handle_error("Could not access the webcam"); return
        self._pending_img_b64 = b64
        self._pending_mode    = "camera"
        self.root.after(0, lambda: self._show_cam_preview(pil))
        self._set_state("recording", "Ask about the photo")
        self._start_ffmpeg()

    def _show_cam_preview(self, pil_img):
        try:
            thumb = pil_img.copy(); thumb.thumbnail((200, 150))
            photo = ImageTk.PhotoImage(thumb)
            prev  = tk.Toplevel(self.root)
            prev.overrideredirect(True)
            prev.wm_attributes("-topmost", True)
            prev.wm_attributes("-alpha", 0.93)
            tk.Label(prev, image=photo, bg=BG, bd=1, relief="solid").pack()
            prev.image = photo
            px = self.root.winfo_x() - thumb.width - 14
            if px < 10:
                px = self.root.winfo_x() + self.WIN_W + 14
            prev.geometry(f"+{px}+{self.root.winfo_y()}")
            self.root.after(3000, prev.destroy)
        except Exception as e:
            log.warning("preview: %s", e)

    # === Video Coach ===

    def _coach_click(self):
        # While recording, the Coach button stops the recording (continues coaching)
        if self._coaching and self.state == "recording":
            self._stop_recording(); return
        if self._coaching:
            self._coach_stop(); return
        if self.state != "idle": return
        if not HAS_CV2:
            self._handle_error("opencv-python is not installed"); return
        threading.Thread(target=self._coach_start, daemon=True).start()

    def _coach_start(self):
        """Step 0: snap a baseline photo, record the task description."""
        self._set_state("thinking", "Capturing baseline frame")
        b64, pil = self._capture_frame()
        if not b64:
            self._handle_error("Could not access the webcam"); return

        self._coaching            = True
        self._coaching_task       = ""
        self._coaching_step       = 0
        self._coaching_history    = []
        self._coaching_img_b64    = b64
        self._coaching_completion = ""

        try:
            if os.path.exists(COACH_FILE):
                os.unlink(COACH_FILE)
        except: pass

        self.root.after(0, lambda: self._show_cam_preview(pil))
        self._set_state("coaching", "Describe the task")
        self.root.after(600, lambda: self._set_state("recording", "Describe the task. Click to stop."))
        self.root.after(700, self._start_ffmpeg)

    def _coach_first_step(self):
        """Step 1: parse the task, lock in a completion description."""
        prompt = (
            f"Task: {self._coaching_task}\n\n"
            "You are looking at the BASELINE frame (before any work has started).\n"
            "Reply on exactly two lines:\n"
            "STEP: [one concrete action to take right now]\n"
            "DONE_WHEN: [one short sentence describing what the scene looks like "
            "when the task is fully complete]"
        )

        img_bytes = self._encode_for_ollama(self._coaching_img_b64)
        if not img_bytes:
            self._coaching = False
            self._handle_error("Could not prepare the image"); return

        self._set_state("thinking", "Analyzing task")
        try:
            raw = self._ollama_chat(
                [{"role": "system", "content": COACH_SYSTEM},
                 {"role": "user",   "content": prompt, "images": [img_bytes]}])
        except Exception as e:
            self._coaching = False
            self._handle_error(f"Ollama: {e}"); return

        if not raw:
            self._coaching = False
            self._handle_error("No response from the model"); return

        step_text = ""
        completion = ""
        for line in raw.splitlines():
            ln = line.strip()
            if ln.upper().startswith("STEP:"):
                step_text = ln[5:].strip()
            elif ln.upper().startswith("DONE_WHEN:"):
                completion = ln[10:].strip()
        if not step_text:
            step_text = raw.splitlines()[0].strip() if raw else raw
        if not completion:
            completion = "task fully completed as described"

        self._coaching_completion = completion
        self._coaching_history    = [{"role": "user",      "content": prompt},
                                     {"role": "assistant", "content": raw}]
        self._coaching_step       = 1
        self._memory_log("coaching", self._coaching_task, step_text)
        self._coach_save_state("in_progress")

        self.root.after(0, lambda: self.bubble.show(
            f"[Step 1] {step_text}",
            self.root.winfo_x(), self.root.winfo_y(),
            self.WIN_W, self.WIN_H,
            accent=ACCENT["coaching"]))
        self._set_state("speaking", "Step 1")
        threading.Thread(
            target=self._speak_coach, args=(step_text,), daemon=True).start()

    def _coach_next_step(self, verify: bool = False):
        """Step N: snap a new frame, compare against the baseline."""
        if not self._coaching:
            self._set_state("idle"); return

        self._set_state("thinking",
                        "Verifying completion" if verify else "Analyzing progress")

        b64, _ = self._capture_frame()
        if not b64:
            self._coaching = False
            self._handle_error("Could not access the webcam"); return

        step_num = self._coaching_step + 1

        if verify:
            prompt = (
                f"Task: {self._coaching_task}\n"
                f"Expected end state: {self._coaching_completion}\n\n"
                "You see TWO images:\n"
                "  1. BEFORE (baseline frame)\n"
                "  2. NOW (current frame)\n\n"
                "Compare them carefully. Is the task DEFINITIVELY complete?\n"
                f"Yes (clear visual evidence): {COACH_DONE_KEYWORD}\n"
                "No (something is missing): describe exactly what is still needed."
            )
        else:
            prompt = (
                f"Task: {self._coaching_task}\n"
                f"Step {step_num}. Expected end state: {self._coaching_completion}\n\n"
                "You see TWO images:\n"
                "  1. BEFORE (baseline frame)\n"
                "  2. NOW (current frame)\n\n"
                "Compare them and judge progress.\n"
                f"Use the word {COACH_DONE_KEYWORD} ONLY if there is clear, "
                "unambiguous evidence the task is fully complete. If in any doubt, "
                "give the next concrete step.\n"
                f"Done: {COACH_DONE_KEYWORD} | Not done: one short next step."
            )

        before_bytes = self._encode_for_ollama(self._coaching_img_b64)
        after_bytes  = self._encode_for_ollama(b64)
        if not before_bytes or not after_bytes:
            self._coaching = False
            self._handle_error("Could not prepare images"); return

        ctx = self._coaching_history[-4:] + [
            {"role": "user", "content": prompt,
             "images": [before_bytes, after_bytes]}]

        try:
            answer = self._ollama_chat(
                [{"role": "system", "content": COACH_SYSTEM}] + ctx)
        except Exception as e:
            self._coaching = False
            self._handle_error(f"Ollama: {e}"); return

        if not answer:
            self._coaching = False
            self._handle_error("No response from the model"); return

        # Text-only history (images stay out of the conversation log)
        self._coaching_history.append({"role": "user",      "content": prompt})
        self._coaching_history.append({"role": "assistant", "content": answer})
        self._coaching_step = step_num

        is_done = COACH_DONE_KEYWORD in answer.upper()

        if is_done and step_num < COACH_MIN_STEPS:
            log.info("Coach: completion claim too early (step %d), continuing", step_num)
            is_done = False

        if is_done and not verify:
            log.info("Coach: first completion at step %d, verifying", step_num)
            self._coach_save_state("verifying")
            verify_msg = "Let me double check that."
            self.root.after(0, lambda: self.bubble.show(
                verify_msg,
                self.root.winfo_x(), self.root.winfo_y(),
                self.WIN_W, self.WIN_H,
                accent=ACCENT["coaching"]))
            self._set_state("speaking", "Verifying")
            threading.Thread(
                target=self._speak_coach,
                args=(verify_msg, True), daemon=True).start()

        elif is_done and verify:
            log.info("Coach: completion confirmed at step %d", step_num)
            self._coach_save_state("done")
            done_msg = "Task complete. Nice work."
            self._coaching = False
            self._memory_log("coaching", self._coaching_task, done_msg)
            try: os.unlink(COACH_FILE)
            except: pass
            self.root.after(0, lambda: self.bubble.show(
                done_msg,
                self.root.winfo_x(), self.root.winfo_y(),
                self.WIN_W, self.WIN_H,
                accent=ACCENT["speaking"]))
            self._set_state("speaking")
            threading.Thread(
                target=self._speak, args=(done_msg,), daemon=True).start()

        else:
            self._coach_save_state("in_progress")
            self._memory_log("coaching", f"step {step_num}", answer)
            self.root.after(0, lambda: self.bubble.show(
                f"[Step {step_num}] {answer}",
                self.root.winfo_x(), self.root.winfo_y(),
                self.WIN_W, self.WIN_H,
                accent=ACCENT["coaching"]))
            self._set_state("speaking", f"Step {step_num}")
            threading.Thread(
                target=self._speak_coach,
                args=(answer, False), daemon=True).start()

    def _coach_save_state(self, status: str):
        try:
            data = {
                "task":       self._coaching_task,
                "completion": self._coaching_completion,
                "step":       self._coaching_step,
                "status":     status,
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            with open(COACH_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.warning("coach_save_state: %s", e)

    def _coach_stop(self):
        self._coaching            = False
        self._coaching_task       = ""
        self._coaching_step       = 0
        self._coaching_history    = []
        self._coaching_img_b64    = None
        self._coaching_completion = ""
        try:
            if os.path.exists(COACH_FILE):
                os.unlink(COACH_FILE)
        except: pass
        if self.ffmpeg_proc and self.ffmpeg_proc.poll() is None:
            try:
                self.ffmpeg_proc.stdin.write(b"q")
                self.ffmpeg_proc.stdin.flush()
                self.ffmpeg_proc.stdin.close()
            except: pass
        self._set_state("idle", "Video Coach stopped")
        self.root.after(2000, lambda: self._set_state("idle"))

    def _speak_coach(self, text: str, next_verify: bool = False):
        """Speak a step, then trigger the next analysis if still active."""
        text = strip_emoji(text)
        try:
            if has_internet(timeout=2):
                try:
                    self._speak_online(text)
                except Exception as e:
                    log.warning("edge-tts coach: %s. Falling back to SAPI.", e)
                    try:    self._speak_offline(text)
                    except Exception as e2: log.exception("offline TTS: %s", e2)
            else:
                try:    self._speak_offline(text)
                except Exception as e: log.exception("offline TTS coach: %s", e)
        finally:
            self.root.after(0, self.bubble.tts_done)

        if self._coaching:
            time.sleep(1.5)
            threading.Thread(
                target=self._coach_next_step,
                args=(next_verify,), daemon=True).start()
        else:
            self._set_state("idle")

    # === Audio capture (ffmpeg + dshow) ===

    def _find_audio_device(self):
        try:
            proc = subprocess.run(
                [FFMPEG, "-hide_banner", "-list_devices", "true",
                 "-f", "dshow", "-i", "dummy"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
            raw = re.sub(r"\n\s+", " ", (proc.stderr or "") + (proc.stdout or ""))
            for i, line in enumerate(raw.split("\n")):
                if "(audio)" not in line: continue
                m = re.search(r'"([^"]+)"\s*\(audio\)', line)
                if not m: continue
                rest = "\n".join(raw.split("\n")[i:i + 5])
                alt  = re.search(r'Alternative name\s+"([^"]+)"', rest)
                return alt.group(1) if alt else m.group(1)
        except Exception as e:
            log.exception("find_audio: %s", e)
        return None

    def _start_ffmpeg(self):
        if not self.audio_device_name:
            self._handle_error("No microphone detected"); return
        fd, self.tmp_wav = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try: os.unlink(self.tmp_wav)
        except: pass
        cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
               "-f", "dshow", "-i", f"audio={self.audio_device_name}",
               "-ar", "16000", "-ac", "1", "-acodec", "pcm_s16le",
               self.tmp_wav]
        try:
            self.ffmpeg_proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW)
        except Exception as e:
            self._handle_error(f"ffmpeg: {e}")

    def _stop_recording(self):
        self._set_state("thinking", "Processing")
        if not self.ffmpeg_proc:
            self._set_state("idle"); return
        if self.ffmpeg_proc.poll() is None:
            try:
                self.ffmpeg_proc.stdin.write(b"q")
                self.ffmpeg_proc.stdin.flush()
                self.ffmpeg_proc.stdin.close()
            except: pass
        try:
            _, err = self.ffmpeg_proc.communicate(timeout=5)
            if err:
                log.info("ffmpeg: %s", err.decode("utf-8", "replace")[:200])
        except subprocess.TimeoutExpired:
            self.ffmpeg_proc.kill()
            self.ffmpeg_proc.communicate()
        threading.Thread(target=self._transcribe, daemon=True).start()

    # === Speech to text ===

    def _load_local_stt(self):
        if not HAS_FASTER_WHISPER:
            log.warning("faster-whisper is not installed. Offline STT disabled.")
            return
        try:
            log.info("Loading faster-whisper base model")
            self.local_stt = FasterWhisper("base", device="cpu", compute_type="int8")
            log.info("faster-whisper ready")
        except Exception as e:
            log.exception("faster-whisper: %s", e)

    def _test_ollama(self):
        try:
            models = [x.get("model", x.get("name", "?"))
                      for x in ollama.list().get("models", [])]
            log.info("Ollama OK. Models: %s", models)
            if OLLAMA_MODEL not in models and not any(
                    m.startswith(OLLAMA_MODEL.split(":")[0]) for m in models):
                log.warning("Configured model %s is not in Ollama. "
                            "Pull it with: ollama pull %s",
                            OLLAMA_MODEL, OLLAMA_MODEL)
        except Exception as e:
            log.exception("Ollama: %s", e)
            self._set_state("error", "Ollama is not running")

    def _stt_offline(self, wav_path: str):
        """Returns transcribed text, or None if a hard error was already reported."""
        if not self.local_stt and HAS_FASTER_WHISPER:
            self._set_status("Loading offline STT")
            for _ in range(30):
                time.sleep(1)
                if self.local_stt: break

        if not self.local_stt:
            if self._coaching: self._coach_stop()
            self._handle_error("Offline STT unavailable. Install faster-whisper.")
            return None

        try:
            self._set_status("Transcribing offline")
            segs, _ = self.local_stt.transcribe(
                wav_path, language=STT_LANGUAGE.split("-")[0], beam_size=5)
            text = " ".join(s.text for s in segs).strip()
            log.info("faster-whisper: %r", text)
            return text
        except Exception as e:
            log.exception("faster-whisper: %s", e)
            if self._coaching: self._coach_stop()
            self._handle_error("Offline STT failed")
            return None

    def _transcribe(self):
        tmp = self.tmp_wav
        if not tmp or not os.path.exists(tmp) or os.path.getsize(tmp) < 1000:
            if self._coaching: self._coach_stop()
            self._pending_img_b64 = None
            self._set_state("idle", "Recording was too short")
            return

        self._set_status("Checking connection")
        net = has_internet(timeout=2)
        log.info("Internet: %s", "yes" if net else "no")

        self._set_status("Recognizing speech")
        user_text = ""
        try:
            if net:
                try:
                    with sr.AudioFile(tmp) as src:
                        audio = self.recognizer.record(src)
                    user_text = self.recognizer.recognize_google(
                        audio, language=STT_LANGUAGE)
                    log.info("Google STT: %r", user_text)
                except sr.UnknownValueError:
                    log.warning("Google STT: speech was not understood")
                except Exception as e:
                    log.warning("Google STT: %s. Falling back to offline.", e)
                    user_text = self._stt_offline(tmp)
                    if user_text is None: return
            else:
                log.info("Offline mode: using faster-whisper")
                user_text = self._stt_offline(tmp)
                if user_text is None: return
        finally:
            try: os.unlink(tmp)
            except: pass

        if not user_text:
            if self._coaching: self._coach_stop()
            self._pending_img_b64 = None
            self._set_state("idle", "Did not catch that"); return

        preview = user_text[:38] + "..." if len(user_text) > 38 else user_text
        self._set_status(f"You: {preview}")

        img  = self._pending_img_b64
        mode = self._pending_mode if img else "voice"
        self._pending_img_b64 = None
        self._pending_mode    = "voice"

        if self._coaching and not self._coaching_task:
            self._coaching_task = user_text
            threading.Thread(
                target=self._coach_first_step, daemon=True).start()
        else:
            self._ask_ollama(user_text, img, mode)

    # === Ollama LLM ===

    @staticmethod
    def _encode_for_ollama(img_b64: str) -> bytes:
        """
        Decode a base64 PNG, resize to IMG_MAX_PX on the longest side,
        return JPEG bytes. Returns b'' on failure.

        Passing bytes (not a base64 string and not a path) works with all
        ollama-python releases because the library always accepts bytes
        and base64-encodes them itself.
        """
        try:
            raw = base64.b64decode(img_b64)
            pil = Image.open(io.BytesIO(raw)).convert("RGB")
            w, h = pil.size
            if max(w, h) > IMG_MAX_PX:
                ratio = IMG_MAX_PX / max(w, h)
                pil = pil.resize(
                    (int(w * ratio), int(h * ratio)), Image.LANCZOS)
            buf = io.BytesIO()
            pil.save(buf, format="JPEG", quality=85)
            data = buf.getvalue()
            log.info("Image: %dx%d, %.1f KB JPEG", w, h, len(data) / 1024)
            return data
        except Exception as e:
            log.error("encode_for_ollama: %s", e)
            return b""

    def _ollama_chat(self, messages, timeout: int = OLLAMA_TIMEOUT) -> str:
        """Call ollama.chat in a worker thread with a hard timeout."""
        result = [None]
        error  = [None]
        done   = threading.Event()

        def run():
            try:
                resp = ollama.chat(model=OLLAMA_MODEL, messages=messages)
                result[0] = resp["message"]["content"].strip()
            except Exception as e:
                error[0] = e
            finally:
                done.set()

        threading.Thread(target=run, daemon=True).start()
        if not done.wait(timeout=timeout):
            log.error("Ollama timeout after %ds", timeout)
            return ""
        if error[0]:
            raise error[0]
        return result[0] or ""

    def _ask_ollama(self, user_text: str, img_b64, mode: str = "voice"):
        self._set_state("thinking")

        msg = {"role": "user", "content": user_text}

        if img_b64:
            img_bytes = self._encode_for_ollama(img_b64)
            if img_bytes:
                msg["images"] = [img_bytes]
            else:
                log.warning("Image is unavailable. Sending text only.")

        if "images" in msg:
            messages = [{"role": "system", "content": SYSTEM_PROMPT}, msg]
        else:
            self.history.append(msg)
            if len(self.history) > MAX_HISTORY * 2:
                self.history = self.history[-MAX_HISTORY * 2:]
            messages = [{"role": "system", "content": SYSTEM_PROMPT}] + self.history

        try:
            answer = self._ollama_chat(messages)
        except Exception as e:
            log.exception("Ollama: %s", e)
            self._handle_error(f"Ollama: {e}")
            return

        if not answer:
            self._handle_error("Ollama did not respond in time")
            return

        if "images" not in msg:
            self.history.append({"role": "assistant", "content": answer})

        self._memory_log(mode, user_text, answer)

        self.root.after(0, lambda: self.bubble.show(
            answer, self.root.winfo_x(), self.root.winfo_y(),
            self.WIN_W, self.WIN_H))
        self._set_state("speaking")
        threading.Thread(
            target=self._speak, args=(answer,), daemon=True).start()

    # === Text to speech ===

    def _speak(self, text: str):
        text = strip_emoji(text)
        try:
            if has_internet(timeout=2):
                try:
                    self._speak_online(text)
                    return
                except Exception as e:
                    log.warning("edge-tts: %s. Falling back to SAPI.", e)
            else:
                log.info("Offline TTS: SAPI")
            try:
                self._speak_offline(text)
            except Exception as e2:
                log.exception("offline TTS: %s", e2)
        finally:
            self.root.after(0, self.bubble.tts_done)
            self._set_state("idle")

    def _speak_online(self, text: str):
        async def run():
            fd, tmp = tempfile.mkstemp(suffix=".mp3")
            os.close(fd)
            try:
                comm = edge_tts.Communicate(text, TTS_VOICE_ON)
                await asyncio.wait_for(comm.save(tmp), timeout=10)
                pygame.mixer.music.load(tmp)
                pygame.mixer.music.play()
                while pygame.mixer.music.get_busy():
                    time.sleep(0.1)
            finally:
                try:
                    pygame.mixer.music.unload()
                    os.unlink(tmp)
                except: pass
        asyncio.run(run())

    def _speak_offline(self, text: str):
        """pyttsx3 can hang. Run it in a thread with a hard timeout."""
        import pyttsx3
        done_evt = threading.Event()
        exc_box  = [None]

        def run():
            try:
                engine = pyttsx3.init()
                voices = engine.getProperty("voices")
                for v in voices:
                    if TTS_VOICE_OFF.lower() in v.name.lower():
                        engine.setProperty("voice", v.id); break
                engine.setProperty("rate", 175)
                engine.say(text)
                engine.runAndWait()
                engine.stop()
            except Exception as e:
                exc_box[0] = e
            finally:
                done_evt.set()

        t = threading.Thread(target=run, daemon=True)
        t.start()
        timeout = max(20, min(120, len(text.split()) * 10))
        if not done_evt.wait(timeout=timeout):
            log.warning("pyttsx3 timeout after %ds", timeout)
        if exc_box[0]:
            raise exc_box[0]

    # === Errors ===

    def _handle_error(self, msg: str):
        log.error("ERROR: %s", msg)
        self._pending_img_b64 = None
        self._set_state("error", msg[:55])


# ======================================================================
if __name__ == "__main__":
    try:
        PeekyAgent()
    except Exception as e:
        log.exception("FATAL: %s", e)
        input("Press Enter to close...")
