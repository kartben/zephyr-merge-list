/*
 * Zephyr Merge List — frontend renderer.
 *
 * Loads the structured data.json produced by merge_list.py and renders it as a
 * single DataTable grouped (via RowGroup) into three plain-language states:
 *   ready → "Ready to merge now"
 *   waiting → "Waiting on review window"
 *   attention → "Needs attention"
 *
 * The whole merge policy is already resolved server-side into each PR's
 * `state`, `blockers` and `ready_at`; this file is purely presentation.
 */
(function () {
  "use strict";

  var DATA_URL = "data.json";
  var MOTD_URL =
    "https://raw.githubusercontent.com/wiki/zephyrproject-rtos/zephyr/Merge-List-MOTD.md";
  var REFRESH_MS = 60000;

  // Display metadata for each state group. `rank` fixes the group order.
  var GROUPS = {
    ready: {
      rank: 0, cls: "group-ready", title: "Ready to merge now",
      hint: "Meets every requirement — safe to merge",
    },
    waiting: {
      rank: 1, cls: "group-waiting", title: "Waiting on review window",
      hint: "Approved — just waiting out the mandatory clock",
    },
    attention: {
      rank: 2, cls: "group-attention", title: "Needs attention",
      hint: "Something needs a human",
    },
  };

  var table = null;
  var lastData = null;
  var stateFilter = null; // null | "ready" | "waiting" | "attention"

  document.addEventListener("DOMContentLoaded", init);

  function init() {
    setupTheme();
    setupLegend();
    setupControls();
    setupAutoRefresh();
    loadMOTD();
    load();

    // Keep the "updated N min ago" label fresh without a full reload.
    setInterval(function () {
      if (lastData) renderUpdated(lastData.generated_at);
    }, 30000);

    setInterval(function () {
      if (document.getElementById("auto-refresh").checked) load();
    }, REFRESH_MS);
  }

  /* ---------- data ---------- */

  function load() {
    fetch(DATA_URL, { cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(render)
      .catch(showError);
  }

  function render(data) {
    lastData = data;
    renderHeader(data);

    var rows = (data.prs || []).map(decorate);
    buildTable(rows);
    updateCounts(rows);

    var empty = document.getElementById("empty-state");
    if (!rows.length) {
      empty.hidden = false;
      empty.textContent = "No approved PRs are in the merge list right now.";
    } else {
      empty.hidden = true;
    }
  }

  // Attach the display-only grouping/ordering helpers to each PR record.
  function decorate(pr) {
    pr._group = groupOf(pr);
    pr._rank = GROUPS[pr._group].rank;
    // Secondary sort within a group: waiting shows soonest-ready first;
    // everything else shows the newest PR first.
    pr._within = pr._group === "waiting"
      ? (pr.time_left_hours || 0)
      : (10000000 - pr.number);
    return pr;
  }

  function groupOf(pr) {
    if (pr.state === "ready" || pr.state === "ready_backport") return "ready";
    if (pr.state === "waiting") return "waiting";
    return "attention";
  }

  /* ---------- header ---------- */

  function renderHeader(data) {
    var repo = data.repo || "zephyrproject-rtos/zephyr";
    document.getElementById("repo-link").href = "https://github.com/" + repo;

    // Release phase.
    var rel = data.release || {};
    var phaseEl = document.getElementById("phase-value");
    var phasePill = document.getElementById("phase-pill");
    if (rel.phase === "freeze") {
      phaseEl.textContent = "Feature freeze";
      phasePill.title =
        "Feature freeze: approaching the " + (rel.latest_tag || "next") +
        " release. Only PRs milestoned for the upcoming release are listed.";
    } else {
      phaseEl.textContent = "Integration";
      phasePill.title =
        "Integration: normal development. Latest release " +
        (rel.latest_tag || "") + ".";
    }

    // CI headline.
    var ci = data.ci || {};
    var overall = ci.overall || "unknown";
    var ciMap = {
      pass: ["dot-pass", "passing"],
      fail: ["dot-fail", "failing"],
      running: ["dot-running", "running"],
      cancelled: ["dot-unknown", "cancelled"],
      no_data: ["dot-unknown", "no data"],
      unknown: ["dot-unknown", "unknown"],
    };
    var info = ciMap[overall] || ciMap.unknown;
    var ciEl = document.getElementById("ci-value");
    ciEl.innerHTML = '<span class="dot ' + info[0] + '"></span>' + info[1];
    var ciPill = document.getElementById("ci-pill");
    ciPill.href = "https://github.com/" + repo + "/actions";
    ciPill.title = ciRunsTooltip(ci.runs);

    renderUpdated(data.generated_at);
    renderActionsBadge(data.self_repo);
  }

  function ciRunsTooltip(runs) {
    if (!runs || !runs.length) return "Main branch CI";
    return runs.map(function (r) {
      var s = r.status;
      if (s === "running" && r.progress) s = "running " + r.progress;
      return r.name + ": " + s;
    }).join("\n");
  }

  function renderUpdated(ts) {
    if (!ts) return;
    var el = document.getElementById("updated");
    var m = moment(ts);
    el.textContent = "Updated " + m.fromNow();
    el.title = m.format("LLLL");
  }

  function renderActionsBadge(self) {
    var link = document.getElementById("actions-badge");
    var img = document.getElementById("actions-badge-img");
    if (!self) {
      link.hidden = true;
      return;
    }
    link.hidden = false;
    link.href = "https://github.com/" + self + "/actions/workflows/update.yaml";
    img.src =
      "https://github.com/" + self + "/actions/workflows/update.yaml/badge.svg";
  }

  /* ---------- table ---------- */

  function buildTable(rows) {
    if (table) {
      table.clear();
      table.rows.add(rows);
      table.draw(false);
      return;
    }

    // Only-once: a custom state filter driven by the summary cards.
    DataTable.ext.search.push(function (settings, arr, dataIndex) {
      if (!stateFilter) return true;
      var row = settings.oInstance.api().row(dataIndex).data();
      return row && row._group === stateFilter;
    });

    table = new DataTable("#prs", {
      data: rows,
      deferRender: true,
      pageLength: 50,
      lengthChange: false,
      autoWidth: false,
      responsive: true,
      // Group order is pinned; user column clicks sort *within* each group.
      orderFixed: { pre: [[8, "asc"]] },
      order: [[9, "asc"]],
      rowGroup: { dataSrc: function (r) { return r._group; }, startRender: renderGroup },
      layout: { topStart: null, topEnd: null, bottomStart: "info", bottomEnd: "paging" },
      language: {
        info: "Showing _TOTAL_ PRs",
        infoEmpty: "No PRs",
        infoFiltered: " (filtered from _MAX_)",
        zeroRecords: "No PRs match your search.",
        paginate: { previous: "‹", next: "›" },
      },
      columnDefs: [{ targets: [8, 9], visible: false, searchable: false }],
      rowCallback: function (rowEl, data) {
        rowEl.classList.remove("row-ready", "row-waiting", "row-attention");
        rowEl.classList.add("row-" + data._group);
      },
      columns: [
        { data: "number", className: "pr-num", render: renderNumber },
        { data: "title", render: renderTitle },
        { data: "author", render: renderAuthor },
        { data: "assignees", orderable: false, render: renderReviewers },
        { data: "state", orderable: false, render: renderStatus },
        { data: "base", render: renderBase },
        { data: "milestone", render: renderMilestone },
        { data: "tags", orderable: false, searchable: false, render: renderTags },
        { data: "_rank" },
        { data: "_within" },
      ],
    });
  }

  function renderGroup(rowsApi, groupKey) {
    var g = GROUPS[groupKey] || GROUPS.attention;
    var n = rowsApi.count();
    var html =
      '<div class="group-title ' + g.cls + '">' +
      '<span class="state-dot"></span>' + g.title +
      '<span class="group-count">' + n + " PR" + (n === 1 ? "" : "s") + "</span>" +
      '<span class="group-hint">· ' + g.hint + "</span></div>";
    return $('<tr class="group-header ' + g.cls + '"><td colspan="8">' + html + "</td></tr>");
  }

  function renderNumber(n, type, row) {
    if (type !== "display") return n;
    return '<a class="pr-num" href="' + row.url + '">#' + n + "</a>";
  }

  function renderTitle(t, type, row) {
    if (type !== "display") return t;
    return '<a class="pr-title" href="' + row.url + '" title="' + esc(t) + '">' +
      esc(t) + "</a>";
  }

  function renderAuthor(a, type) {
    if (type !== "display") return a || "";
    return '<span class="people">' + esc(a) + "</span>";
  }

  function renderReviewers(as, type, row) {
    var assignees = row.assignees || [];
    var approvers = row.approvers || [];
    if (type !== "display") return assignees.concat(approvers).join(" ");
    var html = '<div class="people">';
    html += assignees.length
      ? "<div>" + esc(assignees.join(", ")) + "</div>"
      : '<div class="approvers">no assignee</div>';
    if (approvers.length) {
      html += '<div class="approvers" title="Approved by">✓ ' +
        esc(approvers.join(", ")) + "</div>";
    }
    return html + "</div>";
  }

  function renderStatus(s, type, row) {
    if (type !== "display") return s;
    var parts = [];
    if (row.state === "ready" || row.state === "ready_backport") {
      parts.push(chip("ok", "✓ No conflicts"));
      parts.push(chip("ok", "✓ Assignee approved"));
      parts.push(chip("ok", "✓ Review window met"));
    } else if (row.state === "waiting") {
      parts.push('<span class="chip chip-wait eta">⏳ ' + esc(etaText(row)) + "</span>");
      parts.push(chip("ok", "✓ Approved"));
      parts.push(chip("ok", "✓ No conflicts"));
    } else {
      var blockers = row.blockers || [];
      if (!blockers.length) blockers = ["Not ready to merge"];
      blockers.forEach(function (b) { parts.push(chip("block", "⚠ " + b)); });
    }
    return '<div class="status-cell">' + parts.join("") + "</div>";
  }

  function renderBase(b, type, row) {
    if (type !== "display") return b;
    if (row.is_backport) {
      return '<span class="base-backport" title="Backport branch">' + esc(b) + "</span>";
    }
    return '<span class="base-badge">' + esc(b) + "</span>";
  }

  function renderMilestone(m, type) {
    if (type !== "display") return m || "";
    if (!m) return '<span class="milestone-tag" style="opacity:.45">—</span>';
    return '<span class="milestone-tag">' + esc(m) + "</span>";
  }

  function renderTags(t, type, row) {
    if (type !== "display") return "";
    var tags = row.tags || {};
    var out = [];
    if (tags.hotfix) out.push(tag("hotfix", "hotfix"));
    if (tags.trivial) out.push(tag("trivial", "trivial"));
    if (tags.override_required) out.push(tag("override", "override required"));
    if (tags.old_ci) {
      out.push(tag("oldci", "ci " + (tags.ci_age_days != null ? tags.ci_age_days + "d" : "old")));
    }
    if (tags.review_dismissed) out.push(tag("dismissed", "review dismissed"));
    if (tags.dnm) out.push(tag("dnm", "dnm"));
    return '<div class="tags-cell">' + out.join("") + "</div>";
  }

  function chip(kind, text) {
    return '<span class="chip chip-' + kind + '">' + esc(text) + "</span>";
  }

  function tag(kind, text) {
    return '<span class="tag tag-' + kind + '">' + esc(text) + "</span>";
  }

  // Human ETA for a waiting PR: relative within a day, weekday+time beyond that.
  function etaText(row) {
    if (row.time_elapsed) return "ready now";
    if (row.ready_at) {
      var m = moment(row.ready_at);
      if (m.diff(moment(), "hours") <= 24) return "ready " + m.fromNow();
      return "ready " + m.format("ddd HH:mm");
    }
    if (row.time_left_hours) return "ready in ~" + row.time_left_hours + "h";
    return "ready soon";
  }

  /* ---------- counts & filtering ---------- */

  function updateCounts(rows) {
    var counts = { ready: 0, waiting: 0, attention: 0 };
    rows.forEach(function (r) { counts[r._group]++; });
    document.getElementById("count-ready").textContent = counts.ready;
    document.getElementById("count-waiting").textContent = counts.waiting;
    document.getElementById("count-attention").textContent = counts.attention;

    // "Open all ready" only opens main-targeting ready PRs (not backports).
    var openable = rows.filter(function (r) { return r.state === "ready"; });
    var btn = document.getElementById("open-ready");
    btn.disabled = openable.length === 0;
    btn.textContent = openable.length
      ? "Open all ready PRs (" + openable.length + ")"
      : "Open all ready PRs";
  }

  function applyStateFilter(state) {
    stateFilter = (stateFilter === state) ? null : state;

    document.querySelectorAll(".card").forEach(function (c) {
      c.classList.toggle("is-active", c.dataset.state === stateFilter);
    });

    var wrap = document.getElementById("active-filter");
    if (stateFilter) {
      wrap.hidden = false;
      document.getElementById("active-filter-name").textContent =
        GROUPS[stateFilter].title;
    } else {
      wrap.hidden = true;
    }
    if (table) table.draw();
  }

  /* ---------- controls ---------- */

  function setupControls() {
    var search = document.getElementById("search");
    var clear = document.getElementById("search-clear");

    // Restore a deep-linked query (#q=...).
    var hashQ = parseHashQuery();
    if (hashQ) search.value = hashQ;

    search.addEventListener("input", function () {
      clear.hidden = !search.value;
      if (table) table.search(search.value).draw();
      setHashQuery(search.value);
    });
    clear.addEventListener("click", function () {
      search.value = "";
      clear.hidden = true;
      if (table) table.search("").draw();
      setHashQuery("");
      search.focus();
    });
    if (hashQ) clear.hidden = false;

    document.querySelectorAll(".card").forEach(function (card) {
      card.addEventListener("click", function () {
        applyStateFilter(card.dataset.state);
      });
    });
    document.getElementById("filter-clear").addEventListener("click", function () {
      applyStateFilter(stateFilter); // toggles it off
    });

    document.getElementById("open-ready").addEventListener("click", openReady);
  }

  function openReady() {
    if (!lastData) return;
    var openable = (lastData.prs || []).filter(function (r) {
      return r.state === "ready";
    });
    if (!openable.length) {
      alert("No PRs are ready to merge into main right now.");
      return;
    }
    openable.forEach(function (r) { window.open(r.url, "_blank", "noopener"); });
  }

  function parseHashQuery() {
    var m = /(?:^|[#&])q=([^&]*)/.exec(location.hash || "");
    return m ? decodeURIComponent(m[1]) : "";
  }

  function setHashQuery(q) {
    if (q) location.replace("#q=" + encodeURIComponent(q));
    else if (location.hash) location.replace("#");
  }

  /* ---------- theme ---------- */

  function setupTheme() {
    document.getElementById("theme-toggle").addEventListener("click", function () {
      var next = effectiveTheme() === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      localStorage.setItem("theme", next);
    });
  }

  function effectiveTheme() {
    var t = document.documentElement.getAttribute("data-theme");
    if (t === "light" || t === "dark") return t;
    return matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }

  /* ---------- legend ---------- */

  function setupLegend() {
    var dlg = document.getElementById("legend");
    document.getElementById("legend-open").addEventListener("click", function () {
      dlg.showModal();
    });
    // Open once on a visitor's first ever visit.
    if (!localStorage.getItem("legendSeen")) {
      try { dlg.showModal(); } catch (e) { /* ignore */ }
      localStorage.setItem("legendSeen", "1");
    }
  }

  /* ---------- auto-refresh ---------- */

  function setupAutoRefresh() {
    var cb = document.getElementById("auto-refresh");
    cb.checked = localStorage.getItem("autoRefresh") === "true";
    cb.addEventListener("change", function () {
      localStorage.setItem("autoRefresh", cb.checked);
    });
  }

  /* ---------- MOTD ---------- */

  function loadMOTD() {
    fetch(MOTD_URL)
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.text();
      })
      .then(function (text) {
        text = (text || "").trim();
        if (!text || text === "empty") return;
        document.getElementById("motd-text").textContent = text;
        document.getElementById("motd").hidden = false;
      })
      .catch(function () { /* no MOTD is fine */ });
  }

  /* ---------- misc ---------- */

  function showError(err) {
    var empty = document.getElementById("empty-state");
    empty.hidden = false;
    empty.textContent = "Could not load merge list data (" + err.message + ").";
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
})();
