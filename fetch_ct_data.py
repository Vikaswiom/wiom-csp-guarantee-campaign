#!/usr/bin/env python3
"""
fetch_ct_data.py  —  Pull CleverTap events for the two audit-completed
CSP-Guarantee campaigns (Optical Power / Service SLA) and write data.json
for dashboard.html.

WHY events export (not counts API): /1/events.json returns raw events with
profile.identity, so we can dedupe to UNIQUE users per event, and split by the
offer_id prop. The counts API returns taps (~2.4x users) and can't split cleanly.
(See the CleverTap poller-campaign reference.)

CREDS (never commit these): set env vars, or put them in C:\\credentials\\.env
  CLEVERTAP_ACCOUNT=...        # X-CleverTap-Account-Id
  CLEVERTAP_PASSCODE=...       # X-CleverTap-Passcode
  CLEVERTAP_REGION=eu1         # Wiom = eu1

USAGE:
  python fetch_ct_data.py                 # from START_DATE (below) to today
  python fetch_ct_data.py 20260716        # override the start date (YYYYMMDD)

Then:  git commit -am "refresh dashboard" && git push   (GitHub Pages serves data.json)
"""
import os, sys, json, time, datetime, urllib.request, urllib.error

# ---- config -----------------------------------------------------------------
START_DATE = "20260715"                      # campaign launch (YYYYMMDD); override via argv[1]
OFFERS = {"sehat_optical": "Optical Power", "sehat_sla": "Service SLA"}
COLORS = {"sehat_optical": "#D9008D", "sehat_sla": "#2563EB"}
# funnel event name -> data key
FUNNEL = [
    ("Sehat_View_education", "reached"),
    ("Sehat_Learn_More",     "learn_more"),
    ("Sehat_View_plan",      "view_plan"),
    ("Sehat_Start_Quiz",     "start_quiz"),
    ("Sehat_Quiz_Complete",  "quiz_complete"),
    ("Sehat_OptIn",          "enrolled"),
]
STEP_LABELS = {
    "reached": "Reached", "learn_more": "Learn more", "view_plan": "Viewed plan",
    "start_quiz": "Started quiz", "quiz_complete": "Completed quiz", "enrolled": "Enrolled (opt-in)",
}

# ---- creds ------------------------------------------------------------------
def load_creds():
    acc = os.environ.get("CLEVERTAP_ACCOUNT"); pas = os.environ.get("CLEVERTAP_PASSCODE")
    reg = os.environ.get("CLEVERTAP_REGION", "eu1")
    if not (acc and pas):
        envf = r"C:\credentials\.env"
        if os.path.exists(envf):
            for line in open(envf, encoding="utf-8"):
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1); k = k.strip(); v = v.strip().strip('"').strip("'")
                    if k == "CLEVERTAP_ACCOUNT" and not acc: acc = v
                    if k == "CLEVERTAP_PASSCODE" and not pas: pas = v
                    if k == "CLEVERTAP_REGION": reg = v or reg
    if not (acc and pas):
        sys.exit("ERROR: set CLEVERTAP_ACCOUNT and CLEVERTAP_PASSCODE (env or C:\\credentials\\.env)")
    return acc, pas, reg

ACCOUNT, PASSCODE, REGION = load_creds()
BASE = f"https://{REGION}.api.clevertap.com"

def _req(url, method="GET", body=None, with_ct=False):
    headers = {"X-CleverTap-Account-Id": ACCOUNT, "X-CleverTap-Passcode": PASSCODE}
    data = None
    if body is not None:
        data = json.dumps(body).encode(); headers["Content-Type"] = "application/json"  # POST only
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                time.sleep(1.5 * (attempt + 1)); continue
            raise
        except Exception:
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"request failed after retries: {url}")

def export_event(event_name, frm, to):
    """Yield every event record {profile, ts, event_props} for the date range."""
    url = f"{BASE}/1/events.json?batch_size=5000"
    resp = _req(url, method="POST", body={"event_name": event_name, "from": int(frm), "to": int(to)})
    cursor = resp.get("cursor")
    seen_any = False
    while cursor:
        page = _req(f"{BASE}/1/events.json?cursor={cursor}", method="GET")  # GET: no Content-Type
        recs = page.get("records") or []
        for rec in recs:
            seen_any = True; yield rec
        cursor = page.get("cursor")
        if not recs:
            break
    if not seen_any:
        for rec in (resp.get("records") or []):
            yield rec

def identity_of(rec):
    p = rec.get("profile") or {}
    return p.get("identity") or p.get("objectId") or p.get("email") or None

def offer_of(rec):
    return ((rec.get("event_props") or {}).get("offer_id")) or ""

# ---- pull -------------------------------------------------------------------
def main():
    frm = sys.argv[1] if len(sys.argv) > 1 else START_DATE
    to = datetime.date.today().strftime("%Y%m%d")
    print(f"CleverTap {REGION} · {frm} -> {to}")

    # per offer: {key: set(identity)} ; daily {offer: {day: {reached:set, enrolled:set}}}
    uniq = {o: {k: set() for _, k in FUNNEL} for o in OFFERS}
    daily = {o: {} for o in OFFERS}
    quiz = {o: {"q1c": set(), "q1t": set(), "q2c": set(), "q2t": set()} for o in OFFERS}

    for event_name, key in FUNNEL:
        n = 0
        for rec in export_event(event_name, frm, to):
            o = offer_of(rec); ident = identity_of(rec)
            if o not in OFFERS or not ident: continue
            uniq[o][key].add(ident); n += 1
            if key in ("reached", "enrolled"):
                day = str(rec.get("ts", ""))[:8]
                d = daily[o].setdefault(day, {"reached": set(), "enrolled": set()})
                d[key].add(ident)
        print(f"  {event_name:22s} -> {n} events")

    # quiz accuracy from Sehat_Quiz_Answered
    for rec in export_event("Sehat_Quiz_Answered", frm, to):
        o = offer_of(rec); ident = identity_of(rec)
        if o not in OFFERS or not ident: continue
        pr = rec.get("event_props") or {}
        q = str(pr.get("question")); correct = str(pr.get("correct")).lower() in ("true", "1")
        if q == "1":
            quiz[o]["q1t"].add(ident)
            if correct: quiz[o]["q1c"].add(ident)
        elif q == "2":
            quiz[o]["q2t"].add(ident)
            if correct: quiz[o]["q2c"].add(ident)

    campaigns = []
    for o, label in OFFERS.items():
        funnel = {k: len(uniq[o][k]) for _, k in FUNNEL}
        days = sorted(daily[o].keys())
        daily_list = [{"date": f"{d[:4]}-{d[4:6]}-{d[6:8]}",
                       "reached": len(daily[o][d]["reached"]),
                       "enrolled": len(daily[o][d]["enrolled"])} for d in days if len(d) == 8]
        campaigns.append({
            "key": o, "label": label, "color": COLORS[o], "funnel": funnel,
            "quiz": {"q1_correct": len(quiz[o]["q1c"]), "q1_total": len(quiz[o]["q1t"]),
                     "q2_correct": len(quiz[o]["q2c"]), "q2_total": len(quiz[o]["q2t"])},
            "daily": daily_list,
        })

    out = {
        "generated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "sample": False, "region": REGION,
        "funnel_steps": [[k, STEP_LABELS[k], ev] for ev, k in FUNNEL],
        "campaigns": campaigns,
    }
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")
    json.dump(out, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("wrote", path)
    for c in campaigns:
        f = c["funnel"]; rate = round(100 * f["enrolled"] / f["reached"]) if f["reached"] else 0
        print(f"  {c['label']:14s} reached={f['reached']:4d}  enrolled={f['enrolled']:4d}  opt-in={rate}%")

if __name__ == "__main__":
    main()
