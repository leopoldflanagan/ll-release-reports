#!/usr/bin/env python3
"""
LL QA Roadmap — data refresh.

Pulls live data from Jira Cloud and rewrites ONLY the data arrays inside
qa-roadmap/index.html (FEATURES, FEATURES_ALL, ALL, BUGS, NOPOD) plus the
snapshot date (TODAY) and the rolling QA week window (WEEKS). All CSS/JS/markup
is left untouched.

Credentials come from environment variables (never hard-coded):
  JIRA_BASE_URL   e.g. https://wellfit.atlassian.net
  JIRA_EMAIL      the Atlassian account email that owns the API token
  JIRA_API_TOKEN  the API token (stored as a GitHub Actions secret)

Run locally:
  JIRA_BASE_URL=https://wellfit.atlassian.net \
  JIRA_EMAIL=you@example.com \
  JIRA_API_TOKEN=xxxx \
  python3 refresh_dashboard.py qa-roadmap/index.html
"""

import os
import sys
import json
import base64
import datetime as dt
import urllib.request
import urllib.parse
import urllib.error

# ---- Jira field constants (verified for this instance) --------------------
CLOUD_FIELDS = {
    "pod":    "customfield_11626",   # Pod[Dropdown]
    "ted":    "customfield_10023",   # Target End date
    "tshirt": "customfield_10044",   # T-shirt Size
}
FIELDS = ["summary", "status", "fixVersions", "assignee", "reporter",
          "project", "customfield_11626", "customfield_10023",
          "customfield_10044", "customfield_10020", "priority", "created", "labels",
          "parent", "customfield_10014", "issuelinks",
          "customfield_12146", "customfield_10071", "resolutiondate", "duedate"]
# 10020 = Sprint, 10014 = Epic Link, 12146 = Theme, 10071 = Triage

THEMES = ["LL-MVP", "LL-Fast Follows", "ACH", "ACH-Fast Follows"]

# Camille: P3/P4 bugs should hang under this epic ("LL - Low Prior Bugs").
# We flag bugs NOT linked to it so they can be actioned.
LOWPRIO_EPIC = "PLANS-20751"

# ---------------------------------------------------------------------------

def env(name):
    v = os.environ.get(name)
    if not v:
        sys.exit(f"ERROR: missing environment variable {name}")
    return v

BASE = env("JIRA_BASE_URL").rstrip("/")
EMAIL = env("JIRA_EMAIL")
TOKEN = env("JIRA_API_TOKEN")
AUTH = base64.b64encode(f"{EMAIL}:{TOKEN}".encode()).decode()


def jira_search(jql, fields=FIELDS, max_total=500):
    """Run a JQL search, paginating with nextPageToken. Returns list of issues."""
    issues = []
    token = None
    while True:
        payload = {"jql": jql, "fields": fields, "maxResults": 100}
        if token:
            payload["nextPageToken"] = token
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{BASE}/rest/api/3/search/jql",
            data=data, method="POST",
            headers={"Authorization": f"Basic {AUTH}",
                     "Content-Type": "application/json",
                     "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                res = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            sys.exit(f"ERROR: Jira HTTP {e.code} for JQL [{jql}]\n{body}")
        issues += res.get("issues", [])
        if res.get("isLast", True) or not res.get("nextPageToken"):
            break
        token = res["nextPageToken"]
        if len(issues) >= max_total:
            break
    return issues


def clean(s):
    return " ".join((s or "").split())


def pick_sprint(sprint_objs):
    """Choose the sprint to display from Jira's sprint array (which lists every
    sprint the issue was ever in, in arbitrary order). Priority: active > future
    > most-recently-closed. Returns the sprint name or None."""
    if not sprint_objs:
        return None
    def recency(s):  # sort key for "most recent" among a state group
        return s.get("startDate") or s.get("completeDate") or s.get("endDate") or ""
    active = [s for s in sprint_objs if s.get("state") == "active"]
    future = [s for s in sprint_objs if s.get("state") == "future"]
    closed = [s for s in sprint_objs if s.get("state") == "closed"]
    if active:
        active.sort(key=recency)
        return active[-1].get("name")
    if future:
        # nearest upcoming first (by start date, then id)
        future.sort(key=lambda s: (s.get("startDate") or "", s.get("id") or 0))
        return future[0].get("name")
    if closed:
        closed.sort(key=recency)
        return closed[-1].get("name")
    return sprint_objs[-1].get("name")


def parse_issue(i, theme=None):
    f = i["fields"]
    pod = f.get(CLOUD_FIELDS["pod"]) or {}
    ts = f.get(CLOUD_FIELDS["tshirt"]) or {}
    fv = f.get("fixVersions") or []
    assignee = f.get("assignee") or {}
    reporter = f.get("reporter") or {}
    prio = f.get("priority") or {}
    sprint = f.get("customfield_10020") or []
    sprint_objs = [x for x in sprint if isinstance(x, dict)] if isinstance(sprint, list) else []
    sprint_names = [x.get("name") for x in sprint_objs]
    # Jira returns ALL sprints an issue has ever been in, NOT in chronological order,
    # so we pick by state (active > future > most-recent closed) instead of taking the last.
    sprint_name = pick_sprint(sprint_objs)
    # Camille: a bug is "in LL - Low Prior Bugs" only if it has BOTH the sprint AND the epic.
    lp_sprint = any("low prior bugs" in (n or "").lower() for n in sprint_names)
    # Is this issue linked to the LL - Low Prior Bugs epic? (parent / Epic Link / issue link)
    parent = f.get("parent") or {}
    epic_link = f.get("customfield_10014")
    link_keys = set()
    for l in (f.get("issuelinks") or []):
        for side in ("inwardIssue", "outwardIssue"):
            o = l.get(side) or {}
            if o.get("key"):
                link_keys.add(o["key"])
    linked_lowprio = (parent.get("key") == LOWPRIO_EPIC
                      or epic_link == LOWPRIO_EPIC
                      or LOWPRIO_EPIC in link_keys)
    theme_field = f.get("customfield_12146") or {}   # Theme (select) — populated for Features; being rolled out to Bugs
    triage_field = f.get("customfield_10071") or {}  # Triage (select)
    return {
        "key": i["key"],
        "name": clean(f.get("summary")),
        "status": f["status"]["name"],
        "pod": (pod.get("value") if pod else "") or "",
        "proj": f["project"]["key"],
        "ted": f.get(CLOUD_FIELDS["ted"]),
        "fv": (fv[0]["name"] if fv else None),
        "who": clean(assignee.get("displayName")) or "Unassigned",
        "reporter": clean(reporter.get("displayName")) or "",
        "tshirt": (ts.get("value") if ts else None),
        "sprint": sprint_name,
        "prio": (prio.get("name") if prio else None) or "3: Standard",
        "created": (f.get("created") or ""),   # full ISO timestamp (enables last-hour / last-24h windows)
        "resolved": (f.get("resolutiondate") or ""),  # full ISO timestamp for closed bugs (empty while open)
        "due": (f.get("duedate") or ""),              # standard Jira Due date (YYYY-MM-DD)
        "labels": f.get("labels") or [],
        "linked_lowprio": linked_lowprio,
        "lp_sprint": lp_sprint,
        # Parent the bug hangs from. Jira returns key + fields.summary for `parent`;
        # Epic Link (customfield_10014) is only a key, so the name can come back empty.
        "parent_key": (parent.get("key")
                       or (epic_link if isinstance(epic_link, str) else "")
                       or ""),
        "parent_name": clean(((parent.get("fields") or {}).get("summary")) or ""),
        "theme": theme or (theme_field.get("value") if isinstance(theme_field, dict) else "") or "",
        "triage": (triage_field.get("value") if isinstance(triage_field, dict) else "") or "",
    }


# ---- Pull the datasets ----------------------------------------------------

def pull_ll_features():
    jql = ('issuetype = Feature AND cf[11626] = "LL" '
           'AND statusCategory != Done ORDER BY assignee ASC')
    rows = [parse_issue(i) for i in jira_search(jql)]
    keep = ["key", "name", "status", "ted", "fv", "who", "tshirt"]
    return [{k: r[k] for k in keep} for r in rows]


def pull_theme_features():
    """One query per theme (the dropdown value isn't returned in fields)."""
    seen = {}
    for th in THEMES:
        jql = (f'issuetype = Feature AND "Theme[Dropdown]" = "{th}" '
               f'AND statusCategory != Done ORDER BY status ASC')
        for i in jira_search(jql):
            r = parse_issue(i, theme=th)
            if r["key"] not in seen:  # a key belongs to one theme
                seen[r["key"]] = r
    keep = ["key", "name", "status", "ted", "fv", "who", "tshirt", "pod", "theme"]
    return [{k: r[k] for k in keep} for r in seen.values()]


def build_features_all(ll_features, theme_features):
    """Merge LL (pod=LL, maybe no theme) with cross-pod theme features."""
    bykey = {}
    for r in ll_features:
        rr = dict(r); rr["pod"] = "LL"; rr["theme"] = ""
        bykey[rr["key"]] = rr
    for t in theme_features:
        if t["key"] in bykey:
            bykey[t["key"]]["theme"] = t["theme"]
        else:
            bykey[t["key"]] = dict(t)
    out = list(bykey.values())
    for r in out:
        r.setdefault("theme", ""); r["pod"] = r.get("pod") or ""
    order = {t: i for i, t in enumerate(THEMES)}
    out.sort(key=lambda r: (order.get(r["theme"], 99), r["key"]))
    keep = ["key", "name", "status", "ted", "fv", "who", "tshirt", "pod", "theme"]
    return [{k: r.get(k) for k in keep} for r in out]


def build_all(features_all):
    """The roadmap's cross-project ALL set = every theme feature, shaped for ALL."""
    keep = ["key", "name", "pod", "proj", "status", "fv", "ted", "who", "theme"]
    out = []
    for r in features_all:
        proj = r["key"].split("-")[0]
        out.append({"key": r["key"], "name": r["name"], "pod": r.get("pod") or "",
                    "proj": proj, "status": r["status"], "fv": r.get("fv"),
                    "ted": r.get("ted"), "who": r.get("who") or "Unassigned",
                    "theme": r.get("theme") or ""})
    return out


def pull_bugs():
    jql = ('issuetype = Bug AND cf[11626] IN ("LL", "PLANS") '
           'AND statusCategory != Done ORDER BY status ASC')
    rows = [parse_issue(i) for i in jira_search(jql)]
    NEAR = {"In Stage", "In QA", "Dev Verification", "PR Merged"}
    WIP = {"In Development", "PR Created", "WAITING REVIEW", "PR Merged",
           "Dev Verification", "In Stage", "In QA"}
    out = []
    for r in rows:
        cat = "In Progress" if (r["status"] in WIP or r["status"] in NEAR
                                or "hold" in r["status"].lower()) else "To Do"
        out.append({"key": r["key"], "name": r["name"], "pod": (r.get("pod") or ""),
                    "status": r["status"], "cat": cat, "fv": r.get("fv"),
                    "sprint": r.get("sprint"), "who": r.get("who") or "Unassigned",
                    "reporter": r.get("reporter") or "", "prio": r.get("prio") or "3: Standard",
                    "created": r.get("created") or "", "resolved": "", "due": r.get("due") or "", "labels": r.get("labels") or [],
                    "theme": r.get("theme") or "", "triage": r.get("triage") or "",
                    "parentKey": r.get("parent_key") or "",
                    "parentName": r.get("parent_name") or "",
                    "lpEpic": bool(r.get("linked_lowprio")),
                    "lpSprint": bool(r.get("lp_sprint")),
                    "linkedLP": bool(r.get("linked_lowprio") and r.get("lp_sprint"))})
    return out


def pull_closed_bugs(days=90):
    """Bugs closed (statusCategory = Done) within the last `days` days, for the
    Closed filter + the Statistics tab (open-vs-closed rate, throughput, projection)."""
    jql = ('issuetype = Bug AND cf[11626] IN ("LL", "PLANS") '
           f'AND statusCategory = Done AND resolutiondate >= -{days}d '
           'ORDER BY resolutiondate DESC')
    rows = [parse_issue(i) for i in jira_search(jql, max_total=3000)]
    out = []
    for r in rows:
        out.append({"key": r["key"], "name": r["name"], "pod": (r.get("pod") or ""),
                    "status": r["status"], "cat": "Done", "fv": r.get("fv"),
                    "sprint": r.get("sprint"), "who": r.get("who") or "Unassigned",
                    "reporter": r.get("reporter") or "", "prio": r.get("prio") or "3: Standard",
                    "created": r.get("created") or "", "resolved": r.get("resolved") or "",
                    "due": r.get("due") or "", "labels": r.get("labels") or [],
                    "theme": r.get("theme") or "", "triage": r.get("triage") or "",
                    "parentKey": r.get("parent_key") or "",
                    "parentName": r.get("parent_name") or "",
                    "lpEpic": bool(r.get("linked_lowprio")),
                    "lpSprint": bool(r.get("lp_sprint")),
                    "linkedLP": bool(r.get("linked_lowprio") and r.get("lp_sprint"))})
    return out


CLOSED_DAYS = 90  # rolling window for closed bugs


def pull_nopod():
    jql = ('project = PLANS AND issuetype = Bug AND "Pod[Dropdown]" IS EMPTY '
           'AND statusCategory != Done ORDER BY created DESC')
    rows = [parse_issue(i) for i in jira_search(jql)]
    return [{"key": r["key"], "name": r["name"], "status": r["status"],
             "who": r.get("who") or "Unassigned", "reporter": r.get("reporter") or ""}
            for r in rows]


# ---- QA week window (rolling, based on today) -----------------------------

# Release calendar (deploy dates) — mirrors RELEASES in qa-roadmap/index.html.
# Keep both in sync with Confluence "Release Schedule 2026" (space WR).
# Across the whole 2026 schedule the feature freeze is 10 days before deploy.
RELEASES = [
    ("R9.6",  dt.date(2026, 8, 5)),
    ("R9.7",  dt.date(2026, 9, 2)),
    ("R9.8",  dt.date(2026, 10, 7)),
    ("R9.9",  dt.date(2026, 11, 4)),
    ("R9.10", dt.date(2026, 12, 2)),
]
FREEZE_LEAD_DAYS = 10


def build_weeks(today):
    """7 Monday-anchored weeks starting the Monday of the current week."""
    monday = today - dt.timedelta(days=today.weekday())
    weeks = []
    for n in range(7):
        s = monday + dt.timedelta(days=7 * n)
        e = s + dt.timedelta(days=6)
        label = f"{s.strftime('%b')} {s.day}\u2013" + (
            f"{e.day}" if s.month == e.month else f"{e.strftime('%b')} {e.day}")
        w = {"label": label, "start": s.isoformat(), "end": e.isoformat()}
        weeks.append(w)
    # Guard: once the calendar runs out, every release name on the page becomes
    # a guess. Say so in the run log instead of failing quietly.
    if not any(deploy >= today for _, deploy in RELEASES):
        print("WARNING: release calendar is out of date - last known release is "
              f"{RELEASES[-1][0]} ({RELEASES[-1][1]}). Update RELEASES here and in "
              "qa-roadmap/index.html from Confluence 'Release Schedule 2026'.")
    # freeze annotations, derived from RELEASES so they advance with the schedule
    for w in weeks:
        s = dt.date.fromisoformat(w["start"]); e = dt.date.fromisoformat(w["end"])
        for rid, deploy in RELEASES:
            fd = deploy - dt.timedelta(days=FREEZE_LEAD_DAYS)
            if s <= fd <= e:
                w["freeze"] = f"{rid} freeze {fd.strftime('%b')} {fd.day}"
    return weeks


# ---- Emit + splice --------------------------------------------------------

def js_array(name, rows):
    return f"const {name} = " + json.dumps(rows, ensure_ascii=False) + ";"


def splice(html, marker_decl, new_decl):
    """Replace `const NAME = [ ... ];` (greedy to the terminating `];`)."""
    import re
    name = marker_decl
    pat = re.compile(r"const " + re.escape(name) + r" = \[.*?\];", re.S)
    if not pat.search(html):
        sys.exit(f"ERROR: could not find array {name} in HTML")
    return pat.sub(lambda m: new_decl, html, count=1)


# ---- Bug history accumulator ----------------------------------------------
# The bug lists above are a rolling 90-day window: anything older drops out and
# the history is gone with it. This builds a SEPARATE file that only ever grows,
# holding the per-release counts the trend views need.
#
# Two choices worth knowing about:
#   * Windows run deploy-to-deploy, and a bug belongs to the window it was
#     REPORTED in - the release that was live when it showed up. Its fixVersion
#     says where it is scheduled to be FIXED, which is a different question and
#     a different number.
#   * Counts come from Jira directly rather than from the lists above, so a
#     window is still correct when it predates the 90-day window.
#
# HISTORY_TEAMS is a dict on purpose: DS and EDW can be added here without
# touching any logic below, which is what the per-team reports will need.

HISTORY_SCHEMA = 4
HISTORY_TEAMS = {
    "LL":    'cf[11626] = "LL"',
    "PLANS": 'cf[11626] = "PLANS"',
}
WORK_TYPES = '"Story", "Task"'

# T-shirt size is a label, not a number, so comparing release volume needs a scale.
# These weights are a convention, not a measurement - they are published on the page
# so anyone can argue with them, and changing them here changes the report.
TSHIRT_POINTS = {"Tiny": 1, "Small": 2, "Moderate": 3, "Large": 5, "X-Large": 8, "Huge": 13}

# Deploy dates confirmed against each fixVersion's releaseDate in Jira. Kept
# separate from RELEASES because that list drives the roadmap's freeze markers
# and prose, where adding past releases would change what the page displays.
HISTORY_RELEASES = [
    ("R9.4", dt.date(2026, 6, 3)),
    ("R9.5", dt.date(2026, 7, 1)),
] + RELEASES


def release_windows(today):
    """Deploy to the next deploy. A window that has not started yet is skipped;
    the one containing today is open and gets recomputed on every run."""
    out = []
    for n, (rid, start) in enumerate(HISTORY_RELEASES):
        end = HISTORY_RELEASES[n + 1][1] if n + 1 < len(HISTORY_RELEASES) else None
        if start > today:
            continue
        out.append({"id": rid, "start": start, "end": end,
                    "closed": bool(end and end <= today)})
    return out


def _pct(vals_sorted, q):
    if not vals_sorted:
        return None
    k = min(len(vals_sorted) - 1, int(round((len(vals_sorted) - 1) * q)))
    return vals_sorted[k]


def _prio_key(issue):
    name = ((issue["fields"].get("priority") or {}).get("name") or "")
    head = name.split(":")[0].strip()
    return head if head in ("1", "2", "3", "4") else "other"


def _count_by_prio(issues):
    out = {"1": 0, "2": 0, "3": 0, "4": 0, "other": 0}
    for i in issues:
        out[_prio_key(i)] += 1
    return out


def _window_clause(field, w):
    c = f'{field} >= "{w["start"].isoformat()}"'
    if w["end"]:
        c += f' AND {field} < "{w["end"].isoformat()}"'
    return c


def _weekly(issues, field):
    """Monday-anchored counts inside the window. The per-release totals are the headline,
    but the weekly shape is what shows whether a good release was a steady month or one
    very good Tuesday - and data.json only keeps 90 days of it."""
    out = {}
    for i in issues:
        v = i["fields"].get(field)
        if not v:
            continue
        d = dt.date.fromisoformat(v[:10])
        k = (d - dt.timedelta(days=d.weekday())).isoformat()
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items()))


def measure_window(pred, w):
    """One team's row for one window."""
    flds = ["priority", "created", "resolutiondate"]
    reported = jira_search(f'issuetype = Bug AND {pred} AND {_window_clause("created", w)}',
                           flds, max_total=5000)
    resolved = jira_search(f'issuetype = Bug AND {pred} AND {_window_clause("resolved", w)}',
                           flds, max_total=5000)

    buckets = {}
    for i in resolved:
        f = i["fields"]
        if not (f.get("created") and f.get("resolutiondate")):
            continue
        days = (dt.date.fromisoformat(f["resolutiondate"][:10])
                - dt.date.fromisoformat(f["created"][:10])).days
        buckets.setdefault(_prio_key(i), []).append(max(0, days))
    ttr = {}
    for k, v in buckets.items():
        v.sort()
        ttr[k] = {"n": len(v), "median": _pct(v, 0.5), "p90": _pct(v, 0.9)}

    # Denominator for "bugs against how much we shipped". Counted, not weighted:
    # story points are filled on roughly 6% of this project's stories, so any
    # points-based figure would be mostly guesswork dressed up as a number.
    work = jira_search(f'issuetype IN ({WORK_TYPES}) AND {pred} AND {_window_clause("resolved", w)}',
                       ["priority"], max_total=5000)

    # How big the release was, in features. Without this a raw bug count cannot be
    # compared across releases: a window that ships twice the features should draw
    # more bugs. It also decides which windows are comparable at all - R9.4 carried
    # two features against a median of eighteen, so it is history, not a reference.
    # Will Not Implement is excluded on purpose. A cancelled feature shipped no
    # scope, so counting it deflates bugs-per-feature, and if it carries a size it
    # also inflates the release's average volume with work nobody ever built.
    # R9.7 carried WT-844 "Plan Design" at Huge (13 of its 134 points) this way.
    feats = jira_search(f'issuetype = Feature AND {pred} AND fixVersion = "{w["id"]}" '
                        f'AND status != "Will Not Implement"',
                        ["priority", CLOUD_FIELDS["tshirt"]], max_total=5000)

    # Count alone says a release shipped 25 features; it does not say whether they were
    # 25 Smalls or 25 Huges. The mix is stored raw so the page can show it, and the
    # points total is what makes "bugs per unit of volume" possible at all.
    sizes, pts, sized = {}, 0, 0
    for f in feats:
        v = (f["fields"].get(CLOUD_FIELDS["tshirt"]) or {}).get("value") or ""
        sizes[v or "unsized"] = sizes.get(v or "unsized", 0) + 1
        if v in TSHIRT_POINTS:
            pts += TSHIRT_POINTS[v]
            sized += 1

    return {"reported": _count_by_prio(reported),
            "resolved": _count_by_prio(resolved),
            "ttr_days": ttr,
            "work_items": len(work),
            "features": len(feats),
            "sizes": sizes,
            "size_points": pts,
            "sized_features": sized,
            "weeks": {"reported": _weekly(reported, "created"),
                      "resolved": _weekly(resolved, "resolutiondate")}}


def build_history(path, today):
    """Grow history.json. A closed window is measured once and then left alone;
    the open window is refreshed every run."""
    try:
        with open(path, encoding="utf-8") as fh:
            prev = json.load(fh)
    except (OSError, ValueError):
        prev = {}

    # What we carry forward vs what we compare against are two different things.
    # A schema bump rebuilds every window, but the figures already on disk are
    # still the best record of what those windows held, so they stay available
    # for the zero-check below.
    stored = prev.get("releases") or {}
    if prev and prev.get("schema") != HISTORY_SCHEMA:
        print(f"  history: schema {prev.get('schema')} != {HISTORY_SCHEMA}, rebuilding")
        rel = {}
    else:
        rel = dict(stored)

    teams_now = sorted(HISTORY_TEAMS)
    for w in release_windows(today):
        have = rel.get(w["id"]) or {}
        prior = stored.get(w["id"]) or {}
        # Frozen only counts if it was measured for the same set of teams -
        # otherwise adding a team would leave old windows silently short a column.
        if have.get("closed") and have.get("teams_measured") == teams_now:
            continue
        row = {"start": w["start"].isoformat(),
               "end": w["end"].isoformat() if w["end"] else None,
               "closed": w["closed"],
               "teams_measured": teams_now,
               "measured_at": today.isoformat(),
               "teams": {t: measure_window(pred, w) for t, pred in HISTORY_TEAMS.items()}}
        tot = sum(sum(t["reported"].values()) for t in row["teams"].values())
        nf = sum(t.get("features", 0) for t in row["teams"].values())
        npt = sum(t.get("size_points", 0) for t in row["teams"].values())
        nsz = sum(t.get("sized_features", 0) for t in row["teams"].values())
        # A frozen window is only ever re-measured on a rebuild (schema bump, or an
        # unreadable file). If Jira hands back nothing for a window that previously
        # had bugs, that is Jira having changed under us - a renamed field, archived
        # issues - not a month in which nobody reported anything. Keep what we had.
        was = sum(sum(t["reported"].values())
                  for t in (prior.get("teams") or {}).values())
        if was and not tot:
            print(f"  history: {w['id']} re-measured as 0 but held {was} - keeping the "
                  "stored figures. Check the Pod field and issue type names.")
            rel[w["id"]] = prior
            continue
        rel[w["id"]] = row
        avg = (npt / nsz) if nsz else 0
        print(f"  history: {w['id']} ({'frozen' if w['closed'] else 'open'}) - "
              f"{tot} reported over {nf} features "
              f"(avg size {avg:.1f} pts from {nsz} of {nf} sized)")

    out = {"schema": HISTORY_SCHEMA,
           "updated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
           "teams": teams_now,
           "releases": rel}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    return out


# ---- Guard: never publish a collapsed snapshot ----------------------------
# Every feature query depends on field values that live ON the Feature itself
# (Pod cf[11626], Theme cf[12146]) and on the issue type still being named
# "Feature". The Sept 2026 move of Features into the WT "Wellfit Portfolio"
# project left all three intact - but if a later step moves Theme up to the
# Initiative parent, the JQL keeps returning HTTP 200 with zero rows. Without
# this check the bot would publish an empty data.json, the dashboard would go
# blank, and the run log would say nothing was wrong.
COLLAPSE_FLOOR = 0.5   # abort if a dataset falls below half the last snapshot
GUARDED = ("features", "features_all", "bugs")


def guard_snapshot(path, payload):
    """Abort before writing if the pulls came back empty or collapsed."""
    if os.environ.get("ALLOW_COLLAPSE") == "1":
        print("  guard: skipped (ALLOW_COLLAPSE=1)")
        return

    empty = [k for k in GUARDED if not payload.get(k)]
    if empty:
        sys.exit(
            "ERROR: refusing to publish - these datasets came back EMPTY: "
            + ", ".join(empty) + ".\n"
            "The Jira queries returned no rows. Most likely a field or issue "
            "type changed: Pod cf[11626], Theme cf[12146], or 'issuetype = "
            "Feature'. Check THEMES and the JQL in this file.\n"
            "data.json was NOT modified.")

    try:
        with open(path, encoding="utf-8") as fh:
            prev = json.load(fh)
    except (OSError, ValueError):
        print("  guard: no readable previous snapshot - size check skipped")
        return

    for k in GUARDED:
        was, now = len(prev.get(k) or []), len(payload.get(k) or [])
        if was and now < was * COLLAPSE_FLOOR:
            sys.exit(
                f"ERROR: refusing to publish - '{k}' collapsed from {was} to "
                f"{now} ({now / was:.0%} of the previous snapshot).\n"
                "If this drop is real, re-run once with ALLOW_COLLAPSE=1 to "
                "accept it. data.json was NOT modified.")
    print("  guard: snapshot sizes OK")


def main():
    # New architecture: the script writes a data-only JSON file.
    # The HTML never gets touched by the bot — it fetches this JSON at load time.
    # This makes bot writes and manual HTML edits touch DIFFERENT files → no merge conflicts, ever.
    if len(sys.argv) < 2:
        sys.exit("usage: refresh_dashboard.py path/to/data.json")
    path = sys.argv[1]

    today = dt.date.today()
    print(f"Refreshing data → {path} — snapshot {today.isoformat()}")

    ll = pull_ll_features()
    print(f"  LL features: {len(ll)}")
    theme = pull_theme_features()
    print(f"  theme features: {len(theme)}")
    features_all = build_features_all(ll, theme)
    print(f"  FEATURES_ALL: {len(features_all)}")
    all_set = build_all(features_all)
    bugs = pull_bugs()
    print(f"  bugs (active): {len(bugs)}")
    bugs_closed = pull_closed_bugs(CLOSED_DAYS)
    print(f"  bugs (closed, last {CLOSED_DAYS}d): {len(bugs_closed)}")
    nopod = pull_nopod()
    print(f"  no-pod bugs: {len(nopod)}")
    weeks = build_weeks(today)

    now_iso = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()

    payload = {
        "generated_at": now_iso,
        "today": today.isoformat(),
        "features": ll,
        "features_all": features_all,
        "all": all_set,
        "bugs": bugs,
        "bugs_closed": bugs_closed,
        "closed_days": CLOSED_DAYS,
        "nopod": nopod,
        "weeks": weeks,
    }

    guard_snapshot(path, payload)

    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    print(f"Done — wrote {path}, generated at {now_iso}")

    hist_path = os.path.join(os.path.dirname(os.path.abspath(path)), "history.json")
    print(f"Bug history → {hist_path}")
    build_history(hist_path, today)


if __name__ == "__main__":
    main()
