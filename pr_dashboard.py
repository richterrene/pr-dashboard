#!/usr/bin/env python3
#
# MIT License — Copyright (c) 2026 richterrene
# See LICENSE for the full text.
#
"""Local dashboard of the GitHub PRs *you* authored — fetch + scheduler in one file.

Pure Python standard library: nothing to pip-install. Talks to GitHub only
through the `gh` CLI you're already logged into, so your data never leaves your
machine. Writes `data.js`, which the bundled `index.html` renders with a
pending-work board and all-time stats.

    python3 pr_dashboard.py             # check env, fetch, build + open dashboard
    python3 pr_dashboard.py --no-open   # fetch + build only (used by the scheduler)
    python3 pr_dashboard.py --check     # environment preflight only, then exit
    python3 pr_dashboard.py --install   # build, then auto-refresh on a schedule
    python3 pr_dashboard.py --uninstall # remove the scheduled auto-refresh job
    python3 pr_dashboard.py --install --interval 15   # refresh every 15 min

Auto-refresh backend per OS:
  macOS    launchd LaunchAgent
  Linux    systemd --user timer (falls back to a crontab entry)
  Windows  Scheduled Task (schtasks)

The fetch is resumable: results are cached in `cache.json` (saved after every
page), past years with no open PRs are frozen and skipped, and only the current
year + any year still holding an open PR are re-queried. So scheduled refreshes
are cheap and stay well under GitHub's rate limits.
"""
import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import webbrowser
from datetime import datetime, timezone
from http.server import HTTPServer, SimpleHTTPRequestHandler

MIN_PYTHON = (3, 7)

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.abspath(__file__)
CACHE_PATH = os.path.join(HERE, "cache.json")
CONFIG_PATH = os.path.join(HERE, "config.json")
DATA_PATH = os.path.join(HERE, "data.js")
INDEX_HTML = os.path.join(HERE, "index.html")
LOG_PATH = os.path.join(HERE, "agent.log")
ERR_PATH = os.path.join(HERE, "agent.err.log")

GH = shutil.which("gh") or "gh"   # absolute path so schedulers can find it

FALLBACK_FIRST_YEAR = 2008  # GitHub launch year; used only if detection fails
PAGE_PAUSE = 1.0            # seconds between page requests (avoid secondary limits)
SECONDARY_LIMIT_WAIT = 90   # seconds to wait when GitHub reports a secondary limit
DEFAULT_INTERVAL_MIN = 30   # default auto-refresh cadence

# Scheduler identifiers / paths (per-OS; only the relevant one is used).
HOME = os.path.expanduser("~")
JOB_LABEL = "io.github.prdashboard"   # macOS launchd / Linux systemd unit id
JOB_NAME = "PRDashboard"              # human-facing Windows Scheduled Task name
JOB_DISPLAY_NAME = "PR Dashboard"     # macOS Login Items display name
PLIST_PATH = os.path.join(HOME, "Library", "LaunchAgents", JOB_LABEL + ".plist")
# macOS labels a Login Item after the launched executable's enclosing .app
# bundle, so we run Python through a tiny wrapper bundle. Without this it shows
# up as "python3.14" instead of "PR Dashboard".
APP_BUNDLE = os.path.join(HOME, "Library", "Application Support",
                          JOB_LABEL, JOB_DISPLAY_NAME + ".app")
SYSTEMD_DIR = os.path.join(HOME, ".config", "systemd", "user")
SYSTEMD_SERVICE = os.path.join(SYSTEMD_DIR, JOB_LABEL + ".service")
SYSTEMD_TIMER = os.path.join(SYSTEMD_DIR, JOB_LABEL + ".timer")


# ---------------------------------------------------------------------------
# GitHub fetch (resumable, cached)
# ---------------------------------------------------------------------------

# Cheap sweep query: no commit/CI nesting (that nesting triggers HTTP 502s at scale).
LIST_QUERY = """
query($q: String!, $after: String) {
  search(query: $q, type: ISSUE, first: 100, after: $after) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on PullRequest {
        number title url state isDraft
        createdAt updatedAt mergedAt closedAt
        additions deletions changedFiles
        reviewDecision
        repository { nameWithOwner }
        labels(first: 10) { nodes { name color } }
        comments { totalCount }
        reviews { totalCount }
      }
    }
  }
}
"""

# Enrichment query for OPEN PRs only: includes CI rollup.
CI_QUERY = """
query($q: String!, $after: String) {
  search(query: $q, type: ISSUE, first: 100, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on PullRequest {
        number
        repository { nameWithOwner }
        commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
      }
    }
  }
}
"""


DEFAULT_CONFIG = {"notify": True}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                cfg.update(json.load(f))
        except (ValueError, OSError):
            pass  # corrupt/unreadable config -> fall back to defaults
    return cfg


def save_config(cfg):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)


def _key(repo, number):
    return f"{repo}#{number}"


def load_cache():
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH) as f:
            return json.load(f)
    return {"prs": {}, "yearsComplete": {}, "fetchedAt": None}


def save_cache(cache):
    tmp = CACHE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cache, f)
    os.replace(tmp, CACHE_PATH)  # atomic; survives interruption mid-write


def gh_graphql(query, q, after=None, retries=6):
    args = [GH, "api", "graphql", "-f", f"query={query}", "-f", f"q={q}"]
    if after:
        args += ["-f", f"after={after}"]
    last_err = ""
    for attempt in range(retries):
        res = subprocess.run(args, capture_output=True, text=True)
        if res.returncode == 0:
            try:
                data = json.loads(res.stdout)
            except json.JSONDecodeError:
                last_err = f"bad JSON: {res.stdout[:200]}"
            else:
                if data.get("data", {}).get("search") is not None:
                    return data["data"]["search"]
                last_err = f"graphql errors: {data.get('errors')}"
        else:
            last_err = res.stderr.strip()
        if "secondary rate limit" in last_err.lower():
            sys.stderr.write(f"    secondary rate limit hit; sleeping {SECONDARY_LIMIT_WAIT}s\n")
            time.sleep(SECONDARY_LIMIT_WAIT)
        else:
            wait = min(2 ** attempt, 30)
            sys.stderr.write(f"    retry {attempt + 1}/{retries} "
                             f"({last_err[:100]}); sleeping {wait}s\n")
            time.sleep(wait)
    raise RuntimeError(f"gh failed after {retries} retries: {last_err}")


def detect_first_year():
    """Return the year of the user's earliest authored PR (any state)."""
    page = gh_graphql(LIST_QUERY, "author:@me type:pr sort:created-asc")
    for n in page["nodes"]:
        if n and n.get("createdAt"):
            return int(n["createdAt"][:4])
    return FALLBACK_FIRST_YEAR


def normalize(n):
    return {
        "number": n["number"],
        "title": n["title"],
        "url": n["url"],
        "state": n["state"],            # OPEN | CLOSED | MERGED
        "isDraft": n["isDraft"],
        "createdAt": n["createdAt"],
        "updatedAt": n["updatedAt"],
        "mergedAt": n["mergedAt"],
        "closedAt": n["closedAt"],
        "additions": n["additions"],
        "deletions": n["deletions"],
        "changedFiles": n["changedFiles"],
        "reviewDecision": n["reviewDecision"],
        "repo": n["repository"]["nameWithOwner"],
        "labels": [{"name": l["name"], "color": l["color"]}
                   for l in n["labels"]["nodes"]],
        "comments": n["comments"]["totalCount"],
        "reviews": n["reviews"]["totalCount"],
        "ci": None,  # filled in by the enrichment pass for open PRs
    }


def sweep(cache, q, max_pages=None):
    """Run search query `q`, upserting normalized PRs into cache; save per page.

    Returns the number of pages fetched. `max_pages` caps pagination (used for
    'recently updated' queries where only the first page or two matter).
    """
    after, page_no = None, 0
    while True:
        page = gh_graphql(LIST_QUERY, q, after)
        page_no += 1
        for n in page["nodes"]:
            if not n:
                continue
            rec = normalize(n)
            k = _key(rec["repo"], rec["number"])
            # Preserve a previously-enriched CI value if present.
            if k in cache["prs"] and cache["prs"][k].get("ci") is not None:
                rec["ci"] = cache["prs"][k]["ci"]
            cache["prs"][k] = rec
        save_cache(cache)
        if not page["pageInfo"]["hasNextPage"]:
            break
        if max_pages and page_no >= max_pages:
            break
        after = page["pageInfo"]["endCursor"]
        time.sleep(PAGE_PAUSE)
    return page_no


def fetch_year(year, cache):
    """Fetch all PRs created in `year` (full sweep)."""
    return sweep(cache, f"author:@me type:pr created:{year}-01-01..{year}-12-31")


def incremental_refresh(cache, cur_year):
    """Cheap refresh for an already-populated cache.

    Past non-open PRs are immutable, so we don't re-sweep old years. Instead we
    fetch only what can change: every currently-open PR (any year), the current
    year (to discover newly-opened PRs and this year's status changes), and the
    most recently-closed PRs (to catch merges/closes since the last run). This
    is a handful of pages — seconds, not minutes.
    """
    sweep(cache, "author:@me type:pr state:open")
    sweep(cache, f"author:@me type:pr created:{cur_year}-01-01..{cur_year}-12-31")
    sweep(cache, "author:@me type:pr is:closed sort:updated-desc", max_pages=2)


def enrich_open_ci(cache):
    """Fetch CI rollup for OPEN PRs only (cheap: a handful of PRs)."""
    q = "author:@me type:pr state:open"
    after, seen = None, 0
    while True:
        page = gh_graphql(CI_QUERY, q, after)
        for n in page["nodes"]:
            if not n:
                continue
            k = _key(n["repository"]["nameWithOwner"], n["number"])
            rollup = None
            commits = n.get("commits", {}).get("nodes", [])
            if commits and commits[0]["commit"]["statusCheckRollup"]:
                rollup = commits[0]["commit"]["statusCheckRollup"]["state"]
            if k in cache["prs"]:
                cache["prs"][k]["ci"] = rollup
                seen += 1
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]
        time.sleep(PAGE_PAUSE)
    save_cache(cache)
    return seen


def year_has_open(cache, year):
    ys = f"{year}-"
    return any(p["state"] == "OPEN" and p["createdAt"].startswith(ys)
               for p in cache["prs"].values())


def snapshot(cache):
    """Minimal per-PR state used to diff one refresh against the next."""
    return {k: {"state": p["state"], "reviewDecision": p.get("reviewDecision"),
                "comments": p.get("comments", 0), "ci": p.get("ci"),
                "isDraft": p.get("isDraft", False)}
            for k, p in cache["prs"].items()}


# Event kinds, in priority order (used for notification + feed labelling).
def detect_events(prev, cache):
    """Compare a previous snapshot to the current cache; return a list of events.

    Each event: {kind, key, repo, number, title, url, at}. `prev` is the
    snapshot dict from the last run (empty on first run → no events, avoiding a
    notification storm over the whole backfill).
    """
    if not prev:
        return []
    now_iso = datetime.now(timezone.utc).isoformat()
    events = []
    for k, p in cache["prs"].items():
        old = prev.get(k)
        if not old:
            continue  # brand-new-to-cache PR: skip (often just backfill, not activity)
        base = {"key": k, "repo": p["repo"], "number": p["number"],
                "title": p["title"], "url": p["url"], "at": now_iso}
        # Terminal transitions
        if old["state"] != p["state"]:
            if p["state"] == "MERGED":
                events.append({**base, "kind": "merged"}); continue
            if p["state"] == "CLOSED":
                events.append({**base, "kind": "closed"}); continue
        # Review decision changes (only meaningful while open)
        if p["state"] == "OPEN" and old["reviewDecision"] != p.get("reviewDecision"):
            if p.get("reviewDecision") == "APPROVED":
                events.append({**base, "kind": "approved"})
            elif p.get("reviewDecision") == "CHANGES_REQUESTED":
                events.append({**base, "kind": "changes_requested"})
        # New comments
        if p.get("comments", 0) > old.get("comments", 0):
            events.append({**base, "kind": "commented",
                           "delta": p["comments"] - old["comments"]})
        # CI turned red
        if p["state"] == "OPEN" and old.get("ci") != p.get("ci") \
                and p.get("ci") in ("FAILURE", "ERROR"):
            events.append({**base, "kind": "ci_failed"})
    return events


def write_data_js(cache, me, events_feed, notify_on=True):
    prs = sorted(cache["prs"].values(), key=lambda p: p["createdAt"], reverse=True)
    payload = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "user": me,
        "count": len(prs),
        "prs": prs,
        "events": events_feed[:50],  # most recent first, capped
        "notify": notify_on,         # current notification setting (for the UI)
    }
    with open(DATA_PATH, "w") as f:
        f.write("window.PR_DATA = ")
        json.dump(payload, f)
        f.write(";\n")


def whoami():
    r = subprocess.run([GH, "api", "user", "--jq", ".login"],
                       capture_output=True, text=True, check=False)
    return r.stdout.strip()


def _full_backfill(cache, first_year, cur_year):
    """Sweep every year once (skipping frozen ones). Used on first run / --full."""
    for year in range(first_year, cur_year + 1):
        if cache["yearsComplete"].get(str(year)):
            sys.stderr.write(f"  {year}: cached (complete), skipping\n")
            continue
        pages = fetch_year(year, cache)
        n_year = sum(1 for p in cache["prs"].values()
                     if p["createdAt"].startswith(f"{year}-"))
        if year < cur_year and not year_has_open(cache, year):
            cache["yearsComplete"][str(year)] = True
            tag = "complete"
        else:
            tag = "will recheck"
        sys.stderr.write(f"  {year}: {n_year} PRs, {pages} page(s) [{tag}]\n")


def build_data(full=False, notify=True):
    """Fetch PRs into the cache and (re)generate data.js. Returns a summary dict.

    First run (empty cache) or `full=True` does a complete year-by-year backfill.
    Otherwise an incremental refresh fetches only what can change — fast enough
    to run on a frequent schedule. Events are diffed against the previous run and
    surfaced as desktop notifications (when `notify`) and a feed in data.js.
    """
    now = datetime.now(timezone.utc)
    me = whoami()
    cache = load_cache()
    cur_year = now.year
    is_first_run = not cache["prs"]

    # Snapshot the pre-refresh state so we can diff for events afterwards.
    prev = snapshot(cache)

    first_year = cache.get("firstYear")
    if not first_year:
        first_year = detect_first_year()
        cache["firstYear"] = first_year
        save_cache(cache)

    interrupted = False
    try:
        if is_first_run or full:
            sys.stderr.write(f"Full backfill for @{me} ({first_year}–{cur_year})\n")
            _full_backfill(cache, first_year, cur_year)
        else:
            sys.stderr.write(f"Incremental refresh for @{me}\n")
            incremental_refresh(cache, cur_year)
        n = enrich_open_ci(cache)
        sys.stderr.write(f"  CI enriched for {n} open PR(s)\n")
    except (RuntimeError, KeyboardInterrupt) as e:
        interrupted = True
        sys.stderr.write(f"\n!! Stopped early ({e}).\n"
                         f"   Progress saved to cache.json — just re-run to resume.\n")

    # Diff for events (suppressed on the very first run to avoid a storm).
    events = [] if is_first_run else detect_events(prev, cache)
    feed = cache.get("events", [])
    if events:
        feed = events + feed          # newest first
        cache["events"] = feed[:200]  # bounded history in the cache
        sys.stderr.write("  %d event(s): %s\n"
                         % (len(events), ", ".join(e["kind"] for e in events)))

    cache["fetchedAt"] = now.isoformat()
    save_cache(cache)
    notify_on = load_config().get("notify", True)
    write_data_js(cache, me, cache.get("events", []), notify_on)

    notified = False
    if notify and notify_on and events:
        notify_events(events)
        notified = True

    if interrupted:
        raise RuntimeError("fetch interrupted; data.js written from partial cache")
    return {"user": me, "count": len(cache["prs"]), "events": len(events),
            "first_run": is_first_run, "notified": notified, "notify_on": notify_on}


# ---------------------------------------------------------------------------
# Environment preflight
# ---------------------------------------------------------------------------

def _have(cmd):
    return shutil.which(cmd) is not None


def check_environment(require_auth=True):
    """Verify Python, the gh CLI, and (optionally) gh auth. Exits on any fatal problem."""
    if sys.version_info < MIN_PYTHON:
        sys.exit("  - Python %d.%d+ required, but this is %s."
                 % (MIN_PYTHON[0], MIN_PYTHON[1], platform.python_version()))
    print("  - Python %s ✓" % platform.python_version())

    if GH == "gh" and not _have("gh"):
        sys.exit("  - GitHub CLI (gh) not found. Install it: https://cli.github.com/")
    print("  - gh CLI: %s ✓" % GH)

    if require_auth:
        r = subprocess.run([GH, "auth", "status"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                           text=True, check=False)
        if r.returncode != 0:
            sys.exit("  - gh is not authenticated. Run: gh auth login\n"
                     "    (%s)" % r.stderr.strip().splitlines()[-1:] or "")
        me = whoami()
        print("  - gh authenticated as @%s ✓" % me)


# ---------------------------------------------------------------------------
# Scheduler: macOS launchd
# ---------------------------------------------------------------------------
_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key><array>
    <string>{exe}</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>{path}</string>
  </dict>
  <key>StartInterval</key><integer>{interval}</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{err}</string>
</dict></plist>
"""

_APP_INFO_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>{name}</string>
  <key>CFBundleDisplayName</key><string>{name}</string>
  <key>CFBundleIdentifier</key><string>{label}</string>
  <key>CFBundleExecutable</key><string>{name}</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSUIElement</key><true/>
</dict></plist>
"""


def _build_app_bundle():
    """Create a minimal .app wrapper so macOS shows a friendly Login Item name.

    launchd/Login Items label an agent after the running executable's name. Run
    Python directly and it shows as "python3.14"; run it through a bundle whose
    executable is named "PR Dashboard" and that's what the user sees instead.
    Returns the path to the wrapper executable to point the LaunchAgent at.
    """
    shutil.rmtree(APP_BUNDLE, ignore_errors=True)  # rebuild from scratch
    macos_dir = os.path.join(APP_BUNDLE, "Contents", "MacOS")
    os.makedirs(macos_dir, exist_ok=True)
    with open(os.path.join(APP_BUNDLE, "Contents", "Info.plist"), "w") as f:
        f.write(_APP_INFO_PLIST.format(name=JOB_DISPLAY_NAME, label=JOB_LABEL))
    run = os.path.join(macos_dir, JOB_DISPLAY_NAME)  # named after the bundle
    cmd = " ".join('"%s"' % a for a in [sys.executable, SCRIPT, "--no-open"])
    with open(run, "w") as f:
        f.write("#!/bin/sh\nexec %s\n" % cmd)
    os.chmod(run, 0o755)
    # Unsigned bundles get attributed to the raw executable name in Login Items;
    # an ad-hoc signature makes macOS honor CFBundleName instead.
    if _have("codesign"):
        subprocess.run(["codesign", "--force", "--deep", "-s", "-", APP_BUNDLE],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    return run


def _install_launchd(interval_sec):
    os.makedirs(os.path.dirname(PLIST_PATH), exist_ok=True)
    exe = _build_app_bundle()
    with open(PLIST_PATH, "w") as f:
        f.write(_PLIST.format(label=JOB_LABEL, exe=exe, interval=interval_sec,
                              path=os.environ.get("PATH", ""),
                              log=LOG_PATH, err=ERR_PATH))
    subprocess.run(["launchctl", "unload", PLIST_PATH],
                   stderr=subprocess.DEVNULL, check=False)
    r = subprocess.run(["launchctl", "load", PLIST_PATH], check=False)
    if r.returncode == 0:
        print("Installed launchd agent → %s (every %d min)"
              % (PLIST_PATH, interval_sec // 60))
    else:
        sys.exit("launchctl load failed (rc=%d)." % r.returncode)


def _uninstall_launchd():
    removed = False
    if os.path.exists(PLIST_PATH):
        subprocess.run(["launchctl", "unload", PLIST_PATH],
                       stderr=subprocess.DEVNULL, check=False)
        os.remove(PLIST_PATH)
        print("Removed launchd agent %s" % PLIST_PATH)
        removed = True
    if os.path.isdir(APP_BUNDLE):
        shutil.rmtree(APP_BUNDLE, ignore_errors=True)
    return removed


# ---------------------------------------------------------------------------
# Scheduler: Linux systemd --user timer, with crontab fallback
# ---------------------------------------------------------------------------
_SYSTEMD_SERVICE = """[Unit]
Description=Refresh my GitHub PR dashboard

[Service]
Type=oneshot
Environment=PATH={path}
ExecStart={python} {script} --no-open
"""

_SYSTEMD_TIMER = """[Unit]
Description=Refresh my GitHub PR dashboard every {min} min

[Timer]
OnBootSec=2min
OnUnitActiveSec={sec}s
Persistent=true

[Install]
WantedBy=timers.target
"""


def _systemd_user_available():
    if not _have("systemctl"):
        return False
    r = subprocess.run(["systemctl", "--user", "show-environment"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    return r.returncode == 0


def _install_systemd(interval_sec):
    os.makedirs(SYSTEMD_DIR, exist_ok=True)
    with open(SYSTEMD_SERVICE, "w") as f:
        f.write(_SYSTEMD_SERVICE.format(python=sys.executable, script=SCRIPT,
                                        path=os.environ.get("PATH", "")))
    with open(SYSTEMD_TIMER, "w") as f:
        f.write(_SYSTEMD_TIMER.format(min=interval_sec // 60, sec=interval_sec))
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    r = subprocess.run(["systemctl", "--user", "enable", "--now",
                        JOB_LABEL + ".timer"], check=False)
    if r.returncode == 0:
        print("Installed systemd --user timer (every %d min)." % (interval_sec // 60))
    else:
        sys.exit("systemctl --user enable failed (rc=%d)." % r.returncode)


def _read_crontab():
    r = subprocess.run(["crontab", "-l"], stdout=subprocess.PIPE,
                       stderr=subprocess.DEVNULL, check=False)
    return r.stdout.decode() if r.returncode == 0 else ""


def _write_crontab(text):
    p = subprocess.run(["crontab", "-"], input=text.encode(), check=False)
    return p.returncode == 0


def _install_cron(interval_sec):
    if not _have("crontab"):
        sys.exit("Neither systemd --user nor crontab is available; cannot "
                 "schedule. Run `--no-open` periodically yourself.")
    redirect = ">> %s 2>> %s" % (LOG_PATH, ERR_PATH)
    line = "PATH=%s\n*/%d * * * * %s %s --no-open %s # %s" % (
        os.environ.get("PATH", ""), interval_sec // 60,
        sys.executable, SCRIPT, redirect, JOB_LABEL)
    kept = "\n".join(l for l in _read_crontab().splitlines() if JOB_LABEL not in l)
    new = (kept + "\n" if kept.strip() else "") + line + "\n"
    if _write_crontab(new):
        print("Installed crontab entry (every %d min)." % (interval_sec // 60))
    else:
        sys.exit("Failed to write crontab.")


def _install_linux(interval_sec):
    if _systemd_user_available():
        _install_systemd(interval_sec)
    else:
        print("systemd --user not available; using crontab instead.")
        _install_cron(interval_sec)


def _uninstall_linux():
    removed = False
    if _have("systemctl") and os.path.exists(SYSTEMD_TIMER):
        subprocess.run(["systemctl", "--user", "disable", "--now",
                        JOB_LABEL + ".timer"], stderr=subprocess.DEVNULL, check=False)
        for p in (SYSTEMD_TIMER, SYSTEMD_SERVICE):
            if os.path.exists(p):
                os.remove(p)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        print("Removed systemd --user timer.")
        removed = True
    if _have("crontab"):
        ct = _read_crontab()
        if JOB_LABEL in ct:
            kept = "\n".join(l for l in ct.splitlines() if JOB_LABEL not in l)
            _write_crontab(kept + ("\n" if kept.strip() else ""))
            print("Removed crontab entry.")
            removed = True
    return removed


# ---------------------------------------------------------------------------
# Scheduler: Windows schtasks
# ---------------------------------------------------------------------------
def _install_windows(interval_sec):
    cmd = '"%s" "%s" --no-open' % (sys.executable, SCRIPT)
    r = subprocess.run(["schtasks", "/Create", "/TN", JOB_NAME, "/SC", "MINUTE",
                        "/MO", str(max(1, interval_sec // 60)), "/TR", cmd, "/F"],
                       check=False)
    if r.returncode == 0:
        print("Installed Scheduled Task '%s' (every %d min)."
              % (JOB_NAME, interval_sec // 60))
    else:
        sys.exit("schtasks /Create failed (rc=%d)." % r.returncode)


def _uninstall_windows():
    r = subprocess.run(["schtasks", "/Delete", "/TN", JOB_NAME, "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    if r.returncode == 0:
        print("Removed Scheduled Task '%s'." % JOB_NAME)
        return True
    return False


def install_agent(interval_sec):
    s = sys.platform
    if s == "darwin":
        _install_launchd(interval_sec)
    elif s.startswith("linux"):
        _install_linux(interval_sec)
    elif s.startswith("win"):
        _install_windows(interval_sec)
    else:
        sys.exit("Auto-refresh not supported on platform '%s'. Run with "
                 "--no-open on a schedule yourself." % s)
    print("Open this file any time: %s" % INDEX_HTML)


def uninstall_agent():
    s = sys.platform
    if s == "darwin":
        removed = _uninstall_launchd()
    elif s.startswith("linux"):
        removed = _uninstall_linux()
    elif s.startswith("win"):
        removed = _uninstall_windows()
    else:
        removed = False
    if not removed:
        print("No auto-refresh job was installed.")


def open_in_browser(path):
    try:
        if webbrowser.open("file://" + os.path.abspath(path)):
            return
    except webbrowser.Error:
        pass
    print("Open this file in your browser: %s" % path)


# ---------------------------------------------------------------------------
# --serve: localhost-only web server so the dashboard can toggle config live
# ---------------------------------------------------------------------------
class _Handler(SimpleHTTPRequestHandler):
    """Serves the dashboard dir plus a tiny /api/notify config endpoint.

    Bound to 127.0.0.1 only. The /api/notify endpoint lets the page read and
    flip the persistent notification setting (config.json) — the same file the
    background agent reads each run, so changes take effect immediately.
    """

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=HERE, **kw)

    def log_message(self, *a):
        pass  # quiet

    def _send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?")[0] == "/api/notify":
            return self._send_json({"notify": load_config().get("notify", True)})
        return super().do_GET()

    def do_POST(self):
        if self.path.split("?")[0] != "/api/notify":
            return self._send_json({"error": "not found"}, 404)
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
            cfg = load_config()
            cfg["notify"] = bool(payload.get("notify"))
            save_config(cfg)
            self._send_json({"notify": cfg["notify"]})
        except (ValueError, OSError) as e:
            self._send_json({"error": str(e)}, 400)


def serve(port, do_open=True):
    httpd = HTTPServer(("127.0.0.1", port), _Handler)
    url = "http://127.0.0.1:%d/index.html" % httpd.server_address[1]
    print("Serving dashboard at %s  (Ctrl-C to stop)" % url)
    print("  Notification toggle in the page writes config.json live.")
    if do_open:
        try:
            webbrowser.open(url)
        except webbrowser.Error:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        httpd.server_close()


# ---------------------------------------------------------------------------
# Native OS notifications (best-effort; never raise)
# ---------------------------------------------------------------------------
_EVENT_LABEL = {
    "approved": "✅ Approved",
    "changes_requested": "🔧 Changes requested",
    "commented": "💬 New comment",
    "merged": "🎉 Merged",
    "closed": "🚫 Closed",
    "ci_failed": "❌ CI failed",
}


def _notify_one(title, message, url=None):
    """Fire a single native desktop notification on the current OS."""
    s = sys.platform
    try:
        if s == "darwin":
            if _have("terminal-notifier"):
                cmd = ["terminal-notifier", "-title", title, "-message", message]
                if url:
                    cmd += ["-open", url]
                subprocess.run(cmd, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, check=False)
            else:
                # osascript can't open a URL on click, but always works out of the box.
                script = 'display notification %s with title %s' % (
                    json.dumps(message), json.dumps(title))
                subprocess.run(["osascript", "-e", script],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               check=False)
        elif s.startswith("linux"):
            if _have("notify-send"):
                subprocess.run(["notify-send", title, message],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               check=False)
        elif s.startswith("win"):
            ps = (
                "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null;"
                "$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
                "$x=$t.GetElementsByTagName('text');"
                "$x.Item(0).AppendChild($t.CreateTextNode(%s)) > $null;"
                "$x.Item(1).AppendChild($t.CreateTextNode(%s)) > $null;"
                "$n=[Windows.UI.Notifications.ToastNotification]::new($t);"
                "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('PR Dashboard').Show($n);"
                % (_ps_quote(title), _ps_quote(message))
            )
            subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           check=False)
    except Exception:
        pass  # notifications are best-effort; never break the refresh


def _ps_quote(s):
    return "'" + s.replace("'", "''") + "'"


def notify_events(events):
    """Send desktop notifications for a batch of events (coalesced if many)."""
    if not events:
        return
    if len(events) > 5:
        # Avoid a notification storm: summarize.
        kinds = {}
        for e in events:
            kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
        summary = ", ".join("%d %s" % (v, _EVENT_LABEL.get(k, k))
                            for k, v in kinds.items())
        _notify_one("PR Dashboard — %d updates" % len(events), summary)
        return
    for e in events:
        label = _EVENT_LABEL.get(e["kind"], e["kind"])
        body = "%s #%d — %s" % (e["repo"], e["number"], e["title"])
        _notify_one(label, body, e.get("url"))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="Local dashboard of the GitHub PRs you authored "
                    "(cross-platform, zero dependencies, uses the gh CLI).")
    ap.add_argument("--no-open", action="store_true",
                    help="fetch + build without opening the browser (used by the scheduler)")
    ap.add_argument("--check", action="store_true",
                    help="run the environment preflight only, then exit")
    ap.add_argument("--install", action="store_true",
                    help="fetch + build, then schedule auto-refresh (launchd/systemd/schtasks)")
    ap.add_argument("--uninstall", action="store_true",
                    help="remove the scheduled auto-refresh job")
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_MIN, metavar="MIN",
                    help="auto-refresh interval in minutes (default %d)" % DEFAULT_INTERVAL_MIN)
    ap.add_argument("--full", action="store_true",
                    help="force a complete year-by-year re-fetch (not just the incremental refresh)")
    ap.add_argument("--no-notify", action="store_true",
                    help="don't send desktop notifications for new events this run (one-off)")
    ap.add_argument("--notify", choices=["on", "off", "status"], metavar="on|off|status",
                    help="turn desktop notifications on/off persistently (applies to the "
                         "installed background agent too), or show the current setting")
    ap.add_argument("--serve", action="store_true",
                    help="serve the dashboard on localhost so you can toggle notifications "
                         "from within the page (instead of opening the file directly)")
    ap.add_argument("--port", type=int, default=8765, metavar="PORT",
                    help="port for --serve (default 8765)")
    args = ap.parse_args()

    # Persistent notification toggle — takes effect immediately for the installed
    # agent (it reads config.json each run); no reinstall needed.
    if args.notify:
        cfg = load_config()
        if args.notify == "status":
            print("Desktop notifications are currently %s."
                  % ("ON" if cfg.get("notify", True) else "OFF"))
        else:
            cfg["notify"] = (args.notify == "on")
            save_config(cfg)
            print("Desktop notifications turned %s." % args.notify.upper())
        return

    if args.uninstall:
        uninstall_agent()
        return

    print("Environment check (Python %s on %s):"
          % (platform.python_version(), platform.system()))
    check_environment(require_auth=True)
    print("Environment OK.\n")
    if args.check:
        return

    summary = build_data(full=args.full, notify=not args.no_notify)
    msg = "Wrote data.js: %d PRs for @%s" % (summary["count"], summary["user"])
    if summary["first_run"]:
        msg += " (first run — notifications start next refresh)"
    elif summary["events"]:
        msg += " — %d new event(s)%s" % (
            summary["events"],
            " (notified)" if summary["notified"]
            else " (notifications off)" if not summary["notify_on"] else "")
    print(msg)

    if args.install:
        interval_sec = max(1, args.interval) * 60
        install_agent(interval_sec)
    elif args.serve:
        serve(args.port, do_open=not args.no_open)
    elif not args.no_open:
        open_in_browser(INDEX_HTML)


if __name__ == "__main__":
    main()
