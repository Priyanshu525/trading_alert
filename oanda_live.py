# oanda_live.py
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
            if not rows:
                time.sleep(POLL_INTERVAL)
                continue
            instruments = sorted({r[2] for r in rows if r[2]})
            prices = fetch_prices(instruments)
            for r in rows:
                aid, inst, oanda_inst, direction, target = r
                data = prices.get(oanda_inst)
                if not data:
                    continue
                last_val = data.get("mid") or data.get("bid") or data.get("ask")
                if last_val is None:
                    continue
                try:
                    last_val = float(last_val)
                    target_val = float(target)
                except Exception:
                    continue
                triggered = (
                    (direction == "above" and last_val >= target_val)
                    or (direction == "below" and last_val <= target_val)
                    or (direction == "touch" and abs(last_val - target_val) <= (0.1 if "XAU" in oanda_inst else 0.0001))
                )
                if triggered:
                    ts = int(time.time())
                    db_execute("UPDATE alerts SET status='triggered', triggered_ts=?, triggered_price=? WHERE id=?", (ts, last_val, aid))
                    note_row = db_execute("SELECT note FROM alerts WHERE id=?", (aid,), fetch=True)
                    note = (note_row[0][0].strip() if note_row and note_row[0] and note_row[0][0] else "") if note_row else ""
                    msg = f"🔔 ALERT\n{inst} {direction.upper()} {target_val}\nPrice: {last_val}\nTime: {datetime.utcfromtimestamp(ts).isoformat()}Z"
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

# Flask 3.x compatible — run worker on first request
@app.before_request
def _ensure_worker_running():
    if not _worker_started:
        print("before_request: starting background worker once")
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
INDEX_HTML = """ ... (same HTML as before, unchanged) ... """  # keep your template

# (keep all the route functions exactly as you had them)

# ======== RUN ========
if __name__ == "__main__":
    print("Starting local server. DB:", DB_FILE_ABS)
    start_worker_background()
    try:
        app.run(host="0.0.0.0", port=5000, debug=False)
    finally:
        _shutdown()
