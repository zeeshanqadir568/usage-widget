# Live Usage Widget — Claude Code + Antigravity + anything else

A small, frameless window you park next to your editor. Simple by default; the
detail is one click away.

| Default view | ▾ more details | Minimized strip |
|---|---|---|
| ![main](docs/widget-main.png) | ![details](docs/widget-details.png) | ![minimized](docs/widget-min.png) |

**Visibility.** By default it behaves like an IDE plugin: it is shown **only
while the Antigravity IDE window is focused**, slides out of the way when you
switch to another app, and **quits when you close the Antigravity IDE**. Switch
to plain always-on-top from the right-click menu → **Visibility → Always
visible** (or set `"visibility": "always"` in the config).

The **plan tier** (Pro / Max …) and **days left in the billing period** are
auto-detected from Claude Code's own login files — no configuration needed. The
`by source` rows attribute usage to every place Claude Code runs on the machine
(OpenClaw, Claude Desktop, plain Claude Code, …).

---

## What each agent shows

### Session agents — Claude Code, Codex, custom

The default view is deliberately minimal — **this session only**:

| Stat | Meaning |
|---|---|
| **tokens · this session** | every token *this session* used (input + output + cache) |
| **msgs · this session** | how many prompts **a human actually typed** this session — tool-result carriers, system reminders, slash-command echoes and hook output are classified out, not counted |
| **session time** | live clock since **the Antigravity IDE was launched** (read from the IDE process's own start time). It does *not* reset when Claude Code opens a new chat log, and it keeps counting even if the widget itself is restarted — it only returns to `0:00` when the **IDE** is restarted. In `always` visibility mode with no IDE running it falls back to the widget's own start time. |

Every stat in the top strip is scoped to the **current session**. Cumulative
figures live in the **usage** table below and under **▾ more details**.

Then two small tables:

- **models** — each model you used this session · its tokens · its message count.
- **usage** —
  - **plan · <tier>** · *renews in Nd* — the plan tier (Pro / Max …), days left
    in the billing period and the renewal date. **Auto-detected** from Claude
    Code's own login files (`~/.claude/.credentials.json`, `~/.claude.json`) —
    no config needed. Only the plan tier, account label and subscription dates
    are read; tokens/secrets never are. Override with a `subscription` block if
    the guessed date is wrong. Shows *trial ends in Nd* instead while a Claude
    Code trial is active. Plan tier, rate-limit tier and account also appear
    under **▾ more details**.
  - **this session** · `daily` + `weekly` — how much *this* session has spent
    against each ceiling. Same token count, two bars, because the daily and
    weekly ceilings differ.
  - **all sessions** · `weekly` — every session combined for the week. (There is
    no "all sessions / daily" row — every past session already rolls up into the
    weekly window, so it added noise.)
  - **by source · share of limit** — one row per place Claude Code runs on this
    PC (each `~/.claude/projects/<folder>` folds into a named source — e.g.
    **OpenClaw**, **Claude Desktop**, plain **Claude Code**), showing its tokens
    this week and its share of the **daily** and **weekly ceilings**. With
    ceilings set, the weekly share is naturally smaller than the daily one;
    without them it falls back to share-of-total (which coincides on the day the
    weekly window resets).

  Each row shows the reset date. Set your plan's ceilings in the config (see
  below) and the middle column becomes **`used / limit`** with a bar that turns
  amber past 60% and red past 85%. The `5-hour` window lives under
  **▾ more details**.

- **▾ more details** — session cost (est.), input/output split, cache write/read,
  a **message-mix** breakdown (user prompts · tool results · system/injected ·
  assistant replies) so you can see what the traffic was actually made of,
  burn rate ($/hr, last hour), today's total, last-7-days total, session count,
  plus the auto-detected plan / rate-limit tier / account.
  For Codex, the 5-hour and weekly rate-limit % come straight from its own logs.
- **history ›** — a window listing **every past session** with the same columns
  (when · duration · msgs · tokens · cost · models).

Data comes from the agent's own local logs
(`~/.claude/projects/**/*.jsonl`, `~/.codex/sessions/**/*.jsonl`, …). Nothing
leaves your machine.

### Quota agents — Antigravity, OpenRouter

These don't have "sessions" — they have per-model allowances that refill on a
cycle, so the view is a table:

| Column | Meaning |
|---|---|
| **model** | the model name |
| **used / left** | % of that model's quota consumed / remaining |
| bar | green → amber → red as it drains; red = exhausted |
| **resets in** | the calendar date the quota recycles, e.g. `Mon, 14 Sep 2026` |

Top stats: models available · models exhausted · time to next reset.
**▾ more details** shows plan tier, prompt/flow-credit balances, account.

Antigravity data is read from the IDE's local Language Server (the private
Connect RPC the "Antigravity Token Usage" extension uses — found by scanning for
`language_server_windows_*.exe` and its CSRF token; auto-rediscovers when the IDE
restarts). OpenRouter is the **only** networked provider and is off until you add
a key.

---

## Controls

| Action | How |
|---|---|
| Move | drag anywhere on the body |
| **Resize** | drag the striped corner grip, **or** right-click → **Size** (Compact / Small / Medium / Large / Tall / Cycle) |
| **Minimize** | `‒` in the title bar, or double-click the title bar — collapses the window to just its title strip so you can park it out of the way; click `□` (same spot) or double-click again to restore |
| **Minimized-strip width** | while collapsed, drag the `⇔` grip on the strip's left edge, **or** right-click → **Minimized width** (Tiny / Short / Medium / Match window). Persists. |
| **Find the minimized strip** | hover it for ~2 s — the accent line shimmers through colours until you move away |
| Scroll | mouse-wheel |
| Opacity | `Ctrl` + mouse-wheel, or right-click → Opacity |
| Refresh now | `↻` in the title bar (all title-bar buttons are the same size / weight) |
| Open config | `⚙` in the title bar |
| Visibility mode | right-click → Visibility |
| More details | `▾ more details` under each section (remembered per agent) |
| History | `history ›` under each session agent |
| Close | `✕` |

Every title-bar button is the same size/weight and shows a tooltip on hover.
Window size, position, opacity, visibility mode, collapsed state + minimized
width, and which sections are expanded persist to `~/.claude-usage-widget.json`.

---

## Run it

**Silent:** double-click **`run-widget.vbs`**
**Or:** `run-widget.bat` · **Or:** `pythonw usage_widget.pyw`

Python 3.11+ with Tkinter (stock python.org build). No `pip install`.
**Start with Windows:** `Win+R` → `shell:startup` → shortcut to `run-widget.vbs`.

---

## Config — `~/.claude-usage-widget.json`

`providers` controls what appears. Each built-in auto-activates when its data is
present; set `"enabled": false` to hide one, `"enabled": true` to force it.

```jsonc
"visibility": "antigravity",      // or "always"
"providers": {
  "claude_code": {
    "enabled": true, "poll_seconds": 5,
    "limits": {                   // fill in your plan's ceilings to get % + bar
      "five_hour": { "cost_usd": null, "tokens": null },
      "daily":     { "cost_usd": null, "tokens": null },
      "weekly":    { "cost_usd": null, "tokens": null }
    },
    "sources": {                  // optional: rename ~/.claude/projects folders
      "openclaw":  { "name": "OpenClaw", "badge": "OC", "color": "#d97706" }
    },
    "subscription": {             // optional — auto-detected from Claude's login
      "renews": "2026-10-01"      //   files; set this only to correct the date.
      // "renews_day": 1          //   day-of-month the plan renews on (1-28)
    }
  },
  "antigravity": { "enabled": true,  "poll_seconds": 30 },
  "codex":       { "poll_seconds": 10 },          // auto once ~/.codex/sessions exists
  "openrouter":  { "enabled": false, "api_key": "" },
  "custom": [
    {
      "enabled": true,
      "key": "myagent", "label": "My Agent", "color": "#cc7722",
      "glob": "~/.myagent/logs/*.jsonl",
      "ts_field": "timestamp", "model_field": "model", "role_field": "role",
      "input_field": "usage.input_tokens", "output_field": "usage.output_tokens",
      "cache_read_field": ""
    }
  ]
}
```

A **custom** provider treats each matching `.jsonl` file as one session, counts
records where `role_field == "user"` as messages, and sums the token fields
(dotted paths allowed). It then gets the same simple view, "more details" and
"history" as Claude Code.

Cost figures are **estimates** — exact token counts, priced by the `PRICING`
table at the top of `usage_sources.py` (Claude + OpenAI models; cache-read 0.1×,
cache-write 1.25×). Unknown models are counted but priced at $0.

---

## Files

| File | Purpose |
|---|---|
| `usage_widget.pyw` | window: layout, rendering, drag/resize/scroll, tooltips, history window |
| `usage_sources.py` | providers — one class per agent, all returning the same snapshot shape |
| `run-widget.vbs` / `.bat` | launchers |
| `~/.claude-usage-widget.json` | saved window state + provider config |

---

## Troubleshooting

- **Antigravity: "not running"** — open the IDE; it's re-found automatically.
- **Codex section missing** — appears once Codex has written a session log to
  `~/.codex/sessions/`; force with `"enabled": true`.
- **Can't resize** — use the striped bottom-right grip, or right-click → Size.
- **Widget off-screen / wrong size** — delete `~/.claude-usage-widget.json`
  (back up your `providers` block first).
- **Wrong cost** — edit `PRICING` in `usage_sources.py`.
