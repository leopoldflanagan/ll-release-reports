#!/usr/bin/env python3
# Reads qa-roadmap/data.json, computes the QA digest, POSTs to Slack and/or Teams.
# Env: SLACK_WEBHOOK / TEAMS_WEBHOOK. Arg1 optional data.json path.
# KEY RULE (Polo, Aug 26): the Due date is the DEV deadline to hand a bug to Testing (reach "In Stage").
# Once a bug is In Stage / In QA it is NOT overdue anymore. Overdue = past due AND still before testing.
import json, os, sys, datetime, urllib.request, urllib.error

# ---- DST-safe time guard ----------------------------------------------------
# The workflow fires at a superset of UTC times covering both CDT and CST. This guard
# posts ONLY when the current America/Chicago (US Central) local time is within 45 min of
# one of the four intended slots (8:30 / 10:30 / 14:00 / 18:00 CT) — so the schedule stays
# exact year-round and self-adjusts for daylight saving. Set DIGEST_GUARD=1 to enable.
if os.environ.get("DIGEST_GUARD"):
    try:
        from zoneinfo import ZoneInfo
        now = datetime.datetime.now(ZoneInfo("America/Chicago"))
        mins = now.hour*60 + now.minute
        TARGETS = [8*60+30, 10*60+30, 14*60, 18*60]
        if not any(abs(mins-t) <= 45 for t in TARGETS):
            print("guard: %02d:%02d CT is not a digest slot — skipping." % (now.hour, now.minute))
            sys.exit(0)
        print("guard: %02d:%02d CT — posting." % (now.hour, now.minute))
    except Exception as e:
        print("guard: timezone check failed (%s) — posting anyway." % e)

DATA = sys.argv[1] if len(sys.argv)>1 else "data.json"
d = json.load(open(DATA))
BUGS = d.get("bugs",[]); CLOSED = d.get("bugs_closed",[])
TODAY = datetime.date.fromisoformat(d.get("today"))
DASH = "https://leopoldflanagan.github.io/ll-release-reports/qa-roadmap/"
JIRA="https://wellfit.atlassian.net/browse/"
TESTING={"In Stage","In QA"}                 # handed off to testing → due no longer applies
NOTSTARTED={"Backlog","Ready for Development"}
def dt(s): return datetime.date.fromisoformat(str(s)[:10]) if s else None
def lvl(p): return int(p[0]) if p and p[0].isdigit() else 5
def grp(b):
    if b["status"] in TESTING: return "testing"
    if b["status"] in NOTSTARTED: return "notstarted"
    return "indev"
for b in BUGS: b["_l"]=lvl(b.get("prio")); b["_g"]=grp(b); b["_due"]=dt(b.get("due"))
def overdue(b): return b["_due"] and b["_due"]<TODAY and b["status"] not in TESTING
def soon(b): return b["_due"] and TODAY<=b["_due"]<=TODAY+datetime.timedelta(days=7) and b["status"] not in TESTING

total=len(BUGS); p1=sum(1 for b in BUGS if b["_l"]==1); p2=sum(1 for b in BUGS if b["_l"]==2)
def pipe(tier):
    s=[b for b in BUGS if b["_l"] in tier]; from_collections=None
    return (sum(1 for b in s if b["_g"]=="notstarted"),sum(1 for b in s if b["_g"]=="indev"),sum(1 for b in s if b["_g"]=="testing"))
ns,idev,test=pipe((1,2))
od=[b for b in BUGS if overdue(b)]; sn=[b for b in BUGS if soon(b)]
od_p1=[b for b in od if b["_l"]==1]; od_p2=[b for b in od if b["_l"]==2]; od_p3=[b for b in od if b["_l"]==3]; od_p4=[b for b in od if b["_l"]==4]
odp12=[b["key"] for b in od if b["_l"] in (1,2)]
cut1=TODAY-datetime.timedelta(days=1)
created24=sum(1 for b in BUGS+CLOSED if dt(b.get("created")) and dt(b.get("created"))>=cut1)
resolved24=sum(1 for b in CLOSED if dt(b.get("resolved")) and dt(b.get("resolved"))>=cut1)
newp1=sum(1 for b in BUGS+CLOSED if dt(b.get("created")) and dt(b.get("created"))>=cut1 and lvl(b.get("prio"))==1)
net=created24-resolved24; nets=("+%d"%net if net>0 else str(net))
r97=datetime.date(2026,9,2); dr=(r97-TODAY).days

# ---- clear-date read per tier (same method as the report's History & projection) ----
# BEST-CASE: projection uses close rate ONLY (resolved/wk, trailing 28d) — new bugs are NOT
# subtracted, since feature testing is wrapping up and P1/P2 creation should fall to ~0.
def _tier(b):
    l=lvl(b.get("prio")); return "p1" if l==1 else "p2" if l==2 else "p34"
def _rate(t):
    cut=TODAY-datetime.timedelta(days=28)
    res=sum(1 for b in CLOSED if dt(b.get("resolved")) and dt(b.get("resolved"))>=cut and _tier(b)==t)
    openn=sum(1 for b in BUGS if _tier(b)==t)
    return {"res":round(res/4,1),"open":openn}
def _clear(t):
    r=_rate(t); c=r["res"]; o=r["open"]
    if o<=0: return "cleared"
    if c<=0.2: return "no closures at this pace"
    wks=o/c
    if wks>78: return "18+ months out"
    dd=TODAY+datetime.timedelta(days=round(wks*7))
    return "~"+dd.strftime("%b %Y")
cr_p1=_clear("p1"); cr_p2=_clear("p2")

def slack():
    b=[
      {"type":"header","text":{"type":"plain_text","text":"🧭 LL / PLANS QA — daily snapshot","emoji":True}},
      {"type":"context","elements":[{"type":"mrkdwn","text":"%s · R9.7 release Sep 2 (%d days)"%(TODAY.strftime("%b %d, %Y"),dr)}]},
      {"type":"section","fields":[
        {"type":"mrkdwn","text":"*Open bugs*\n%d  (%s vs yest.)"%(total,nets)},
        {"type":"mrkdwn","text":"*P1 / P2 blockers*\n%d / %d"%(p1,p2)},
        {"type":"mrkdwn","text":"*Opened (24h)*\n%d  · %d new P1"%(created24,newp1)},
        {"type":"mrkdwn","text":"*Resolved (24h)*\n:white_check_mark: %d"%resolved24},
      ]},
      {"type":"section","text":{"type":"mrkdwn","text":"*P1+P2 pipeline*   :red_circle: %d not started   ·   :large_yellow_circle: %d in dev   ·   :large_green_circle: %d in testing"%(ns,idev,test)}},
      {"type":"divider"},
      {"type":"section","text":{"type":"mrkdwn","text":"*:alarm_clock: Overdue to testing*  (past due, still in dev — not yet In Stage)\n*%d*   ·   *P1 %d* | *P2 %d* | P3 %d | P4 %d"%(len(od),len(od_p1),len(od_p2),len(od_p3),len(od_p4))}},
    ]
    if odp12:
        links=", ".join("<%s%s|%s>"%(JIRA,k,k) for k in odp12[:10])
        b.append({"type":"section","text":{"type":"mrkdwn","text":":rotating_light: *P1/P2 to push:* %s"%links}})
    b.append({"type":"context","elements":[{"type":"mrkdwn","text":":calendar: %d more due this week (still to reach testing)"%len(sn)}]})
    b.append({"type":"section","text":{"type":"mrkdwn","text":"*:chart_with_downwards_trend: Best-case clear date* (close rate only*)\n:red_circle: P1 — *%s*   ·   :large_yellow_circle: P2 — *%s*"%(cr_p1,cr_p2)}})
    b.append({"type":"context","elements":[{"type":"mrkdwn","text":"_*Open ÷ close rate (resolved/wk, last 4 wks), assuming new P1/P2 creation drops to ~0 as feature testing wraps. A trend, not a commitment._"}]})
    b.append({"type":"actions","elements":[{"type":"button","text":{"type":"plain_text","text":"Open dashboard →"},"url":DASH,"style":"primary"}]})
    return {"blocks":b}

def post(url,payload):
    req=urllib.request.Request(url,data=json.dumps(payload).encode(),headers={"Content-Type":"application/json"})
    try:
        r=urllib.request.urlopen(req,timeout=25); return r.status,r.read().decode()[:120]
    except urllib.error.HTTPError as e: return e.code,e.read().decode()[:200]
    except Exception as e: return "ERR",str(e)[:200]

print("summary:",dict(total=total,p1=p1,p2=p2,pipe=(ns,idev,test),overdue=len(od),odp12=odp12,soon=len(sn),c24=created24,r24=resolved24,newp1=newp1,clear=(cr_p1,cr_p2)))
sw=os.environ.get("SLACK_WEBHOOK")
if sw: print("slack:",post(sw,slack()))
