/* Renders the PR dashboard from window.PR_DATA (see data.js). */
(function () {
  "use strict";

  const data = window.PR_DATA;
  if (!data || !Array.isArray(data.prs)) {
    const e = document.getElementById("error");
    e.style.display = "block";
    e.textContent =
      "No data found. Run `python3 pr_dashboard.py` to generate data.js, then reload.";
    return;
  }

  const prs = data.prs;
  const HOUR = 3600e3,
    DAY = 24 * HOUR;
  const now = Date.now();

  const fmtInt = (n) => n.toLocaleString();
  const parse = (s) => (s ? Date.parse(s) : null);

  function humanDur(ms) {
    if (ms == null) return "—";
    if (ms < 0) ms = 0; // guard against clock skew / future timestamps
    const h = ms / HOUR;
    if (h < 1) return Math.round(ms / 60000) + "m";
    if (h < 48) return h.toFixed(h < 10 ? 1 : 0) + "h";
    const d = ms / DAY;
    if (d < 60) return d.toFixed(d < 10 ? 1 : 0) + "d";
    return (d / 30.44).toFixed(1) + "mo";
  }
  function ageClass(ms) {
    if (ms > 14 * DAY) return "old";
    if (ms > 4 * DAY) return "stale";
    return "";
  }
  function median(arr) {
    if (!arr.length) return null;
    const s = [...arr].sort((a, b) => a - b);
    const m = Math.floor(s.length / 2);
    return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
  }

  // ---- meta header ----
  const gen = data.generatedAt ? new Date(data.generatedAt) : null;
  document.getElementById("meta").textContent =
    `@${data.user} · ${fmtInt(prs.length)} PRs · data generated ${
      gen ? gen.toLocaleString() : "unknown"
    }`;

  if (data.sample) {
    const b = document.createElement("div");
    b.style.cssText =
      "background:#1c2230;border:1px solid var(--amber);color:var(--amber);" +
      "border-radius:8px;padding:10px 14px;margin-bottom:18px;font-size:0.93rem;";
    b.innerHTML =
      "👋 <b>Showing bundled sample data.</b> Run " +
      "<code>python3 pr_dashboard.py</code> to load <i>your</i> PRs.";
    document.querySelector(".wrap").prepend(b);
  }

  // ---- notification toggle ----
  // The page can only WRITE the setting when served via `pr_dashboard.py --serve`
  // (the /api/notify endpoint). Opened as a plain file, it shows the current
  // state read-only plus the CLI command to flip it.
  setupNotifToggle(data.notify !== false);

  function setupNotifToggle(initialOn) {
    if (data.sample) return; // nothing to toggle for the demo
    const el = document.getElementById("notif");
    el.style.display = "";
    let served = false;
    let state = initialOn;

    function paint() {
      el.classList.toggle("on", state);
      el.classList.toggle("live", served);
      const icon = state ? "🔔" : "🔕";
      const word = state ? "Notifications on" : "Notifications off";
      if (served) {
        el.innerHTML = `${icon} ${word} <span class="hint">· click to toggle</span>`;
        el.title = "Click to turn notifications " + (state ? "off" : "on");
      } else {
        const cmd = state ? "--notify off" : "--notify on";
        el.innerHTML = `${icon} ${word} <span class="hint">· python3 pr_dashboard.py ${cmd}</span>`;
        el.title = "Serve with `python3 pr_dashboard.py --serve` to toggle from here";
      }
    }

    // Probe the API: present only when running under --serve.
    fetch("/api/notify", { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then((j) => {
        served = true;
        state = j.notify !== false;
        paint();
      })
      .catch(() => paint()); // file:// or plain static server -> read-only

    el.addEventListener("click", () => {
      if (!served) return;
      const next = !state;
      el.style.opacity = "0.5";
      fetch("/api/notify", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ notify: next }),
      })
        .then((r) => r.json())
        .then((j) => {
          state = j.notify !== false;
        })
        .catch(() => {})
        .finally(() => {
          el.style.opacity = "1";
          paint();
        });
    });

    paint();
  }

  // ---- categorize open PRs into pending-work buckets ----
  const open = prs.filter((p) => p.state === "OPEN");
  const buckets = { draft: [], review: [], changes: [], ready: [] };
  for (const p of open) {
    if (p.isDraft) buckets.draft.push(p);
    else if (p.reviewDecision === "CHANGES_REQUESTED") buckets.changes.push(p);
    else if (p.reviewDecision === "APPROVED") buckets.ready.push(p);
    else buckets.review.push(p); // null / REVIEW_REQUIRED -> awaiting first review
  }

  const COLS = [
    { key: "ready", cls: "ready", title: "✅ Ready to merge", dot: "var(--green)" },
    { key: "changes", cls: "changes", title: "🔧 Changes requested", dot: "var(--red)" },
    { key: "review", cls: "review", title: "👀 Awaiting review", dot: "var(--amber)" },
    { key: "draft", cls: "draft", title: "✏️ Draft", dot: "var(--gray)" },
  ];

  function ciPill(p) {
    if (!p.ci) return "";
    const label = { SUCCESS: "CI ✓", FAILURE: "CI ✗", ERROR: "CI ✗", PENDING: "CI …", EXPECTED: "CI …" }[p.ci] || "CI";
    return `<span class="pill ci-${p.ci}">${label}</span>`;
  }
  function labelChips(p) {
    return (p.labels || [])
      .map((l) => {
        const c = "#" + (l.color || "888888");
        return `<span class="lbl-chip" style="color:${c};border-color:${c}55;background:${c}14">${l.name}</span>`;
      })
      .join("");
  }
  function card(p) {
    const age = now - parse(p.createdAt);
    const updated = now - parse(p.updatedAt);
    return `<div class="card">
      <a class="t" href="${p.url}" target="_blank" rel="noopener">${escapeHtml(p.title)}</a>
      <span class="repo">${p.repo} #${p.number}</span>
      <div class="row2">
        ${ciPill(p)}
        ${labelChips(p)}
        <span title="opened ${new Date(p.createdAt).toLocaleString()}">opened ${humanDur(age)} ago</span>
        ${p.comments ? `<span>💬 ${p.comments}</span>` : ""}
        <span class="age ${ageClass(updated)}" title="last activity ${new Date(p.updatedAt).toLocaleString()}">↻ ${humanDur(updated)}</span>
      </div>
    </div>`;
  }

  const board = document.getElementById("board");
  board.innerHTML = COLS.map((c) => {
    const list = buckets[c.key].sort(
      (a, b) => parse(a.updatedAt) - parse(b.updatedAt) // most stale first
    );
    const body = list.length
      ? list.map(card).join("")
      : `<div class="empty">Nothing here 🎉</div>`;
    return `<div class="col ${c.cls}">
      <h3><span><span class="dot" style="background:${c.dot}"></span> ${c.title}</span>
          <span class="badge">${list.length}</span></h3>
      <div class="col-body">${body}</div>
    </div>`;
  }).join("");

  // ---- latest-updates feed ----
  const EV_LABEL = {
    approved: "✅ Approved",
    changes_requested: "🔧 Changes requested",
    commented: "💬 New comment",
    merged: "🎉 Merged",
    closed: "🚫 Closed",
    ci_failed: "❌ CI failed",
  };
  function relTime(iso) {
    const ms = now - parse(iso);
    if (ms < 0) return "just now";
    if (ms < HOUR) return Math.max(1, Math.round(ms / 60000)) + "m ago";
    if (ms < DAY) return Math.round(ms / HOUR) + "h ago";
    return Math.round(ms / DAY) + "d ago";
  }
  const events = Array.isArray(data.events) ? data.events : [];
  if (events.length) {
    document.getElementById("updates-h").style.display = "";
    document.getElementById("updates-box").style.display = "";
    const lbl = (e) =>
      EV_LABEL[e.kind] +
      (e.kind === "commented" && e.delta > 1 ? ` (+${e.delta})` : "");
    const feedEl = document.getElementById("feed");

    function renderFeed(limit) {
      const shown = limit === "all" ? events : events.slice(0, limit);
      feedEl.innerHTML = shown
        .map(
          (e) => `<li>
        <span class="ev ${e.kind}">${lbl(e)}</span>
        <a class="ftitle" href="${e.url}" target="_blank" rel="noopener">${escapeHtml(e.title)}</a>
        <span class="frepo">${e.repo} #${e.number}</span>
        <span class="fwhen">${relTime(e.at)}</span>
      </li>`
        )
        .join("");
    }

    // Switchable count: 10 (default) / 50 / 100 / All. Only offer sizes the
    // feed can actually reach, plus "All" to navigate the complete list.
    const OPTIONS = [10, 50, 100].filter((n) => n < events.length);
    OPTIONS.push("all");
    const labelFor = (o) => (o === "all" ? `All (${events.length})` : String(o));

    let feedLimit = sessionStorage.getItem("prDashFeedLimit") || "10";
    feedLimit = feedLimit === "all" ? "all" : +feedLimit;
    if (feedLimit !== "all" && !OPTIONS.includes(feedLimit)) feedLimit = OPTIONS[0];

    const seg = document.getElementById("feed-seg");
    if (OPTIONS.length > 1) {
      seg.innerHTML = OPTIONS.map(
        (o) => `<button data-n="${o}">${labelFor(o)}</button>`
      ).join("");
      const paintSeg = () =>
        seg.querySelectorAll("button").forEach((b) =>
          b.classList.toggle("active", b.dataset.n === String(feedLimit))
        );
      seg.addEventListener("click", (ev) => {
        const b = ev.target.closest("button");
        if (!b) return;
        feedLimit = b.dataset.n === "all" ? "all" : +b.dataset.n;
        sessionStorage.setItem("prDashFeedLimit", feedLimit);
        paintSeg();
        renderFeed(feedLimit);
      });
      paintSeg();
    }
    renderFeed(feedLimit);
  }

  // ---- all-time KPIs ----
  const merged = prs.filter((p) => p.state === "MERGED");
  const closed = prs.filter((p) => p.state === "CLOSED");
  const ttms = merged
    .map((p) => parse(p.mergedAt) - parse(p.createdAt))
    .filter((v) => v != null && v >= 0);
  const medTtm = median(ttms);
  const meanTtm = ttms.length ? ttms.reduce((a, b) => a + b, 0) / ttms.length : null;
  const repos = new Set(prs.map((p) => p.repo));
  const totalAdds = prs.reduce((a, p) => a + (p.additions || 0), 0);
  const totalDels = prs.reduce((a, p) => a + (p.deletions || 0), 0);
  const mergeRate = prs.length ? (merged.length / prs.length) * 100 : 0;

  // merged in last 30 / 90 days
  const mergedLast = (days) =>
    merged.filter((p) => now - parse(p.mergedAt) <= days * DAY).length;

  const kpis = [
    { num: fmtInt(prs.length), lbl: "Total PRs", sub: `across ${repos.size} repos` },
    { num: fmtInt(merged.length), lbl: "Merged", sub: `${mergeRate.toFixed(1)}% merge rate` },
    { num: fmtInt(open.length), lbl: "Open now", sub: `${buckets.ready.length} ready · ${buckets.changes.length} need changes` },
    { num: fmtInt(closed.length), lbl: "Closed unmerged", sub: `${((closed.length / prs.length) * 100).toFixed(1)}% of total` },
    { num: humanDur(medTtm), lbl: "Median time-to-merge", sub: `mean ${humanDur(meanTtm)}` },
    { num: fmtInt(mergedLast(30)), lbl: "Merged · last 30d", sub: `${fmtInt(mergedLast(90))} in last 90d` },
    { num: "+" + fmtInt(totalAdds), lbl: "Lines added", sub: `−${fmtInt(totalDels)} removed` },
  ];
  document.getElementById("kpis").innerHTML = kpis
    .map(
      (k) =>
        `<div class="kpi"><div class="num">${k.num}</div><div class="lbl">${k.lbl}</div><div class="sub">${k.sub}</div></div>`
    )
    .join("");

  // ---- yearly aggregation ----
  const years = {};
  for (const p of prs) {
    const y = p.createdAt.slice(0, 4);
    (years[y] ||= { total: 0, merged: 0, closed: 0, open: 0, ttms: [] });
    years[y].total++;
    if (p.state === "MERGED") {
      years[y].merged++;
      const t = parse(p.mergedAt) - parse(p.createdAt);
      if (t >= 0) years[y].ttms.push(t);
    } else if (p.state === "CLOSED") years[y].closed++;
    else years[y].open++;
  }
  const yKeys = Object.keys(years).sort();

  // ---- charts ----
  Chart.defaults.color = "#8b949e";
  Chart.defaults.borderColor = "#30363d";
  Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
  const noLegend = { plugins: { legend: { display: false } } };

  new Chart(document.getElementById("byYear"), {
    type: "bar",
    data: {
      labels: yKeys,
      datasets: [{ data: yKeys.map((y) => years[y].total), backgroundColor: "#58a6ff" }],
    },
    options: { ...noLegend, responsive: true, maintainAspectRatio: false },
  });

  new Chart(document.getElementById("outcome"), {
    type: "bar",
    data: {
      labels: yKeys,
      datasets: [
        { label: "Merged", data: yKeys.map((y) => years[y].merged), backgroundColor: "#3fb950" },
        { label: "Closed", data: yKeys.map((y) => years[y].closed), backgroundColor: "#6e7681" },
        { label: "Open", data: yKeys.map((y) => years[y].open), backgroundColor: "#d29922" },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      scales: { x: { stacked: true }, y: { stacked: true } },
    },
  });

  // time-to-merge histogram buckets
  const ttmBuckets = [
    { lbl: "<1h", max: HOUR },
    { lbl: "1–6h", max: 6 * HOUR },
    { lbl: "6–24h", max: DAY },
    { lbl: "1–3d", max: 3 * DAY },
    { lbl: "3–7d", max: 7 * DAY },
    { lbl: "1–4wk", max: 28 * DAY },
    { lbl: ">4wk", max: Infinity },
  ];
  const ttmCounts = ttmBuckets.map(() => 0);
  for (const t of ttms) {
    const i = ttmBuckets.findIndex((b) => t < b.max);
    ttmCounts[i >= 0 ? i : ttmBuckets.length - 1]++;
  }
  new Chart(document.getElementById("ttmDist"), {
    type: "bar",
    data: {
      labels: ttmBuckets.map((b) => b.lbl),
      datasets: [{ data: ttmCounts, backgroundColor: "#a371f7" }],
    },
    options: { ...noLegend, responsive: true, maintainAspectRatio: false },
  });

  new Chart(document.getElementById("ttmYear"), {
    type: "line",
    data: {
      labels: yKeys,
      datasets: [
        {
          data: yKeys.map((y) => {
            const m = median(years[y].ttms);
            return m == null ? null : +(m / HOUR).toFixed(1);
          }),
          borderColor: "#3fb950",
          backgroundColor: "rgba(63,185,80,.15)",
          tension: 0.3,
          fill: true,
          spanGaps: true,
        },
      ],
    },
    options: { ...noLegend, responsive: true, maintainAspectRatio: false },
  });

  // ---- repo table ----
  const repoAgg = {};
  for (const p of prs) {
    const r = (repoAgg[p.repo] ||= { repo: p.repo, total: 0, merged: 0, open: 0, closed: 0, ttms: [] });
    r.total++;
    if (p.state === "MERGED") {
      r.merged++;
      const t = parse(p.mergedAt) - parse(p.createdAt);
      if (t >= 0) r.ttms.push(t);
    } else if (p.state === "CLOSED") r.closed++;
    else r.open++;
  }
  let rows = Object.values(repoAgg).map((r) => ({
    ...r,
    mergeRate: r.total ? (r.merged / r.total) * 100 : 0,
    medTtm: median(r.ttms),
  }));
  const maxTotal = Math.max(...rows.map((r) => r.total));

  // Persist the chosen sort across auto-reloads so a background refresh isn't disruptive.
  let sortKey = sessionStorage.getItem("prDashSortKey") || "total",
    sortDir = +(sessionStorage.getItem("prDashSortDir") || -1);
  function renderTable() {
    rows.sort((a, b) => {
      let av = a[sortKey],
        bv = b[sortKey];
      if (sortKey === "repo") return sortDir * av.localeCompare(bv);
      av = av == null ? -1 : av;
      bv = bv == null ? -1 : bv;
      return sortDir * (av - bv);
    });
    const tb = document.querySelector("#repoTable tbody");
    tb.innerHTML = rows
      .map((r) => {
        const repoUrl = "https://github.com/" + r.repo;
        const w = ((r.total / maxTotal) * 100).toFixed(1);
        return `<tr>
        <td class="bar-cell"><div class="mini-bar" style="width:${w}%"></div><span style="position:relative"><a href="${repoUrl}/pulls?q=is%3Apr+author%3A${data.user}" target="_blank" rel="noopener">${r.repo}</a></span></td>
        <td class="num">${r.total}</td>
        <td class="num">${r.merged}</td>
        <td class="num">${r.open || ""}</td>
        <td class="num">${r.closed || ""}</td>
        <td class="num">${r.mergeRate.toFixed(0)}%</td>
        <td class="num">${humanDur(r.medTtm)}</td>
      </tr>`;
      })
      .join("");
  }
  document.querySelectorAll("#repoTable th").forEach((th) => {
    th.addEventListener("click", () => {
      const k = th.dataset.k;
      if (k === sortKey) sortDir *= -1;
      else {
        sortKey = k;
        sortDir = k === "repo" ? 1 : -1;
      }
      sessionStorage.setItem("prDashSortKey", sortKey);
      sessionStorage.setItem("prDashSortDir", sortDir);
      renderTable();
    });
  });
  renderTable();

  // ---- footnote ----
  document.getElementById("footnote").textContent =
    `Pending board shows your ${open.length} open PRs. ` +
    `Time-to-merge computed from ${ttms.length} merged PRs. ` +
    `CI status shown for open PRs only. Re-run pr_dashboard.py to refresh.`;

  function escapeHtml(s) {
    return s.replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
    );
  }

  // ---- auto-reload when the background refresh writes new data ----
  // A scheduled `pr_dashboard.py --no-open` rewrites data.js. If this tab is
  // left open, poll data.js and reload once its generatedAt changes, so the
  // view stays current without manual refreshing. (No-op for sample data.)
  if (!data.sample) {
    const myStamp = data.generatedAt;
    setInterval(() => {
      fetch("data.js?_=" + Date.now(), { cache: "no-store" })
        .then((r) => (r.ok ? r.text() : null))
        .then((txt) => {
          if (!txt) return;
          const m = txt.match(/"generatedAt"\s*:\s*"([^"]+)"/);
          if (m && m[1] !== myStamp) location.reload();
        })
        .catch(() => {}); // offline / file:// fetch blocked — ignore, manual reload still works
    }, 120000); // every 2 min
  }
})();
