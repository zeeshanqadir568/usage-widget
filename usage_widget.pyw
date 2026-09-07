"""
Floating "live usage" widget.

Simple by default: for each agent it shows how much you've used THIS session -
tokens burned, messages sent, session time, and which models you used.

  * "more details"  reveals cost, burn rate, cache split, today / week totals,
    and any hard limits.
  * "history"       opens a table of every past session with the same stats.

  drag        - left-click anywhere and move
  resize      - drag the striped corner grip (or right-click -> Size)
  scroll      - mouse-wheel when content overflows
  opacity     - Ctrl + mouse-wheel
  menu        - right-click

State + provider config live in ~/.claude-usage-widget.json.
Run:  pythonw usage_widget.pyw   (or double-click run-widget.vbs)
"""

from __future__ import annotations

import ctypes
import json
import os
import queue
import subprocess
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from ctypes import wintypes

import usage_sources as U

# ---- Windows: which app is in the foreground? --------------------------------
_ANTIGRAVITY_EXES = {"antigravity ide.exe", "antigravity.exe"}
_SELF_EXES = {"pythonw.exe", "python.exe"}


def _foreground_exe() -> str | None:
    """basename (lowercase) of the process that owns the foreground window."""
    if os.name != "nt":
        return None
    try:
        u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
        hwnd = u32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = wintypes.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return None
        h = k32.OpenProcess(0x1000, False, pid.value)   # QUERY_LIMITED_INFORMATION
        if not h:
            return None
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value).lower()
        finally:
            k32.CloseHandle(h)
    except Exception:
        return None
    return None


def _process_running(exe_lower: str) -> bool:
    if os.name != "nt":
        return True
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {exe_lower}", "/NH"],
            capture_output=True, text=True, timeout=6,
            creationflags=0x08000000)
        return exe_lower.split(".")[0].lower() in (out.stdout or "").lower()
    except Exception:
        return True


def _ide_start_ts() -> float | None:
    """Unix time the Antigravity IDE was launched (earliest of its processes).

    This anchors "session time" to the *IDE session*, not to this widget's own
    process - so the clock keeps counting even if the widget is restarted, and
    only goes back to 0 when the IDE itself is restarted.
    """
    if os.name != "nt":
        return None
    ps = ("$p = Get-Process -Name 'Antigravity IDE','Antigravity' "
          "-ErrorAction SilentlyContinue | Where-Object { $_.StartTime } | "
          "Sort-Object StartTime | Select-Object -First 1; "
          "if ($p) { $e = [datetime]::new(1970,1,1,0,0,0,"
          "[System.DateTimeKind]::Utc); "
          "'{0:0}' -f ($p.StartTime.ToUniversalTime() - $e).TotalSeconds }")
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=8,
            creationflags=0x08000000)
        s = (out.stdout or "").strip().split(".")[0]
        return float(s) if s.lstrip("-").isdigit() else None
    except Exception:
        return None

CFG = U.CONFIG_PATH
DEFAULT_POLL = {"claude_code": 5, "antigravity": 30, "codex": 10,
                "openrouter": 120}
FALLBACK_POLL = 15
SIZE_PRESETS = {"Compact": (320, 220), "Small": (370, 400),
                "Medium": (400, 600), "Large": (450, 780),
                "Tall": (400, 940)}

# palette ------------------------------------------------------------------
BG = "#0e1015"
CARD = "#161a24"
BG_BAR = "#232838"
FG = "#f2f4f8"
FG_DIM = "#9aa1b2"
FG_FAINT = "#5c636f"
LINK = "#7aa2f7"
ACCENT = "#7aa2f7"
GOOD = "#3fb765"
WARN = "#e0a52e"
BAD = "#e0574e"
TRACK = "#242a38"


def _bar_color(pct):
    if pct is None:
        return "#333a49"
    if pct >= 85:
        return BAD
    if pct >= 60:
        return WARN
    return GOOD


# ------------------------------------------------------------------ tooltip
class Tooltip:
    _tip = None

    def __init__(self, widget, text, delay=350):
        self.widget = widget
        self.text = text
        self.delay = delay
        self._after = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _e=None):
        self._cancel()
        self._after = self.widget.after(self.delay, self._show)

    def _cancel(self):
        if self._after:
            self.widget.after_cancel(self._after)
            self._after = None

    def _show(self):
        Tooltip._destroy()
        try:
            x = self.widget.winfo_rootx()
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        except tk.TclError:
            return
        t = tk.Toplevel(self.widget)
        t.wm_overrideredirect(True)
        try:
            t.wm_attributes("-topmost", True)
        except tk.TclError:
            pass
        t.configure(bg="#000000")
        tk.Label(t, text=self.text, bg="#2b2f3a", fg="#f2f4f8",
                 font=("Consolas", 10), padx=9, pady=5,
                 relief="solid", bd=1).pack()
        t.update_idletasks()
        # keep it on-screen horizontally
        sw = t.winfo_screenwidth()
        if x + t.winfo_width() > sw - 8:
            x = sw - t.winfo_width() - 8
        t.wm_geometry(f"+{max(4, x)}+{y}")
        t.lift()
        Tooltip._tip = t

    def _hide(self, _e=None):
        self._cancel()
        Tooltip._destroy()

    @classmethod
    def _destroy(cls):
        if cls._tip is not None:
            try:
                cls._tip.destroy()
            except tk.TclError:
                pass
            cls._tip = None


# ------------------------------------------------------------------- poller
class Poller(threading.Thread):
    def __init__(self, provider, interval, out: queue.Queue):
        super().__init__(daemon=True)
        self.provider = provider
        self.interval = interval
        self.out = out
        self._stop = threading.Event()
        self.force = threading.Event()

    def run(self):
        while not self._stop.is_set():
            try:
                self.provider.poll()
                self.out.put((self.provider.key, self.provider.snapshot()))
            except Exception as exc:                       # pragma: no cover
                self.out.put((self.provider.key, {
                    "ok": False, "error": str(exc), "label": self.provider.label,
                    "color": self.provider.color, "mode": "session",
                    "headline": "", "session": None, "models": [],
                    "history": [], "details": []}))
            waited = 0.0
            while (waited < self.interval and not self._stop.is_set()
                   and not self.force.is_set()):
                time.sleep(0.2)
                waited += 0.2
            self.force.clear()

    def stop(self):
        self._stop.set()


# ===================================================================== main
class UsageWidget(tk.Tk):
    def __init__(self):
        super().__init__()
        self.cfg = self._load()
        self.alpha = float(self.cfg.get("alpha", 0.96))
        self.expanded = dict(self.cfg.get("expanded", {}))   # per-provider bool

        self.overrideredirect(True)
        self.attributes("-topmost", True)
        try:
            self.attributes("-alpha", self.alpha)
        except tk.TclError:
            pass
        self.configure(bg=BG)
        self.geometry(self._sane_geo(self.cfg.get("geometry")))
        self.minsize(240, 150)

        self.f_label = tkfont.Font(family="Consolas", size=10, weight="bold")
        self.f_title = tkfont.Font(family="Segoe UI", size=11, weight="bold")
        self.f_big = tkfont.Font(family="Consolas", size=18, weight="bold")
        self.f_stat = tkfont.Font(family="Consolas", size=14, weight="bold")
        self.f_btn = tkfont.Font(family="Consolas", size=13, weight="bold")
        self.f_row = tkfont.Font(family="Consolas", size=9)
        self.f_small = tkfont.Font(family="Consolas", size=9)
        self.f_tiny = tkfont.Font(family="Consolas", size=8)

        self.visibility = self.cfg.get("visibility", "antigravity")  # or "always"
        # "session time" is measured from when the IDE was launched (see
        # _ide_start_ts).  Start with the widget's own start time and refine it
        # once the IDE process has been queried; re-check periodically so an IDE
        # restart (new launch time) restarts the clock.
        self._proc_start = time.time()
        self._start_ts = self._proc_start
        self._ide_start = None
        self._hidden = False
        self._saved_geo = self.cfg.get("geometry")
        self._vis_ticks = 0
        self._first_drawn = False
        self._collapsed = False
        self._pre_min_geo = self.cfg.get("pre_min_geo") or self.cfg.get("geometry")
        self._collapsed_w = self.cfg.get("collapsed_w")     # px, None = full width
        self._hover_after = None                            # 2s dwell timer
        self._hover_leave_check = None
        self._shimmer_job = None                            # running colour cycle
        self._shimmer_i = 0

        self.q: queue.Queue = queue.Queue()
        self.snaps: dict[str, dict] = {}
        self._drag: list[tk.Widget] = []

        self.providers = U.build_providers(self.cfg)
        self._build()
        self._menu()

        self.pollers = []
        for p in self.providers:
            iv = (self.cfg.get("providers", {}).get(p.key, {}).get("poll_seconds")
                  or DEFAULT_POLL.get(p.key, FALLBACK_POLL))
            pl = Poller(p, iv, self.q)
            pl.start()
            self.pollers.append(pl)

        self.after(120, self._drain)
        self.after(1000, self._tick)
        self.after(300, self._assert_top)          # topmost sticks after mapping
        self.after(800, self._visibility_tick)
        self.after(250, lambda: self._sync_ide_anchor(initial=True))
        self.protocol("WM_DELETE_WINDOW", self._quit)

        if self.cfg.get("collapsed"):
            self.after(200, self._toggle_min)

    @staticmethod
    def _sane_geo(geo: str | None) -> str:
        """Reject off-screen / garbage geometry left in a stale config."""
        import re as _re
        default = "420x620+48+48"
        m = _re.match(r"(\d+)x(\d+)([+-]\d+)([+-]\d+)$", geo or "")
        if not m:
            return default
        w, h, gx, gy = int(m[1]), int(m[2]), int(m[3]), int(m[4])
        if not (200 <= w <= 1600 and 120 <= h <= 2000):
            return default
        if abs(gx) > 8000 or abs(gy) > 8000:      # parked off-screen in a stale file
            return f"{w}x{h}+48+48"
        return f"{w}x{h}{m[3]}{m[4]}"

    # -------------------------------------------------------------- config
    def _load(self):
        try:
            with open(CFG, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return {}

    def _save(self):
        try:
            live_geo = (self._pre_min_geo if self._collapsed and self._pre_min_geo
                        else self.geometry())
            self.cfg["geometry"] = self._sane_geo(live_geo)
            self.cfg["alpha"] = round(self.alpha, 3)
            self.cfg["expanded"] = self.expanded
            self.cfg["visibility"] = self.visibility
            self.cfg["collapsed"] = self._collapsed
            self.cfg["pre_min_geo"] = self._sane_geo(self._pre_min_geo)
            self.cfg["collapsed_w"] = self._collapsed_w
            with open(CFG, "w", encoding="utf-8") as fh:
                json.dump(self.cfg, fh, indent=2)
        except Exception:
            pass

    # --------------------------------------------------------------- build
    def _build(self):
        for w in self.winfo_children():
            w.destroy()
        self._drag = []

        shell = tk.Frame(self, bg=TRACK, padx=1, pady=1)
        shell.pack(fill="both", expand=True)
        root = tk.Frame(shell, bg=BG)
        root.pack(fill="both", expand=True)

        # title bar ---------------------------------------------------
        bar = tk.Frame(root, bg=CARD, height=32)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        self._bar = bar

        # right-side icon buttons (all identical size + weight)
        self._min_btn = None
        for txt, cb, tip in (("✕", self._quit, "Close widget"),
                             ("⚙", self._open_cfg, "Open config file"),
                             ("↻", self._refresh, "Refresh all now"),
                             ("‒", self._toggle_min,
                              "Minimize — collapse to the title bar")):
            b = tk.Label(bar, text=txt, bg=CARD, fg=FG_DIM, font=self.f_btn,
                         cursor="hand2", width=2, padx=6)
            b.pack(side="right", fill="y")
            b.bind("<Button-1>", lambda e, c=cb: c())
            b.bind("<Enter>", lambda e, w=b: w.config(fg=FG, bg=BG_BAR))
            b.bind("<Leave>", lambda e, w=b: w.config(fg=FG_DIM, bg=CARD))
            Tooltip(b, tip)
            if cb == self._toggle_min:
                self._min_btn = b

        # centred wordmark, sitting on top of the bar
        tw = tk.Frame(bar, bg=CARD)
        self._tw = tw
        tw.place(relx=0.5, rely=0.5, anchor="center")
        self._title_dot = tk.Label(tw, text="●", bg=CARD, fg=ACCENT,
                                   font=self.f_small)
        self._title_dot.pack(side="left", padx=(0, 6))
        tk.Label(tw, text="Live Usage", bg=CARD, fg=FG, font=self.f_title
                 ).pack(side="left")
        for w in (tw, *tw.winfo_children()):
            w.bind("<Button-1>", self._drag_start, add="+")
            w.bind("<B1-Motion>", self._drag_move, add="+")
            w.bind("<Double-Button-1>", lambda e: self._toggle_min())
            w.bind("<MouseWheel>", self._wheel)

        self._accent_line = tk.Frame(root, bg=ACCENT, height=2)   # accent underline
        self._accent_line.pack(fill="x")

        # left-edge grip: only shown while collapsed, drags the strip narrower/wider
        self._minigrip = tk.Label(bar, text="⇔", bg=CARD, fg=FG_FAINT,
                                  font=self.f_btn, cursor="size_we", width=2)
        self._minigrip.bind("<Button-1>", self._cw_start)
        self._minigrip.bind("<B1-Motion>", self._cw_move)
        self._minigrip.bind("<ButtonRelease-1>", lambda e: self._save())
        Tooltip(self._minigrip, "Drag to set the minimized strip's width")

        self._drag += [root, bar]
        bar.bind("<Double-Button-1>", lambda e: self._toggle_min())
        bar.bind("<Enter>", self._hover_enter, add="+")
        bar.bind("<Leave>", self._hover_leave, add="+")

        # scroll body ----------------------------------------------
        self.canvas = tk.Canvas(root, bg=BG, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.body = tk.Frame(self.canvas, bg=BG)
        self._bwin = self.canvas.create_window((0, 0), window=self.body,
                                               anchor="nw")
        self.body.bind("<Configure>", lambda e: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(
            self._bwin, width=e.width))
        for w in (self.canvas, self.body):
            w.bind("<MouseWheel>", self._wheel)
        self._drag += [self.canvas, self.body]

        # footer + resize grip -----------------------------------
        foot = tk.Frame(root, bg=CARD)
        foot.pack(fill="x", side="bottom")
        self._foot = foot
        self.status = tk.Label(foot, text="starting…", bg=CARD, fg=FG_FAINT,
                               font=self.f_tiny, anchor="w", padx=6)
        self.status.pack(side="left")
        grip = tk.Frame(foot, bg=CARD, width=18, height=16, cursor="size_nw_se")
        grip.pack(side="right")
        gc = tk.Canvas(grip, width=18, height=16, bg=CARD, highlightthickness=0,
                       cursor="size_nw_se")
        gc.pack()
        for i in (4, 9, 14):
            gc.create_line(17 - i, 15, 17, 15 - i, fill=FG_FAINT)
        for w in (grip, gc):
            w.bind("<Button-1>", self._rz_start)
            w.bind("<B1-Motion>", self._rz_move)
            w.bind("<ButtonRelease-1>", lambda e: self._save())
        Tooltip(gc, "Drag to resize  ·  right-click → Size for presets")
        self._drag.append(self.status)

        # sections --------------------------------------------------
        self.sec: dict[str, dict] = {}
        if not self.providers:
            tk.Label(self.body, text="\n  No agents detected yet.\n\n"
                     "  Claude Code logs, the Antigravity IDE,\n"
                     "  Codex or an OpenRouter key appear here\n"
                     "  automatically.  Right-click → Open config\n"
                     "  file to add a custom one.\n",
                     bg=BG, fg=FG_DIM, font=self.f_row, justify="left"
                     ).pack(anchor="w")
        for p in self.providers:
            self.sec[p.key] = self._section(p)

        self._bind_drag()

    def _section(self, prov) -> dict:
        wrap = tk.Frame(self.body, bg=BG)
        wrap.pack(fill="x", pady=(0, 2))
        stripe = tk.Frame(wrap, bg=prov.color, width=3)
        stripe.pack(side="left", fill="y")
        inner = tk.Frame(wrap, bg=BG, padx=9, pady=7)
        inner.pack(side="left", fill="both", expand=True)

        head = tk.Frame(inner, bg=BG)
        head.pack(fill="x")
        tk.Label(head, text=prov.label, bg=BG, fg=prov.color,
                 font=self.f_label).pack(side="left")
        hint = tk.Label(head, text="", bg=BG, fg=FG_FAINT, font=self.f_small)
        hint.pack(side="right")

        big = tk.Frame(inner, bg=BG)          # session-mode stat strip
        big.pack(fill="x", pady=(5, 2))
        stats = []
        stat_tips = []
        for _ in range(3):
            cell = tk.Frame(big, bg=BG)
            cell.pack(side="left", expand=True, fill="x")
            val = tk.Label(cell, text="–", bg=BG, fg=FG, font=self.f_stat)
            val.pack(anchor="w")
            cap = tk.Label(cell, text="", bg=BG, fg=FG_FAINT, font=self.f_tiny)
            cap.pack(anchor="w")
            stats.append((val, cap))
            stat_tips.append(Tooltip(cell, ""))
            for w in (cell, val, cap):
                w.bind("<MouseWheel>", self._wheel)

        table = tk.Frame(inner, bg=BG)       # models (session) / quota grid
        table.pack(fill="x", pady=(3, 0))
        table2 = tk.Frame(inner, bg=BG)      # limit windows (session mode)
        table2.pack(fill="x", pady=(3, 0))

        details = tk.Frame(inner, bg=CARD)   # "more details" body

        links = tk.Frame(inner, bg=BG)
        links.pack(fill="x", pady=(5, 0))
        more = tk.Label(links, text="▾ more details", bg=BG, fg=LINK,
                        font=self.f_small, cursor="hand2")
        more.pack(side="left")
        more.bind("<Button-1>", lambda e, k=prov.key: self._toggle_more(k))
        Tooltip(more, "Show cost, burn rate, cache split, today / week totals")
        hist = tk.Label(links, text="history ›", bg=BG, fg=LINK,
                        font=self.f_small, cursor="hand2")
        if prov.mode == "session":
            hist.pack(side="right")
            hist.bind("<Button-1>", lambda e, k=prov.key: self._open_history(k))
            Tooltip(hist, "Every past session with the same stats")

        tk.Frame(self.body, bg=TRACK, height=1).pack(fill="x")

        for w in (wrap, inner, head, big, table, table2):
            w.bind("<MouseWheel>", self._wheel)

        return {"prov": prov, "wrap": wrap, "hint": hint, "big": big,
                "stats": stats, "stat_tips": stat_tips, "table": table,
                "table2": table2, "details": details, "more": more}

    # ------------------------------------------------------------ behaviour
    def _bind_drag(self):
        for t in self._drag:
            t.bind("<Button-1>", self._drag_start)
            t.bind("<B1-Motion>", self._drag_move)
            t.bind("<ButtonRelease-1>", lambda e: self._save())
        self.bind("<Control-MouseWheel>", self._wheel_alpha)

    def _drag_start(self, e):
        self._dx = e.x_root - self.winfo_x()
        self._dy = e.y_root - self.winfo_y()

    def _drag_move(self, e):
        self.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def _rz_start(self, e):
        self._rw = self.winfo_width() - e.x_root
        self._rh = self.winfo_height() - e.y_root

    def _rz_move(self, e):
        w = max(self.minsize()[0], e.x_root + self._rw)
        h = max(self.minsize()[1], e.y_root + self._rh)
        self.geometry(f"{int(w)}x{int(h)}")

    def _wheel(self, e):
        self.canvas.yview_scroll(int(-e.delta / 120), "units")

    def _wheel_alpha(self, e):
        self.alpha = min(1.0, max(0.3, self.alpha + (0.05 if e.delta > 0 else -0.05)))
        try:
            self.attributes("-alpha", self.alpha)
        except tk.TclError:
            pass
        self._save()

    def _set_size(self, name):
        w, h = SIZE_PRESETS[name]
        self.geometry(f"{w}x{h}")
        self._save()

    def _cycle_size(self):
        if self._collapsed:
            return
        order = list(SIZE_PRESETS)
        cur = self.winfo_width(), self.winfo_height()
        idx = 0
        for i, n in enumerate(order):
            if abs(SIZE_PRESETS[n][0] - cur[0]) < 6:
                idx = i
        self._set_size(order[(idx + 1) % len(order)])

    # -- collapse / restore -----------------------------------------
    MIN_STRIP_W = 70          # narrowest the collapsed strip may go

    def _toggle_min(self):
        """Collapse the widget down to just its title bar (and back)."""
        if self._collapsed:
            self._collapsed = False
            self._stop_shimmer()
            self._minigrip.place_forget()
            self._tw.place_configure(relx=0.5, x=0, anchor="center")
            self.minsize(240, 150)
            self.canvas.pack(fill="both", expand=True)
            self._foot.pack(fill="x", side="bottom")
            if self._min_btn:
                self._min_btn.config(text="‒")
            if self._pre_min_geo:
                self.geometry(self._sane_geo(self._pre_min_geo))
        else:
            self._collapsed = True
            self._pre_min_geo = self.geometry()
            self._foot.pack_forget()
            self.canvas.pack_forget()
            self.update_idletasks()
            h = max(20, self._bar.winfo_reqheight()) + 2
            self.minsize(self.MIN_STRIP_W, h)
            w = self._collapsed_w or self.winfo_width()
            self.geometry(f"{int(max(self.MIN_STRIP_W, w))}x{h}")
            self._minigrip.place(x=0, rely=0.5, anchor="w")   # left-edge grip
            # left-align the wordmark so it never sits under the buttons
            self._tw.place_configure(relx=0.0, x=26, anchor="w")
            if self._min_btn:
                self._min_btn.config(text="□")
        self._assert_top(hard=False)
        self._save()

    def _set_collapsed_w(self, w):
        """Menu preset for the minimized strip width. w=None -> match window."""
        if w is None:
            self._collapsed_w = None
            target = self._sane_geo(self._pre_min_geo)
            full = int(target.split("x")[0]) if "x" in target else 380
            w = full
        else:
            self._collapsed_w = int(w)
        if self._collapsed:
            self.geometry(f"{int(max(self.MIN_STRIP_W, w))}x{self.winfo_height()}")
        self._save()

    def _cw_start(self, e):
        self._cw_x = e.x_root
        self._cw_w0 = self.winfo_width()

    def _cw_move(self, e):
        # dragging the left grip right shrinks the strip; left grows it
        w = self._cw_w0 - (e.x_root - self._cw_x)
        w = max(self.MIN_STRIP_W, min(self.winfo_screenwidth(), int(w)))
        self._collapsed_w = w
        self.geometry(f"{w}x{self.winfo_height()}")

    # -- hover shimmer on the collapsed strip ----------------------
    _SHIMMER = ["#7aa2f7", "#9d7cf0", "#e0574e", "#e0a52e",
                "#3fb765", "#4bc3d8", "#7aa2f7"]

    def _hover_enter(self, _e=None):
        if self._hover_leave_check:
            self.after_cancel(self._hover_leave_check)
            self._hover_leave_check = None
        if not self._collapsed or self._hover_after or self._shimmer_job:
            return
        self._hover_after = self.after(2000, self._start_shimmer)

    def _hover_leave(self, _e=None):
        # debounce: crossing onto a child button also fires <Leave> on the bar
        if self._hover_leave_check:
            self.after_cancel(self._hover_leave_check)
        self._hover_leave_check = self.after(140, self._hover_really_left)

    def _hover_really_left(self):
        self._hover_leave_check = None
        try:
            w = self.winfo_containing(self.winfo_pointerx(), self.winfo_pointery())
        except tk.TclError:
            w = None
        p = w
        while p is not None:
            if p is self._bar:
                return                       # still inside the strip
            p = getattr(p, "master", None)
        if self._hover_after:
            self.after_cancel(self._hover_after)
            self._hover_after = None
        self._stop_shimmer()

    def _start_shimmer(self):
        self._hover_after = None
        self._shimmer_i = 0
        self._shimmer_step()

    def _shimmer_step(self):
        cols = self._SHIMMER
        a = cols[self._shimmer_i % len(cols)]
        b = cols[(self._shimmer_i + 1) % len(cols)]
        try:
            self._accent_line.config(bg=a)
            self._minigrip.config(fg=b)
        except tk.TclError:
            return
        self._shimmer_i += 1
        self._shimmer_job = self.after(320, self._shimmer_step)

    def _stop_shimmer(self):
        if self._shimmer_job:
            self.after_cancel(self._shimmer_job)
            self._shimmer_job = None
        try:
            self._accent_line.config(bg=ACCENT)
            self._minigrip.config(fg=FG_FAINT)
        except tk.TclError:
            pass

    # -- session-clock anchor (tied to the IDE launch, not this process) --
    def _sync_ide_anchor(self, initial=False):
        def work():
            ts = _ide_start_ts()
            try:
                self.after(0, lambda: self._apply_ide_anchor(ts))
            except (RuntimeError, tk.TclError):
                pass
        threading.Thread(target=work, daemon=True).start()

    def _apply_ide_anchor(self, ts):
        if not ts or ts <= 0 or ts > time.time() + 60:
            return
        # first successful detection, or the IDE was restarted -> re-anchor
        if self._ide_start is None or abs(ts - self._ide_start) > 5:
            self._ide_start = ts
            self._start_ts = ts
            try:
                self._render_clocks()
            except tk.TclError:
                pass

    def _menu(self):
        m = tk.Menu(self, tearoff=0, bg=CARD, fg=FG,
                    activebackground="#2b2f3a", activeforeground=FG)
        m.add_command(label="Refresh now", command=self._refresh)
        m.add_command(label="Minimize / restore", command=self._toggle_min)
        sz = tk.Menu(m, tearoff=0, bg=CARD, fg=FG)
        for name in SIZE_PRESETS:
            sz.add_command(label=name, command=lambda n=name: self._set_size(n))
        sz.add_separator()
        sz.add_command(label="Cycle", command=self._cycle_size)
        m.add_cascade(label="Size", menu=sz)
        mw = tk.Menu(m, tearoff=0, bg=CARD, fg=FG)
        for lbl, w in (("Tiny", 80), ("Short", 130), ("Medium", 200),
                       ("Match window", None)):
            mw.add_command(label=lbl, command=lambda x=w: self._set_collapsed_w(x))
        m.add_cascade(label="Minimized width", menu=mw)
        op = tk.Menu(m, tearoff=0, bg=CARD, fg=FG)
        for v in (1.0, 0.9, 0.8, 0.7, 0.55, 0.4):
            op.add_command(label=f"{int(v*100)}%",
                           command=lambda x=v: self._set_alpha(x))
        m.add_cascade(label="Opacity", menu=op)
        vis = tk.Menu(m, tearoff=0, bg=CARD, fg=FG)
        vis.add_command(label="Only while Antigravity IDE is focused",
                        command=lambda: self._set_visibility("antigravity"))
        vis.add_command(label="Always visible",
                        command=lambda: self._set_visibility("always"))
        m.add_cascade(label="Visibility", menu=vis)
        m.add_separator()
        m.add_command(label="Open config file", command=self._open_cfg)
        m.add_command(label="Quit", command=self._quit)
        self.bind("<Button-3>", lambda e: m.tk_popup(e.x_root, e.y_root))

    def _set_visibility(self, mode):
        self.visibility = mode
        if mode == "always":
            self._show()
        self._save()

    def _set_alpha(self, v):
        self.alpha = v
        try:
            self.attributes("-alpha", v)
        except tk.TclError:
            pass
        self._save()

    def _toggle_more(self, key):
        self.expanded[key] = not self.expanded.get(key, False)
        self._render()
        self._save()

    def _refresh(self):
        for pl in self.pollers:
            pl.force.set()
        self.status.config(text="  refreshing…")

    def _open_cfg(self):
        if not os.path.exists(CFG):
            self._save()
        try:
            os.startfile(CFG)                       # type: ignore[attr-defined]
        except Exception:
            try:
                subprocess.Popen(["notepad.exe", CFG])
            except Exception:
                pass

    # -------------------------------------------------------------- polling
    def _drain(self):
        changed = False
        try:
            while True:
                k, s = self.q.get_nowait()
                self.snaps[k] = s
                changed = True
        except queue.Empty:
            pass
        if changed or not self._first_drawn:
            self._first_drawn = True
            self._render()
        self.after(400, self._drain)

    def _tick(self):
        self._render_clocks()
        self.after(1000, self._tick)

    # -------------------------------------------------- show only in the IDE
    def _visibility_tick(self):
        try:
            if self.visibility == "always":
                self._show()
            elif time.time() - self._proc_start < 3.0:
                self._show()                       # grace period after launch
            else:
                fg = _foreground_exe()
                keep = (fg is None or fg in _ANTIGRAVITY_EXES or fg in _SELF_EXES)
                if keep:
                    self._show()
                    if self._vis_ticks % 20 == 0:
                        self._assert_top(hard=False)   # gently keep on top
                else:
                    self._hide()

            # every ~5s: has the Antigravity IDE gone away?
            self._vis_ticks += 1
            if (self.visibility != "always" and self._vis_ticks % 7 == 0
                    and not _process_running("antigravity ide.exe")):
                self._quit()
                return

            # every ~45s: re-anchor the session clock to the IDE launch time
            # (catches an IDE restart while the widget keeps running)
            if self._vis_ticks % 60 == 0:
                self._sync_ide_anchor()
        except Exception:
            pass
        self.after(750, self._visibility_tick)

    def _assert_top(self, hard=True):
        try:
            if hard:
                self.attributes("-topmost", False)
            self.attributes("-topmost", True)
            self.lift()
        except tk.TclError:
            pass

    def _show(self):
        if self._hidden:
            self._hidden = False
            try:
                self.deiconify()
                self.overrideredirect(True)
            except tk.TclError:
                pass
            self._assert_top()
            self.after(60, self._assert_top)

    def _hide(self):
        if not self._hidden:
            self._saved_geo = self.geometry()
            self._hidden = True
            try:
                self.withdraw()
            except tk.TclError:
                pass

    # --------------------------------------------------------------- render
    def _render(self):
        states = []
        for p in self.providers:
            sec = self.sec.get(p.key)
            if not sec:
                continue
            snap = self.snaps.get(p.key)
            self._render_section(sec, snap)
            if snap is not None:
                states.append(p.key.split("_")[0] + ("·ok" if snap.get("ok")
                                                     else "·!"))
        tail = "  ·  ide-only" if self.visibility == "antigravity" else ""
        self.status.config(
            text=("  " + "   ".join(states) + tail) if states else "  …")

        # recolour the wordmark dot: green = all good, red = something is off
        if getattr(self, "_title_dot", None) is not None:
            snaps = [self.snaps.get(p.key) for p in self.providers]
            snaps = [s for s in snaps if s is not None]
            if not snaps:
                col = FG_FAINT
            elif all(s.get("ok") for s in snaps):
                col = GOOD
            else:
                col = WARN
            try:
                self._title_dot.config(fg=col)
            except tk.TclError:
                pass

    def _render_clocks(self):
        # "session time" = how long this widget (i.e. this IDE session) has been
        # open, NOT the age of whichever chat log is newest.  It never resets
        # until the widget/IDE is closed.
        uptime = U.fmt_clock(time.time() - self._start_ts)
        for p in self.providers:
            snap = self.snaps.get(p.key)
            sec = self.sec.get(p.key)
            if not sec or not snap or snap.get("mode") != "session":
                continue
            if snap.get("session"):
                sec["stats"][2][0].config(text=uptime)

    def _render_section(self, sec, snap):
        if snap is None:
            sec["hint"].config(text="loading…")
            return
        if not snap.get("ok"):
            sec["hint"].config(text=str(snap.get("error", "unavailable"))[:40])
            for v, c in sec["stats"]:
                v.config(text="–"); c.config(text="")
            self._clear(sec["table"])
            self._clear(sec["table2"])
            sec["details"].pack_forget()
            return

        mode = snap.get("mode")
        if mode == "session":
            self._render_session(sec, snap)
        else:
            self._render_quota(sec, snap)

        # more-details body
        exp = self.expanded.get(sec["prov"].key, False)
        sec["more"].config(text=("▴ hide details" if exp else "▾ more details"))
        if exp and snap.get("details"):
            self._render_kv(sec["details"], snap["details"])
            sec["details"].pack(fill="x", pady=(5, 0), before=sec["more"].master)
        else:
            sec["details"].pack_forget()

    # -- session mode -------------------------------------------------
    def _render_session(self, sec, snap):
        sess = snap.get("session")
        if not sess:
            sec["hint"].config(text="no session activity yet")
            for v, c in sec["stats"]:
                v.config(text="–"); c.config(text="")
            self._clear(sec["table"])
            self._clear(sec["table2"])
            return
        sec["hint"].config(text="active " + U.fmt_ago(sess.get("last_ts")))
        (v0, c0), (v1, c1), (v2, c2) = sec["stats"]
        v0.config(text=U.fmt_tokens(sess["tokens"])); c0.config(text="session tok")
        v1.config(text=str(sess["messages"])); c1.config(text="session msgs")
        v2.config(text=U.fmt_clock(time.time() - self._start_ts))
        c2.config(text="session time")
        tips = sec.get("stat_tips") or []
        if len(tips) == 3:
            tips[0].text = ("Tokens used in the current session — this one open "
                            "conversation only (input + output + cache).")
            tips[1].text = ("Prompts a human actually typed this session. Tool "
                            "results and system / injected messages are not counted.")
            tips[2].text = ("Time since the Antigravity IDE was launched. It keeps "
                            "counting if the widget itself restarts; it only goes "
                            "back to 0:00 when the IDE is restarted.")

        # models used this session  (name · tokens · messages)
        rows = sess.get("model_rows", [])
        self._grid_table(
            sec["table"],
            headers=["model", "tokens", "", "msgs"],
            widths=[16, 9, 0, 5],
            rows=[[self._short(r["name"], 16), U.fmt_tokens(r["tokens"]),
                   None, str(r["messages"])] for r in rows],
            neutral=True)

        # daily / weekly usage — this session vs. all sessions, plus who spent it
        self._render_usage(sec["table2"], snap.get("usage"))

    # -- daily / weekly usage block (session + overall + by source) ---
    def _render_usage(self, box, usage):
        if not usage:
            self._clear(box)
            box._grid_sig = None
            return
        rows = []

        sub = usage.get("subscription")
        if sub and sub.get("days_left") is not None:
            tag = ("plan · " + sub["plan"]) if sub.get("plan") else "plan"
            lead = "  trial ends in" if sub.get("is_trial") else "  renews in"
            rows.append([tag, "", None, ""])
            rows.append([lead, f"{sub['days_left']}d", None,
                         sub.get("renews_on", "—")])

        # real rolling limits from Claude Code's own /usage cache
        if usage.get("source") == "claude":
            ft = usage.get("fetched_ts")
            hdr = "limits · from Claude" + (f" ({U.fmt_ago(ft)})" if ft else "")
        else:
            hdr = "limits · estimated (open Claude Code once)"
        rows.append([hdr, "", None, ""])
        for lr in usage.get("limit_rows", []):
            rows.append(["  " + lr["label"], lr["value"], lr.get("pct"),
                         U.fmt_reset_date(lr.get("reset_ts"))])

        sr = usage.get("session_rows") or []
        if sr:
            rows.append(["this session · est. share", "", None, ""])
            for r in sr:
                rows.append(["  of " + r["label"], f"~{r['pct']:.0f}%",
                             r["pct"], ""])

        src = usage.get("by_source") or []
        if src:
            rows.append(["by source · this week", "", None, ""])
            for it in src[:6]:
                wp = ("~%.0f%%" % it["weekly_pct"]
                      if it.get("weekly_pct") is not None else "–")
                rows.append([self._short(f"{it['badge']} {it['name']}", 13),
                             U.fmt_tokens(it["tok_week"]),
                             it.get("weekly_pct"), wp + " of wk"])
        self._grid_table(box, headers=["usage", "used", "", "resets"],
                         widths=[13, 12, 0, 12], rows=rows, neutral=False)

    # -- quota mode -------------------------------------------------
    def _render_quota(self, sec, snap):
        self._clear(sec["table2"])
        models = snap.get("models", [])
        healthy = sum(1 for m in models if not m.get("exhausted"))
        sec["hint"].config(text=f"{healthy}/{len(models)} models available"
                           if models else "no model data")
        for (v, c), txt, cap in zip(
                sec["stats"],
                [str(len(models)), str(len(models) - healthy),
                 U.fmt_dur(min((m["reset_in"] for m in models if m.get("reset_in")),
                               default=0))],
                ["models", "exhausted", "next reset"]):
            v.config(text=txt); c.config(text=cap)
        tips = sec.get("stat_tips") or []
        for t, txt in zip(tips, [
                "Models this plan can call.",
                "Models whose quota is currently used up.",
                "Time until the next model quota refills."]):
            t.text = txt

        self._grid_table(
            sec["table"],
            headers=["model", "used / left", "", "resets in"],
            widths=[19, 11, 0, 15],
            rows=[[self._short(m["name"], 19),
                   f"{m['used_pct']:.0f}% / {m['left_pct']:.0f}%",
                   m["used_pct"],
                   U.fmt_reset_date(m.get("reset_ts"))] for m in models],
            neutral=False)

    # -- a small aligned grid: [text][text][bar][text] --------------
    def _grid_table(self, box, headers, widths, rows, neutral):
        # Skip the full teardown/rebuild when nothing visible changed - this is
        # what stops the widget from visibly flickering every poll.
        sig = repr((headers, widths, rows, neutral))
        if getattr(box, "_grid_sig", None) == sig:
            return
        self._clear(box)
        box._grid_sig = sig
        for ci, (h, w) in enumerate(zip(headers, widths)):
            if ci == 2:
                # fixed-width bar column; the last column takes the slack so the
                # reset text on the right is never pushed off the edge
                box.grid_columnconfigure(ci, weight=0, minsize=52)
                continue
            box.grid_columnconfigure(ci, weight=(1 if ci == 3 else 0))
            lbl = tk.Label(box, text=h.upper(), bg=BG, fg=FG_FAINT,
                           font=self.f_tiny, anchor="w",
                           width=w if w else None)
            lbl.grid(row=0, column=ci, sticky="w", padx=(0, 6))
            lbl.bind("<MouseWheel>", self._wheel)
        for ri, cells in enumerate(rows[:20], start=1):
            name, mid, pct, right = cells
            if mid == "" and pct is None and right == "":     # section header
                tk.Label(box, text=name.upper(), bg=BG, fg=FG_FAINT,
                         font=self.f_tiny, anchor="w").grid(
                    row=ri, column=0, columnspan=4, sticky="w", pady=(4, 1))
                continue
            tk.Label(box, text=name, bg=BG, fg=FG_DIM, font=self.f_small,
                     anchor="w", width=widths[0]).grid(
                row=ri, column=0, sticky="w", padx=(0, 6), pady=1)
            tk.Label(box, text=mid, bg=BG, fg=FG, font=self.f_small,
                     anchor="w", width=widths[1]).grid(
                row=ri, column=1, sticky="w", padx=(0, 6))
            # explicit width keeps Tk's default 378px canvas request from
            # blowing the grid's natural width past the window
            bar = tk.Canvas(box, height=7, width=46, bg=TRACK,
                            highlightthickness=0)
            bar.grid(row=ri, column=2, sticky="ew", padx=(0, 6))
            bar.bind("<Configure>",
                     lambda e, b=bar, p=pct, n=neutral: self._paint(b, p, n))
            self._paint(bar, pct, neutral)
            tk.Label(box, text=right, bg=BG, fg=FG_DIM, font=self.f_small,
                     anchor="w").grid(row=ri, column=3, sticky="w")
        for w in box.winfo_children():
            w.bind("<MouseWheel>", self._wheel)

    def _render_kv(self, box, rows):
        self._clear(box)
        inner = tk.Frame(box, bg=CARD, padx=8, pady=5)
        inner.pack(fill="x")
        for k, v in rows:
            r = tk.Frame(inner, bg=CARD)
            r.pack(fill="x")
            is_head = (str(v) == "" and (k.startswith("—") or k.endswith("—")
                                         or k.endswith(":")))
            if is_head:
                tk.Label(r, text=k.strip("— ").upper() or "·", bg=CARD,
                         fg=FG_FAINT, font=self.f_tiny, anchor="w"
                         ).pack(side="left", pady=(3, 1))
            else:
                tk.Label(r, text=k, bg=CARD, fg=FG_FAINT, font=self.f_small,
                         width=18, anchor="w").pack(side="left")
                tk.Label(r, text=str(v), bg=CARD, fg=FG_DIM, font=self.f_small,
                         anchor="w").pack(side="left", fill="x")
        for w in [box, inner] + inner.winfo_children():
            w.bind("<MouseWheel>", self._wheel)

    # -- history window -------------------------------------------
    def _open_history(self, key):
        snap = self.snaps.get(key) or {}
        hist = snap.get("history", [])
        prov = next(p for p in self.providers if p.key == key)
        win = tk.Toplevel(self)
        win.title(f"{prov.label} — session history")
        win.configure(bg=BG)
        win.geometry("560x460")
        try:
            win.attributes("-topmost", True)
        except tk.TclError:
            pass
        tk.Label(win, text=f"  {prov.label}  ·  {len(hist)} sessions",
                 bg=CARD, fg=prov.color, font=self.f_label, anchor="w"
                 ).pack(fill="x", ipady=4)

        cv = tk.Canvas(win, bg=BG, highlightthickness=0)
        cv.pack(side="left", fill="both", expand=True)
        sb = tk.Scrollbar(win, command=cv.yview)
        sb.pack(side="right", fill="y")
        cv.configure(yscrollcommand=sb.set)
        inner = tk.Frame(cv, bg=BG)
        cv.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>",
                   lambda e: cv.configure(scrollregion=cv.bbox("all")))
        cv.bind_all("<MouseWheel>",
                    lambda e: cv.yview_scroll(int(-e.delta / 120), "units"))

        cols = [("when", 16), ("duration", 10), ("msgs", 6),
                ("tokens", 9), ("cost", 8), ("models", 22)]
        hdr = tk.Frame(inner, bg=BG)
        hdr.pack(fill="x", padx=8, pady=(6, 2))
        for name, w in cols:
            tk.Label(hdr, text=name.upper(), bg=BG, fg=FG_FAINT,
                     font=self.f_tiny, width=w, anchor="w").pack(side="left")
        for v in hist:
            row = tk.Frame(inner, bg=BG)
            row.pack(fill="x", padx=8, pady=1)
            models = ", ".join(r["name"] for r in v.get("model_rows", [])) or "—"
            vals = [U.fmt_when(v.get("last_ts")),
                    U.fmt_dur(v.get("duration_sec")),
                    str(v.get("messages", 0)),
                    U.fmt_tokens(v.get("tokens", 0)),
                    f"${v.get('cost', 0):.2f}",
                    models]
            for (name, w), val in zip(cols, vals):
                tk.Label(row, text=val, bg=BG, fg=FG_DIM, font=self.f_small,
                         width=w, anchor="w").pack(side="left")
        if not hist:
            tk.Label(inner, text="\n  no sessions on record yet\n", bg=BG,
                     fg=FG_FAINT, font=self.f_row).pack(anchor="w")

    # -- primitives ------------------------------------------------
    @staticmethod
    def _short(t, n):
        t = t or "?"
        return t if len(t) <= n else t[: n - 1] + "…"

    @staticmethod
    def _clear(box):
        for w in box.winfo_children():
            w.destroy()
        box._grid_sig = None

    @staticmethod
    def _paint(cv: tk.Canvas, pct, neutral):
        try:
            cv.delete("all")
        except tk.TclError:
            return
        w = cv.winfo_width()
        if w <= 1:                       # not laid out yet - repaint on idle
            cv.after_idle(lambda: UsageWidget._paint(cv, pct, neutral))
            return
        h = int(cv["height"])
        cv.create_rectangle(0, 0, w, h, fill=TRACK, outline="")
        if pct is None:
            return
        frac = max(0.0, min(1.0, pct / 100.0))
        if frac > 0:
            color = "#5f7fb2" if neutral else _bar_color(pct)
            fill_w = max(2, int(w * frac))
            cv.create_rectangle(0, 0, fill_w, h, fill=color, outline="")

    def _quit(self):
        self._save()
        for pl in self.pollers:
            pl.stop()
        self.destroy()


if __name__ == "__main__":
    UsageWidget().mainloop()
