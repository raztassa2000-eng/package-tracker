#!/usr/bin/env python3
"""
package_tracker.py — reads synced iMessage/SMS from the local Messages DB,
finds "your package arrived / ready for pickup" texts, and organizes them:
pickup location, time left to collect, courier (sender), and tracking/pickup code.

Usage:
  python3 package_tracker.py                 # pretty table, active pickups
  python3 package_tracker.py --all           # include already-expired / old
  python3 package_tracker.py --days 30       # only scan last N days (default 30)
  python3 package_tracker.py --json          # machine-readable output
  python3 package_tracker.py --debug         # show why messages matched
"""
import sqlite3, re, os, sys, json, argparse, hashlib
from datetime import datetime, timedelta, timezone

DB = os.path.expanduser("~/Library/Messages/chat.db")
APPLE_EPOCH = 978307200  # 2001-01-01 in unix seconds
MIN_DATE = datetime(2026, 1, 1)  # ignore anything before 2026
COLLECTED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "collected.txt")

def parcel_key(courier, received, ident):
    """Stable short id for a parcel, used to mark it collected."""
    raw = f"{courier}|{received}|{ident or ''}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]

def load_collected():
    try:
        with open(COLLECTED_FILE, encoding="utf-8") as f:
            return {ln.strip() for ln in f if ln.strip()}
    except FileNotFoundError:
        return set()

def add_collected(key):
    have = load_collected()
    if key not in have:
        with open(COLLECTED_FILE, "a", encoding="utf-8") as f:
            f.write(key + "\n")

def remove_collected(key):
    have = load_collected()
    have.discard(key)
    with open(COLLECTED_FILE, "w", encoding="utf-8") as f:
        f.write("".join(k + "\n" for k in sorted(have)))

# ---- Known Israeli couriers / pickup services (sender identity) -------------
COURIERS = [
    (r"דואר ?ישראל|בית ?הדואר|דבר ?דואר|רשות ?הדואר|israel ?post", "דואר ישראל"),
    (r"\bboxit\b|בוקסיט", "BOXIT"),
    (r"\bcubbo\b|קובו", "Cubbo"),
    (r"נקסטר|nexter", "Nexter"),
    (r"\bhfd\b|איץ׳ אף די", "HFD"),
    (r"צ׳יטה|צ'יטה|cheetah", "צ'יטה"),
    (r"\brunner\b|ראנר|רانر", "Runner"),
    (r"יילו|yellow", "Yellow"),
    (r"\bups\b", "UPS"),
    (r"\bfedex\b|פדקס", "FedEx"),
    (r"\bdhl\b", "DHL"),
    (r"amazon|אמזון", "Amazon"),
    (r"aliexpress|עליאקספרס|علي", "AliExpress"),
    (r"iherb|איהרב", "iHerb"),
    (r"נקודת ?איסוף|pickup ?point", "נקודת איסוף"),
    (r"\blocker\b|לוקר", "Locker"),
]

# ---- Message must look like a delivery/pickup notification ------------------
PACKAGE_SIGNALS = [
    r"חביל", r"משלוח", r"מסירה", r"איסוף", r"לאסוף", r"אספקה",
    r"נקודת ?איסוף", r"תא ?מס", r"לוקר", r"\blocker\b",
    r"ממתינ", r"מחכה ל", r"הגיע", r"מוכנה? ?לאיסוף", r"מוכן ?לאיסוף",
    r"קוד ?איסוף", r"קוד ?לפתיחת", r"pickup", r"parcel", r"package",
    r"ready for pickup", r"out for delivery", r"מספר ?מעקב", r"tracking",
    r"דבר ?דואר", r"בית ?הדואר",
]

# ---- Extraction patterns ----------------------------------------------------
# pickup code (e.g. "קוד איסוף 12345", "קוד לפתיחת התא: 4821", "code: 8842")
CODE_RE = re.compile(
    r"(?:קוד(?:\s*(?:איסוף|לפתיחת(?:\s*התא)?|מסירה|ל?קבלת(?:\s*ה?חבילה)?))?|code|pin)"
    r"\s*[:\-]?\s*([0-9]{3,8})",
    re.IGNORECASE)
# tracking number (long digit run, or letters+digits like RR123456789IL)
TRACK_RE = re.compile(r"\b([A-Z]{2}\d{6,}[A-Z]{2}|\d{9,})\b")
# first useful URL in the message (skip unsubscribe / marketing links)
URL_RE = re.compile(r"https?://[^\s]+")
UNSUB = re.compile(r"הסר|hasr|/rm/|fls\.cx|unsub", re.IGNORECASE)

def extract_link(text):
    for url in URL_RE.findall(text or ""):
        if not UNSUB.search(url):
            return url.rstrip(".,)")
    return None
# deadline: "עד 30.8", "עד ה-30/08/2026", "עד תאריך 30.08", "until 30/08"
DEADLINE_RE = re.compile(
    r"(?:עד(?:\s*ה?[-־]?)?(?:\s*תאריך)?|until|by)\s*"
    r"(\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?)", re.IGNORECASE)
# "יש לך 5 ימים" / "בתוך 3 ימי עסקים" / "within 7 days"
DAYS_LEFT_RE = re.compile(
    r"(?:תוך|בתוך|יש ?לך|נותרו|בעוד|within|in)\s*(\d{1,2})\s*"
    r"(?:ימי[םי]?|ימי ?עסקים|days?|ימים)", re.IGNORECASE)
# location: text after an explicit place anchor
LOC_RE = re.compile(
    r"(?:מחנות|בחנות|חנות|בסניף|מסניף|סניף|נקודת ?ה?איסוף(?:\s*המשויכת)?|"
    r"פרטי ?הנקודה|בכתובת|כתובת|לכתובת)\s*[:\-]?\s*"
    r"([^\n.,;()]{3,50})", re.IGNORECASE)
# reject extractions that are clearly not an address/branch
BAD_LOC = re.compile(
    r"ימי ?עסקים|במשך|בתוך|המשויכת|חבילה ?בודדת|באתר|בעל ?כרטיס|"
    r"במועד|י?שלח|תטופל|בהקדם|שסופק|^\d+\s*[-–]\s*\d+$", re.IGNORECASE)
# cut trailing noise that leaks into the branch/address text
STOP_LOC = re.compile(
    r"\s*(?:מוכנ|ממתינ|קוד|פרטים|שעות|תודה|חובה|במשלוח|תעודת|נא\b|יש\b|הזמנ).*$")

def clean_location(text):
    m = LOC_RE.search(text or "")
    if not m:
        return None
    loc = STOP_LOC.sub("", m.group(1)).strip(" :-")
    if BAD_LOC.search(loc) or len(loc) < 3:
        return None
    return loc

def apple_to_dt(raw):
    # message.date is nanoseconds since 2001 (modern) or seconds (very old)
    secs = raw / 1_000_000_000 if raw > 1_000_000_000_000 else raw
    return datetime.fromtimestamp(secs + APPLE_EPOCH)

def detect_courier(sender, text):
    if sender in SENDER_MAP:
        return SENDER_MAP[sender]
    blob = f"{sender or ''} {text or ''}"
    for pat, name in COURIERS:
        if re.search(pat, blob, re.IGNORECASE):
            return name
    # If sender is an alphanumeric shortcode/name, show it raw
    if sender and not re.match(r"^\+?\d[\d\s\-]+$", sender):
        return sender
    return sender or "לא ידוע"

# A real pickup notice must contain one of these STRONG phrases.
STRONG_PICKUP = re.compile("|".join([
    r"מוכנ[הים]? ?לאיסוף", r"ממתינ\w* (?:לך|עבורך)\w* (?:ב|באיסוף|בסניף|בנקודת|בנקודה)",
    r"ממתינ\w* ב(?:סניף|נקודת|נקודה)", r"נקודת ?ה?איסוף", r"נקודת ?ה?חלוקה",
    r"נקודת ?ה?מסירה", r"קוד ?ל?קבלת ?ה?חבילה", r"קוד ?איסוף", r"קוד ?לפתיחת",
    r"לאיסוף ?הזמנה", r"לאסוף\w* (?:אות[הו]|מ?חנות|מ?סניף)", r"הזמנתך[^.\n]{0,40}מוכנה",
    r"דבר ?דואר", r"בית ?ה?דואר", r"הונחה ?בנקודה", r"הגיעה ?לנקודת",
    r"ready for pickup", r"החבילה ?שלך ?(?:מוכנה|ממתינה|הגיעה)",
]), re.IGNORECASE)

# If any of these match, it is NOT a parcel pickup (telecom bundle, promo, bureaucracy).
EXCLUDE = re.compile("|".join([
    r"חבילת ?גלישה", r"שירות ?הגלישה", r"ללא ?חבילה", r"גלישה", r"דקות ו?SMS",
    r"קופון", r"מבצע", r"משלוח ?חינם", r"מינימום ?הזמנה", r"שובר", r"הזדמנות ?אחרונה",
    r"בעיצומו", r"הזמ[ןנ] ?עכשיו", r"הנחה", r"בשווי", r"₪ ?בלבד", r"חוגגים",
    r"רישיון ?לייזר", r"משרד ?הת", r"התמ", r"שי ל", r"נופש",
    r"בדלפק", r"dorix", r"ארומה",  # in-store food/coffee counter pickups, not parcels
]), re.IGNORECASE)

# phone gateways that map to a known courier
SENDER_MAP = {"+972529997777": "HFD"}

# Order / shipment id used to collapse duplicate updates of the same parcel.
ID_RE = re.compile(
    r"(?:הזמנ(?:ה|תך)|משלוח|שמספרו|הזמנה ?מספר|order)\s*(?:מספר\s*)?[:\-]?\s*"
    r"([A-Za-z]{0,4}\d[\w]{4,})", re.IGNORECASE)
ID_FALLBACK = re.compile(r"\b([A-Z]{2}\d{6,}[A-Z]{0,2}|[A-Za-z]{2,4}\d{6,}|\d{8,})\b")

def canonical_id(text):
    m = ID_RE.search(text or "")
    if m:
        return m.group(1).upper()
    m = ID_FALLBACK.search(text or "")
    return m.group(1).upper() if m else None

def is_package(text):
    if not text:
        return False
    if EXCLUDE.search(text):
        return False
    return bool(STRONG_PICKUP.search(text))

def parse_deadline(text, msg_dt):
    m = DEADLINE_RE.search(text)
    if m:
        raw = m.group(1)
        parts = re.split(r"[./]", raw)
        try:
            d, mth = int(parts[0]), int(parts[1])
            yr = int(parts[2]) if len(parts) > 2 else msg_dt.year
            if yr < 100: yr += 2000
            dl = datetime(yr, mth, d, 23, 59)
            # if date already passed relative to msg by a lot, roll year
            if dl < msg_dt - timedelta(days=1) and len(parts) <= 2:
                dl = dl.replace(year=yr + 1)
            return dl
        except (ValueError, IndexError):
            pass
    m = DAYS_LEFT_RE.search(text)
    if m:
        return msg_dt + timedelta(days=int(m.group(1)))
    return None

def first(regex, text):
    m = regex.search(text or "")
    return m.group(1).strip() if m else None

def fetch(days, include_all):
    if not os.path.exists(DB):
        sys.exit(f"לא נמצא בסיס נתונים של Messages ב-{DB}\nודא ש-Messages in iCloud מסונכרן ל-Mac.")
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    # cutoff = the later of (N-day window) and the hard floor of 2026-01-01
    day_cut = datetime.now() - timedelta(days=days)
    cutoff = max(day_cut, MIN_DATE)
    cutoff_ns = int((cutoff - datetime(2001, 1, 1)).total_seconds() * 1_000_000_000)
    rows = con.execute("""
        SELECT m.date, m.text, h.id, m.is_from_me
        FROM message m LEFT JOIN handle h ON m.handle_id = h.ROWID
        WHERE m.is_from_me = 0 AND m.text IS NOT NULL AND m.date > ?
        ORDER BY m.date DESC
    """, (cutoff_ns,)).fetchall()
    con.close()
    return rows

DEMO_FLAG = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".demo")

def _demo_card():
    return {
        "received": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "courier": "BOXIT (בדיקה)", "sender_raw": "DEMO",
        "location": "סופר-פארם דיזנגוף 50, תל אביב",
        "pickup_code": "4821", "tracking": "RR123456785IL",
        "deadline": (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d"),
        "time_left": "2 ימים", "urgency": "soon", "expired": False,
        "text": "כרטיס בדיקה — יימחק אוטומטית",
    }

def scan(days=30, include_all=False):
    now = datetime.now()
    results = []
    if os.path.exists(DEMO_FLAG):
        results.append(_demo_card())
    for raw_date, text, sender, _ in fetch(days, include_all):
        if not is_package(text):
            continue
        dt = apple_to_dt(raw_date)
        deadline = parse_deadline(text, dt)
        time_left, urgency = None, "none"
        if deadline:
            delta = deadline - now
            if delta.total_seconds() > 0:
                days_left = delta.days
                time_left = f"{days_left} ימים" if days_left >= 1 else "היום"
                urgency = "soon" if days_left <= 2 else "ok"
            else:
                time_left = f"פג לפני {abs(delta.days)} ימים"
                urgency = "expired"
        results.append({
            "received": dt.strftime("%Y-%m-%d %H:%M"),
            "_dt": dt,
            "courier": detect_courier(sender, text),
            "sender_raw": sender or "",
            "location": clean_location(text),
            "pickup_code": first(CODE_RE, text),
            "tracking": first(TRACK_RE, text),
            "order_id": canonical_id(text),
            "link": extract_link(text),
            "deadline": deadline.strftime("%Y-%m-%d") if deadline else None,
            "time_left": time_left,
            "urgency": urgency,
            "expired": bool(deadline and deadline < now),
            "text": text.strip(),
        })

    results = dedupe(results)
    if not include_all:
        results = [r for r in results if not r["expired"]]
    # assign a stable key and drop anything already marked collected
    collected = load_collected()
    final = []
    for r in results:
        r["key"] = parcel_key(r["courier"], r["received"], r["order_id"] or r["location"])
        r.pop("_dt", None)
        if r["key"] not in collected:
            final.append(r)
    return final


def _info_score(r):
    # prefer the message that carries the most actionable detail
    return (bool(r["pickup_code"]), bool(r["location"]), bool(r["deadline"]), r["_dt"])

def dedupe(results):
    """Collapse multiple updates of the same parcel into one best entry,
    merging in any fields the winner is missing from its siblings."""
    groups = {}
    order = []
    for r in results:
        key = (r["courier"], r["order_id"]) if r["order_id"] else ("_uniq", id(r))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(r)
    out = []
    for key in order:
        grp = groups[key]
        best = max(grp, key=_info_score)
        for field in ("location", "pickup_code", "tracking", "link", "deadline", "time_left"):
            if not best[field]:
                for sib in grp:
                    if sib[field]:
                        best[field] = sib[field]
                        if field in ("deadline", "time_left"):
                            best["urgency"] = sib["urgency"]
                        break
        out.append(best)

    # second pass: merge leftovers that share courier + location within 30 days
    # (same parcel announced in differently-phrased messages with no shared id)
    merged, used = [], [False] * len(out)
    for i, a in enumerate(out):
        if used[i]:
            continue
        if a["location"]:
            akey = " ".join(a["location"].split()[:3])
            for j in range(i + 1, len(out)):
                b = out[j]
                if used[j] or not b["location"] or b["courier"] != a["courier"]:
                    continue
                if " ".join(b["location"].split()[:3]) == akey \
                        and abs((a["_dt"] - b["_dt"]).days) <= 30:
                    for field in ("pickup_code", "tracking", "link", "deadline", "time_left"):
                        if not a[field] and b[field]:
                            a[field] = b[field]
                            if field in ("deadline", "time_left"):
                                a["urgency"] = b["urgency"]
                    used[j] = True
        merged.append(a)

    # finalize: drop bogus deadlines (earlier than the message itself) and
    # recompute expiry/countdown consistently after all merging
    now = datetime.now()
    for r in merged:
        if r["deadline"]:
            try:
                dl = datetime.strptime(r["deadline"], "%Y-%m-%d").replace(hour=23, minute=59)
            except ValueError:
                dl = None
            if not dl or dl.date() < r["_dt"].date():
                r["deadline"] = r["time_left"] = None
                r["urgency"] = "none"; r["expired"] = False
            else:
                r["expired"] = dl < now
                days_left = (dl - now).days
                if dl < now:
                    r["time_left"] = f"פג לפני {abs(days_left)} ימים"; r["urgency"] = "expired"
                else:
                    r["time_left"] = f"{days_left} ימים" if days_left >= 1 else "היום"
                    r["urgency"] = "soon" if days_left <= 2 else "ok"
    merged.sort(key=lambda r: r["_dt"], reverse=True)
    return merged


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--all", action="store_true", help="include expired/old pickups")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--serve", action="store_true", help="run live dashboard server")
    ap.add_argument("--port", type=int, default=7654)
    ap.add_argument("--collect", metavar="KEY", help="mark a parcel collected (hides it)")
    ap.add_argument("--uncollect", metavar="KEY", help="un-mark a collected parcel")
    ap.add_argument("--list-collected", action="store_true", help="show collected keys")
    args = ap.parse_args()

    if args.collect:
        add_collected(args.collect)
        print(f"collected {args.collect}")
        return
    if args.uncollect:
        remove_collected(args.uncollect)
        print(f"uncollected {args.uncollect}")
        return
    if args.list_collected:
        print("\n".join(sorted(load_collected())) or "(none)")
        return

    if args.serve:
        serve(args.port, args.days)
        return

    results = scan(args.days, args.all)

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return

    if not results:
        print("לא נמצאו הודעות חבילות בטווח שנסרק.")
        print(f"(נסרקו {args.days} ימים אחורה. אם ההודעות בדיוק סונכרנו, נסה שוב בעוד כמה דקות.)")
        return

    print(f"\n📦  נמצאו {len(results)} חבילות\n" + "═" * 70)
    for r in results:
        head = f"  {r['courier']}"
        if r["time_left"]:
            head += f"   ⏳ {r['time_left']}"
        print(head)
        if r["location"]:    print(f"     📍 מיקום:   {r['location']}")
        if r["pickup_code"]: print(f"     🔑 קוד:     {r['pickup_code']}")
        if r["tracking"]:    print(f"     🔎 מעקב:    {r['tracking']}")
        if r["deadline"]:    print(f"     📅 עד:      {r['deadline']}")
        if r.get("link"):    print(f"     🔗 קישור:   {r['link']}")
        print(f"     🕒 התקבל:  {r['received']}   מ-{r['sender_raw']}")
        if args.debug:       print(f"     💬 {r['text'][:150]}")
        print("─" * 70)

def render_html(results):
    now = datetime.now().strftime("%H:%M:%S")
    UC = {"soon": "#ff5c5c", "ok": "#3ddc84", "expired": "#8a8a8a", "none": "#f0b90b"}
    cards = []
    for r in results:
        u = r["urgency"]
        badge = f'<span class="left" style="color:{UC[u]}">⏳ {r["time_left"]}</span>' if r["time_left"] else ""
        rows = ""
        if r["location"]:    rows += f'<div class="row"><span>📍</span><b>{r["location"]}</b></div>'
        if r["pickup_code"]: rows += f'<div class="row"><span>🔑</span>קוד: <b class="code">{r["pickup_code"]}</b></div>'
        if r["tracking"]:    rows += f'<div class="row"><span>🔎</span>מעקב: {r["tracking"]}</div>'
        if r["deadline"]:    rows += f'<div class="row"><span>📅</span>עד {r["deadline"]}</div>'
        cards.append(f'''
        <div class="card" style="border-right:5px solid {UC[u]}">
          <div class="card-head"><span class="courier">{r["courier"]}</span>{badge}</div>
          {rows}
          <div class="meta">🕒 {r["received"]} · {r["sender_raw"]}</div>
        </div>''')
    body = "".join(cards) if cards else \
        '<div class="empty">אין חבילות ממתינות לאיסוף 📭<br><small>הודעות חדשות יופיעו כאן אוטומטית</small></div>'
    return f'''<!doctype html><html lang="he" dir="rtl"><head>
<meta charset="utf-8"><meta http-equiv="refresh" content="60">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>📦 החבילות שלי</title><style>
*{{box-sizing:border-box;margin:0}}
body{{font-family:-apple-system,"Heebo",Arial,sans-serif;background:#0d1117;color:#e6edf3;padding:28px;min-height:100vh}}
h1{{font-size:26px;margin-bottom:4px}} .sub{{color:#8b949e;font-size:13px;margin-bottom:22px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:14px;padding:16px 18px}}
.card-head{{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}}
.courier{{font-size:19px;font-weight:700}} .left{{font-weight:700;font-size:15px}}
.row{{display:flex;gap:8px;align-items:center;padding:3px 0;font-size:15px;color:#c9d1d9}}
.row span{{width:20px;text-align:center}} .code{{font-family:monospace;font-size:17px;color:#f0b90b;letter-spacing:1px}}
.meta{{margin-top:12px;font-size:12px;color:#6e7681;border-top:1px solid #21262d;padding-top:10px}}
.empty{{text-align:center;color:#8b949e;font-size:20px;padding:80px 0;line-height:2}}
.empty small{{font-size:14px;color:#6e7681}}
</style></head><body>
<h1>📦 החבילות שלי</h1>
<div class="sub">{len(results)} ממתינות · עודכן {now} · מתרענן אוטומטית כל דקה</div>
<div class="grid">{body}</div>
</body></html>'''


def serve(port, days):
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            try:
                if self.path.startswith("/json"):
                    payload = json.dumps(scan(days), ensure_ascii=False).encode()
                    ctype = "application/json"
                else:
                    payload = render_html(scan(days)).encode()
                    ctype = "text/html; charset=utf-8"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.end_headers()
                self.wfile.write(payload)
            except Exception as e:
                self.send_response(500); self.end_headers()
                self.wfile.write(f"error: {e}".encode())

    srv = HTTPServer(("127.0.0.1", port), H)
    print(f"📦 דשבורד חבילות רץ על http://localhost:{port}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
