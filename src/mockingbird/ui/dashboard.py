"""Call journal, analytics and live dashboard for mockingbird.

Two pages, switched with tab, in the design language of the user's own tools:
  Live       nodder's dashboard: header totals, a braille chart, a table with sparklines, the recent log.
  Analytics  gotodo's analytics page: rounded stat tiles, then titled sections with bars.
Palette, tab chips and the key-hint footer follow gotodo (Tokyo Night); dates are DD/MM/YYYY.

Every call the server answers goes into a SQLite journal (~/.local/state/mockingbird/calls.db), so history and
totals survive restarts and add up across projects, like nodder's decision log. Only metadata is kept: time,
model, source, duration, status, project, and a label naming tools or text length. Never prompts or answers.
Stdlib curses and sqlite3 only. Everything above the drawing section is pure, so the self-check tests it.
"""
from __future__ import annotations

import collections
import curses
import json
import os
import re
import shlex
import sqlite3
import textwrap
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence
from urllib.parse import urlsplit

DEFAULT_DB = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "mockingbird" / "calls.db"
#: One answer cache for every project, beside the journal, so it never depends on where the server started.
CACHE_DIR = DEFAULT_DB.parent / "cache"
SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY, at REAL NOT NULL, model TEXT, api TEXT, source TEXT,
    seconds REAL, status INTEGER, label TEXT, project TEXT
);
CREATE INDEX IF NOT EXISTS calls_at ON calls (at);
"""

#: Recent calls kept on screen.
RECENT = 10
#: (label, seconds covered, chart buckets). `w` cycles through them.
WINDOWS = (("60 min", 3600, 60), ("24 h", 86400, 96), ("7 days", 7 * 86400, 84))
#: Buckets behind each model's sparkline.
TREND_BUCKETS = 18
#: Header chart height bounds; it grows into spare vertical space.
CHART_MIN_HEIGHT, CHART_MAX_HEIGHT = 3, 10
CHART_MIN_WIDTH = 24
#: Days on the Analytics throughput line (gotodo uses 14).
THROUGHPUT_DAYS = 14
#: Seconds between snapshot rebuilds (journal reads happen off the drawing thread).
REFRESH = 1.0
#: Columns kept free on the right: multiplexer sidebars (herdr) can steal a few from the reported width.
MARGIN = 3
PAGES = ("Live", "Analytics", "Cache")

FRESH = ("FRESH", "AGENT", "LIVE")  # answered just now: auto CLI, a subagent, or the real API (--real)


@dataclass
class Call:
    at: float
    model: str
    api: str
    source: str  # CACHE | FRESH | AGENT | ERROR
    seconds: float
    status: int
    label: str
    project: str
    key: str = ""  # the request's cache key, so replays can be counted per saved answer
    answered_by: str = ""


class Journal:
    """Every call, on disk. Written from request threads, read by the screen and `mockingbird stats`."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else DEFAULT_DB
        self._lock = threading.Lock()
        self._ready = False
        self._inflight: dict[str, tuple[str, str, float]] = {}

    def _connect(self) -> sqlite3.Connection:
        # One connection per operation: request threads write concurrently and connections can't be shared.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10.0)
        if not self._ready:
            conn.executescript(SCHEMA)
            with conn:
                conn.execute("BEGIN IMMEDIATE")  # more than one bridge can open the journal
                columns = {row[1] for row in conn.execute("PRAGMA table_info(calls)")}
                for name in ("key", "answered_by"):
                    if name not in columns:
                        conn.execute(f"ALTER TABLE calls ADD COLUMN {name} TEXT DEFAULT ''")
            self._ready = True
        return conn

    def start(self, rid: str, model: str, api: str) -> None:
        with self._lock:
            self._inflight[rid] = (model, api, time.time())

    def record(self, rid: str | None, call: Call) -> None:
        with self._lock:
            self._inflight.pop(rid or "", None)
        conn = self._connect()
        try:
            with conn:
                conn.execute("INSERT INTO calls (at, model, api, source, seconds, status, label, project, key, answered_by)"
                             " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             (call.at, call.model, call.api, call.source, call.seconds, call.status, call.label,
                               call.project, call.key, call.answered_by))
        finally:
            conn.close()

    def inflight(self) -> list[tuple[str, str, float]]:
        with self._lock:
            return list(self._inflight.values())

    def calls(self) -> list[Call]:
        # shortcut: reads the whole journal each refresh; fine to ~100k calls, then move the analytics into SQL.
        if not self.path.exists():
            return []
        conn = self._connect()
        try:
            rows = conn.execute("SELECT at, model, api, source, seconds, status, label, project, COALESCE(key, ''),"
                                " COALESCE(answered_by, '')"
                                " FROM calls ORDER BY at").fetchall()
        finally:
            conn.close()
        return [Call(*row) for row in rows]


# --- answer cache (reads the folder the server writes) ---------------------

@dataclass
class CacheEntry:
    key: str
    saved: float
    model: str
    project: str
    asked: str
    label: str
    answer: str  # readable form of the saved answer
    size: int
    used: int = 0  # times it was replayed
    last_used: float | None = None
    legacy: bool = False  # saved before v1.7: the file holds only the answer
    via: str = ""  # set for answers from the real API (serve --real): where they came from
    tokens_in: int | None = None
    tokens_out: int | None = None
    answered_by: str = "Unknown"


def standin_name(command: str) -> str:
    """Use the saved executable and an explicit model only, never the app's requested model."""
    if not isinstance(command, str):
        return "Unknown"
    if command == "agent":
        return "Subagent"
    try:
        argv = shlex.split(command)
    except ValueError:
        return "Unknown"
    if not argv:
        return "Unknown"
    cli = Path(argv[0]).name
    name = {"claude": "Claude Code", "codex": "Codex", "opencode": "OpenCode"}.get(cli)
    if name is None:
        return "Custom stand-in"
    for i, arg in enumerate(argv):
        flag, _, model = arg.partition("=")
        if flag in ("-m", "--model"):
            model = model or (argv[i + 1] if i + 1 < len(argv) else "")
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/#-]{0,79}", model):
                if cli == "claude" and model in ("sonnet", "opus", "haiku"):
                    model = model.title()
                return f"{name} ({model})"
            break
    return name


def _http_events(ctype: str, body: str):
    chunks = (line[5:].strip() for line in body.splitlines() if line.startswith("data:")) \
        if "event-stream" in ctype else (body,)
    for chunk in chunks:
        try:
            event = json.loads(chunk)
        except (TypeError, ValueError):
            continue
        if isinstance(event, dict):
            yield event


def real_answerer(base: str, ctype: str, body: str) -> str:
    try:
        host = (urlsplit(base).hostname or "") if isinstance(base, str) else ""
    except ValueError:
        host = ""
    provider = ("Azure" if host.endswith(".azure.com") else
                "OpenAI" if host == "api.openai.com" else "Anthropic" if host == "api.anthropic.com" else "")
    label = f"Real API ({provider})" if provider else "Real API"
    for event in _http_events(ctype, body):
        for obj in (event, event.get("message"), event.get("response")):
            model = obj.get("model") if isinstance(obj, dict) else None
            if isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/#-]{0,79}", model):
                return f"{label} · {model}"
    return label


def cached_answer(data: dict) -> dict:
    """v1.7 files are {"meta", "answer"}; older ones are the bare answer."""
    return data["answer"] if "answer" in data and "meta" in data else data


def http_answer_text(ctype: str, body: str) -> str:
    """Readable text of a real API response (JSON or streamed), for the Cache tab."""
    if "event-stream" in ctype:
        parts = []
        for line in body.splitlines():
            if not line.startswith("data:"):
                continue
            try:
                event = json.loads(line[5:].strip())
            except ValueError:
                continue
            delta = event.get("delta")
            if isinstance(delta, dict) and delta.get("type") == "text_delta":
                parts.append(delta.get("text", ""))
            for choice in event.get("choices") or []:
                parts.append((choice.get("delta") or {}).get("content") or "")
        return "".join(parts) or "(streamed response)"
    try:
        data = json.loads(body)
    except ValueError:
        return body[:2000]
    if data.get("object") == "response" and isinstance(data.get("output"), list):
        return "\n".join(part.get("text", "") for item in data["output"] if isinstance(item, dict)
                         for part in item.get("content", []) if isinstance(part, dict)
                         and part.get("type") == "output_text")
    if isinstance(data.get("content"), list):  # Anthropic
        out = [b.get("text", "") for b in data["content"] if b.get("type") == "text"]
        out += [f"→ calls {b.get('name')}({json.dumps(b.get('input', {}))})"
                for b in data["content"] if b.get("type") == "tool_use"]
        return "\n".join(o for o in out if o)
    for choice in data.get("choices") or []:  # OpenAI and Azure
        message = choice.get("message") or {}
        out = [message.get("content") or ""]
        out += [f"→ calls {t['function']['name']}({t['function'].get('arguments', '')})"
                for t in message.get("tool_calls") or [] if "function" in t]
        return "\n".join(o for o in out if o)
    return json.dumps(data)[:2000]


def answer_text(ans: dict) -> str:
    if "raw" in ans:
        return json.dumps(ans["raw"])[:2000]
    parts = [ans.get("text") or ""]
    parts += [f"→ calls {t.get('name')}({json.dumps(t.get('input', {}))})" for t in ans.get("tool_calls") or []]
    return "\n".join(p for p in parts if p)


def cached_tokens(data: dict) -> tuple[int | None, int | None]:
    """Original response usage, not new usage incurred by a cache replay."""
    http = data.get("http")
    if isinstance(http, dict):
        body = http.get("body", "")
        events = []
        if "event-stream" in http.get("content_type", ""):
            for line in body.splitlines():
                if line.startswith("data:"):
                    try:
                        events.append(json.loads(line[5:].strip()))
                    except ValueError:
                        pass
        else:
            try:
                events.append(json.loads(body))
            except (ValueError, TypeError):
                pass
        usage = {}
        for event in events:
            if not isinstance(event, dict):
                continue
            message = event.get("message")
            for candidate in ((message.get("usage") if isinstance(message, dict) else None), event.get("usage")):
                if isinstance(candidate, dict):
                    usage.update(candidate)
    else:
        answer = cached_answer(data)
        usage = answer.get("usage", {}) if isinstance(answer, dict) else {}
    if not isinstance(usage, dict):
        return None, None
    def count(*keys):
        for key in keys:
            value = usage.get(key)
            if type(value) is int and value >= 0:
                return value
        return None
    prompt = count("prompt_tokens", "input_tokens")
    if prompt is not None and "prompt_tokens" not in usage:
        prompt += (count("cache_read_input_tokens") or 0) + (count("cache_creation_input_tokens") or 0)
    return prompt, count("completion_tokens", "output_tokens")


def cache_entries(folder: Path | None, calls: Sequence[Call]) -> list[CacheEntry]:
    """Every saved answer, newest first, with how often each was replayed (from the journal)."""
    # shortcut: reads every cache file each refresh; fine to a few thousand answers, then index them.
    if not folder or not folder.is_dir():
        return []
    used: collections.Counter[str] = collections.Counter()
    last: dict[str, float] = {}
    for c in calls:
        if c.key and c.source == "CACHE":
            used[c.key] += 1
            last[c.key] = c.at
    out = []
    for f in folder.glob("*.json"):
        try:
            data = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        meta = data.get("meta") if ("answer" in data or "http" in data) else None
        http = data.get("http")
        tokens_in, tokens_out = cached_tokens(data)
        info = meta or {}
        answered_by = (real_answerer(info.get("via", ""), http["content_type"], http["body"]) if http else
                       standin_name(info.get("backend") or ""))
        out.append(CacheEntry(
            key=f.stem, saved=(meta or {}).get("saved") or f.stat().st_mtime, model=(meta or {}).get("model", "—"),
            project=(meta or {}).get("project", "—"), asked=(meta or {}).get("asked", "(saved before v1.7)"),
            label=(meta or {}).get("label", ""),
            answer=http_answer_text(http["content_type"], http["body"]) if http else answer_text(cached_answer(data)),
            size=f.stat().st_size, used=used[f.stem], last_used=last.get(f.stem), legacy=meta is None,
            via=info.get("via", "") if http else "", tokens_in=tokens_in, tokens_out=tokens_out,
            answered_by=answered_by))
    return sorted(out, key=lambda e: -e.saved)


def _size(n: int) -> str:
    return f"{n} B" if n < 1024 else f"{n / 1024:.1f} KB" if n < 1024 ** 2 else f"{n / 1024 ** 2:.1f} MB"


def cache_report(folder: Path | None, calls: Sequence[Call]) -> str:
    """`mockingbird cache`: the saved answers as plain text."""
    entries = cache_entries(folder, calls)
    lines = [f"mockingbird cache · {len(entries)} saved answers · {_size(sum(e.size for e in entries))} · {folder}"]
    for e in entries:
        lines.append(f"  {time.strftime('%d/%m/%Y %H:%M', time.localtime(e.saved))}  {e.model[:20]:<20} "
                     f"used {e.used:>3}×  {e.asked[:50]:<50}  → {(e.label or e.answer)[:30]}")
    return "\n".join(lines)


# --- analytics (pure) ------------------------------------------------------

@dataclass
class ModelStats:
    model: str
    calls: int
    cached: int
    fresh: int
    errors: int
    mean_fresh: float | None


@dataclass
class Analytics:
    total: int = 0
    cached: int = 0
    fresh: int = 0
    errors: int = 0
    hit_rate: float | None = None
    saved: float | None = None  # seconds a fresh answer would have taken for every cached one
    p50: float | None = None
    p95: float | None = None
    slowest: float | None = None
    error_kinds: list[tuple[int, int]] = field(default_factory=list)
    error_rows: list[tuple[int, int, str, float]] = field(default_factory=list)  # status, count, last label, last at
    models: list[tuple[str, int]] = field(default_factory=list)
    per_model: list[ModelStats] = field(default_factory=list)
    projects: list[tuple[str, int]] = field(default_factory=list)
    daily: list[tuple[float, int, int]] = field(default_factory=list)  # day start, cached, fresh + errors
    first_at: float | None = None


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def _day_start(at: float) -> float:
    t = time.localtime(at)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def analyse(calls: Sequence[Call], now: float | None = None) -> Analytics:
    now = time.time() if now is None else now
    a = Analytics(total=len(calls))
    today = _day_start(now)
    days = [_day_start(today - (THROUGHPUT_DAYS - 1 - i) * 86400 + 3600 * 12) for i in range(THROUGHPUT_DAYS)]
    a.daily = [(d, 0, 0) for d in days]
    if not calls:
        return a
    fresh_by_model: dict[str, list[float]] = collections.defaultdict(list)
    for c in calls:
        if c.source in FRESH:
            fresh_by_model[c.model].append(c.seconds)
    fresh = [s for secs in fresh_by_model.values() for s in secs]
    a.cached = sum(c.source == "CACHE" for c in calls)
    a.fresh = len(fresh)
    a.errors = sum(c.source == "ERROR" for c in calls)
    a.hit_rate = a.cached / (a.cached + a.fresh) if a.cached + a.fresh else None
    mean = {m: sum(s) / len(s) for m, s in fresh_by_model.items()}
    if fresh:
        overall = sum(fresh) / len(fresh)
        a.saved = max(0.0, sum(mean.get(c.model, overall) - c.seconds for c in calls if c.source == "CACHE"))
        a.p50, a.p95, a.slowest = _quantile(fresh, 0.5), _quantile(fresh, 0.95), max(fresh)

    errors = [c for c in calls if c.source == "ERROR"]
    a.error_kinds = collections.Counter(c.status for c in errors).most_common(3)
    last_error: dict[int, Call] = {c.status: c for c in errors}
    a.error_rows = [(s, n, last_error[s].label, last_error[s].at)
                    for s, n in collections.Counter(c.status for c in errors).most_common(5)]
    a.models = collections.Counter(c.model for c in calls).most_common()
    a.per_model = [ModelStats(m, n, sum(c.source == "CACHE" for c in calls if c.model == m), len(fresh_by_model[m]),
                              sum(c.source == "ERROR" for c in calls if c.model == m), mean.get(m))
                   for m, n in a.models]
    a.projects = collections.Counter(c.project for c in calls).most_common(5)
    index = {d: i for i, d in enumerate(days)}
    daily = [[d, 0, 0] for d in days]
    for c in calls:
        i = index.get(_day_start(c.at))
        if i is not None:
            daily[i][1 if c.source == "CACHE" else 2] += 1
    a.daily = [(d, cached, other) for d, cached, other in daily]
    a.first_at = calls[0].at
    return a


def duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m {int(seconds % 60):02d}s"
    return f"{int(seconds // 3600)}h {int(seconds % 3600 // 60):02d}m"


def day(at: float | None) -> str:
    return time.strftime("%d/%m/%Y", time.localtime(at)) if at else "—"


def report(journal: Journal) -> str:
    """`mockingbird stats`: the analytics as plain text."""
    a = analyse(journal.calls())
    if not a.total:
        return f"mockingbird · no calls recorded yet ({journal.path})"
    lines = [f"mockingbird · {a.total:,} calls since {day(a.first_at)}"]
    hit = f"({a.hit_rate:.0%})" if a.hit_rate is not None else ""
    lines.append(f"  cached    {a.cached:>7,} {hit:<7} ~{duration(a.saved)} saved")
    lines.append(f"  fresh     {a.fresh:>7,}         median {duration(a.p50)} · p95 {duration(a.p95)}"
                 f" · slowest {duration(a.slowest)}")
    kinds = " · ".join(f"{status} ×{n}" for status, n in a.error_kinds)
    lines.append(f"  errors    {a.errors:>7,}         {kinds}")
    lines.append("  models    " + " · ".join(f"{m} {n:,}" for m, n in a.models[:4]))
    lines.append("  projects  " + " · ".join(f"{p} {n:,}" for p, n in a.projects))
    lines.append(f"  journal   {journal.path}")
    return "\n".join(lines)


# --- snapshot (pure) -------------------------------------------------------

@dataclass
class Row:
    model: str
    api: str
    state: str  # asking | error | idle
    calls: int = 0
    cached: int = 0
    fresh: int = 0
    errors: int = 0
    avg: float | None = None
    asking_for: float = 0.0
    trend: list[int] = field(default_factory=list)


@dataclass
class Snapshot:
    window: str = WINDOWS[0][0]
    rows: list[Row] = field(default_factory=list)
    recent: list[Call] = field(default_factory=list)
    cached_series: list[int] = field(default_factory=list)
    fresh_series: list[int] = field(default_factory=list)  # fresh answers and errors: the calls that cost time
    in_window: int = 0
    asking: int = 0
    stats: Analytics = field(default_factory=Analytics)
    entries: list[CacheEntry] = field(default_factory=list)
    asked: dict[str, str] = field(default_factory=dict)  # cache key -> what was asked


def activity(calls: Sequence[Call], since: float, until: float, buckets: int) -> list[int]:
    out = [0] * buckets
    span = (until - since) / buckets
    for c in calls:
        if since <= c.at < until:
            out[min(buckets - 1, int((c.at - since) / span))] += 1
    return out


def build(calls: Sequence[Call], inflight: Sequence[tuple[str, str, float]], now: float, window: int = 0) -> Snapshot:
    label, span, buckets = WINDOWS[window]
    since = now - span
    recent_window = [c for c in calls if c.at >= since]

    rows: dict[tuple[str, str], Row] = {}
    last_source: dict[tuple[str, str], str] = {}
    for c in calls:  # every model ever seen keeps a row, like nodder keeps every pane
        rows.setdefault((c.model, c.api), Row(c.model, c.api, "idle"))
        if c.at >= since:  # an old failure must not pin a model to "error" forever
            last_source[(c.model, c.api)] = c.source
    for model, api, started in inflight:
        r = rows.setdefault((model, api), Row(model, api, "idle"))
        r.state, r.asking_for = "asking", max(r.asking_for, now - started)
    for key, r in rows.items():
        mine = [c for c in recent_window if (c.model, c.api) == key]
        r.calls = len(mine)
        r.cached = sum(c.source == "CACHE" for c in mine)
        r.errors = sum(c.source == "ERROR" for c in mine)
        fresh = [c.seconds for c in mine if c.source in FRESH]
        r.fresh, r.avg = len(fresh), (sum(fresh) / len(fresh) if fresh else None)
        r.trend = activity(mine, since, now, TREND_BUCKETS)
        if r.state != "asking" and last_source.get(key) == "ERROR":
            r.state = "error"

    order = {"asking": 0, "error": 1, "idle": 2}
    return Snapshot(
        window=label,
        rows=sorted(rows.values(), key=lambda r: (order[r.state], -r.calls, r.model)),
        recent=list(calls[-RECENT:]),
        cached_series=activity([c for c in recent_window if c.source == "CACHE"], since, now, buckets),
        fresh_series=activity([c for c in recent_window if c.source != "CACHE"], since, now, buckets),
        in_window=len(recent_window),
        asking=len(inflight),
        stats=analyse(calls, now),
    )


# --- plotting primitives (pure; same approach as nodder's viz and gotodo's chart) --

BRAILLE_BASE = 0x2800
DOTS = ((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))
BLOCKS = " ▁▂▃▄▅▆▇█"
HBLOCKS = " ▏▎▍▌▋▊▉"


def resample(values: Sequence[float], count: int) -> list[float]:
    """Squeeze or stretch to `count` points. Down-sampling keeps each bucket's max, so bursts survive."""
    if count <= 0:
        return []
    if not values:
        return [0.0] * count
    if len(values) >= count:
        out = []
        for i in range(count):
            start = i * len(values) // count
            out.append(float(max(values[start:max(start + 1, (i + 1) * len(values) // count)])))
        return out
    return [float(values[i * len(values) // count]) for i in range(count)]


def area_cells(values: Sequence[float], width: int, height: int, ceiling: float) -> list[list[int]]:
    """Braille dot masks for a filled area chart, 2x4 dots per cell, scaled to `ceiling`."""
    columns, rows = width * 2, height * 4
    grid = [[0] * width for _ in range(height)]
    if ceiling <= 0:
        return grid
    for x, value in enumerate(resample(values, columns)):
        filled = round(max(0.0, value) / ceiling * rows)
        if value > 0:
            filled = max(1, filled)  # a quiet minute must not look like a dead one
        for step in range(min(filled, rows)):
            y = rows - 1 - step
            grid[y // 4][x // 2] |= DOTS[y % 4][x % 2]
    return grid


def area(values: Sequence[float], width: int, height: int) -> list[str]:
    if width <= 0 or height <= 0:
        return []
    grid = area_cells(values, width, height, max(values, default=0))
    return ["".join(chr(BRAILLE_BASE + cell) for cell in row) for row in grid]


def sparkline(values: Sequence[float], width: int, peak: float | None = None) -> str:
    if width <= 0:
        return ""
    points = resample(values, width)
    ceiling = peak if peak is not None else max(points, default=0.0)
    if ceiling <= 0:
        return "·" * width
    return "".join("·" if v <= 0 else BLOCKS[max(1, min(round(v / ceiling * 8), 8))] for v in points)


def bar(fraction: float, width: int) -> str:
    """Horizontal bar, eighth-of-a-cell accurate, on a light track."""
    if width <= 0:
        return ""
    filled = max(0.0, min(1.0, fraction)) * width
    whole = int(filled)
    out = "█" * whole
    part = round((filled - whole) * 8)
    if whole < width and part:
        out += HBLOCKS[part]  # left-eighth blocks: a horizontal bar must not end in a bottom-eighth "▁"
    return out + "░" * (width - len(out))


# --- drawing ---------------------------------------------------------------
#
# Design rules for every page:
#   * One vocabulary: a call is "replayed" (from the cache), "fresh" (asked just now) or "failed".
#   * Colour has one job each: green replayed, amber fresh, rose failed, lilac model names,
#     blue structure and selection, dust for labels. Nothing else is coloured.
#   * Sentence case, no all-caps labels; numbers double as the legend.
#   * The header's right side is the server's pulse; the footer holds keys only.

#: gotodo's Tokyo Night palette as xterm-256 indices, with an 8-colour fallback.
_PALETTE_256 = {"text": 254, "muted": 245, "accent": 111, "ok": 149, "fresh": 179, "err": 211, "name": 141,
                "border": 238}
_PALETTE_8 = {"text": -1, "muted": -1, "accent": curses.COLOR_BLUE, "ok": curses.COLOR_GREEN,
              "fresh": curses.COLOR_YELLOW, "err": curses.COLOR_RED, "name": curses.COLOR_MAGENTA, "border": -1}

WORD = {"CACHE": "replayed", "FRESH": "fresh", "AGENT": "fresh", "ERROR": "failed"}
BUCKET = {"60 min": "min", "24 h": "15 min", "7 days": "2 h"}


def _model(name: str) -> str:
    return "unsupported call" if name == "(no model)" else name


def _colours() -> dict[str, int]:
    names = list(_PALETTE_256) + ["chip"]
    if not curses.has_colors():
        return {n: curses.A_REVERSE if n == "chip" else curses.A_NORMAL for n in names}
    curses.start_color()
    curses.use_default_colors()
    rich = curses.COLORS >= 256
    palette = _PALETTE_256 if rich else _PALETTE_8
    out = {}
    for i, (name, colour) in enumerate(palette.items(), start=1):
        curses.init_pair(i, colour, -1)
        out[name] = curses.color_pair(i)
    if not rich:
        out["muted"] = out["border"] = curses.A_DIM
    curses.init_pair(len(palette) + 1, 234 if rich else curses.COLOR_BLACK, palette["accent"])
    out["chip"] = curses.color_pair(len(palette) + 1) | curses.A_BOLD
    return out


def _put(win, y: int, x: int, text: str, attr: int = curses.A_NORMAL) -> int:
    """Write text clipped to the usable width; returns the column after it. The footer row is never written
    by page content (callers pass rows below it), and curses raises on the last cell, so errors are dropped."""
    height, width = win.getmaxyx()
    room = width - MARGIN - x
    if y < 0 or y >= height or room <= 0:
        return x + len(text)
    try:
        win.addnstr(y, x, text, room, attr)
    except curses.error:
        pass
    return x + len(text)


def _title(win, row: int, text: str, note: str, c: dict[str, int]) -> int:
    x = _put(win, row, 0, text, c["text"] | curses.A_BOLD)
    if note:
        _put(win, row, x + 2, note, c["muted"])
    return row + 1


#: Column span of each tab chip on row 0, for mouse clicks.
_CHIPS: list[tuple[int, int]] = []


def _header(win, c: dict[str, int], page: int) -> None:
    """Tabs only (gotodo's chips). The server's status is in the footer, bottom right."""
    x = 0
    _CHIPS.clear()
    for i, name in enumerate(PAGES):
        start = x
        x = _put(win, 0, x, f" {i + 1} {name} ", c["chip"] if i == page else c["muted"])
        _CHIPS.append((start, x))
        x += 1


def _status(c: dict[str, int], snap: Snapshot, meta: dict[str, str]) -> list[tuple[str, int]]:
    """The server's state: the listening port and mode, or what it is waiting on."""
    port = meta["address"].rsplit(":", 1)[-1]
    mode = meta["mode"]
    if mode == "real API, repeats replayed":
        mode = "real API"
    elif mode == "offline replay":
        mode = "replay"
    if snap.asking:
        longest = max((r.asking_for for r in snap.rows if r.state == "asking"), default=0.0)
        parts = [("● ", c["fresh"]), (f"asking {snap.asking} {'call' if snap.asking == 1 else 'calls'}, "
                                      f"{longest:.0f}s", c["fresh"] | curses.A_BOLD)]
    else:
        parts = [("● ", c["ok"]), (f":{port}", c["text"]), (f"  {mode}", c["muted"])]
    return parts


def _footer(win, c: dict[str, int], hints: Sequence[tuple[str, str]], toast: str = "", toast_attr: int = 0,
            status: Sequence[tuple[str, int]] = ()) -> None:
    """Keys on the left (gotodo's hint bar); the server's status on the right; a toast just left of the status."""
    height, width = win.getmaxyx()
    x = 0
    for key, verb in hints:
        x = _put(win, height - 1, x, key, c["accent"] | curses.A_BOLD)
        x = _put(win, height - 1, x, f" {verb}    ", c["muted"])
    right = width - MARGIN
    if toast:
        _put(win, height - 1, max(x + 2, right - len(toast)), toast, toast_attr)
        return
    status_x = max(x + 2, right - sum(len(t) for t, _ in status))
    xs = status_x
    for text, attr in status:
        xs = _put(win, height - 1, xs, text, attr)


def draw(win, snap: Snapshot, c: dict[str, int], meta: dict[str, str], page: int = 0, selected: int = 0,
         confirm: bool = False, toast: str = "") -> None:
    win.erase()
    _header(win, c, page)
    if page == 0:
        _live(win, snap, c, meta)
    elif page == 1:
        _analytics_page(win, snap.stats, c)
    else:
        _cache_page(win, snap.entries, c, meta, selected)
    nxt = WINDOWS[(next(i for i, w in enumerate(WINDOWS) if w[0] == snap.window) + 1) % len(WINDOWS)][0]
    hints = {0: [("1 2 3", "Pages"), ("w", f"Show {nxt}"), ("p", "Mode"), ("q", "Quit")],
             1: [("1 2 3", "Pages"), ("p", "Mode"), ("q", "Quit")],
             2: [("1 2 3", "Pages"), ("↑↓", "Select"), ("x", "Delete"), ("p", "Mode"), ("q", "Quit")]}[page]
    if confirm:
        toast, attr = "Press x again to delete this answer. The same call will then be asked fresh.", c["fresh"]
    else:
        attr = c["ok"]
    _footer(win, c, hints, toast, attr, status=_status(c, snap, meta))
    win.noutrefresh()
    curses.doupdate()


# Live: nodder's dashboard, quieter. The chart is the page's one loud element.

_COLS = (("Model", 2, 22), ("State", 24, 12), ("Calls", 36, 6), ("Replayed", 42, 10), ("Fresh", 52, 7),
         ("Avg fresh", 59, 11))
_TREND_AT = 73


def _empty_state(win, c, icon, title, message, hint=""):
    """Center a compact empty state without drawing over the tabs or footer."""
    height, width = win.getmaxyx()
    usable = max(0, width - MARGIN)
    lines = [(icon, c["accent"]), (title, c["text"] | curses.A_BOLD), (message, c["muted"])]
    if hint:
        lines.append((hint, c["accent"]))
    available = max(0, height - 3)
    if available < len(lines):
        lines = lines[1:1 + available]
    start = max(2, 1 + (available - len(lines)) // 2)
    for i, (text, attr) in enumerate(lines):
        text = text[:usable]
        _put(win, start + i, max(0, (usable - len(text)) // 2), text, attr)


def _live(win, snap: Snapshot, c: dict[str, int], meta: dict[str, str]) -> None:
    height, width = win.getmaxyx()
    usable = width - MARGIN
    row = 2
    if not snap.stats.total:
        frames = ("●  ·  ·  ·  ·", "·  ●  ·  ·  ·", "·  ·  ●  ·  ·",
                  "·  ·  ·  ●  ·", "·  ·  ·  ·  ●")
        _empty_state(win, c, frames[int(time.monotonic() / 0.8) % len(frames)],
                     "No calls yet", "Waiting for your first request",
                     f"ANTHROPIC_BASE_URL={meta['address']}")
        return

    replayed = sum(snap.cached_series)
    fresh = sum(r.fresh for r in snap.rows)
    failed = sum(r.errors for r in snap.rows)
    x = _put(win, row, 0, "Calls", c["text"] | curses.A_BOLD)
    x = _put(win, row, x + 2, f"last {snap.window}", c["muted"]) + 4
    for count, word, attr in ((replayed, "replayed", c["ok"]), (fresh, "fresh", c["fresh"]),
                              (failed, "failed", c["err"])):
        if word == "failed" and not count:
            continue
        x = _put(win, row, x, f"{count:,}", attr | curses.A_BOLD)
        x = _put(win, row, x, f" {word}    ", c["muted"])
    total = [a + b for a, b in zip(snap.cached_series, snap.fresh_series)]
    peak = f"peak {max(total, default=0)} calls per {BUCKET[snap.window]}"
    _put(win, row, max(x, usable - len(peak)), peak, c["muted"])
    row += 1

    reserved = row + len(snap.rows) + RECENT + 8
    row = _chart(win, snap, total, c, row, usable, min(CHART_MAX_HEIGHT, height - reserved))

    row = _title(win, row, "Requested models", f"last {snap.window}", c)
    for name, at, size in _COLS:
        _put(win, row, at, f"{name:>{size - 1}}" if at >= 36 else name, c["muted"])
    trend_width = min(TREND_BUCKETS, usable - _TREND_AT)
    if trend_width >= 8:
        _put(win, row, _TREND_AT, "Activity", c["muted"])
    row += 1
    peak_trend = max((max(r.trend, default=0) for r in snap.rows), default=0)
    for r in snap.rows[:max(1, height - RECENT - 5 - row)]:
        asking = r.state == "asking"
        _put(win, row, 0, "▸" if asking else " ", c["fresh"])
        _put(win, row, 2, _model(r.model)[:21], c["name"])
        state = {"asking": f"asking {r.asking_for:.0f}s", "error": "failed", "idle": "idle"}[r.state]
        _put(win, row, 24, state, c["fresh"] | curses.A_BOLD if asking else c["err"] if r.state == "error"
             else c["muted"])
        _put(win, row, 36, f"{r.calls:>5}", c["text"] if r.calls else c["muted"])
        _put(win, row, 42, f"{r.cached:>9}", c["ok"] if r.cached else c["muted"])
        _put(win, row, 52, f"{r.fresh:>6}", c["fresh"] if r.fresh else c["muted"])
        _put(win, row, 59, f"{duration(r.avg):>10}", c["muted"])
        if trend_width >= 8:
            _put(win, row, _TREND_AT, sparkline(r.trend, trend_width, peak=peak_trend or None), c["ok"])
        row += 1

    row = _title(win, row + 1, "Recent", "requested model → answering backend", c)
    attr = {"CACHE": c["ok"], "FRESH": c["fresh"], "AGENT": c["fresh"], "ERROR": c["err"]}
    cached_backends = {entry.key: entry.answered_by for entry in snap.entries}
    recent_room = max(0, height - row - 2)
    for call in snap.recent[-recent_room:] if recent_room else ():
        x = _put(win, row, 0, time.strftime("%H:%M:%S", time.localtime(call.at)), c["muted"])
        x = _put(win, row, x + 2, f"{_model(call.model)[:20]:<20}", c["name"])
        x = _put(win, row, x + 1, f"{WORD.get(call.source, call.source):<9}", attr.get(call.source, c["text"]))
        x = _put(win, row, x, f"{call.seconds:>5.1f}s", c["muted"])
        backend = call.answered_by or cached_backends.get(call.key) or {
            "LIVE": "Real API", "AGENT": "Subagent"}.get(call.source, "")
        if backend:
            x = _put(win, row, x + 2, _ellipsis(backend, min(36, usable - x - 2)), c["accent"])
        what = " ".join((snap.asked.get(call.key) or call.label).split())  # what was asked, when the cache has it
        room = usable - x - 3
        what = what if len(what) <= room else what[:max(0, room - 1)] + "…"
        _put(win, row, x + 3, what, c["err"] if call.source == "ERROR" else c["text"])
        row += 1


def _chart(win, snap: Snapshot, total: list[int], c: dict[str, int], row: int, usable: int,
           chart_height: int) -> int:
    """Stacked braille area: green replayed underneath, amber fresh (and failed) on top."""
    chart_width = usable - 2
    if chart_width < CHART_MIN_WIDTH or chart_height < CHART_MIN_HEIGHT:
        return row + 1
    if not any(total):
        _put(win, row + chart_height // 2, 2, f"Nothing in the last {snap.window}. Press w for a longer view.",
             c["muted"])
    else:
        ceiling = max(total)
        whole = area_cells(total, chart_width, chart_height, ceiling)
        fresh = area_cells(snap.fresh_series, chart_width, chart_height, ceiling)
        for y in range(chart_height):
            col = 0
            while col < chart_width:  # one call per run of same-coloured cells
                is_fresh = bool(fresh[y][col])
                end = col
                while end < chart_width and bool(fresh[y][end]) == is_fresh:
                    end += 1
                text = "".join(chr(BRAILLE_BASE + cell) for cell in whole[y][col:end])
                _put(win, row + y, 1 + col, text, c["fresh"] if is_fresh else c["ok"])
                col = end
    row += chart_height
    _put(win, row, 1, f"└{'─' * (chart_width - 2)}┘", c["border"])
    _put(win, row, 3, f" {snap.window} ago ", c["muted"])
    _put(win, row, max(4, chart_width - 5), " now ", c["muted"])
    return row + 2


# Analytics: gotodo's analytics page. Four tiles, then sections that each answer one question.

def _tile(win, y: int, x: int, value: str, label: str, value_attr: int, c: dict[str, int]) -> int:
    inner = max(len(value), len(label)) + 6
    _put(win, y, x, "╭" + "─" * inner + "╮", c["border"])
    for dy, text, attr in ((1, value, value_attr | curses.A_BOLD), (2, label, c["muted"])):
        _put(win, y + dy, x, "│", c["border"])
        _put(win, y + dy, x + 1, text.center(inner), attr)
        _put(win, y + dy, x + inner + 1, "│", c["border"])
    _put(win, y + 3, x, "╰" + "─" * inner + "╯", c["border"])
    return x + inner + 3


def _hbar(win, y: int, label: str, fraction: float, value: str, colour: int, c: dict[str, int], width: int) -> None:
    track = bar(fraction, width)
    filled = track.rstrip("░")
    x = _put(win, y, 2, f"{label[:21]:<22}", c["name"])
    x = _put(win, y, x, filled, colour)
    x = _put(win, y, x, track[len(filled):], c["border"])
    _put(win, y, x + 2, value, c["muted"])


def _days(win, a: Analytics, c: dict[str, int], row: int, usable: int) -> int:
    """Stacked day columns: replayed (green) under fresh and failed (amber), dates beneath, today in blue."""
    height_rows = 4
    peak = max((cached + other for _, cached, other in a.daily), default=0)
    col_w = 4 if usable >= 70 else 3
    for r in range(height_rows):
        y = row + height_rows - 1 - r  # r counts up from the bottom row
        for i, (_, cached, other) in enumerate(a.daily):
            total = cached + other
            filled = round(total / peak * height_rows * 8) if peak else 0
            if total:
                filled = max(1, filled)
            cell = max(0, min(8, filled - r * 8))
            green_top = round(cached / peak * height_rows * 8) if peak else 0
            colour = c["ok"] if r * 8 + 4 < green_top else c["fresh"]
            glyph = BLOCKS[cell] * (col_w - 1) if cell else ("·" if r == 0 else " ")
            _put(win, y, 2 + i * col_w, glyph, colour if cell else c["border"])
    row += height_rows
    today = time.strftime("%d")
    for i, (d, _, _) in enumerate(a.daily):
        label = time.strftime("%d", time.localtime(d))
        _put(win, row, 2 + i * col_w, label, c["accent"] | curses.A_BOLD if label == today and i == len(a.daily) - 1
             else c["muted"])
    row += 1
    calls = sum(cached + other for _, cached, other in a.daily)
    busiest = max(a.daily, key=lambda d: d[1] + d[2])
    note = (f"{calls:,} calls between {day(a.daily[0][0])} and {day(a.daily[-1][0])}. "
            f"Busiest was {day(busiest[0])} with {busiest[1] + busiest[2]:,}.") if calls else "No calls in these days."
    _put(win, row, 2, note, c["muted"])
    return row + 1


def _analytics_page(win, a: Analytics, c: dict[str, int]) -> None:
    height, width = win.getmaxyx()
    usable = width - MARGIN
    bottom = height - 2  # keep the footer row clear
    bar_width = max(16, min(40, usable - 60))
    row = 2
    if not a.total:
        _empty_state(win, c, "▁  ▃  ▂  ▅  ▃", "Nothing to analyse yet",
                     "Your insights will appear after your first call")
        return

    tiles = [(f"{a.total:,}", "calls", c["accent"]),
             (f"{a.hit_rate:.0%}" if a.hit_rate is not None else "—", "replayed", c["ok"]),
             (f"{duration(a.saved)}" if a.saved is not None else "—", "time saved", c["ok"]),
             (duration(a.p50), "median fresh answer", c["fresh"])]
    x = 0
    for value, label, attr in tiles:
        if x + max(len(value), len(label)) + 8 > usable:
            break
        x = _tile(win, row, x, value, label, attr, c)
    since = f"Since {day(a.first_at)}"
    if x + len(since) + 4 < usable:
        _put(win, row + 2, x + 1, since, c["muted"])
    row += 5

    sections = []

    def last_days(r: int) -> int:
        r = _title(win, r, "Last 14 days", "", c)
        return _days(win, a, c, r, usable)
    sections.append(last_days)

    def speed(r: int) -> int:
        r = _title(win, r, "Speed of fresh answers",
                   f"median {duration(a.p50)}, p95 {duration(a.p95)}, slowest {duration(a.slowest)}", c)
        timed = [m for m in a.per_model if m.mean_fresh is not None]
        longest = max((m.mean_fresh or 0 for m in timed), default=0)
        for m in timed[:5]:
            _hbar(win, r, _model(m.model), (m.mean_fresh or 0) / longest if longest else 0,
                  f"{duration(m.mean_fresh)} on average", c["fresh"], c, bar_width)
            r += 1
        if not timed:
            _put(win, r, 2, "No fresh answers yet.", c["muted"])
            r += 1
        return r
    sections.append(speed)

    def replays(r: int) -> int:
        saved = f"saving about {duration(a.saved)}" if a.saved is not None else ""
        r = _title(win, r, "Replayed from the cache",
                   f"{a.cached:,} of {a.cached + a.fresh:,} answers" + (f", {saved}" if saved else ""), c)
        for m in [m for m in a.per_model if m.cached][:5]:
            rate = m.cached / (m.cached + m.fresh)
            _hbar(win, r, _model(m.model), rate, f"{rate:.0%} of {m.cached + m.fresh:,}", c["ok"], c, bar_width)
            r += 1
        never = [_model(m.model) for m in a.per_model if m.fresh and not m.cached]
        if never:
            _put(win, r, 2, "Not replayed yet: " + ", ".join(never), c["muted"])
            r += 1
        return r
    sections.append(replays)

    def failures(r: int) -> int:
        r = _title(win, r, "Failed calls", f"{a.errors:,} in total" if a.errors else "", c)
        if not a.error_rows:
            _put(win, r, 2, "None so far.", c["muted"])
            return r + 1
        for status, count, label, at in a.error_rows:
            x = _put(win, r, 2, f"{status}", c["err"] | curses.A_BOLD)
            x = _put(win, r, x + 2, label[:52], c["text"])
            times = "once" if count == 1 else f"{count} times"
            _put(win, r, max(x + 2, 60), f"{times}, last at {time.strftime('%H:%M', time.localtime(at))} "
                                          f"on {day(at)}", c["muted"])
            r += 1
        return r
    sections.append(failures)

    def projects(r: int) -> int:
        r = _title(win, r, "Projects", "", c)
        busiest = max((n for _, n in a.projects), default=0)
        for name, n in a.projects:
            _hbar(win, r, name, n / busiest if busiest else 0, f"{n:,} calls", c["accent"], c, bar_width)
            r += 1
        return r
    sections.append(projects)

    # Spend spare terminal height on whitespace, but keep the compact layout when
    # the sections need the room (for example, with several models or errors).
    heights = [
        7,
        1 + max(1, min(5, sum(m.mean_fresh is not None for m in a.per_model))),
        1 + min(5, sum(bool(m.cached) for m in a.per_model))
        + int(any(m.fresh and not m.cached for m in a.per_model)),
        1 + max(1, len(a.error_rows)),
        1 + len(a.projects),
    ]
    gap = max(1, min(3, (bottom + 1 - row - sum(heights)) // len(sections)))
    for section in sections:
        if row + gap + 2 > bottom:
            break
        row = section(row + gap)


# Cache: gotodo's list. A row per saved answer, the selected one with an accent bar, the full text below.

def _cells(char: str) -> int:
    if unicodedata.combining(char) or unicodedata.category(char) == "Cf":
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def _clip_cells(text: str, width: int) -> str:
    used = 0
    for i, char in enumerate(text):
        used += _cells(char)
        if used > width:
            return text[:i]
    return text


def _ellipsis(text: str, width: int) -> str:
    if width <= 0:
        return ""
    clipped = _clip_cells(text, width)
    return clipped if clipped == text else _clip_cells(clipped, width - 1) + "…"


def _panel_lines(text: str, width: int):
    """Word-wrap one paragraph, splitting wide glyphs on terminal columns, not code points."""
    text = "".join(" " if unicodedata.category(c) == "Cc" else c for c in text.expandtabs(4))
    if not text.strip():
        yield ""
        return
    for line in textwrap.wrap(text, width, replace_whitespace=False, drop_whitespace=False):
        while line:
            part = _clip_cells(line, width)
            if not part:
                break
            yield part.rstrip()
            line = line[len(part):]


def _pretty_answer(answer: str) -> str:
    if answer.lstrip().startswith(("{", "[")):
        try:
            return json.dumps(json.loads(answer), ensure_ascii=False, indent=2)
        except (ValueError, RecursionError):
            pass
    return answer


def _cache_page(win, entries: list[CacheEntry], c: dict[str, int], meta: dict[str, str], selected: int) -> None:
    height, width = win.getmaxyx()
    usable = width - MARGIN
    row = 2
    folder = meta.get("cache") or ""
    if not folder:
        _put(win, row, 0, "The cache is off", c["text"] | curses.A_BOLD)
        _put(win, row + 2, 0, "Every call is asked fresh. Unset BRIDGE_CACHE to save and replay answers.", c["muted"])
        return
    if not entries:
        _empty_state(win, c, "[  ↻  ]", "No saved answers yet", "Answers will appear here, ready to replay")
        return
    home = str(Path.home())
    shown = "~" + folder[len(home):] if folder.startswith(home) else folder
    row = _title(win, row, "Saved answers",
                 f"{len(entries):,} answers, {_size(sum(e.size for e in entries))}, in {shown}", c)
    row += 1

    if height < 7 or usable < 22:
        return
    backend_x = 20
    used_x = usable - 7
    backend_w = min(30, max(0, used_x - backend_x - 2))
    asked_x = backend_x + backend_w + 2
    answer_w = 30 if usable - asked_x >= 54 else 0
    asked_w = max(0, used_x - asked_x - answer_w)
    if asked_w < 10:
        asked_w = 0
    for name, at, capacity in (("Saved", 2, 16), ("Answered by", backend_x, backend_w),
                               ("Asked", asked_x, asked_w - 2), ("Answer", used_x - answer_w, answer_w),
                               ("Used", used_x, max(0, usable - used_x) if backend_w >= 10 else 0)):
        _put(win, row, at, _ellipsis(name, capacity), c["muted"])
    row += 1
    list_rows = min(len(entries), max(1, (height - row - 12) // 2))
    selected = max(0, min(selected, len(entries) - 1))
    first = min(max(0, selected - list_rows + 1), max(0, len(entries) - list_rows))
    for i, e in enumerate(entries[first:first + list_rows], start=first):
        mine = i == selected
        bold = curses.A_BOLD if mine else 0
        _put(win, row, 0, "▌" if mine else " ", c["accent"])
        _put(win, row, 2, time.strftime("%d/%m %H:%M", time.localtime(e.saved)), (c["accent"] if mine else c["muted"]) | bold)
        _put(win, row, backend_x, _ellipsis(e.answered_by, backend_w), c["name"] | bold)
        _put(win, row, asked_x, _ellipsis(" ".join(e.asked.split()), asked_w - 2),
             (c["accent"] if mine else (c["muted"] if e.legacy else c["text"])) | bold)
        _put(win, row, used_x - answer_w, _ellipsis(" ".join(e.answer.split()), answer_w - 2), c["muted"] | bold)
        if backend_w >= 10:
            _put(win, row, used_x, _ellipsis(f"{e.used}×" if e.used else "—", 7), c["ok"] if e.used else c["muted"])
        row += 1
    if len(entries) > list_rows:
        _put(win, row, 2, f"{selected + 1} of {len(entries)}", c["muted"])
    row += 2

    e = entries[selected]
    box_w = min(usable - 1, 120)
    if row + 2 >= height - 1:
        return
    x = _put(win, row, 0, "╭─ ", c["border"])
    x = _put(win, row, x, "Selected answer", c["accent"] | curses.A_BOLD)
    _put(win, row, x, " " + "─" * max(0, box_w - x - 1) + "╮", c["border"])
    row += 1
    replays = (f"replayed {e.used} {'time' if e.used == 1 else 'times'}, most recently at "
               f"{time.strftime('%H:%M', time.localtime(e.last_used))} on {day(e.last_used)}"
               if e.last_used else "not replayed yet")
    tokens = (f"Tokens  in {e.tokens_in if e.tokens_in is not None else '—'}  "
              f"out {e.tokens_out if e.tokens_out is not None else '—'} (original answer)")
    details = [(f"Answered by: {e.answered_by}", c["accent"] | curses.A_BOLD),
               (f"Requested: {e.model}", c["muted"]), (tokens, c["muted"]),
               (f"{e.project} · saved {day(e.saved)} · {replays}", c["muted"]), ("", 0),
               ("Asked (preview)", c["accent"] | curses.A_BOLD)]
    text_width = box_w - 5
    room = height - row - 2
    lines = [(line, attr) for text, attr in details for line in _panel_lines(text, text_width)]
    asked = list(_panel_lines(" ".join(e.asked.split()) or "—", text_width))
    preview_rows = max(1, (room - len(lines) - 2) // 2)
    if len(asked) > preview_rows:
        asked = asked[:preview_rows]
        asked[-1] = _clip_cells(asked[-1], text_width - 1).rstrip() + "…"
    lines += [(line, c["text"]) for line in asked]
    lines += [("", 0), ("Answered", c["accent"] | curses.A_BOLD)]
    for para in _pretty_answer(e.answer).splitlines() or ["—"]:
        lines.extend((line, c["text"]) for line in _panel_lines(para, text_width))
    if len(lines) > room:
        lines = lines[:max(0, room - 1)] + [("…", c["muted"])]
    for text, attr in lines:
        _put(win, row, 0, "│", c["border"])
        _put(win, row, 3, text, attr)
        _put(win, row, box_w, "│", c["border"])
        row += 1
    _put(win, row, 0, "╰" + "─" * (box_w - 1) + "╯", c["border"])


class Board:
    """Builds snapshots off the drawing thread, like nodder's Dashboard: journal reads never stall the screen."""

    def __init__(self, journal: Journal, cache_dir: Path | None = None) -> None:
        self.journal = journal
        self.cache_dir = cache_dir
        self.window = 0
        self._lock = threading.Lock()
        self._snapshot = Snapshot()
        self.stop = threading.Event()

    @property
    def snapshot(self) -> Snapshot:
        with self._lock:
            return self._snapshot

    def refresh(self) -> None:
        try:
            calls = self.journal.calls()
            snap = build(calls, self.journal.inflight(), time.time(), self.window)
            snap.entries = cache_entries(self.cache_dir, calls)
            snap.asked = {e.key: e.asked for e in snap.entries if not e.legacy}
        except sqlite3.Error:
            return  # keep the last good screen; the next refresh retries
        with self._lock:
            self._snapshot = snap

    def run(self) -> None:
        while not self.stop.is_set():
            self.refresh()
            self.stop.wait(REFRESH)


#: Keys that jump straight to a page. Tab alone is not enough: multiplexers like herdr can swallow it.
PAGE_KEYS = {ord("1"): 0, ord("l"): 0, ord("L"): 0, ord("2"): 1, ord("a"): 1, ord("A"): 1,
             ord("3"): 2, ord("c"): 2, ord("C"): 2}


def run(journal: Journal, meta: dict[str, str], page: int = 0, cache_dir: Path | None = None,
        switch_mode=None) -> None:
    """Show the dashboard until the user presses q or Ctrl+C."""
    board = Board(journal, cache_dir)
    board.refresh()
    threading.Thread(target=board.run, name="dashboard", daemon=True).start()

    def loop(win):
        nonlocal page
        curses.curs_set(0)
        win.nodelay(True)
        win.keypad(True)
        curses.mousemask(curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED)
        colour = _colours()
        selected, confirm, toast, toast_until = 0, False, "", 0.0
        while True:
            entries = board.snapshot.entries
            selected = max(0, min(selected, len(entries) - 1))
            draw(win, board.snapshot, colour, meta, page, selected, confirm,
                 toast if time.time() < toast_until else "")
            key = win.getch()
            if key == -1:
                curses.napms(80)
                continue
            if key != ord("x"):
                confirm = False
            if key in (ord("q"), ord("Q")):
                return
            if key in (ord("p"), ord("P")) and switch_mode:
                toast, toast_until = switch_mode(), time.time() + 4
                continue
            if key in PAGE_KEYS:
                page = PAGE_KEYS[key]
            elif key in (ord("\t"), curses.KEY_RIGHT):
                page = (page + 1) % len(PAGES)
            elif key in (curses.KEY_BTAB, curses.KEY_LEFT):
                page = (page - 1) % len(PAGES)
            elif key == curses.KEY_MOUSE:
                try:
                    _, mx, my, _, _ = curses.getmouse()
                except curses.error:
                    continue
                for i, (start, end) in enumerate(_CHIPS):
                    if my == 0 and start <= mx < end:
                        page = i
            elif key in (ord("w"), ord("W")) and page == 0:
                board.window = (board.window + 1) % len(WINDOWS)
                board.refresh()
            elif key in (ord("r"), ord("R")):
                board.refresh()
            elif page == 2 and key in (curses.KEY_DOWN, ord("j")):
                selected += 1
            elif page == 2 and key in (curses.KEY_UP, ord("k")):
                selected -= 1
            elif page == 2 and key == ord("x") and entries and cache_dir:
                if confirm:  # second press deletes; the next identical call is asked fresh
                    (cache_dir / f"{entries[selected].key}.json").unlink(missing_ok=True)
                    board.refresh()
                    toast, toast_until = "Deleted. The same call will be asked fresh next time.", time.time() + 4
                    confirm = False
                else:
                    confirm = True
            curses.napms(40)

    try:
        curses.wrapper(loop)
    except KeyboardInterrupt:
        pass
    finally:
        board.stop.set()
