"""
Providers for the floating usage widget.

Two shapes of provider, both returning the same snapshot dict:

  mode "session"  (Claude Code, Codex, custom)
      -> a "session" block: tokens / messages / duration / per-model use
         for the *current* session, plus "history" (all past sessions) and
         "details" (cost, burn rate, week totals, limits) for the expander.

  mode "quota"    (Antigravity)
      -> a "models" list with used% / left% / reset timestamp, plus
         "details" (plan, credits).

Everything is local except OpenRouter (needs a key) and custom HTTP.
"""

from __future__ import annotations

import datetime
import glob
import json
import os
import re
import ssl
import subprocess
import time
import urllib.request

CONFIG_PATH = os.path.expanduser("~/.claude-usage-widget.json")
_RETENTION_DAYS = 60
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _parse_iso(ts) -> float:
    if not isinstance(ts, str):
        try:
            return float(ts or 0.0)
        except Exception:
            return 0.0
    try:
        s = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
        return datetime.datetime.fromisoformat(s).timestamp()
    except Exception:
        return 0.0


def _pwsh(cmd: str, timeout: float = 6.0) -> str:
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
            capture_output=True, text=True, timeout=timeout,
            creationflags=_CREATE_NO_WINDOW,
        )
        return out.stdout or ""
    except Exception:
        return ""


def fmt_tokens(n) -> str:
    n = int(n or 0)
    if n >= 1_000_000_000:
        return f"{n / 1e9:.2f}B"
    if n >= 1_000_000:
        return f"{n / 1e6:.2f}M"
    if n >= 1_000:
        return f"{n / 1e3:.1f}k"
    return str(n)


def fmt_dur(seconds) -> str:
    seconds = int(max(0, seconds or 0))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h >= 24:
        d, h = divmod(h, 24)
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def fmt_clock(seconds) -> str:
    seconds = int(max(0, seconds or 0))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def fmt_reset_date(reset_ts) -> str:
    """'Thu 10 Sep'  (or 'today HH:MM' / 'tomorrow HH:MM' if close; year if not this one)."""
    if not reset_ts:
        return "—"
    dt = datetime.datetime.fromtimestamp(reset_ts)
    today = datetime.date.today()
    delta_days = (dt.date() - today).days
    if delta_days == 0:
        return f"today {dt:%H:%M}"
    if delta_days == 1:
        return "tomorrow" if dt.hour == 0 else f"tmrw {dt:%H:%M}"
    if dt.year != today.year:
        return f"{dt:%d %b %y}"
    return f"{dt:%a %d %b}"


def fmt_when(ts) -> str:
    if not ts:
        return "—"
    dt = datetime.datetime.fromtimestamp(ts)
    today = datetime.date.today()
    d = (today - dt.date()).days
    if d == 0:
        return f"today {dt:%H:%M}"
    if d == 1:
        return f"yesterday {dt:%H:%M}"
    if d < 7:
        return f"{dt:%a} {dt:%H:%M}"
    return f"{dt:%d %b} {dt:%H:%M}"


def fmt_ago(ts) -> str:
    if not ts:
        return "—"
    diff = time.time() - ts
    if diff < 60:
        return f"{int(diff)}s ago"
    if diff < 3600:
        return f"{int(diff / 60)}m ago"
    if diff < 86400:
        return f"{int(diff / 3600)}h ago"
    return f"{int(diff / 86400)}d ago"


# per-1M-token (input, output) USD. cache-write = 1.25x in, cache-read = 0.1x in
PRICING = {
    "claude-opus-4": (5.0, 25.0), "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-4": (3.0, 15.0), "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4": (1.0, 5.0), "claude-3-5-haiku": (0.80, 4.0),
    "claude-3-opus": (15.0, 75.0),
    "gpt-5": (1.25, 10.0), "gpt-5-mini": (0.25, 2.0), "gpt-5-nano": (0.05, 0.40),
    "gpt-4.1": (2.0, 8.0), "gpt-4.1-mini": (0.40, 1.60), "gpt-4o": (2.5, 10.0),
    "gpt-4o-mini": (0.15, 0.60), "o4-mini": (1.1, 4.4), "o3": (2.0, 8.0),
    "codex-mini": (1.5, 6.0),
}


def price_for(model: str):
    best = None
    m = (model or "").lower()
    for key, val in PRICING.items():
        if m.startswith(key) and (best is None or len(key) > len(best[0])):
            best = (key, val)
    return best[1] if best else None


def cost_of(model, inp, out, cw=0, cr=0) -> float:
    p = price_for(model)
    if not p:
        return 0.0
    pin, pout = p
    return (inp / 1e6 * pin + out / 1e6 * pout
            + cw / 1e6 * pin * 1.25 + cr / 1e6 * pin * 0.10)


def _short_model(name: str) -> str:
    return (name or "?").replace("claude-", "").replace("-20250929", "") \
        .replace("-latest", "")


# tags Claude Code / agents wrap around injected context that is delivered as a
# "user" turn but was never typed by a person.
_INJECTED_TAGS = (
    "system-reminder", "command-name", "command-message", "command-args",
    "local-command-stdout", "local-command-stderr", "user-prompt-submit-hook",
    "budget", "important-instruction-reminders", "system",
)
_INJECTED_RE = re.compile(
    r"<(%s)\b[^>]*>.*?</\1>" % "|".join(_INJECTED_TAGS), re.S | re.I)


def classify_user_turn(rec: dict) -> str:
    """
    'prompt'       - a message a human actually typed
    'tool_result'  - the transcript carrier for a tool's output
    'injected'     - system reminders, slash-command echoes, hook output …
    'other'        - empty / unrecognised
    """
    if rec.get("isMeta") or rec.get("isCompactSummary"):
        return "injected"
    c = (rec.get("message") or {}).get("content")
    if isinstance(c, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result"
               for b in c):
            return "tool_result"
        text = " ".join(b.get("text", "") for b in c
                        if isinstance(b, dict) and b.get("type") == "text")
    elif isinstance(c, str):
        text = c
    else:
        return "other"
    text = text.strip()
    if not text:
        return "other"
    if text.lstrip().startswith(("<command-name>", "<command-message>",
                                 "<local-command-stdout>")):
        return "injected"
    residue = re.sub(r"<[^>]+>", "", _INJECTED_RE.sub("", text)).strip()
    return "prompt" if residue else "injected"


# ---------------------------------------------------------------------------
# where a Claude Code session actually came from
# ---------------------------------------------------------------------------
# Every tool that drives Claude Code writes into ~/.claude/projects/<folder>/,
# where <folder> is the mangled working directory.  We fold those folders into
# a handful of friendly "sources" so the widget can show who spent what.
_SOURCE_RULES = [
    ("openclaw",           {"name": "OpenClaw",       "badge": "OC", "color": "#d97706"}),
    ("scratch-workspaces", {"name": "Claude Desktop", "badge": "CD", "color": "#8b5cf6"}),
    ("roaming-claude",     {"name": "Claude Desktop", "badge": "CD", "color": "#8b5cf6"}),
]
_DEFAULT_SOURCE = {"name": "Claude Code", "badge": "CC", "color": "#c98a3a"}


def friendly_source(folder: str, overrides: dict | None = None) -> dict:
    """Map a ~/.claude/projects/<folder> name to {name, badge, color}."""
    low = (folder or "").lower()
    for frag, meta in (overrides or {}).items():
        if not frag or frag.startswith("_") or not isinstance(meta, dict):
            continue
        if frag.lower() in low:
            return {"name": meta.get("name") or frag,
                    "badge": meta.get("badge") or "?",
                    "color": meta.get("color") or _DEFAULT_SOURCE["color"]}
    for frag, meta in _SOURCE_RULES:
        if frag in low:
            return dict(meta)
    return dict(_DEFAULT_SOURCE)


def _roll_future(ts: float, period: float, now: float) -> float:
    """Push a window-reset timestamp forward until it is actually in the future."""
    if not ts or period <= 0:
        return ts
    while ts <= now:
        ts += period
    return ts


def subscription_days_left(spec: dict | None) -> dict | None:
    """
    From a config block describing when the plan renews, work out how many
    whole days are left in the current billing period.

      {"renews": "2026-10-01"}   explicit next-renewal date (rolls monthly)
      {"renews_day": 1}          day-of-month the plan renews on (1-28)
    """
    if not isinstance(spec, dict):
        return None
    today = datetime.date.today()
    target = None
    raw = spec.get("renews")
    if raw:
        try:
            target = datetime.date.fromisoformat(str(raw)[:10])
        except Exception:
            target = None
        while target and target <= today:                 # roll month by month
            m, y = target.month % 12 + 1, target.year + (target.month // 12)
            day = min(target.day, [31, 29 if y % 4 == 0 and (y % 100 or not y % 400)
                                   else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30,
                                   31][m - 1])
            target = datetime.date(y, m, day)
    elif spec.get("renews_day"):
        d = max(1, min(28, int(spec["renews_day"])))
        target = today.replace(day=d)
        if target <= today:
            m, y = today.month % 12 + 1, today.year + (today.month // 12)
            target = datetime.date(y, m, d)
    if not target:
        return None
    return {"days_left": (target - today).days,
            "renews_on": target.strftime("%d %b %Y")}


# ---------------------------------------------------------------------------
# auto-detect the Claude account / plan from Claude Code's own local files
# (so the widget works with zero manual config).  Secrets are never read out -
# only the plan tier, account label and subscription dates.
# ---------------------------------------------------------------------------
_PLAN_LABELS = [
    ("max_20x", "Max 20×"), ("max20x", "Max 20×"),
    ("max_5x", "Max 5×"), ("max5x", "Max 5×"),
    ("claude_max", "Max"), ("max", "Max"),
    ("claude_pro", "Pro"), ("pro", "Pro"),
    ("team", "Team"), ("enterprise", "Enterprise"),
]


def _pretty_plan(*vals) -> str | None:
    for v in vals:
        if not v:
            continue
        low = str(v).lower()
        for frag, name in _PLAN_LABELS:
            if frag in low:
                return name
    return None


def _next_monthly(anniversary_ts: float) -> str | None:
    if not anniversary_ts:
        return None
    d0 = datetime.datetime.fromtimestamp(anniversary_ts)
    today = datetime.date.today()
    day = min(d0.day, 28)
    cand = today.replace(day=day)
    if cand <= today:
        m = today.month % 12 + 1
        y = today.year + (today.month // 12)
        cand = datetime.date(y, m, day)
    return cand.isoformat()


def detect_claude_account() -> dict:
    """Best-effort {plan, rate_tier, email, org, renews_date, trial_ends_ts}."""
    out: dict = {}
    try:
        with open(os.path.expanduser("~/.claude/.credentials.json")) as fh:
            oa = (json.load(fh) or {}).get("claudeAiOauth") or {}
        out["plan_raw"] = oa.get("subscriptionType")
        out["rate_tier"] = oa.get("rateLimitTier")
    except Exception:
        pass
    try:
        with open(os.path.expanduser("~/.claude.json")) as fh:
            acc = (json.load(fh) or {}).get("oauthAccount") or {}
        out["rate_tier"] = (out.get("rate_tier") or acc.get("userRateLimitTier")
                            or acc.get("organizationRateLimitTier"))
        out["email"] = acc.get("emailAddress")
        out["org"] = acc.get("organizationName")
        out["org_type"] = acc.get("organizationType")
        out["billing_type"] = acc.get("billingType")
        out["sub_created_ts"] = _parse_iso(acc.get("subscriptionCreatedAt")) or None
        out["trial_ends_ts"] = _parse_iso(acc.get("claudeCodeTrialEndsAt")) or None
    except Exception:
        pass
    out["plan"] = _pretty_plan(out.get("plan_raw"), out.get("org_type"),
                               out.get("rate_tier"))
    out["renews_date"] = _next_monthly(out.get("sub_created_ts"))
    return out


# ---------------------------------------------------------------------------
# base
# ---------------------------------------------------------------------------
class Provider:
    key = "base"
    label = "Base"
    color = "#888888"
    mode = "session"

    def available(self) -> bool:
        return True

    def poll(self) -> None:
        ...

    def snapshot(self) -> dict:
        return {"ok": False, "error": "not polled", "label": self.label,
                "color": self.color, "mode": self.mode, "headline": "",
                "session": None, "models": [], "history": [], "details": []}


# ===========================================================================
# session-log providers share this JSONL walker
# ===========================================================================
class _SessionLogProvider(Provider):
    """
    Subclasses implement:
        _log_glob()        -> glob pattern for the *.jsonl session files
        _ingest(session, record)   -> update the per-session accumulator
    Sessions are keyed by file path. The 'current' session is the file with the
    most recent activity that has at least one model message.
    """

    def __init__(self):
        self._sessions: dict[str, dict] = {}
        self._sizes: dict[str, int] = {}
        self._snap = super().snapshot()

    # -- file handling ------------------------------------------------
    def _blank_session(self, path: str) -> dict:
        return {"path": path, "id": os.path.basename(path).split(".")[0],
                "source": self._source_of(path),
                "first_ts": None, "last_ts": None,
                "messages": 0, "assistant_msgs": 0,
                "tool_results": 0, "injected": 0,
                "inp": 0, "out": 0, "cw": 0, "cr": 0, "cost": 0.0,
                "by_model": {}}

    # subclasses that live under ~/.claude/projects override this
    def _source_of(self, path: str) -> dict | None:
        return None

    def _model_bucket(self, sess: dict, model: str) -> dict:
        return sess["by_model"].setdefault(
            model, {"tok": 0, "cost": 0.0, "msgs": 0})

    def poll(self) -> None:
        cutoff = time.time() - _RETENTION_DAYS * 86400
        try:
            files = glob.glob(self._log_glob(), recursive=True)
        except Exception as exc:
            self._snap = {**super().snapshot(), "error": f"scan failed: {exc}"}
            return
        for path in files:
            try:
                mtime = os.path.getmtime(path)
                size = os.path.getsize(path)
            except OSError:
                continue
            if mtime < cutoff:
                continue
            if self._sizes.get(path) == size and path in self._sessions:
                continue                              # unchanged
            self._sizes[path] = size
            sess = self._blank_session(path)
            try:
                with open(path, "rb") as fh:
                    blob = fh.read()
                for line in blob.decode("utf-8", "ignore").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    self._ingest(sess, rec)
            except Exception:
                continue
            self._sessions[path] = sess
        self._snap = self._build()

    # -- snapshot assembly -----------------------------------------
    def _session_view(self, sess: dict) -> dict:
        tok = sess["inp"] + sess["out"] + sess["cw"] + sess["cr"]
        dur = 0.0
        if sess["first_ts"] and sess["last_ts"]:
            dur = max(0.0, sess["last_ts"] - sess["first_ts"])
        rows = []
        mtot = sum(v["tok"] for v in sess["by_model"].values()) or 1
        for name, v in sorted(sess["by_model"].items(),
                              key=lambda kv: -kv[1]["tok"]):
            rows.append({"name": _short_model(name), "tokens": v["tok"],
                         "pct": v["tok"] / mtot * 100, "messages": v["msgs"],
                         "cost": v["cost"]})
        return {"id": sess["id"], "tokens": tok, "messages": sess["messages"],
                "assistant_msgs": sess["assistant_msgs"],
                "tool_results": sess.get("tool_results", 0),
                "injected": sess.get("injected", 0),
                "duration_sec": dur, "started_ts": sess["first_ts"],
                "last_ts": sess["last_ts"], "cost": sess["cost"],
                "inp": sess["inp"], "out": sess["out"],
                "cw": sess["cw"], "cr": sess["cr"], "model_rows": rows,
                "source": sess.get("source"), "path": sess.get("path")}

    # subclasses may override to add rolling account-limit rows to the
    # simple view.  Each row:
    #   {"name", "configured": bool, "left_pct": float|None,
    #    "value": str, "reset_ts": float|None}
    def _limit_rows(self, span, views, now) -> list[dict]:
        return []

    # subclasses may override to add the split daily/weekly usage block
    # (this-session vs all-sessions) plus the per-source breakdown.
    def _usage_block(self, views, now) -> dict | None:
        return None

    # subclasses may override to append rows to the "more details" panel
    def _extra_details(self) -> list[tuple]:
        return []

    def _build(self) -> dict:
        views = [self._session_view(s) for s in self._sessions.values()]
        views = [v for v in views if v["tokens"] > 0 or v["messages"] > 0]
        if not views:
            return {**super().snapshot(), "ok": True, "error": None,
                    "headline": "0", "session": None}
        views.sort(key=lambda v: v["last_ts"] or 0, reverse=True)
        cur = views[0]

        now = time.time()
        n = datetime.datetime.now()
        midnight = datetime.datetime(n.year, n.month, n.day).timestamp()
        wk = now - 7 * 86400

        def span(since):
            d = {"cost": 0.0, "tok": 0, "msgs": 0}
            for v in views:
                if (v["last_ts"] or 0) >= since:
                    d["cost"] += v["cost"]
                    d["tok"] += v["tokens"]
                    d["msgs"] += v["messages"]
            return d

        today = span(midnight)
        week = span(wk)
        recent_hr = sum(v["cost"] for v in views if (v["last_ts"] or 0) >= now - 3600)

        details = [
            ("session cost (est)", f"${cur['cost']:.2f}"),
            ("input / output", f"{fmt_tokens(cur['inp'])} / {fmt_tokens(cur['out'])}"),
            ("cache write / read",
             f"{fmt_tokens(cur['cw'])} / {fmt_tokens(cur['cr'])}"),
            ("— message mix · this session —", ""),
            ("user prompts", str(cur["messages"])),
            ("tool results", str(cur.get("tool_results", 0))),
            ("system / injected", str(cur.get("injected", 0))),
            ("assistant replies", str(cur["assistant_msgs"])),
            ("—", ""),
            ("burn rate (1h)", f"${recent_hr:.2f}/hr"),
            ("today", f"${today['cost']:.2f} · {fmt_tokens(today['tok'])} · "
                      f"{today['msgs']} msg"),
            ("last 7 days", f"${week['cost']:.2f} · {fmt_tokens(week['tok'])} · "
                            f"{week['msgs']} msg"),
            ("sessions on record", str(len(views))),
        ]

        lr = self._limit_rows(span, views, now)
        details = (self._extra_details() + details
                   + [(f"limit · {r['name']}", r.get("value", "")) for r in lr])

        return {
            "ok": True, "error": None, "label": self.label, "color": self.color,
            "mode": "session",
            "headline": fmt_tokens(cur["tokens"]),
            "session": cur,
            "models": [], "history": views, "details": details,
            "limits": lr,
            "usage": self._usage_block(views, now),
        }

    def snapshot(self) -> dict:
        return self._snap


# ===========================================================================
# Claude Code
# ===========================================================================
class ClaudeCodeProvider(_SessionLogProvider):
    key = "claude_code"
    label = "CLAUDE CODE"
    color = "#c98a3a"

    def __init__(self, cfg: dict | None = None):
        super().__init__()
        cfg = cfg or {}
        self.projects_dir = os.path.expanduser(
            cfg.get("projects_dir") or "~/.claude/projects")
        lim = cfg.get("limits") or {}
        # your plan's ceilings - Anthropic does not expose them in the logs.
        # each is {"cost_usd": <n>|null, "tokens": <n>|null}
        self.limits = {
            "5-hour": lim.get("five_hour") or {},
            "today": lim.get("daily") or {},
            "this week": lim.get("weekly") or {},
        }
        # optional friendly names for ~/.claude/projects sub-folders:
        #   "sources": { "openclaw": {"name": "OpenClaw", "badge": "OC",
        #                             "color": "#d97706"} }
        self.source_overrides = cfg.get("sources") or {}
        # plan / renewal: auto-detected from Claude Code's own login files,
        # overridden by an explicit {"renews": ...} / {"renews_day": ...} block.
        self.account = detect_claude_account()
        # set false to keep your email / org out of the panel (screen-sharing)
        self.show_account = cfg.get("show_account", True)
        sub = dict(cfg.get("subscription") or {})
        if not sub.get("renews") and not sub.get("renews_day") \
                and self.account.get("renews_date"):
            sub["renews"] = self.account["renews_date"]
            sub["_auto"] = True
        self.subscription = sub

    def available(self) -> bool:
        return os.path.isdir(self.projects_dir)

    def _log_glob(self) -> str:
        return os.path.join(self.projects_dir, "**", "*.jsonl")

    def _source_of(self, path: str) -> dict:
        try:
            rel = os.path.relpath(path, self.projects_dir)
            folder = rel.replace("\\", "/").split("/")[0]
        except Exception:
            folder = ""
        return friendly_source(folder, self.source_overrides)

    def _extra_details(self) -> list[tuple]:
        a = self.account or {}
        rows = []
        if a.get("plan"):
            rows.append(("plan", a["plan"]
                         + ("  (auto-detected)" if self.subscription.get("_auto")
                            else "")))
        if a.get("rate_tier"):
            rows.append(("rate-limit tier", a["rate_tier"]))
        if self.show_account and a.get("email"):
            rows.append(("account", a["email"]))
        if self.show_account and a.get("org") and a.get("org") != a.get("email"):
            rows.append(("organisation", a["org"]))
        return rows

    def _limit_rows(self, span, views, now) -> list[dict]:
        d = datetime.datetime.fromtimestamp(now)
        midnight = datetime.datetime(d.year, d.month, d.day).timestamp()
        week_start = midnight - d.weekday() * 86400          # local Monday 00:00

        active = [v["started_ts"] for v in views
                  if (v["last_ts"] or 0) >= now - 5 * 3600 and v.get("started_ts")]
        anchor_5h = max(now - 5 * 3600, min(active)) if active else now - 5 * 3600

        plan = [
            ("5-hour", span(now - 5 * 3600),
             _roll_future(anchor_5h + 5 * 3600, 5 * 3600, now)),
            ("today", span(midnight), _roll_future(midnight + 86400, 86400, now)),
            ("this week", span(week_start),
             _roll_future(week_start + 7 * 86400, 7 * 86400, now)),
        ]
        rows = []
        for name, agg, reset_ts in plan:
            spec = self.limits.get(name) or {}
            lc, lt = spec.get("cost_usd"), spec.get("tokens")
            if lc:
                left = max(0.0, 100.0 * (1 - agg["cost"] / lc))
                rows.append({"name": name, "configured": True, "left_pct": left,
                             "value": f"${agg['cost']:.2f} / ${lc:g}",
                             "reset_ts": reset_ts})
            elif lt:
                left = max(0.0, 100.0 * (1 - agg["tok"] / lt))
                rows.append({"name": name, "configured": True, "left_pct": left,
                             "value": f"{fmt_tokens(agg['tok'])} / {fmt_tokens(lt)}",
                             "reset_ts": reset_ts})
            else:
                rows.append({"name": name, "configured": False, "left_pct": None,
                             "value": f"${agg['cost']:.2f} · {fmt_tokens(agg['tok'])}",
                             "reset_ts": reset_ts})
        return rows

    # -- split daily / weekly usage + per-source breakdown -----------
    def _usage_block(self, views, now) -> dict | None:
        if not views:
            return None
        d = datetime.datetime.fromtimestamp(now)
        midnight = datetime.datetime(d.year, d.month, d.day).timestamp()
        week_start = midnight - d.weekday() * 86400
        cur = views[0]

        daily = self.limits.get("today") or {}
        weekly = self.limits.get("this week") or {}
        d_tok, d_cost = daily.get("tokens"), daily.get("cost_usd")
        w_tok, w_cost = weekly.get("tokens"), weekly.get("cost_usd")

        def agg(since):
            t = c = 0
            for v in views:
                if (v["last_ts"] or 0) >= since:
                    t += v["tokens"]; c += v["cost"]
            return t, c

        today_tok, today_cost = agg(midnight)
        week_tok, week_cost = agg(week_start)

        def row(part_tok, part_cost, lim_tok, lim_cost):
            if lim_cost:
                return {"mid": f"${part_cost:.2f} / ${lim_cost:g}",
                        "pct": 100.0 * part_cost / lim_cost}
            if lim_tok:
                return {"mid": f"{fmt_tokens(part_tok)} / {fmt_tokens(lim_tok)}",
                        "pct": 100.0 * part_tok / lim_tok}
            return {"mid": fmt_tokens(part_tok), "pct": None}

        # per source, today + this week
        by_name: dict[str, dict] = {}
        for v in views:
            meta = v.get("source") or dict(_DEFAULT_SOURCE)
            b = by_name.setdefault(meta["name"], {
                "name": meta["name"], "badge": meta["badge"],
                "color": meta["color"], "tok_today": 0, "tok_week": 0,
                "cost_week": 0.0})
            lt = v["last_ts"] or 0
            if lt >= week_start:
                b["tok_week"] += v["tokens"]; b["cost_week"] += v["cost"]
            if lt >= midnight:
                b["tok_today"] += v["tokens"]

        def share(part, whole, limit):
            denom = limit or whole
            return (100.0 * part / denom) if denom else None

        by_source = sorted(by_name.values(), key=lambda s: -s["tok_week"])
        for s in by_source:
            s["daily_pct"] = share(s["tok_today"], today_tok, d_tok)
            s["weekly_pct"] = share(s["tok_week"], week_tok, w_tok)

        sub = subscription_days_left(self.subscription)
        if sub:
            sub["plan"] = self.account.get("plan")
            sub["auto"] = bool(self.subscription.get("_auto"))
            te = self.account.get("trial_ends_ts")
            if te and te > now:
                sub["is_trial"] = True
                sub["days_left"] = max(0, int((te - now) // 86400))
                sub["renews_on"] = datetime.datetime.fromtimestamp(te).strftime(
                    "%d %b %Y")

        return {
            "daily_reset": _roll_future(midnight + 86400, 86400, now),
            "weekly_reset": _roll_future(week_start + 7 * 86400, 7 * 86400, now),
            "subscription": sub,
            "session": {
                "daily": row(cur["tokens"], cur["cost"], d_tok, d_cost),
                "weekly": row(cur["tokens"], cur["cost"], w_tok, w_cost),
            },
            "overall": {
                "daily": row(today_tok, today_cost, d_tok, d_cost),
                "weekly": row(week_tok, week_cost, w_tok, w_cost),
            },
            "by_source": by_source,
        }

    def _ingest(self, sess: dict, rec: dict) -> None:
        t = rec.get("type")
        ts = _parse_iso(rec.get("timestamp"))
        if t == "user" and not rec.get("isSidechain"):
            kind = classify_user_turn(rec)
            if kind == "prompt":
                sess["messages"] += 1
                if ts:
                    sess["first_ts"] = sess["first_ts"] or ts
                    sess["last_ts"] = ts
            elif kind == "tool_result":
                sess["tool_results"] += 1
            elif kind == "injected":
                sess["injected"] += 1
            return
        if t != "assistant":
            return
        m = rec.get("message") or {}
        model = m.get("model") or ""
        if not model or model == "<synthetic>":
            return
        u = m.get("usage") or {}
        inp = int(u.get("input_tokens") or 0)
        out = int(u.get("output_tokens") or 0)
        cw = int(u.get("cache_creation_input_tokens") or 0)
        cr = int(u.get("cache_read_input_tokens") or 0)
        if inp == out == cw == cr == 0:
            return
        c = cost_of(model, inp, out, cw, cr)
        sess["assistant_msgs"] += 1
        sess["inp"] += inp; sess["out"] += out
        sess["cw"] += cw; sess["cr"] += cr; sess["cost"] += c
        if ts:
            sess["first_ts"] = sess["first_ts"] or ts
            sess["last_ts"] = ts
        b = self._model_bucket(sess, model)
        b["tok"] += inp + out + cw + cr
        b["cost"] += c
        b["msgs"] += 1


# ===========================================================================
# Codex CLI
# ===========================================================================
class CodexProvider(_SessionLogProvider):
    key = "codex"
    label = "CODEX"
    color = "#10a37f"

    def __init__(self, cfg: dict | None = None):
        super().__init__()
        cfg = cfg or {}
        self.home = os.path.expanduser(cfg.get("home") or "~/.codex")
        self._rate: dict = {}

    def available(self) -> bool:
        return os.path.isdir(os.path.join(self.home, "sessions"))

    def _log_glob(self) -> str:
        return os.path.join(self.home, "sessions", "**", "*.jsonl")

    def _ingest(self, sess: dict, rec: dict) -> None:
        ts = _parse_iso(rec.get("timestamp") or rec.get("ts")) or None
        payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else rec
        info = payload.get("info") if isinstance(payload.get("info"), dict) else payload
        rl = payload.get("rate_limits") or info.get("rate_limits")
        if isinstance(rl, dict):
            self._rate = {"ts": ts or time.time(), **rl}
        ptype = payload.get("type") or rec.get("type")
        if ptype in ("user_message", "message") and (payload.get("role") == "user"
                                                     or ptype == "user_message"):
            sess["messages"] += 1
            if ts:
                sess["first_ts"] = sess["first_ts"] or ts
                sess["last_ts"] = ts
        usage = (info.get("last_token_usage") or info.get("token_usage")
                 or info.get("total_token_usage"))
        if isinstance(usage, dict):
            inp = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
            out = int(usage.get("output_tokens")
                      or usage.get("completion_tokens") or 0)
            cr = int(usage.get("cached_input_tokens")
                     or usage.get("cache_read_input_tokens") or 0)
            model = (info.get("model") or payload.get("model")
                     or rec.get("model") or "")
            if inp or out:
                c = cost_of(model, inp, out, 0, cr)
                sess["assistant_msgs"] += 1
                sess["inp"] += inp; sess["out"] += out
                sess["cr"] += cr; sess["cost"] += c
                if ts:
                    sess["first_ts"] = sess["first_ts"] or ts
                    sess["last_ts"] = ts
                b = self._model_bucket(sess, model or "codex")
                b["tok"] += inp + out + cr
                b["cost"] += c
                b["msgs"] += 1

    def _limit_rows(self, span, views, now) -> list[dict]:
        # Codex records its own rolling limits in the session log.
        rows = []
        for name, k in (("5-hour", "primary"), ("this week", "secondary")):
            blk = self._rate.get(k)
            if isinstance(blk, dict) and blk.get("used_percent") is not None:
                used = float(blk["used_percent"])
                resets = blk.get("resets_in_seconds")
                if resets is None and blk.get("window_minutes"):
                    resets = blk["window_minutes"] * 60
                rows.append({
                    "name": name, "configured": True,
                    "left_pct": max(0.0, 100.0 - used),
                    "value": f"{used:.0f}% used",
                    "reset_ts": (now + resets) if resets else None,
                })
        return rows


# ===========================================================================
# Antigravity  (quota mode)
# ===========================================================================
_CSRF_RE = re.compile(r"--csrf_token[=\s]+([a-fA-F0-9\-]+)")
_SERVER_NAMES = ["language_server_windows_x64.exe",
                 "language_server_windows_arm.exe"]


class AntigravityProvider(Provider):
    key = "antigravity"
    label = "ANTIGRAVITY"
    color = "#5b8def"
    mode = "quota"

    _CTX = ssl.create_default_context()
    _CTX.check_hostname = False
    _CTX.verify_mode = ssl.CERT_NONE

    def __init__(self, cfg: dict | None = None):
        self._conn: dict | None = None
        self._conn_expiry = 0.0
        self._snap = super().snapshot()

    def available(self) -> bool:
        return os.name == "nt"

    def _discover(self):
        procs = []
        for name in _SERVER_NAMES:
            raw = _pwsh("Get-CimInstance Win32_Process -Filter \"name='%s'\" | "
                        "Select-Object ProcessId,CommandLine | "
                        "ConvertTo-Json -Depth 3" % name)
            if not raw.strip():
                continue
            try:
                data = json.loads(raw)
            except Exception:
                continue
            if isinstance(data, dict):
                data = [data]
            for it in data:
                mm = _CSRF_RE.search(it.get("CommandLine") or "")
                if it.get("ProcessId") and mm:
                    procs.append((int(it["ProcessId"]), mm.group(1)))
            if procs:
                break
        for pid, token in procs:
            raw = _pwsh("Get-NetTCPConnection -OwningProcess %d -State Listen "
                        "-ErrorAction SilentlyContinue | "
                        "Select-Object -ExpandProperty LocalPort | "
                        "ConvertTo-Json" % pid)
            ports = []
            try:
                pd = json.loads(raw) if raw.strip() else []
                ports = [pd] if isinstance(pd, int) else [int(p) for p in pd]
            except Exception:
                ports = []
            for port in ports:
                for scheme in ("http", "https"):
                    base = f"{scheme}://127.0.0.1:{port}"
                    if self._fetch(base, token) is not None:
                        return {"base": base, "token": token}
        return None

    def _fetch(self, base, token):
        url = base + "/exa.language_server_pb.LanguageServerService/GetUserStatus"
        body = json.dumps({"metadata": {"ideName": "antigravity",
                                        "extensionName": "antigravity",
                                        "locale": "en"}}).encode()
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Connect-Protocol-Version", "1")
        req.add_header("X-Codeium-Csrf-Token", token)
        try:
            with urllib.request.urlopen(req, context=self._CTX, timeout=5) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            return None

    def poll(self) -> None:
        now = time.time()
        data = None
        if self._conn and now < self._conn_expiry:
            data = self._fetch(self._conn["base"], self._conn["token"])
        if data is None:
            self._conn = self._discover()
            if self._conn:
                self._conn_expiry = now + 45
                data = self._fetch(self._conn["base"], self._conn["token"])
        if data is None:
            self._snap = {**super().snapshot(), "ok": False,
                          "error": "Antigravity IDE not running"}
            return
        self._snap = self._parse(data)

    def _parse(self, data: dict) -> dict:
        us = data.get("userStatus") or {}
        pstat = us.get("planStatus") or {}
        plan = pstat.get("planInfo") or {}
        now = time.time()

        raw = ((us.get("cascadeModelConfigData") or {})
               .get("clientModelConfigs") or [])
        dedup: dict[str, dict] = {}
        for m in raw:
            q = m.get("quotaInfo") or m.get("quota_info") or {}
            frac = q.get("remainingFraction")
            if frac is None:
                frac = q.get("remaining_fraction")
            frac = float(1.0 if frac is None else frac)
            rt = q.get("resetTime") or q.get("reset_time")
            reset_ts = _parse_iso(rt) if rt else 0.0
            name = m.get("label") or m.get("displayName") or "Unknown"
            row = {
                "name": name,
                "used_pct": max(0.0, min(100.0, (1.0 - frac) * 100)),
                "left_pct": max(0.0, min(100.0, frac * 100)),
                "reset_ts": reset_ts or None,
                "reset_in": max(0.0, reset_ts - now) if reset_ts else 0.0,
                "exhausted": bool(m.get("isExhausted")) or frac <= 0.0,
            }
            cur = dedup.get(name)
            if cur is None or row["left_pct"] < cur["left_pct"]:
                dedup[name] = row
        models = sorted(dedup.values(),
                        key=lambda x: (x["left_pct"], x["name"].lower()))

        pc = pstat.get("availablePromptCredits")
        pcx = plan.get("monthlyPromptCredits")
        fc = pstat.get("availableFlowCredits")
        fcx = plan.get("monthlyFlowCredits")
        exhausted = sum(1 for m in models if m["exhausted"])
        details = [
            ("plan", plan.get("planName") or plan.get("teamsTier") or "?"),
            ("account", us.get("email") or "—"),
            ("models exhausted", f"{exhausted} / {len(models)}"),
        ]
        if pc is not None:
            details.append(("prompt credits left",
                            f"{pc}" + (f" / {fmt_tokens(pcx)}" if pcx else "")))
        if fc is not None:
            details.append(("flow credits left",
                            f"{fc}" + (f" / {fmt_tokens(fcx)}" if fcx else "")))
        nxt = min((m["reset_in"] for m in models if m["reset_in"]), default=0.0)
        if nxt:
            details.append(("next reset", fmt_dur(nxt)))

        healthy = sum(1 for m in models if not m["exhausted"])
        return {
            "ok": True, "error": None, "label": self.label, "color": self.color,
            "mode": "quota",
            "headline": f"{healthy}/{len(models)}",
            "session": None, "models": models, "history": [], "details": details,
        }

    def snapshot(self) -> dict:
        return self._snap


# ===========================================================================
# OpenRouter  (network; off by default)  -  quota-ish (credit balance)
# ===========================================================================
class OpenRouterProvider(Provider):
    key = "openrouter"
    label = "OPENROUTER"
    color = "#7d5bed"
    mode = "quota"

    def __init__(self, cfg: dict | None = None):
        cfg = cfg or {}
        self.api_key = (cfg.get("api_key") or os.environ.get("OPENROUTER_API_KEY")
                        or "")
        self._snap = super().snapshot()

    def available(self) -> bool:
        return bool(self.api_key)

    def poll(self) -> None:
        try:
            req = urllib.request.Request(
                "https://openrouter.ai/api/v1/credits",
                headers={"Authorization": f"Bearer {self.api_key}"})
            with urllib.request.urlopen(req, timeout=8) as r:
                d = json.loads(r.read().decode("utf-8")).get("data", {})
        except Exception as exc:
            self._snap = {**super().snapshot(), "ok": False,
                          "error": f"api error: {exc}"}
            return
        total = float(d.get("total_credits") or 0.0)
        used = float(d.get("total_usage") or 0.0)
        left = total - used
        pct_used = (used / total * 100) if total else 0.0
        self._snap = {
            "ok": True, "error": None, "label": self.label, "color": self.color,
            "mode": "quota", "headline": f"${left:.0f}",
            "session": None,
            "models": [{"name": "credit balance", "used_pct": pct_used,
                        "left_pct": 100 - pct_used, "reset_ts": None,
                        "reset_in": 0.0, "exhausted": left <= 0}],
            "history": [],
            "details": [("purchased", f"${total:.2f}"),
                        ("used", f"${used:.2f}"),
                        ("remaining", f"${left:.2f}")],
        }

    def snapshot(self) -> dict:
        return self._snap


# ===========================================================================
# Custom JSONL provider (session mode)
# ===========================================================================
class CustomJsonlProvider(_SessionLogProvider):
    def __init__(self, cfg: dict):
        super().__init__()
        self.key = cfg.get("key") or "custom"
        self.label = (cfg.get("label") or self.key).upper()
        self.color = cfg.get("color") or "#9aa0b4"
        self._glob = os.path.expanduser(cfg.get("glob") or "")
        self.f_ts = cfg.get("ts_field") or "timestamp"
        self.f_model = cfg.get("model_field") or "model"
        self.f_in = cfg.get("input_field") or "usage.input_tokens"
        self.f_out = cfg.get("output_field") or "usage.output_tokens"
        self.f_cr = cfg.get("cache_read_field") or ""
        self.f_role = cfg.get("role_field") or "role"

    def available(self) -> bool:
        return bool(self._glob) and bool(glob.glob(self._glob, recursive=True))

    def _log_glob(self) -> str:
        return self._glob

    @staticmethod
    def _dig(obj, path):
        cur = obj
        for part in (path or "").split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return None
        return cur

    def _ingest(self, sess: dict, rec: dict) -> None:
        ts = _parse_iso(self._dig(rec, self.f_ts)) or None
        if self._dig(rec, self.f_role) == "user":
            sess["messages"] += 1
            if ts:
                sess["first_ts"] = sess["first_ts"] or ts
                sess["last_ts"] = ts
        inp = self._dig(rec, self.f_in) or 0
        out = self._dig(rec, self.f_out) or 0
        try:
            inp, out = int(inp), int(out)
        except Exception:
            return
        if not inp and not out:
            return
        cr = 0
        if self.f_cr:
            try:
                cr = int(self._dig(rec, self.f_cr) or 0)
            except Exception:
                cr = 0
        model = self._dig(rec, self.f_model) or self.key
        c = cost_of(model, inp, out, 0, cr)
        sess["assistant_msgs"] += 1
        sess["inp"] += inp; sess["out"] += out
        sess["cr"] += cr; sess["cost"] += c
        if ts:
            sess["first_ts"] = sess["first_ts"] or ts
            sess["last_ts"] = ts
        b = self._model_bucket(sess, model)
        b["tok"] += inp + out + cr
        b["cost"] += c
        b["msgs"] += 1


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------
_BUILTINS = {
    "claude_code": ClaudeCodeProvider,
    "antigravity": AntigravityProvider,
    "codex": CodexProvider,
    "openrouter": OpenRouterProvider,
}


def build_providers(config: dict | None = None) -> list[Provider]:
    config = config or load_config()
    pcfg = config.get("providers") or {}
    out: list[Provider] = []
    for key, cls in _BUILTINS.items():
        entry = pcfg.get(key, {})
        if entry.get("enabled") is False:
            continue
        try:
            inst = cls(entry)
        except Exception:
            continue
        if entry.get("enabled") is True or inst.available():
            out.append(inst)
    for entry in (pcfg.get("custom") or []):
        if entry.get("enabled") is False:
            continue
        try:
            inst = CustomJsonlProvider(entry)
            if entry.get("enabled") is True or inst.available():
                out.append(inst)
        except Exception:
            continue
    return out


if __name__ == "__main__":
    for p in build_providers():
        p.poll()
        print("=" * 64)
        print(json.dumps(p.snapshot(), indent=2, default=str)[:3000])
