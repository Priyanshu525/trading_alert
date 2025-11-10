# alerts_app_env.py
# pip install flask requests python-dotenv

import os, time, threading, sqlite3, atexit
from datetime import datetime
from threading import Thread, Lock
from flask import Flask, render_template_string, request, redirect, url_for, jsonify
import requests
from dotenv import load_dotenv

# Load .env if present
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
DB_FILE_ABS = os.path.abspath(DB_FILE)

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
    os.makedirs(os.path.dirname(DB_FILE_ABS) or ".", exist_ok=True)
    con = sqlite3.connect(DB_FILE_ABS, check_same_thread=False)
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
    con = sqlite3.connect(DB_FILE_ABS, timeout=10, check_same_thread=False)
    cur = con.cursor()
    cur.execute(q, p)
    rows = cur.fetchall() if fetch else None
    con.commit()
    con.close()
    return rows

# initialize DB right away so UI and worker see same file
init_db()
print("DB initialized at", DB_FILE_ABS)

# ======== HELPERS ========
def to_oanda_instrument(sym: str) -> str:
    s = sym.strip().upper()
    if s in INSTRUMENT_OVERRIDES:
        return INSTRUMENT_OVERRIDES[s]
    if "_" in s:
        return s
    if len(s) > 3:
        return f"{s[:-3]}_{s[-3:]}"
    return s

def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured; message not sent:", text)
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=8)
        if r.status_code != 200:
            print("Telegram error:", r.status_code, r.text)
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
            j = r.json()
            accounts = j.get("accounts") or []
            if not accounts:
                print("No accounts returned for token.")
                return None
            aid = accounts[0].get("id")
            get_account_id.cached = aid
            return aid
        else:
            print("get_account_id error:", r.status_code, r.text)
    except Exception as e:
        print("get_account_id exception:", e)
    return None

def fetch_prices(instruments):
    """Batch pricing call; returns dict by instrument name"""
    out = {}
    if not instruments:
        return out
    aid = get_account_id()
    if not aid:
        return out
    try:
        url = f"{OANDA_BASE}/v3/accounts/{aid}/pricing"
        params = {"instruments": ",".join(instruments)}
        r = requests.get(url, headers=HEADERS, params=params, timeout=8)
        if r.status_code != 200:
            print("OANDA pricing error:", r.status_code, r.text)
            return out
        j = r.json()
        for p in j.get("prices", []):
            instr = p.get("instrument")
            bids = p.get("bids", []); asks = p.get("asks", [])
            bid = float(bids[0]["price"]) if bids else None
            ask = float(asks[0]["price"]) if asks else None
            mid = (bid + ask)/2.0 if (bid is not None and ask is not None) else (bid or ask)
            out[instr] = {"bid": bid, "ask": ask, "mid": mid, "time": p.get("time")}
        return out
    except Exception as e:
        print("fetch_prices exception:", e)
        return out

# ======== WORKER THREAD ========
stop_event = threading.Event()
_worker_started = False
_worker_lock = Lock()
_worker_thread = None

def worker_loop():
    print(f"🟢 Alert worker started; polling every {POLL_INTERVAL}s")
    while not stop_event.is_set():
        try:
            rows = db_execute("SELECT id,instrument,oanda_instrument,direction,target FROM alerts WHERE status='active'", fetch=True) or []
            print("Worker: active alerts =", len(rows))
            if not rows:
                time.sleep(POLL_INTERVAL)
                continue
            instruments = sorted({r[2] for r in rows if r[2]})
            print("Worker: polling instruments:", instruments)
            prices = fetch_prices(instruments)
            print("Worker: prices fetched:", prices)
            for r in rows:
                aid, inst, oanda_inst, direction, target = r
                data = prices.get(oanda_inst)
                print(f"Worker: check id={aid} inst={inst} oanda_inst={oanda_inst} dir={direction} target={target} data={data}")
                if not data:
                    continue
                last = data.get("mid") or data.get("bid") or data.get("ask")
                if last is None:
                    continue
                # log numeric values
                try:
                    last_val = float(last)
                    target_val = float(target)
                except Exception:
                    print("Worker: could not parse last/target as float", last, target)
                    continue
                triggered = False
                if direction == "above" and last_val >= target_val:
                    triggered = True
                elif direction == "below" and last_val <= target_val:
                    triggered = True
                elif direction == "touch":
                    # touch tolerance: use small absolute epsilon (can be adjusted per instrument)
                    eps = 0.0001
                    # if big instruments like gold use larger epsilon approx 0.01 for XAUUSD
                    if oanda_inst.startswith("XAU"):
                        eps = 0.1
                    if abs(last_val - target_val) <= eps:
                        triggered = True
                print(f"Worker: last={last_val} target={target_val} triggered={triggered}")
                if triggered:
                    ts = int(time.time())
                    db_execute("UPDATE alerts SET status='triggered', triggered_ts=?, triggered_price=? WHERE id=?", (ts, last_val, aid))
                    note_row = db_execute("SELECT note FROM alerts WHERE id=?", (aid,), fetch=True)
                    note = (note_row[0][0].strip() if note_row and note_row[0] and note_row[0][0] else "") if note_row else ""
                    msg = (
                        f"🔔 ALERT\n"
                        f"{inst} {direction.upper()} {target_val}\n"
                        f"Price: {last_val}\n"
                        f"Time: {datetime.utcfromtimestamp(ts).isoformat()}Z"
                    )
                    if note:
                        msg += f"\n📝 Note: {note}"
                    print("Worker: Trigger ->", msg)
                    send_telegram(msg)
            time.sleep(POLL_INTERVAL)
        except Exception as e:
            print("Worker exception:", e)
            time.sleep(POLL_INTERVAL)

def start_worker_background():
    global _worker_thread, _worker_started
    with _worker_lock:
        if _worker_started:
            print("start_worker_background: already started")
            return
        _worker_started = True
        _worker_thread = Thread(target=worker_loop, daemon=True)
        _worker_thread.start()
        print("Background worker thread started.")

@app.before_first_request
def _before_first_request():
    print("before_first_request called -> starting worker (if not started)")
    start_worker_background()

def _shutdown():
    print("Shutdown: signaling worker to stop")
    stop_event.set()
    try:
        if _worker_thread and _worker_thread.is_alive():
            _worker_thread.join(timeout=2)
    except Exception:
        pass

atexit.register(_shutdown)

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
<h2>Price Alert Manager</h2>
<p>OANDA env: <b>{{env}}</b> — Base: <b>{{base}}</b></p>
<p>DB: <b>{{dbpath}}</b></p>
<p style="color:darkred">Use practice token while testing. Do not share tokens.</p>

<h3>Create Alert</h3>
<form method="post" action="/create">
  <label>Symbol:
    <select name="symbol">
      {% for s in symbols %}<option value="{{s}}">{{s}}</option>{% endfor %}
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
  <label>Note (optional): <input name="note" placeholder="Comment"></label>
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
    rows_active = db_execute("SELECT id,instrument,oanda_instrument,direction,target,note,created_ts FROM alerts WHERE status='active' ORDER BY id DESC", fetch=True) or []
    active = [{"id":r[0],"instrument":r[1],"direction":r[3],"target":r[4],"note":r[5],"created":datetime.utcfromtimestamp(r[6]).isoformat()+"Z"} for r in rows_active]
    rows_hist = db_execute("SELECT id,instrument,direction,target,status,created_ts,triggered_ts,triggered_price FROM alerts WHERE status!='active' ORDER BY id DESC LIMIT 200", fetch=True) or []
    history = [{"id":r[0],"instrument":r[1],"direction":r[2],"target":r[3],"status":r[4],"created":datetime.utcfromtimestamp(r[5]).isoformat()+"Z","triggered":(datetime.utcfromtimestamp(r[6]).isoformat()+"Z" if r[6] else ""),"price":r[7]} for r in rows_hist]
    return render_template_string(INDEX_HTML, symbols=SYMBOLS, active=active, history=history, env=OANDA_ENV, base=OANDA_BASE, dbpath=DB_FILE_ABS)

@app.route("/create", methods=["POST"])
def create_alert():
    instrument = request.form["symbol"].strip().upper()
    direction = request.form["direction"]
    try:
        target = float(request.form["target"])
    except Exception:
        return "Invalid target", 400
    note = request.form.get("note") or ""
    oanda_instr = to_oanda_instrument(instrument)
    ts = int(time.time())
    db_execute("INSERT INTO alerts (instrument,oanda_instrument,direction,target,note,status,created_ts) VALUES (?,?,?,?,?,?,?)",
               (instrument, oanda_instr, direction, target, note, "active", ts))
    print(f"Web: created alert {instrument} ({oanda_instr}) dir={direction} target={target} note={note}")
    return redirect(url_for("index"))

@app.route("/cancel", methods=["POST"])
def cancel():
    aid = int(request.form["id"])
    db_execute("UPDATE alerts SET status='cancelled' WHERE id=?", (aid,))
    print("Web: cancelled alert", aid)
    return redirect(url_for("index"))

@app.route("/debug_account")
def debug_account():
    if not OANDA_TOKEN:
        return jsonify({"ok": False, "error": "OANDA_TOKEN not set"})
    try:
        r = requests.get(f"{OANDA_BASE}/v3/accounts", headers=HEADERS, timeout=8)
        return jsonify({"ok": r.status_code==200, "status": r.status_code, "text": r.text})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/send_test")
def send_test():
    ok = send_telegram("✅ Test message from your Flask alert app.")
    return jsonify({"sent": ok})

@app.route("/health")
def health():
    return "OK", 200

@app.route("/worker_status")
def worker_status():
    return jsonify({"worker_started": bool(_worker_started)})

@app.route("/dump_prices")
def dump_prices():
    rows = db_execute("SELECT DISTINCT oanda_instrument FROM alerts WHERE status='active'", fetch=True) or []
    instruments = [r[0] for r in rows]
    prices = fetch_prices(instruments)
    return jsonify({"instruments": instruments, "prices": prices})

@app.route("/dump_alerts")
def dump_alerts():
    rows = db_execute("SELECT id,instrument,oanda_instrument,direction,target,status,created_ts FROM alerts ORDER BY id DESC", fetch=True) or []
    out = [{"id":r[0],"instrument":r[1],"oanda":r[2],"direction":r[3],"target":r[4],"status":r[5],"created":r[6]} for r in rows]
    return jsonify(out)

@app.route("/force_trigger/<int:aid>")
def force_trigger(aid):
    ts = int(time.time())
    db_execute("UPDATE alerts SET status='triggered', triggered_ts=?, triggered_price=? WHERE id=?", (ts, 1.234567, aid))
    return jsonify({"ok": True, "id": aid})

# ======== RUN ========
if __name__ == "__main__":
    print("Starting local server. DB:", DB_FILE_ABS)
    start_worker_background()
    try:
        app.run(host="0.0.0.0", port=5000, debug=False)
    finally:
        _shutdown()
