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
          "customfield_10044", "customfield_10020", "priority", "created", "labels"]  # 10020 = Sprint

THEMES = ["LL-MVP", "LL-Fast Follows", "ACH", "ACH-Fast Follows"]

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


def parse_issue(i, theme=None):
    f = i["fields"]
    pod = f.get(CLOUD_FIELDS["pod"]) or {}
    ts = f.get(CLOUD_FIELDS["tshirt"]) or {}
    fv = f.get("fixVersions") or []
    assignee = f.get("assignee") or {}
    reporter = f.get("reporter") or {}
    prio = f.get("priority") or {}
    sprint = f.get("customfield_10020") or []
    sprint_name = None
    if isinstance(sprint, list) and sprint:
        sprint_name = sprint[-1].get("name") if isinstance(sprint[-1], dict) else None
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
        "created": (f.get("created") or "")[:10],
        "labels": f.get("labels") or [],
        "theme": theme or "",
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
    jql = ('issuetype = Bug AND cf[11626] = "LL" '
           'AND statusCategory != Done ORDER BY status ASC')
    rows = [parse_issue(i) for i in jira_search(jql)]
    NEAR = {"In Stage", "In QA", "Dev Verification", "PR Merged"}
    WIP = {"In Development", "PR Created", "WAITING REVIEW", "PR Merged",
           "Dev Verification", "In Stage", "In QA"}
    out = []
    for r in rows:
        cat = "In Progress" if (r["status"] in WIP or r["status"] in NEAR
                                or "hold" in r["status"].lower()) else "To Do"
        out.append({"key": r["key"], "name": r["name"], "pod": "LL",
                    "status": r["status"], "cat": cat, "fv": r.get("fv"),
                    "sprint": r.get("sprint"), "who": r.get("who") or "Unassigned",
                    "reporter": r.get("reporter") or "", "prio": r.get("prio") or "3: Standard",
                    "created": r.get("created") or "", "labels": r.get("labels") or []})
    return out


def pull_nopod():
    jql = ('project = PLANS AND issuetype = Bug AND "Pod[Dropdown]" IS EMPTY '
           'AND statusCategory != Done ORDER BY created DESC')
    rows = [parse_issue(i) for i in jira_search(jql)]
    return [{"key": r["key"], "name": r["name"], "status": r["status"],
             "who": r.get("who") or "Unassigned", "reporter": r.get("reporter") or ""}
            for r in rows]


# ---- QA week window (rolling, based on today) -----------------------------

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
    # freeze annotations (R9.6 ~Jul 26, R9.7 ~Aug 23) if they land in-window
    for w in weeks:
        s = dt.date.fromisoformat(w["start"]); e = dt.date.fromisoformat(w["end"])
        for fd, lab in [(dt.date(2026, 7, 27), "R9.6 freeze Jul 27"),
                        (dt.date(2026, 8, 23), "R9.7 freeze Aug 23")]:
            if s <= fd <= e:
                w["freeze"] = lab
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


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: refresh_dashboard.py path/to/index.html")
    path = sys.argv[1]
    html = open(path, encoding="utf-8").read()

    today = dt.date.today()
    print(f"Refreshing {path} — snapshot {today.isoformat()}")

    ll = pull_ll_features()
    print(f"  LL features: {len(ll)}")
    theme = pull_theme_features()
    print(f"  theme features: {len(theme)}")
    features_all = build_features_all(ll, theme)
    print(f"  FEATURES_ALL: {len(features_all)}")
    all_set = build_all(features_all)
    bugs = pull_bugs()
    print(f"  bugs: {len(bugs)}")
    nopod = pull_nopod()
    print(f"  no-pod bugs: {len(nopod)}")
    weeks = build_weeks(today)

    # splice each data block
    html = splice(html, "FEATURES", js_array("FEATURES", ll))
    html = splice(html, "FEATURES_ALL", js_array("FEATURES_ALL", features_all))
    html = splice(html, "ALL", js_array("ALL", all_set))
    html = splice(html, "BUGS", js_array("BUGS", bugs))
    html = splice(html, "NOPOD", js_array("NOPOD", nopod))

    # TODAY (single-line const) and WEEKS
    import re
    html = re.sub(r'const TODAY = new Date\("[^"]*"\);',
                  f'const TODAY = new Date("{today.isoformat()}");', html, count=1)
    html = splice(html, "WEEKS", js_array("WEEKS", weeks))

    # GENERATED_AT: full UTC timestamp so the header can show freshness (date + time).
    now_iso = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    if re.search(r'const GENERATED_AT = "[^"]*";', html):
        html = re.sub(r'const GENERATED_AT = "[^"]*";',
                      f'const GENERATED_AT = "{now_iso}";', html, count=1)
    else:
        # first run after adding the marker: inject right after TODAY
        html = re.sub(r'(const TODAY = new Date\("[^"]*"\);)',
                      r'\1\nconst GENERATED_AT = "' + now_iso + '";', html, count=1)

    open(path, "w", encoding="utf-8").write(html)
    print(f"Done — generated at {now_iso}")


if __name__ == "__main__":
    main()
