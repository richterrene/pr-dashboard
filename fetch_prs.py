#!/usr/bin/env python3
"""Fetch every PR authored by the authenticated gh user into a local cache.

Design notes
------------
* GitHub's search API returns at most 1000 results per query, so we slice the
  search by calendar year (max per-year volume is well under 1000).
* Merged/closed PRs are immutable. `cache.json` is the source of truth and is
  saved after *every page*, so the fetch is fully resumable: re-running picks up
  where it left off and never re-pulls PRs from a year that is already complete.
* A past year is marked complete once all its cached PRs are in a terminal state
  (MERGED/CLOSED). The current year, and any past year still holding an OPEN PR,
  are re-fetched on each run so their status stays fresh.
* The bulk sweep uses a CHEAP query (no commit/statusCheckRollup nesting) — that
  nesting is what triggered HTTP 502s. CI status is enriched separately for only
  the handful of OPEN PRs.
* `data.js` (consumed by the dashboard) is regenerated from the cache at the end,
  so even a partial/interrupted fetch still produces a usable dashboard.
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(HERE, "cache.json")
DATA_PATH = os.path.join(HERE, "data.js")
FALLBACK_FIRST_YEAR = 2008  # GitHub launch year; used only if detection fails
PAGE_PAUSE = 1.0          # seconds between page requests (avoid secondary limits)
SECONDARY_LIMIT_WAIT = 90  # seconds to wait when GitHub reports a secondary limit

# Cheap sweep query: no commit/CI nesting (that nesting caused 502s at scale).
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


def key(repo, number):
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
    args = ["gh", "api", "graphql", "-f", f"query={query}", "-f", f"q={q}"]
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
        # Secondary rate limit: back off hard rather than hammering.
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


def fetch_year(year, cache):
    """Fetch all PRs created in `year`, upserting into cache; save per page."""
    q = f"author:@me type:pr created:{year}-01-01..{year}-12-31"
    after, page_no = None, 0
    while True:
        page = gh_graphql(LIST_QUERY, q, after)
        page_no += 1
        for n in page["nodes"]:
            if not n:
                continue
            rec = normalize(n)
            k = key(rec["repo"], rec["number"])
            # Preserve a previously-enriched CI value if present.
            if k in cache["prs"] and cache["prs"][k].get("ci") is not None:
                rec["ci"] = cache["prs"][k]["ci"]
            cache["prs"][k] = rec
        save_cache(cache)
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]
        time.sleep(PAGE_PAUSE)
    return page_no


def enrich_open_ci(cache):
    """Fetch CI rollup for OPEN PRs only (cheap: a handful of PRs)."""
    q = "author:@me type:pr state:open"
    after = None
    seen = 0
    while True:
        page = gh_graphql(CI_QUERY, q, after)
        for n in page["nodes"]:
            if not n:
                continue
            k = key(n["repository"]["nameWithOwner"], n["number"])
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


def write_data_js(cache, me):
    prs = sorted(cache["prs"].values(), key=lambda p: p["createdAt"], reverse=True)
    payload = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "user": me,
        "count": len(prs),
        "prs": prs,
    }
    with open(DATA_PATH, "w") as f:
        f.write("window.PR_DATA = ")
        json.dump(payload, f)
        f.write(";\n")


def main():
    now = datetime.now(timezone.utc)
    me = subprocess.run(["gh", "api", "user", "--jq", ".login"],
                        capture_output=True, text=True).stdout.strip()
    cache = load_cache()
    cur_year = now.year

    first_year = cache.get("firstYear")
    if not first_year:
        first_year = detect_first_year()
        cache["firstYear"] = first_year
        save_cache(cache)
    sys.stderr.write(f"Fetching PRs for @{me} from {first_year} to {cur_year}\n")

    interrupted = False
    try:
        for year in range(first_year, cur_year + 1):
            if cache["yearsComplete"].get(str(year)):
                sys.stderr.write(f"  {year}: cached (complete), skipping\n")
                continue
            pages = fetch_year(year, cache)
            n_year = sum(1 for p in cache["prs"].values()
                         if p["createdAt"].startswith(f"{year}-"))
            # A past year with no open PRs will never change again -> freeze it.
            if year < cur_year and not year_has_open(cache, year):
                cache["yearsComplete"][str(year)] = True
                tag = "complete"
            else:
                tag = "will recheck"
            sys.stderr.write(f"  {year}: {n_year} PRs, {pages} page(s) [{tag}]\n")
    except (RuntimeError, KeyboardInterrupt) as e:
        interrupted = True
        sys.stderr.write(f"\n!! Stopped early ({e}).\n"
                         f"   Progress saved to cache.json — just re-run to resume.\n")

    # Enrich CI for open PRs (best-effort; don't lose the sweep if this fails).
    if not interrupted:
        try:
            n = enrich_open_ci(cache)
            sys.stderr.write(f"  CI enriched for {n} open PR(s)\n")
        except RuntimeError as e:
            sys.stderr.write(f"  CI enrichment skipped ({e})\n")

    cache["fetchedAt"] = now.isoformat()
    save_cache(cache)
    write_data_js(cache, me)
    sys.stderr.write(f"Wrote data.js with {len(cache['prs'])} PRs for {me}\n")
    if interrupted:
        sys.exit(1)


if __name__ == "__main__":
    main()
