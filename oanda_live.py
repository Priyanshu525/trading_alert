# alerts_app_env.py
# pip install flask requests python-dotenv

import os, time, threading, sqlite3
from datetime import datetime
from flask import Flask, render_template_string, request, redirect, url_for, jsonify
import requests
from dotenv import load_dotenv

# Load .env config
load_dotenv()

app = Flask(__name__)
app.secret_key = os.urandom(16)

# ========== CONFIG ==========
OANDA_TOKEN = os.getenv("OANDA_TOKEN")
OANDA_ENV = os.getenv("OANDA_ENV", "practice").lower()
OANDA_BASE = "https://api-fxtrade.oanda.com" if OANDA_ENV == "live" else "https://api-fxpractice.oanda.com"
HEADERS = {"Authorization": f"Bearer {OANDA_TOKEN}"} if OANDA_TOKEN else {}

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "2"))
DB_FILE = os.getenv("ALERTS_DB", "alerts.db")

SYMBOLS = [
    "XAUUSD","EURUSD","GBPUSD","USDCHF","USDCAD",
    "AUDUSD","AUDNZD","AUDCAD","AUDCHF","NZDUSD",
    "CADNZD","CADCHF","EURJPY","GBPJPY","CADJPY",
    "EURGBP","EURCAD","EURCHF","GBPCHF","GBPCAD"
]
INSTRUMENT_OVERRIDES = {"XAUUSD": "XAU_USD"}
# ============================

# ======== DATABASE ========
def init_db():
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS alerts (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      instrument TEXT,
      oanda_instrument TEXT,
      direction TEXT,
      target REAL,
      note TEXT,
      status TEXT,
      created_ts INTEGER,
      triggered_ts INTEGER,
      triggered_price REAL
    )""")
    con.commit()
    con.close()

def db_execute(q, p=(), fetch=False):
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute(q, p)
    rows = cur.fetchall() if fetch else None
    con.commit()
    con.close()
    return rows

# ======== HELPERS ========
def to_oanda_instrument(sym: str) -> str:
    s = sym.strip().upper()
    if s in INSTRUMENT_OVERRIDES:
        return INSTRUMENT_OVERRIDES[s]
    if "_" in s:
        return s
    return f"{s[:-3]}_{s[-3:]}"

def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram not configured:", text)
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=6)
        if r.status_code != 200:
            print("Telegram error:", r.text)
        return r.status_code == 200
    except Exception as e:
        print("Telegram exception:", e)
        return False

def get_account_id():
    if getattr(get_account_id, "cached", None):
        return get_account_id.cached
    if not OANDA_TOKEN:
        return None
    try:
        r = requests.get(f"{OANDA_BASE}/v3/accounts", headers=HEADERS, timeout=8)
        if r.status_code == 200:
            aid = r.json()["accounts"][0]["id"]
            get_account_id.cached = aid
            return aid
        else:
            print("Account fetch failed:", r.status_code, r.text)
    except Exception as e:
        print("OANDA account error:", e)
    return None

def fetch_prices(instruments):
    aid = get_account_id()
    if not aid:
        return {}
    url = f"{OANDA_BASE}/v3/accounts/{aid}/pricing"
    try:
        r = requests.get(url, headers=HEADERS, params={"instruments": ",".join(instruments)}, timeout=8)
        if r.status_code != 200:
            print("OANDA pricing error:", r.status_code)
            return {}
        j = r.json()
        out = {}
        for p in j.get("prices", []):
            bids = p.get("bids", []); asks = p.get("asks", [])
            bid = float(bids[0]["price"]) if bids else None
            ask = float(asks[0]["price"]) if asks else None
            mid = (bid + ask)/2 if bid and ask else (bid or ask)
            out[p["instrument"]] = {"mid": mid, "bid": bid, "ask": ask}
        return out
    except Exception as e:
        print("Price fetch error:", e)
        return {}

# ======== WORKER THREAD ========
stop_event = threading.Event()
def worker_loop():
    print(f"🟢 Alert worker started (poll every {POLL_INTERVAL}s)")
    while not stop_event.is_set():
        try:
            rows = db_execute("SELECT id,instrument,oanda_instrument,direction,target FROM alerts WHERE status='active'", fetch=True)
            if not rows:
                time.sleep(POLL_INTERVAL)
                continue
            instruments = sorted({r[2] for r in rows})
            prices = fetch_prices(instruments)
            for r in rows:
                aid, inst, oanda_inst, direction, target = r
                price_data = prices.get(oanda_inst)
                if not price_data or not price_data.get("mid"):
                    continue
                last = price_data["mid"]
                trigger = (
                    (direction == "above" and last >= target)
                    or (direction == "below" and last <= target)
                    or (direction == "touch" and abs(last - target) <= 0.0001)
                )
                if trigger:
                    ts = int(time.time())
                    db_execute("UPDATE alerts SET status='triggered', triggered_ts=?, triggered_price=? WHERE id=?",
                               (ts, last, aid))

                    # 🔥 Fetch note/comment and include it only if not empty
                    note_row = db_execute("SELECT note FROM alerts WHERE id=?", (aid,), fetch=True)
                    note = (note_row[0][0] if note_row else "").strip() if note_row else ""

                    # Build final message
                    msg = (
                        f"🔔 ALERT\n"
                        f"{inst} {direction.upper()} {target}\n"
                        f"Price: {last}\n"
                        f"Time: {datetime.utcfromtimestamp(ts).isoformat()}Z"
                    )
                    if note:
                        msg += f"\n📝 Note: {note}"

                    print(msg)
                    send_telegram(msg)

            time.sleep(POLL_INTERVAL)
        except Exception as e:
            print("Worker exception:", e)
            time.sleep(POLL_INTERVAL)

# ======== FLASK ROUTES ========
INDEX_HTML = """
<!doctype html>
<title>Price Alerts</title>
<style>
body{font-family:Arial;margin:18px} label{display:block;margin-top:8px}
table{border-collapse:collapse;width:100%;margin-top:8px}
th,td{border:1px solid #ddd;padding:8px;text-align:left}
.status-active{color:green;font-weight:bold}
.status-triggered{color:orange}
.status-cancelled{color:gray}
</style>
<h2>Price Alert Manager (.env)</h2>
<p>OANDA env: <b>{{env}}</b> — Base: <b>{{base}}</b></p>

<h3>Create Alert</h3>
<form method="post" action="/create">
  <label>Symbol:
    <select name="symbol">
      {% for s in symbols %}
        <option value="{{s}}">{{s}}</option>
      {% endfor %}
    </select>
  </label>
  <label>Direction:
    <select name="direction">
      <option value="above">Above</option>
      <option value="below">Below</option>
      <option value="touch">Touch</option>
    </select>
  </label>
  <label>Target price: <input name="target" required></label>
  <label>Note (optional): <input name="note" placeholder="Comment / reason"></label>
  <button type="submit">Add Alert</button>
</form>

<h3>Active Alerts</h3>
<table>
<tr><th>ID</th><th>Instrument</th><th>Dir</th><th>Target</th><th>Note</th><th>Created</th><th>Cancel</th></tr>
{% for a in active %}
<tr>
<td>{{a.id}}</td><td>{{a.instrument}}</td><td>{{a.direction}}</td><td>{{'%.5f'|format(a.target)}}</td>
<td>{{a.note}}</td><td>{{a.created}}</td>
<td><form method="post" action="/cancel"><input type="hidden" name="id" value="{{a.id}}"><button>Cancel</button></form></td>
</tr>{% endfor %}
</table>

<h3>History</h3>
<table>
<tr><th>ID</th><th>Instrument</th><th>Dir</th><th>Target</th><th>Status</th><th>Triggered</th><th>Price</th></tr>
{% for h in history %}
<tr><td>{{h.id}}</td><td>{{h.instrument}}</td><td>{{h.direction}}</td><td>{{'%.5f'|format(h.target)}}</td>
<td class="status-{{h.status}}">{{h.status}}</td><td>{{h.triggered}}</td>
<td>{{'%.5f'|format(h.price) if h.price else ''}}</td></tr>
{% endfor %}
</table>
<p><a href="/send_test">Send Test Telegram</a> | <a href="/debug_account">Debug Account</a></p>
"""

@app.route("/")
def index():
    active = [
        dict(id=r[0], instrument=r[1], direction=r[3], target=r[4],
             note=r[5], created=datetime.utcfromtimestamp(r[6]).isoformat()+"Z")
        for r in db_execute("SELECT id,instrument,oanda_instrument,direction,target,note,created_ts FROM alerts WHERE status='active' ORDER BY id DESC", fetch=True)
    ]
    hist = [
        dict(id=r[0], instrument=r[1], direction=r[2], target=r[3],
             status=r[4], triggered=datetime.utcfromtimestamp(r[6]).isoformat()+"Z" if r[6] else '',
             price=r[7])
        for r in db_execute("SELECT id,instrument,direction,target,status,created_ts,triggered_ts,triggered_price FROM alerts WHERE status!='active' ORDER BY id DESC LIMIT 200", fetch=True)
    ]
    return render_template_string(INDEX_HTML, symbols=SYMBOLS, active=active, history=hist, env=OANDA_ENV, base=OANDA_BASE)

@app.route("/create", methods=["POST"])
def create_alert():
    symbol = request.form["symbol"].strip().upper()
    direction = request.form["direction"]
    target = float(request.form["target"])
    note = request.form.get("note") or ""
    ts = int(time.time())
    db_execute("INSERT INTO alerts (instrument,oanda_instrument,direction,target,note,status,created_ts) VALUES (?,?,?,?,?,?,?)",
               (symbol, to_oanda_instrument(symbol), direction, target, note, "active", ts))
    return redirect(url_for("index"))

@app.route("/cancel", methods=["POST"])
def cancel():
    db_execute("UPDATE alerts SET status='cancelled' WHERE id=?", (int(request.form["id"]),))
    return redirect(url_for("index"))

@app.route("/debug_account")
def debug_account():
    try:
        r = requests.get(f"{OANDA_BASE}/v3/accounts", headers=HEADERS, timeout=8)
        return jsonify({"ok": r.status_code==200, "status": r.status_code, "text": r.text})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/send_test")
def send_test():
    ok = send_telegram("✅ Test message from your Flask alert app.")
    return jsonify({"sent": ok})

# ======== RUN ========
if __name__ == "__main__":
    init_db()
    threading.Thread(target=worker_loop, daemon=True).start()
    try:
        app.run(host="0.0.0.0", port=5000, debug=False)
    finally:
        stop_event.set()
