# 📋 PR Dashboard

A self-contained, **local** dashboard for the pull requests *you* have authored
on GitHub. It shows your pending work organized by review status, a live feed of
the latest activity, and all-time statistics — merge rate, time-to-merge,
per-year trends, and a per-repository breakdown. Optionally it runs in the
background and sends you **desktop notifications** when a PR is approved, gets a
new comment, is merged, and so on.

No server, no build step, no third-party services. It runs entirely from your
machine using the [GitHub CLI](https://cli.github.com/) you're already logged
into, and **your data never leaves your computer**.

![sample](docs/screenshot.png)

> The repo ships with **synthetic sample data** so the dashboard renders
> immediately. Run the fetcher to replace it with your own PRs.

## What it shows

**Pending work board** — your open PRs, grouped into actionable columns:

| Column | Meaning |
| --- | --- |
| ✅ Ready to merge | Open, review **approved** |
| 🔧 Changes requested | A reviewer requested changes |
| 👀 Awaiting review | Open, no decision yet (review required / not started) |
| ✏️ Draft | Marked as a draft |

Each card links straight to the PR on GitHub and shows CI status, labels, age,
and time since last activity (stale PRs are highlighted).

**Latest updates** — a feed of recent activity detected between refreshes:
approved ✅, new comment 💬, merged 🎉, changes requested 🔧, CI failed ❌, closed 🚫.

**All-time stats** — total PRs, merge rate, closed-unmerged count, median & mean
**time-to-merge**, PRs merged in the last 30 / 90 days, lines added/removed,
charts (PRs per year, outcome per year, time-to-merge distribution & trend), and
a sortable per-repository table.

## Requirements

- [GitHub CLI](https://cli.github.com/) (`gh`), authenticated: `gh auth login`
- Python 3.7+ (standard library only — nothing to `pip install`)
- Any modern web browser
- *Optional, for desktop notifications:* `terminal-notifier` (macOS) or
  `notify-send` (Linux) — see [Notification permissions per OS](#notification-permissions-per-os)

## Quick start

```bash
git clone https://github.com/<you>/pr-dashboard.git
cd pr-dashboard
python3 pr_dashboard.py     # checks your env, fetches your PRs, opens the dashboard
```

The first run does a full backfill of your PR history (this can take a few
minutes if you have thousands of PRs — GitHub's API is the bottleneck).
Every run after that is an **incremental refresh** that takes seconds.

## Staying up to date

By default the dashboard is a snapshot — it reflects the last time you ran the
fetcher. You can keep it current automatically:

```bash
python3 pr_dashboard.py --install              # refresh in the background every 30 min
python3 pr_dashboard.py --install --interval 10 # ...every 10 min instead
python3 pr_dashboard.py --uninstall            # stop the background refresh
```

`--install` registers a per-user scheduled job using your OS's native mechanism:

| OS | Backend |
| --- | --- |
| macOS | `launchd` LaunchAgent (`~/Library/LaunchAgents`) |
| Linux | `systemd --user` timer (falls back to a `crontab` entry) |
| Windows | Scheduled Task (`schtasks`) |

While the background job runs, it diffs each refresh against the previous one
and sends a **native desktop notification** for new activity. If you leave the
dashboard open in a browser tab, it auto-reloads when fresh data arrives, so the
page stays live without a manual refresh.

### When you get a notification

On each scheduled refresh, the agent compares the new data to the previous run
and fires a notification when it detects one of these changes on one of your PRs:

| Event | Fires when… |
| --- | --- |
| ✅ Approved | the review decision flips to `APPROVED` (while open) |
| 🔧 Changes requested | the review decision flips to `CHANGES_REQUESTED` (while open) |
| 💬 New comment | someone else adds a comment — conversation, inline code review, or a review with a body (shows `+N`) |
| 🎉 Merged | the PR becomes merged |
| 🚫 Closed | the PR is closed without merging |
| ❌ CI failed | checks flip to `FAILURE` / `ERROR` (while open) |

So you get it on the **next scheduled refresh after the change** — i.e. within
one refresh interval (e.g. ≤10 min), **not in real time**. A few details worth
knowing:

- **Not the first run.** The initial backfill is intentionally silent — detection
  starts from the *second* refresh onward, so you don't get a storm.
- **Coalescing.** If a single refresh turns up **more than 5 events**, they're
  combined into one summary notification ("PR Dashboard — N updates") instead of
  a flood.
- **Which PRs are watched.** Notifications only cover what the incremental
  refresh fetches: all your **open** PRs, anything from the **current year**, and
  the most **recently-updated closed** PRs. (A merge/close of an older PR is still
  caught — it was open the previous cycle and shows up in the recently-closed
  sweep.)
- **💬 covers all three comment sources.** Conversation-timeline comments,
  *inline code-review comments*, and reviews that carry a body all count. A bare
  approve / request-changes with no text doesn't fire 💬 — the ✅ / 🔧 event
  already covers it.
- **Your own comments are excluded.** Only activity from *other* people triggers
  💬, so commenting on your own PR won't ping you.
- **One-cycle warm-up.** The first refresh after installing (or upgrading) records
  the comment baseline silently; 💬 detection is live from the cycle after that.
- A net-zero comment change between refreshes (one added, one deleted) won't notify.

### Turning notifications on/off

Notifications can be toggled at any time — the setting lives in `config.json`,
which the background agent re-reads on every run, so there's **no need to
reinstall**:

```bash
python3 pr_dashboard.py --notify off      # silence
python3 pr_dashboard.py --notify on       # re-enable
python3 pr_dashboard.py --notify status   # show current setting
```

You can also toggle it **from within the dashboard**. Launch it in served mode:

```bash
python3 pr_dashboard.py --serve           # http://127.0.0.1:8765, opens your browser
```

…then click the **🔔 / 🔕 pill in the top-right** to flip notifications on/off.
(This needs `--serve` because a page opened as a plain `file://` can't write to
disk. Opened directly, the pill still shows the current state and the command to
change it.) `--no-notify` additionally silences a single run without changing
the saved setting.

### Notification permissions per OS

Desktop notifications are **not fully automatic** — each OS gates them the first
time, and macOS in particular needs a one-time approval:

> **macOS.** There's nothing to `pip install`, but macOS requires a **one-time
> permission grant**, and notifications sent from a background `launchd` agent
> are *silently dropped* until the sending app has been allowed. For this to work
> reliably, install
> [`terminal-notifier`](https://github.com/julienXX/terminal-notifier):
> ```bash
> brew install terminal-notifier
> ```
> The tool uses it automatically when present (it also makes notifications
> **clickable** — opening the PR on GitHub). Fire a test to trigger the one-time
> allow prompt, then approve **terminal-notifier** under *System Settings →
> Notifications*:
> ```bash
> python3 -c "import importlib.util as u; s=u.spec_from_file_location('p','pr_dashboard.py'); m=u.module_from_spec(s); s.loader.exec_module(m); m._notify_one('PR Dashboard','Notifications are working ✅')"
> ```
> Without `terminal-notifier` the tool falls back to `osascript`, which works
> from a terminal but is unreliable from the background agent.
>
> **Linux.** Desktop notifications require `notify-send` (from `libnotify`),
> typically already present on GNOME/KDE; no per-app allow step.
>
> **Windows.** Toasts work out of the box via PowerShell — no manual approval.

## Commands

```bash
python3 pr_dashboard.py            # check env, fetch, build + open the dashboard
python3 pr_dashboard.py --serve [--port PORT]   # fetch, then serve on localhost (in-page toggle)
python3 pr_dashboard.py --no-open  # fetch + build only (what the scheduler runs)
python3 pr_dashboard.py --check    # environment preflight only, then exit
python3 pr_dashboard.py --full     # force a complete re-fetch, not just incremental
python3 pr_dashboard.py --install [--interval MIN]   # schedule background refresh
python3 pr_dashboard.py --uninstall                  # remove the scheduled job
python3 pr_dashboard.py --notify on|off|status       # toggle notifications (persistent)
python3 pr_dashboard.py --no-notify                  # silence just this run
```

## How it works

```
pr_dashboard.py  ──>  cache.json  ──>  data.js  ──>  index.html + dashboard.js
   (gh API)           (source of        (what the      (renders board, feed,
                       truth)            browser loads)  stats; Chart.js local)
```

- `pr_dashboard.py` queries GitHub's GraphQL search API for `author:@me type:pr`,
  sliced by calendar year (the search API caps results at 1000 per query).
- Results are stored in **`cache.json`**, the local source of truth, saved after
  **every page** — so the fetch is **fully resumable**. If it's interrupted (or
  GitHub returns a transient 502), just run it again and it picks up where it
  left off.
- Merged/closed PRs are **immutable**, so a fully-fetched past year is frozen and
  skipped forever after. An incremental refresh re-queries only what can change:
  your open PRs, the current year, and the most recently-closed PRs — a handful
  of pages, fast enough to run on a frequent schedule.
- Each refresh diffs the new data against the previous snapshot to produce the
  **events** feed and trigger desktop notifications.
- `data.js` (a single `window.PR_DATA = {…}` assignment) is regenerated from the
  cache. Loading it as a plain `<script>` avoids `file://` CORS issues, so the
  dashboard works by just opening the HTML file — no local web server required.
- The dashboard is plain HTML/CSS/JS. [Chart.js](https://www.chartjs.org/) is
  vendored under `vendor/` so it works fully offline.

## Privacy

This tool runs locally and talks only to GitHub's API via your own `gh` auth.

**Your PR data is never committed.** `data.js` and `cache.json` (which contain
your PR titles and repository names), plus the scheduler's `agent.log` files, are
in `.gitignore`. The repository contains only the tooling plus synthetic sample
data — nothing personal. If you fork this, keep that `.gitignore` intact before
committing.

## Files

| File | Purpose |
| --- | --- |
| `pr_dashboard.py` | Everything: fetch, build, scheduler install/uninstall, notifications |
| `index.html` | The dashboard page |
| `dashboard.js` | Rendering logic (board, feed, KPIs, charts, table, auto-reload) |
| `data.sample.js` | Synthetic demo data (no real data) |
| `vendor/chart.umd.min.js` | Chart.js, vendored for offline use |
| `data.js`, `cache.json` | **Your** data — generated locally, git-ignored |
| `config.json` | Local settings (notifications on/off) — git-ignored |

## Notes & limitations

- The first full fetch of a long PR history makes many API calls and can take a
  few minutes; subsequent refreshes are quick thanks to the cache.
- GitHub's GraphQL endpoint occasionally returns transient `502`s under load —
  the fetcher retries with backoff and the cache makes it safe to re-run.
- CI status is fetched only for open PRs (it's not meaningful for old merged
  ones and keeps the fetch cheap).
- Time-to-merge is measured from PR creation to merge.
- Notifications are not real-time and have a few caveats (inline comments, timing,
  coalescing) — see [When you get a notification](#when-you-get-a-notification).

## License

MIT
