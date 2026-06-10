"""Competitor monitor dashboard backed by Firecrawl monitors."""

import os
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock, Thread

from flask import Flask, abort, flash, redirect, render_template, request, url_for

import db
from firecrawl_client import FirecrawlClient, FirecrawlError, FirecrawlRateLimitError


def load_env():
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())


load_env()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "competitor-monitor-dev")
try:
    fc = FirecrawlClient()
except FirecrawlError:
    raise SystemExit(
        "FIRECRAWL_API_KEY is not set. Create a .env file with FIRECRAWL_API_KEY=fc-... "
        "(get a key at https://firecrawl.dev) and restart."
    )

SCHEDULE_OPTIONS = [
    "every 30 minutes",
    "hourly",
    "every 6 hours",
    "daily",
    "weekly",
]

DEFAULT_GOAL = (
    "Alert on meaningful competitive changes: new features, pricing or plan changes, "
    "new integrations, product launches, or major announcements. Ignore cosmetic edits, "
    "typo fixes, and date or footer changes."
)

# Firecrawl's free plan allows only a few requests per minute, and a live API
# round-trip takes ~1s — too slow to sit in front of a page render. So pages
# are always served from a SQLite-backed cache (stale-while-revalidate): reads
# return instantly, and when an entry is older than its TTL a daemon thread
# refreshes it behind the scenes. Only the very first fetch of a key blocks.
# Every outbound call goes through a gate that remembers a 429's retry-after
# and sends no traffic until the cooldown passes.
_gate_lock = Lock()
_limited_until = 0.0
_inflight = set()
_inflight_lock = Lock()


def api_call(fn):
    global _limited_until
    with _gate_lock:
        remaining = _limited_until - time.monotonic()
        if remaining > 0:
            raise FirecrawlRateLimitError(
                "Rate limited", retry_after=int(remaining) + 1
            )
    try:
        return fn()
    except FirecrawlRateLimitError as exc:
        with _gate_lock:
            _limited_until = time.monotonic() + (exc.retry_after or 60)
        raise


def _refresh_async(key, fn):
    with _inflight_lock:
        if key in _inflight:
            return
        _inflight.add(key)

    def work():
        try:
            db.cache_put(key, api_call(fn))
        except FirecrawlError:
            pass  # stale data stays; the next request retries
        finally:
            with _inflight_lock:
                _inflight.discard(key)

    Thread(target=work, daemon=True).start()


def swr(key, ttl, fn):
    """Serve from cache instantly; refresh in the background when stale."""
    entry = db.cache_get(key)
    if entry is None:
        value = api_call(fn)
        db.cache_put(key, value)
        return value
    value, age = entry
    if age > ttl:
        _refresh_async(key, fn)
    return value


def swr_background(key, ttl, fn):
    """Like swr, but never blocks: a missing entry returns None and is
    fetched in the background for the next page view."""
    entry = db.cache_get(key)
    if entry is None:
        _refresh_async(key, fn)
        return None
    value, age = entry
    if age > ttl:
        _refresh_async(key, fn)
    return value


def cache_put_monitor(monitor):
    """Insert/replace one monitor in the cached list (after create)."""
    entry = db.cache_get("monitors")
    if entry:
        monitors = [m for m in entry[0] if m["id"] != monitor["id"]]
        db.cache_put("monitors", monitors + [monitor])
    else:
        db.cache_put("monitors", [monitor])


def cache_evict_monitor(monitor_id):
    """Drop one monitor and its checks from the cache (after delete)."""
    entry = db.cache_get("monitors")
    if entry:
        db.cache_put("monitors", [m for m in entry[0] if m["id"] != monitor_id])
    db.cache_delete(f"checks:{monitor_id}")


def cache_prepend_check(monitor_id, check):
    """Show a just-triggered check immediately, without an extra API call."""
    entry = db.cache_get(f"checks:{monitor_id}")
    checks = entry[0] if entry else []
    if isinstance(checks, dict):
        checks = checks.get("checks", [])
    checks = [c for c in checks if c.get("id") != check.get("id")]
    db.cache_put(f"checks:{monitor_id}", [check] + checks)


def rate_limit_message(exc):
    wait = f"~{exc.retry_after} seconds" if exc.retry_after else "a minute"
    return f"Firecrawl rate limit reached — wait {wait} and try again."


@app.template_filter("dt")
def format_datetime(value):
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.strftime("%b %d, %Y %H:%M UTC")
    except ValueError:
        return value


@app.template_filter("ago")
def relative_time(value):
    """'4m ago' / 'in 2h' — falls back to the raw value if unparseable."""
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    delta = datetime.now(timezone.utc) - parsed
    seconds = int(delta.total_seconds())
    future = seconds < 0
    seconds = abs(seconds)
    if seconds < 60:
        text = "just now" if not future else "in under a minute"
        return text
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            count = seconds // size
            return f"in {count}{unit}" if future else f"{count}{unit} ago"
    return value


@app.template_filter("until")
def until_time(value):
    """For future timestamps; a past nextRunAt reads 'due now', not ' ago'."""
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if parsed <= datetime.now(timezone.utc):
        return "due now"
    return relative_time(value)


MAX_DIFF_LINES = 300


@app.template_filter("diff_lines")
def diff_lines(text):
    lines = []
    raw = (text or "").splitlines()
    truncated = len(raw) - MAX_DIFF_LINES
    if truncated > 0:
        raw = raw[:MAX_DIFF_LINES]
    for line in raw:
        if line.startswith("+++") or line.startswith("---"):
            kind = "meta"
        elif line.startswith("@@"):
            kind = "hunk"
        elif line.startswith("+"):
            kind = "add"
        elif line.startswith("-"):
            kind = "del"
        else:
            kind = "ctx"
        # Collapse runs of blank context lines so paragraph gaps read as one.
        if (
            kind == "ctx"
            and not line.strip()
            and lines
            and lines[-1]["kind"] == "ctx"
            and not lines[-1]["text"].strip()
        ):
            continue
        lines.append({"kind": kind, "text": line})
    if truncated > 0:
        lines.append(
            {"kind": "meta", "text": f"… diff truncated ({truncated} more lines)"}
        )
    return lines


PAGE_ORDER = {"changed": 0, "new": 1, "removed": 2, "error": 3, "same": 4}


def is_noise(page):
    """True when the AI judge looked at a changed page and ruled it noise."""
    judgment = page.get("judgment")
    return bool(judgment) and judgment.get("meaningful") is False


def judge_counts(check):
    """Split a check's changed pages into meaningful vs judged-noise."""
    meaningful = noise = 0
    for page in check.get("pages") or []:
        if page.get("status") != "changed":
            continue
        if is_noise(page):
            noise += 1
        else:
            meaningful += 1
    return meaningful, noise


def sort_check_pages(check):
    """Meaningful changes render first; judged-noise changes sink below
    errors so a rotated CAPTCHA token never outranks a real change."""
    if isinstance(check, dict) and check.get("pages"):
        for page in check["pages"]:
            page["isNoise"] = page.get("status") == "changed" and is_noise(page)

        def key(page):
            if page["isNoise"]:
                return 3.5
            return PAGE_ORDER.get(page.get("status"), 5)

        check["pages"] = sorted(check["pages"], key=key)
    return check


def get_check_cached(monitor_id, check_id):
    """Completed checks are immutable, so a cached one is served forever;
    queued/running checks go through the normal SWR path."""
    entry = db.cache_get(f"check:{check_id}")
    if entry and entry[0].get("status") in ("completed", "failed"):
        return entry[0]
    return swr(f"check:{check_id}", 15, lambda: fc.get_check(monitor_id, check_id))


def latest_check_verdict(monitor_id, checks):
    """Judge-aware summary of the latest completed check: how many changed
    pages were meaningful vs noise. None when nothing changed or the check
    detail isn't cached yet — callers fall back to the raw counts."""
    latest = next((c for c in checks if c.get("status") == "completed"), None)
    if not latest or not (latest.get("summary") or {}).get("changed"):
        return None
    detail = swr_background(
        f"check:{latest['id']}",
        10**9,
        lambda: fc.get_check(monitor_id, latest["id"]),
    )
    if not detail:
        return None
    meaningful, noise = judge_counts(detail)
    return {"meaningful": meaningful, "noise": noise}


def fetch_monitor(competitor):
    """Resolve a competitor's monitor from the shared cached list — every page
    is served by the same single list_monitors call."""
    try:
        monitors = swr("monitors", 60, fc.list_monitors)
        competitor["monitor"] = next(
            (m for m in monitors if m["id"] == competitor["monitor_id"]), None
        )
        competitor["monitor_error"] = (
            None if competitor["monitor"] else "Monitor not found on Firecrawl"
        )
    except FirecrawlRateLimitError as exc:
        competitor["monitor"] = None
        competitor["monitor_error"] = rate_limit_message(exc)
    except FirecrawlError as exc:
        competitor["monitor"] = None
        competitor["monitor_error"] = str(exc)
    return competitor


def cached_checks(monitor_id):
    """Non-blocking read of a monitor's checks list for verdict lookups."""
    value = swr_background(
        f"checks:{monitor_id}",
        60,
        lambda: fc.list_checks(monitor_id, limit=20),
    )
    if value is None:
        return []
    return value if isinstance(value, list) else value.get("checks", [])


@app.route("/")
def index():
    competitors = []
    for c in db.list_competitors():
        fetch_monitor(c)
        c["verdict"] = latest_check_verdict(c["monitor_id"], cached_checks(c["monitor_id"]))
        competitors.append(c)
    return render_template("index.html", competitors=competitors)


def render_new_form(form=None):
    return render_template(
        "new.html", schedules=SCHEDULE_OPTIONS, default_goal=DEFAULT_GOAL, form=form
    )


@app.route("/competitors/new")
def new_competitor():
    return render_new_form()


@app.route("/competitors", methods=["POST"])
def create_competitor():
    name = request.form.get("name", "").strip()
    urls = [u.strip() for u in request.form.get("urls", "").splitlines() if u.strip()]
    goal = request.form.get("goal", "").strip() or DEFAULT_GOAL
    schedule = request.form.get("schedule", "every 6 hours")

    if not name or not urls:
        flash("Name and at least one URL are required.", "error")
        return render_new_form(form=request.form)
    if schedule not in SCHEDULE_OPTIONS:
        schedule = "every 6 hours"

    try:
        monitor = api_call(
            lambda: fc.create_monitor(
                name=f"Competitor: {name}", urls=urls, goal=goal, schedule_text=schedule
            )
        )
    except FirecrawlRateLimitError as exc:
        flash(rate_limit_message(exc), "error")
        return render_new_form(form=request.form)
    except FirecrawlError as exc:
        flash(f"Firecrawl rejected the monitor: {exc}", "error")
        return render_new_form(form=request.form)

    competitor_id = db.add_competitor(name, urls, goal, schedule, monitor["id"])
    cache_put_monitor(monitor)

    # Kick off the first check right away so the dashboard has baseline data.
    try:
        api_call(lambda: fc.run_monitor(monitor["id"]))
        flash(f"Monitor created for {name}. First check is running now.", "ok")
    except FirecrawlError:
        flash(f"Monitor created for {name}. First check runs at the next schedule.", "ok")

    return redirect(url_for("competitor_detail", competitor_id=competitor_id))


@app.route("/competitors/<int:competitor_id>")
def competitor_detail(competitor_id):
    competitor = db.get_competitor(competitor_id)
    if not competitor:
        abort(404)
    fetch_monitor(competitor)

    checks = []
    checks_error = None
    if competitor["monitor"]:
        try:
            result = swr(
                f"checks:{competitor['monitor_id']}",
                30,
                lambda: fc.list_checks(competitor["monitor_id"], limit=20),
            )
            checks = result if isinstance(result, list) else result.get("checks", [])
        except FirecrawlRateLimitError as exc:
            checks_error = rate_limit_message(exc)
        except FirecrawlError as exc:
            checks_error = f"Could not load checks: {exc}"
    elif competitor["monitor_error"]:
        checks_error = "Check history is unavailable while the monitor is unreachable."

    verdict = latest_check_verdict(competitor["monitor_id"], checks)
    return render_template(
        "competitor.html",
        competitor=competitor,
        checks=checks,
        checks_error=checks_error,
        verdict=verdict,
    )


@app.route("/competitors/<int:competitor_id>/checks/<check_id>")
def check_detail(competitor_id, check_id):
    competitor = db.get_competitor(competitor_id)
    if not competitor:
        abort(404)
    try:
        check = get_check_cached(competitor["monitor_id"], check_id)
    except FirecrawlRateLimitError as exc:
        flash(rate_limit_message(exc), "error")
        return redirect(url_for("competitor_detail", competitor_id=competitor_id))
    except FirecrawlError as exc:
        flash(f"Could not load check: {exc}", "error")
        return redirect(url_for("competitor_detail", competitor_id=competitor_id))
    check = sort_check_pages(check)
    meaningful, noise = judge_counts(check)
    return render_template(
        "check.html",
        competitor=competitor,
        check=check,
        meaningful=meaningful,
        noise=noise,
    )


@app.route("/competitors/<int:competitor_id>/run", methods=["POST"])
def run_check(competitor_id):
    competitor = db.get_competitor(competitor_id)
    if not competitor:
        abort(404)
    try:
        queued = api_call(lambda: fc.run_monitor(competitor["monitor_id"]))
        # The run response is the queued check itself — put it straight into
        # the cached list so it shows up on the redirect with zero extra calls.
        if isinstance(queued, dict) and queued.get("id"):
            cache_prepend_check(competitor["monitor_id"], queued)
        flash("Check triggered. Results appear below once it completes.", "ok")
    except FirecrawlRateLimitError as exc:
        flash(rate_limit_message(exc), "error")
    except FirecrawlError as exc:
        flash(f"Could not trigger check: {exc}", "error")
    return redirect(url_for("competitor_detail", competitor_id=competitor_id))


@app.route("/competitors/<int:competitor_id>/delete", methods=["POST"])
def remove_competitor(competitor_id):
    competitor = db.get_competitor(competitor_id)
    if not competitor:
        abort(404)
    try:
        api_call(lambda: fc.delete_monitor(competitor["monitor_id"]))
    except FirecrawlRateLimitError as exc:
        # Don't delete the local row on a transient 429 — that would orphan a
        # live monitor on Firecrawl with no way to retry from the app.
        flash(f"{rate_limit_message(exc)} Nothing was deleted.", "error")
        return redirect(url_for("competitor_detail", competitor_id=competitor_id))
    except FirecrawlError as exc:
        flash(f"Monitor deletion on Firecrawl failed ({exc}); removed locally.", "error")
    db.delete_competitor(competitor_id)
    cache_evict_monitor(competitor["monitor_id"])
    flash(f"{competitor['name']} removed.", "ok")
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(debug=True, port=5050)
