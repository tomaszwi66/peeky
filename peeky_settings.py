"""
Peeky settings: persistence and the Settings dialog.

Only two user-facing controls: widget size and RAG top-K.
Appearance stays at the default light theme always.
"""

from __future__ import annotations
import os, json, logging
import tkinter as tk

log = logging.getLogger("peeky.settings")

HERE          = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(HERE, "settings.json")

DEFAULTS = {
    "rag_topk":    4,
    "rag_chunk":   600,
    "embed_model": "nomic-embed-text",
}


def load_settings() -> dict:
    data = dict(DEFAULTS)
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data.update(json.load(f) or {})
    except Exception as e:
        log.warning("load_settings: %s", e)
    return data


def save_settings(data: dict) -> None:
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log.warning("save_settings: %s", e)


class SettingsDialog:

    @staticmethod
    def open(root, current: dict, on_change):
        from peeky import BG, SURFACE, BORDER, TEXT_HI, TEXT_LO, ACCENT

        state = dict(current)

        dlg = tk.Toplevel(root)
        dlg.withdraw()                       # hide until positioned
        dlg.title("Peeky Settings")
        dlg.configure(bg=BG)
        dlg.wm_attributes("-topmost", True)
        dlg.wm_attributes("-alpha", 0.98)
        dlg.resizable(False, False)

        tk.Frame(dlg, bg=ACCENT["idle"], height=4).pack(fill="x")

        pad = tk.Frame(dlg, bg=BG)
        pad.pack(fill="both", expand=True, padx=24, pady=18)

        # label + value on same row
        row = tk.Frame(pad, bg=BG)
        row.pack(fill="x", pady=(0, 6))
        tk.Label(row, text="Chunks sent per question",
                 font=("Segoe UI", 10), bg=BG, fg=TEXT_LO).pack(side="left")
        topk_var = tk.IntVar(value=int(state.get("rag_topk", 4)))
        topk_lbl = tk.Label(row, text=str(topk_var.get()),
                            font=("Segoe UI", 10, "bold"),
                            bg=BG, fg=ACCENT["idle"])
        topk_lbl.pack(side="right")

        def on_topk(v):
            state["rag_topk"] = int(float(v))
            topk_lbl.config(text=str(state["rag_topk"]))

        tk.Scale(pad, from_=1, to=10, orient="horizontal", variable=topk_var,
                 command=on_topk, bg=BG, fg=TEXT_HI, troughcolor=SURFACE,
                 highlightthickness=0, showvalue=False, length=300,
                 sliderrelief="flat").pack(fill="x")

        # buttons
        bf = tk.Frame(dlg, bg=BG)
        bf.pack(pady=(4, 18))

        def save_close():
            on_change(dict(state))
            save_settings(state)
            dlg.destroy()

        def cancel():
            on_change(dict(current))
            dlg.destroy()

        tk.Button(bf, text="Save", command=save_close,
                  font=("Segoe UI", 10, "bold"),
                  bg=ACCENT["idle"], fg="white", relief="flat",
                  padx=22, pady=7, cursor="hand2", bd=0
                  ).pack(side="left", padx=4)
        tk.Button(bf, text="Cancel", command=cancel,
                  font=("Segoe UI", 9), bg=SURFACE, fg=TEXT_HI,
                  relief="flat", padx=14, pady=7, cursor="hand2", bd=0,
                  highlightbackground=BORDER, highlightthickness=1
                  ).pack(side="left", padx=4)

        # auto-size, then position next to the widget (same logic as bubble)
        root.update_idletasks()
        dlg.update_idletasks()
        w  = dlg.winfo_reqwidth() + 20
        h  = dlg.winfo_reqheight() + 10
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        pet_x = root.winfo_x()
        pet_y = root.winfo_y()
        pet_w = root.winfo_width()

        x = pet_x - w - 14
        if x < 10:           x = pet_x + pet_w + 14
        if x + w > sw - 10:  x = sw - w - 10
        y = pet_y
        if y + h > sh - 10:  y = sh - h - 10
        if y < 10:           y = 10

        dlg.geometry(f"{w}x{h}+{x}+{y}")
        dlg.deiconify()
        dlg.lift()
        dlg.focus_force()
