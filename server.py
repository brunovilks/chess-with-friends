"""Gambīts — šaha serveris spēlēm ar draugu pa saiti.

Tikai Python standarta bibliotēka, nekas nav jāinstalē.
Palaišana:  python server.py   (ports no vides mainīgā PORT, citādi 8000)
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.request
import chess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

# Google pieteikšanās (nav obligāta): Google Cloud OAuth klienta ID
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
# slepena atslēga, no kuras atvasina Google spēlētāju paroles; tai jāpaliek nemainīgai starp restartiem
SERVER_KEY = (os.environ.get("SERVER_KEY") or secrets.token_hex(32)).encode()

ROOT = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(ROOT, "public", "index.html")
if not os.path.exists(INDEX):
    INDEX = os.path.join(ROOT, "index.html")
GAME_TTL = 24 * 3600          # neaktīvas spēles dzēš pēc 24 h
POLL_WAIT = 20                # long-poll gaidīšana sekundēs
ONLINE_WINDOW = 25            # cik sekundes skaitās "tiešsaistē"
ABANDON = 45                  # pēc cik sekundēm prombūtnes var pieprasīt uzvaru

PRIVACY_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Gambit — Privacy & Terms</title>
<style>body{font:16px/1.6 system-ui,sans-serif;max-width:680px;margin:0 auto;padding:24px 16px;background:#211f1c;color:#ece8e1}
a{color:#93c95a}h1{font-size:26px}h2{font-size:19px;margin-top:28px}</style></head><body>
<p><a href="/">← Back to Gambit</a></p>
<h1>Privacy Policy & Terms of Use</h1>
<h2>What we store</h2>
<p>Gambit is a free hobby site for playing chess with friends. If you sign in with Google, we receive your Google
account ID, name, email and profile picture from Google. We use only your name and picture to show you to other
players, and a random player ID derived from your Google account to remember your friends list.</p>
<p>Games, chat messages and friend lists are kept in the server's memory only. They are not sold, shared or used
for advertising, and they are erased whenever the server restarts. Your browser also keeps your name, friends and
settings in local storage on your own device.</p>
<h2>Deleting your data</h2>
<p>Sign out and clear this site's data in your browser. Server-side data disappears on the next restart.</p>
<h2>Terms</h2>
<p>Gambit is provided as is, free of charge and without any guarantee. Be respectful in chat. Open-source credits:
piece sets from lichess (CC BY-NC-SA 4.0 / GPLv2+ / Apache 2.0, see the project README), sounds from Freesound
(CC0), rules chess.js and python-chess.</p>
<h2>Ratings</h2>
<p>Rated games store your player ID, rating and the moves of the game so you can see your history.</p>
</body></html>"""

games = {}
games_lock = threading.Lock()


def now_ms():
    return int(time.time() * 1000)


def new_id(n=6):
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(n))


class Game:
    def __init__(self, minutes, inc, creator_color):
        self.id = new_id()
        self.cond = threading.Condition()
        self.minutes = minutes
        self.inc = inc
        self.creator_color = creator_color   # 'w', 'b' vai 'r' (nejauši)
        self.seats = {"w": None, "b": None}  # token
        self.names = {"w": "", "b": ""}
        self.seen = {"w": 0, "b": 0}
        self.chat = []
        self.version = 0
        self.touched = time.time()
        self.reset()

    def reset(self):
        self.gen = getattr(self, "gen", 0) + 1
        self.board = chess.Board()   # serveris ir vienīgais noteikumu tiesnesis
        self.moves = []
        self.clock = {"w": self.minutes * 60000, "b": self.minutes * 60000}
        self.clock_history = []      # pulksteņi pirms katra gājiena (gājiena atsaukšanai)
        self.last_move_at = None
        self.result = None   # '1-0', '0-1', '1/2-1/2'
        self.reason = None
        self.draw_offer = None   # krāsa, kas piedāvā
        self.takeback = None     # krāsa, kas lūdz atsaukt gājienu
        self.rating_change = None
        self.rematch = set()

    def pid_of(self, c):
        tok = self.seats.get(c)
        return next((q for q, pl in list(players.items()) if tok and pl["secret"] == tok), None)

    def judge(self):
        """Pēc gājiena: mats, pats un automātiskie neizšķirti (kā chess.com)."""
        b = self.board
        if b.is_checkmate():
            self.finish("1-0" if b.turn == chess.BLACK else "0-1", "mats")
        elif b.is_stalemate():
            self.finish("1/2-1/2", "pats")
        elif b.is_insufficient_material():
            self.finish("1/2-1/2", "materiāls")
        elif b.can_claim_threefold_repetition():
            self.finish("1/2-1/2", "atkārtošanās")
        elif b.can_claim_fifty_moves():
            self.finish("1/2-1/2", "50 gājieni")
        else:
            return False
        return True

    # --- palīgi ---
    def color_of(self, token):
        for c, t in self.seats.items():
            if t and t == token:
                return c
        return None

    def turn(self):
        return "w" if len(self.moves) % 2 == 0 else "b"

    def timed(self):
        return self.minutes > 0

    def clocks_running(self):
        return self.timed() and self.result is None and len(self.moves) >= 2

    def live_clock(self):
        c = dict(self.clock)
        if self.clocks_running() and self.last_move_at:
            t = self.turn()
            c[t] = max(0, c[t] - (now_ms() - self.last_move_at))
        return c

    def check_flag(self):
        if self.clocks_running():
            c = self.live_clock()
            t = self.turn()
            if c[t] <= 0:
                self.clock = c
                # FIDE: ja pretiniekam nav materiāla, ar ko matot, laika beigas ir neizšķirts
                other = chess.BLACK if t == "w" else chess.WHITE
                if self.board.has_insufficient_material(other):
                    self.finish("1/2-1/2", "laiks-materiāls")
                else:
                    self.finish("0-1" if t == "w" else "1-0", "laiks")
                return True
        return False

    def finish(self, result, reason):
        self.result = result
        self.reason = reason
        self.draw_offer = None
        self.takeback = None
        try:
            self.rating_change = rate_game(self)
        except Exception as e:   # reitinga kļūda nedrīkst sabojāt pašu spēli
            print("rating error:", e, flush=True)
            self.rating_change = None
        self.bump()
        with lobby:
            bump_lobby()

    def bump(self):
        self.version += 1
        self.touched = time.time()
        self.cond.notify_all()

    def state(self, token=None):
        t = time.time()
        cat = category(self.minutes, self.inc)
        pids = {c: self.pid_of(c) for c in ("w", "b")}
        rated = bool(cat and all(p and p.startswith("g") for p in pids.values()))
        return {
            "cat": cat,
            "rated": rated,
            "ratingChange": self.rating_change,
            "id": self.id,
            "v": self.version,
            "gen": self.gen,
            "moves": self.moves,
            "minutes": self.minutes,
            "inc": self.inc,
            "clock": self.live_clock(),
            "running": self.clocks_running(),
            "result": self.result,
            "reason": self.reason,
            "drawOffer": self.draw_offer,
            "takeback": self.takeback,
            "rematch": sorted(self.rematch),
            "players": {
                c: {
                    "pid": pids[c],
                    "taken": self.seats[c] is not None,
                    "name": self.names[c],
                    "online": t - self.seen[c] < ONLINE_WINDOW,
                    "away": round(t - self.seen[c]) if self.seen[c] else 0,
                    "rating": get_rating(pids[c], cat)["rating"] if cat and pids[c] and pids[c].startswith("g") else None,
                    "picture": load_account(pids[c])["picture"] if pids[c] and pids[c].startswith("g") else "",
                }
                for c in ("w", "b")
            },
            "chat": self.chat[-60:],
            "you": self.color_of(token) if token else None,
        }


def get_game(gid):
    with games_lock:
        return games.get(gid)


def cleanup_loop():
    while True:
        time.sleep(600)
        cutoff = time.time() - GAME_TTL
        with games_lock:
            for gid in [g for g, v in games.items() if v.touched < cutoff]:
                del games[gid]


# --- spēlētāji, tiešsaiste un izaicinājumi ---
players = {}        # pid -> {"secret", "name", "seen", "game"}
challenges = {}     # cid -> {"from", "to", "minutes", "inc", "color", "status", "game", "t"}
lobby = threading.Condition()
lobby_v = [0]
PID_OK = set("abcdefghjkmnpqrstuvwxyz23456789")


accounts = {}       # kešatmiņa: Google spēlētāja pid -> {"friends": {pid: vārds}, "picture": url}
ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"

# --- pastāvīgā datubāze: konti, draugi, reitingi, spēļu vēsture ---
DATA_DIR = os.environ.get("DATA_DIR") or os.path.join(ROOT, "data")
os.makedirs(DATA_DIR, exist_ok=True)
db = sqlite3.connect(os.path.join(DATA_DIR, "gambit.db"), check_same_thread=False)
db_lock = threading.Lock()
with db_lock:
    db.executescript("""
    CREATE TABLE IF NOT EXISTS users(pid TEXT PRIMARY KEY, name TEXT, picture TEXT,
        friends TEXT DEFAULT '{}', created REAL);
    CREATE TABLE IF NOT EXISTS ratings(pid TEXT, cat TEXT, rating REAL, games INT, w INT, d INT, l INT,
        best REAL, PRIMARY KEY(pid, cat));
    CREATE TABLE IF NOT EXISTS games(id TEXT, white TEXT, black TEXT, cat TEXT, minutes INT, inc INT,
        result TEXT, reason TEXT, moves TEXT, wdelta INT, bdelta INT, wrating INT, brating INT, ended REAL);
    CREATE INDEX IF NOT EXISTS games_w ON games(white);
    CREATE INDEX IF NOT EXISTS games_b ON games(black);
    """)
    db.commit()

START_RATING = 1200
CATS = ("bullet", "blitz", "rapid", "classical")


def db_all(sql, *args):
    with db_lock:
        return db.execute(sql, args).fetchall()


def db_run(sql, *args):
    with db_lock:
        db.execute(sql, args)
        db.commit()


def category(minutes, inc):
    # tāpat kā lielajās šaha vietnēs: paredzamais spēles ilgums = laiks + 40 × papildlaiks
    if not minutes:
        return None
    est = minutes * 60 + 40 * inc
    if est < 180:
        return "bullet"
    if est < 600:
        return "blitz"
    if est < 1800:
        return "rapid"
    return "classical"


def get_rating(pid, cat):
    rows = db_all("SELECT rating, games, w, d, l, best FROM ratings WHERE pid=? AND cat=?", pid, cat)
    if rows:
        r = rows[0]
        return {"rating": round(r[0]), "games": r[1], "w": r[2], "d": r[3], "l": r[4], "best": round(r[5])}
    return {"rating": START_RATING, "games": 0, "w": 0, "d": 0, "l": 0, "best": START_RATING}


def save_user(pid, name=None, picture=None):
    if not pid.startswith("g"):
        return
    db_run("INSERT INTO users(pid, name, picture, created) VALUES(?,?,?,?) "
           "ON CONFLICT(pid) DO UPDATE SET name=COALESCE(excluded.name, users.name), "
           "picture=COALESCE(excluded.picture, users.picture)", pid, name, picture, time.time())


def load_account(pid):
    if pid not in accounts:
        rows = db_all("SELECT friends, picture FROM users WHERE pid=?", pid)
        friends = json.loads(rows[0][0] or "{}") if rows else {}
        accounts[pid] = {"friends": friends, "picture": (rows[0][1] or "") if rows else ""}
    return accounts[pid]


def rate_game(game):
    """Elo reitinga aprēķins pēc reitingotas spēles. Atgriež {'w': ±n, 'b': ±n} vai None."""
    cat = category(game.minutes, game.inc)
    if not cat or game.result not in ("1-0", "0-1", "1/2-1/2") or len(game.moves) < 2:
        return None
    wp, bp = game.pid_of("w"), game.pid_of("b")
    if not (wp and bp and wp.startswith("g") and bp.startswith("g") and wp != bp):
        return None
    rw, rb = get_rating(wp, cat), get_rating(bp, cat)
    sw = {"1-0": 1.0, "0-1": 0.0, "1/2-1/2": 0.5}[game.result]
    ew = 1 / (1 + 10 ** ((rb["rating"] - rw["rating"]) / 400))
    k = lambda r: 40 if r["games"] < 30 else 20   # jauniem spēlētājiem reitings mainās ātrāk
    dw = round(k(rw) * (sw - ew))
    db_ = round(k(rb) * ((1 - sw) - (1 - ew)))
    for pid, r, d, s in ((wp, rw, dw, sw), (bp, rb, db_, 1 - sw)):
        new = r["rating"] + d
        db_run("INSERT INTO ratings(pid, cat, rating, games, w, d, l, best) VALUES(?,?,?,?,?,?,?,?) "
               "ON CONFLICT(pid, cat) DO UPDATE SET rating=excluded.rating, games=excluded.games, "
               "w=excluded.w, d=excluded.d, l=excluded.l, best=excluded.best",
               pid, cat, new, r["games"] + 1, r["w"] + (s == 1), r["d"] + (s == 0.5), r["l"] + (s == 0),
               max(r["best"], new))
    db_run("INSERT INTO games VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", f"{game.id}-{game.gen}", wp, bp, cat,
           game.minutes, game.inc, game.result, game.reason, " ".join(game.moves), dw, db_,
           rw["rating"] + dw, rb["rating"] + db_, time.time())
    return {"w": dw, "b": db_}


def google_pid(sub):
    # tas pats Google konts vienmēr dod to pašu spēlētāju; Google pid sākas ar "g"
    h = hashlib.sha256(SERVER_KEY + b"|" + sub.encode()).digest()
    return "g" + "".join(ALPHABET[b % len(ALPHABET)] for b in h[:7])


def google_secret(pid):
    return hmac.new(SERVER_KEY, ("pid:" + pid).encode(), hashlib.sha256).hexdigest()[:24]


def verify_google(credential):
    if not GOOGLE_CLIENT_ID or not credential:
        return None
    try:
        url = "https://oauth2.googleapis.com/tokeninfo?id_token=" + quote(credential)
        with urllib.request.urlopen(url, timeout=8) as r:
            info = json.loads(r.read())
    except Exception:
        return None
    if info.get("aud") != GOOGLE_CLIENT_ID:
        return None
    if info.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
        return None
    if int(info.get("exp", 0)) < time.time():
        return None
    return info


def login_required():
    # ja Google ir iestatīts, spēlēt drīkst tikai ar Google kontu
    return bool(GOOGLE_CLIENT_ID)


def token_ok(token):
    if not login_required():
        return True
    return any(q.startswith("g") and p["secret"] == token for q, p in list(players.items()))


def merge_friends(pid, add, remove):
    acc = load_account(pid)
    before = dict(acc["friends"])
    if isinstance(add, dict):
        for q, n in list(add.items())[:500]:
            q = str(q)
            if q != pid and len(q) == 8 and set(q) <= PID_OK:
                acc["friends"][q] = str(n)[:20] or acc["friends"].get(q, "Friend")
    if isinstance(remove, list):
        for q in remove[:500]:
            acc["friends"].pop(str(q), None)
    if acc["friends"] != before:
        save_user(pid)
        db_run("UPDATE users SET friends=? WHERE pid=?", json.dumps(acc["friends"]), pid)
    return acc["friends"]


def bump_lobby():
    lobby_v[0] += 1
    lobby.notify_all()


def auth(data):
    p = players.get(str(data.get("pid", "")))
    if p and p["secret"] == str(data.get("secret", "")):
        return p
    return None


def player_status(pid):
    p = players[pid]
    g = get_game(p["game"]) if p["game"] else None
    return "playing" if g and g.result is None and all(g.seats.values()) else "free"


def lobby_state(pid):
    t = time.time()
    online = [
        {"pid": q, "name": p["name"] or "Player", "status": player_status(q)}
        for q, p in players.items() if t - p["seen"] < ONLINE_WINDOW
    ]
    online.sort(key=lambda x: x["name"].lower())
    mine = [dict(c, cid=cid, fromName=players.get(c["from"], {}).get("name", ""),
                 toName=players.get(c["to"], {}).get("name", ""))
            for cid, c in challenges.items() if pid in (c["from"], c["to"])]
    return {"v": lobby_v[0], "online": online, "challenges": mine}


def lobby_cleanup():
    t = time.time()
    for cid in [c for c, v in challenges.items()
                if v["t"] < t - (7 * 86400 if v.get("friend") and v["status"] == "pending" else 120)]:
        del challenges[cid]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    # --- atbildes ---
    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_index(self):
        with open(INDEX, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, paths, ctype):
        for path in paths:
            if os.path.isfile(path):
                with open(path, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "public, max-age=604800")
                self.end_headers()
                self.wfile.write(body)
                return
        self.send_json({"error": "Not found"}, 404)

    def send_sound(self, name):
        # tikai skaņu faili ar drošu nosaukumu, piem. snd_start.ogg vai wood_move.wav
        if not re.fullmatch(r"(snd_[a-z]+\.(ogg|mp3))|((wood|crisp)_[a-z]+\.wav)", name):
            return self.send_json({"error": "Not found"}, 404)
        ctype = {"ogg": "audio/ogg", "mp3": "audio/mpeg", "wav": "audio/wav"}[name.rsplit(".", 1)[1]]
        self.send_file([os.path.join(ROOT, "sounds", name), os.path.join(ROOT, name)], ctype)

    def send_piece(self, rest):
        # figūru komplekti: /p/<komplekts>/<wK>.svg; GitHub repozitorijā tie glabājas kā pc_<komplekts>_<wK>.svg
        m = re.fullmatch(r"([a-z]+)/([wb][KQRBNP])\.svg", rest)
        if not m:
            return self.send_json({"error": "Not found"}, 404)
        s, p = m.groups()
        self.send_file([os.path.join(ROOT, "pieces", s, p + ".svg"), os.path.join(ROOT, f"pc_{s}_{p}.svg")],
                       "image/svg+xml")

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 20000:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}

    # --- GET ---
    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/local") or url.path.startswith("/g/") or url.path.startswith("/f/"):
            return self.send_index()
        if url.path in ("/privacy", "/terms"):
            body = PRIVACY_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if url.path.startswith("/s/"):
            return self.send_sound(url.path[3:])
        if url.path == "/health":
            return self.send_json({"ok": True, "games": len(games)})
        if url.path == "/api/config":
            return self.send_json({"googleClientId": GOOGLE_CLIENT_ID})
        if url.path.startswith("/p/"):
            return self.send_piece(url.path[3:])
        if url.path == "/api/profile":
            # spēlētāja profils: reitingi pa kategorijām un pēdējās spēles
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            if not token_ok(q.get("token", "")):
                return self.send_json({"error": "Sign in with Google", "login": True}, 401)
            pid = q.get("pid", "")
            rows = db_all("SELECT name, picture FROM users WHERE pid=?", pid)
            if not rows:
                return self.send_json({"error": "Player not found"}, 404)
            recent = []
            for g in db_all("SELECT white, black, cat, result, reason, wdelta, bdelta, wrating, brating, ended "
                            "FROM games WHERE white=? OR black=? ORDER BY ended DESC LIMIT 20", pid, pid):
                me_w = g[0] == pid
                opp = g[1] if me_w else g[0]
                on = db_all("SELECT name FROM users WHERE pid=?", opp)
                res = "draw" if g[3] == "1/2-1/2" else ("win" if (g[3] == "1-0") == me_w else "loss")
                recent.append({"opp": opp, "oppName": on[0][0] if on else "Player", "color": "w" if me_w else "b",
                               "cat": g[2], "res": res, "reason": g[4], "delta": g[5] if me_w else g[6],
                               "rating": g[7] if me_w else g[8], "ended": g[9]})
            return self.send_json({"pid": pid, "name": rows[0][0], "picture": rows[0][1],
                                   "ratings": {c: get_rating(pid, c) for c in CATS}, "recent": recent})
        if url.path == "/api/lobby":
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            since = int(q.get("since", "-1") or -1)
            deadline = time.time() + POLL_WAIT
            with lobby:
                p = auth(q)
                if not p:
                    return self.send_json({"error": "Unknown player"}, 401)
                was_online = time.time() - p["seen"] < ONLINE_WINDOW
                p["seen"] = time.time()
                if not was_online:
                    bump_lobby()
                while lobby_v[0] <= since:
                    left = deadline - time.time()
                    if left <= 0:
                        break
                    lobby.wait(left)
                p["seen"] = time.time()
                lobby_cleanup()
                return self.send_json(lobby_state(q["pid"]))
        if url.path == "/api/state":
            q = parse_qs(url.query)
            game = get_game(q.get("id", [""])[0])
            if not game:
                return self.send_json({"error": "Game not found"}, 404)
            token = q.get("token", [""])[0]
            if not token_ok(token):
                return self.send_json({"error": "Sign in with Google", "login": True}, 401)
            since = int(q.get("since", ["-1"])[0] or -1)
            deadline = time.time() + POLL_WAIT
            with game.cond:
                c = game.color_of(token)
                if c:
                    game.seen[c] = time.time()
                while game.version <= since and not game.check_flag():
                    left = deadline - time.time()
                    if left <= 0:
                        break
                    # pamostas arī tad, kad varētu beigties laiks
                    wait = min(left, 1.0) if game.clocks_running() else left
                    game.cond.wait(wait)
                if c:
                    game.seen[c] = time.time()
                return self.send_json(game.state(token))
        self.send_json({"error": "Not found"}, 404)

    # --- POST ---
    def do_POST(self):
        path = urlparse(self.path).path
        data = self.read_json()

        if path == "/api/new":
            if not token_ok(str(data.get("secret", ""))):
                return self.send_json({"error": "Sign in with Google", "login": True}, 401)
            minutes = int(data.get("minutes", 10))
            inc = int(data.get("inc", 0))
            color = data.get("color", "r")
            if minutes < 0 or minutes > 180 or inc < 0 or inc > 60 or color not in ("w", "b", "r"):
                return self.send_json({"error": "Invalid settings"}, 400)
            game = Game(minutes, inc, color)
            with games_lock:
                games[game.id] = game
            return self.send_json({"id": game.id})

        if path == "/api/ratings":
            # draugu līderu tabulai: vārds, attēls un reitingi vairākiem spēlētājiem
            if not token_ok(str(data.get("token", ""))):
                return self.send_json({"error": "Sign in with Google", "login": True}, 401)
            out = {}
            for pid in [str(p) for p in (data.get("pids") or [])][:200]:
                rows = db_all("SELECT name, picture FROM users WHERE pid=?", pid)
                if rows:
                    out[pid] = {"name": rows[0][0], "picture": rows[0][1],
                                "ratings": {c: get_rating(pid, c)["rating"] for c in CATS},
                                "games": {c: get_rating(pid, c)["games"] for c in CATS}}
            return self.send_json(out)

        if path.startswith("/api/hello") or path.startswith("/api/challenge") or path == "/api/google":
            if path == "/api/google":
                # tīkla pieprasījums Google — ārpus slēdzenes, lai citi negaida
                data["_info"] = verify_google(str(data.get("credential", "")))
            with lobby:
                return self.lobby_post(path, data)

        game = get_game(str(data.get("id", "")))
        if not game:
            return self.send_json({"error": "Game not found"}, 404)
        token = str(data.get("token", ""))
        if not token_ok(token):
            return self.send_json({"error": "Sign in with Google", "login": True}, 401)

        with game.cond:
            if path == "/api/join":
                return self.join(game, token, str(data.get("name", ""))[:20])

            me = game.color_of(token)
            if path == "/api/move":
                return self.move(game, me, data)
            if path == "/api/action":
                return self.action(game, me, data)
        self.send_json({"error": "Not found"}, 404)

    def lobby_post(self, path, data):
        if path == "/api/hello":
            # pēc servera restarta pārlūks atjauno to pašu spēlētāju
            pid, secret = str(data.get("pid", "")), str(data.get("secret", ""))
            name = str(data.get("name", "")).strip()[:20]
            p = players.get(pid)
            if p and p["secret"] != secret:
                p, pid = None, ""
            if not p:
                valid = len(pid) == 8 and set(pid) <= PID_OK and len(secret) == 24 and pid not in players
                # Google spēlētāju var atjaunot tikai ar pareizu atvasināto paroli
                if valid and pid.startswith("g") and not hmac.compare_digest(secret, google_secret(pid)):
                    valid = False
                if login_required() and not (valid and pid.startswith("g")):
                    return self.send_json({"error": "Sign in with Google", "login": True}, 401)
                if not valid:
                    pid = new_id(8)
                    while pid.startswith("g"):
                        pid = new_id(8)
                    secret = secrets.token_hex(12)
                p = players[pid] = {"secret": secret, "name": name, "seen": 0, "game": None}
            if name:
                p["name"] = name
            p["seen"] = time.time()
            out = {"pid": pid, "secret": p["secret"], "name": p["name"]}
            if pid.startswith("g"):
                save_user(pid, p["name"] or None)
                out["friends"] = merge_friends(pid, data.get("friends"), data.get("removed"))
                out["picture"] = accounts[pid].get("picture", "")
            bump_lobby()
            return self.send_json(out)

        if path == "/api/google":
            info = data.get("_info")
            if not info:
                return self.send_json({"error": "Google sign-in failed"}, 401)
            pid = google_pid(info["sub"])
            p = players.get(pid)
            if not p:
                p = players[pid] = {"secret": google_secret(pid), "name": "", "seen": 0, "game": None}
            name = str(data.get("name", "")).strip()[:20]
            if not p["name"]:
                p["name"] = name or str(info.get("given_name") or info.get("name") or "Player")[:20]
            friends = merge_friends(pid, data.get("friends"), data.get("removed"))
            accounts[pid]["picture"] = str(info.get("picture", ""))[:500]
            save_user(pid, p["name"], accounts[pid]["picture"])
            p["seen"] = time.time()
            bump_lobby()
            return self.send_json({"pid": pid, "secret": p["secret"], "name": p["name"],
                                   "picture": accounts[pid]["picture"], "friends": friends})

        p = auth(data)
        if not p:
            return self.send_json({"error": "Unknown player"}, 401)
        me = str(data.get("pid"))

        if path == "/api/challenge":
            to = str(data.get("to", ""))
            minutes, inc = int(data.get("minutes", 10)), int(data.get("inc", 0))
            color = data.get("color", "r")
            if to not in players or to == me:
                return self.send_json({"error": "Player not found"}, 404)
            if data.get("friend"):
                # paziņojums, lai otrs arī pievienotu mani draugiem
                cid = new_id(10)
                challenges[cid] = {"from": me, "to": to, "friend": True, "minutes": 0, "inc": 0, "color": "r",
                                   "status": "pending", "game": None, "t": time.time()}
                bump_lobby()
                return self.send_json({"cid": cid})
            if time.time() - players[to]["seen"] > ONLINE_WINDOW:
                return self.send_json({"error": "Your friend is not online right now"}, 409)
            if not (0 <= minutes <= 180 and 0 <= inc <= 60 and color in ("w", "b", "r")):
                return self.send_json({"error": "Invalid settings"}, 400)
            for cid in [c for c, v in challenges.items() if v["from"] == me and v["status"] == "pending"]:
                del challenges[cid]
            cid = new_id(10)
            challenges[cid] = {"from": me, "to": to, "minutes": minutes, "inc": inc, "color": color,
                               "status": "pending", "game": None, "t": time.time()}
            bump_lobby()
            return self.send_json({"cid": cid})

        c = challenges.get(str(data.get("cid", "")))
        if not c or c["status"] != "pending":
            return self.send_json({"error": "This challenge has expired"}, 409)
        if path == "/api/challenge/cancel" and me == c["from"]:
            c["status"] = "cancelled"
        elif path == "/api/challenge/answer" and me == c["to"]:
            if not data.get("accept") or c.get("friend"):
                c["status"] = "declined"
            else:
                game = Game(c["minutes"], c["inc"], "w")
                fc = c["color"] if c["color"] in ("w", "b") else secrets.choice(["w", "b"])
                tc = "b" if fc == "w" else "w"
                game.seats[fc], game.names[fc] = players[c["from"]]["secret"], players[c["from"]]["name"]
                game.seats[tc], game.names[tc] = p["secret"], p["name"]
                with games_lock:
                    games[game.id] = game
                c["status"], c["game"] = "accepted", game.id
                players[c["from"]]["game"] = p["game"] = game.id
        else:
            return self.send_json({"error": "Not allowed"}, 403)
        c["t"] = time.time()
        bump_lobby()
        return self.send_json({"ok": True, "game": c["game"]})

    def join(self, game, token, name):
        me = game.color_of(token)
        if not me:
            free = [c for c in ("w", "b") if game.seats[c] is None]
            if free:
                if len(free) == 2:
                    pref = game.creator_color
                    me = pref if pref in ("w", "b") else secrets.choice(["w", "b"])
                else:
                    me = free[0]
                if len(token) < 16:
                    token = secrets.token_hex(12)
                game.seats[me] = token
        if me:
            for pl in players.values():
                if pl["secret"] == token:
                    pl["game"] = game.id
            if name:
                game.names[me] = name
            game.seen[me] = time.time()
            game.bump()
            with lobby:
                bump_lobby()
            return self.send_json({"color": me, "token": token})
        return self.send_json({"color": None, "token": None})

    def move(self, game, me, data):
        if not me:
            return self.send_json({"error": "You are a spectator"}, 403)
        if game.result:
            return self.send_json({"error": "The game is over"}, 409)
        if game.check_flag():
            return self.send_json({"error": "Time is up"}, 409)
        if game.turn() != me:
            return self.send_json({"error": "Not your move"}, 409)
        if int(data.get("ply", -1)) != len(game.moves):
            return self.send_json({"error": "Out of date — refreshing"}, 409)
        uci = str(data.get("uci", ""))
        try:
            mv = chess.Move.from_uci(uci)
        except ValueError:
            mv = None
        # serveris pats pārbauda, vai gājiens ir legāls (lapai neuzticamies)
        if mv is None or not game.board.is_legal(mv):
            return self.send_json({"error": "Illegal move"}, 400)

        t = now_ms()
        game.clock_history.append((dict(game.clock), game.last_move_at))
        if game.clocks_running() and game.last_move_at:
            game.clock[me] = max(0, game.clock[me] - (t - game.last_move_at)) + game.inc * 1000
        game.board.push(mv)
        game.moves.append(mv.uci())
        game.last_move_at = t
        game.draw_offer = None
        game.takeback = None
        if not game.judge():
            game.bump()
        return self.send_json({"ok": True})

    def action(self, game, me, data):
        kind = data.get("type")
        if kind == "chat":
            text = str(data.get("text", "")).strip()[:200]
            if text:
                who = game.names.get(me) if me else ""
                who = who or {"w": "White", "b": "Black"}.get(me, "Spectator")
                game.chat.append({"who": who, "c": me, "text": text, "t": now_ms()})
                game.chat = game.chat[-100:]
                game.bump()
            return self.send_json({"ok": True})
        if kind == "flag":
            game.check_flag()
            return self.send_json({"ok": True})
        if not me:
            return self.send_json({"error": "You are a spectator"}, 403)
        other = "b" if me == "w" else "w"

        if kind == "resign" and not game.result:
            game.finish("0-1" if me == "w" else "1-0", "padošanās")
        elif kind == "abort" and not game.result and len(game.moves) < 2:
            game.finish("*", "pārtraukta")
        elif kind == "draw_offer" and not game.result:
            if game.draw_offer == other:
                game.finish("1/2-1/2", "vienošanās")
            else:
                game.draw_offer = me
                game.bump()
        elif kind == "draw_decline" and game.draw_offer == other:
            game.draw_offer = None
            game.bump()
        elif kind == "takeback" and not game.result and game.moves:
            # lūgums atsaukt pēdējo savu gājienu; ja pretinieks jau lūdz — pieņem
            if game.takeback == other:
                self.do_takeback(game, other)
            elif game.takeback != me:
                game.takeback = me
                game.bump()
        elif kind == "claim" and not game.result and len(game.moves) >= 2:
            # pretinieks pameta spēli: pēc ABANDON sekundēm var pieprasīt uzvaru
            if time.time() - game.seen[other] > ABANDON:
                game.finish("1-0" if me == "w" else "0-1", "pamesta")
        elif kind == "takeback_accept" and game.takeback == other and not game.result:
            self.do_takeback(game, other)
        elif kind == "takeback_decline" and game.takeback == other:
            game.takeback = None
            game.bump()
        elif kind == "rematch" and game.result:
            game.rematch.add(me)
            if len(game.rematch) == 2:
                game.seats["w"], game.seats["b"] = game.seats["b"], game.seats["w"]
                game.names["w"], game.names["b"] = game.names["b"], game.names["w"]
                game.seen["w"], game.seen["b"] = game.seen["b"], game.seen["w"]
                game.reset()
                game.chat.append({"who": "", "c": None, "text": "Rematch! Colors swapped.", "t": now_ms()})
            game.bump()
        elif kind == "name":
            game.names[me] = str(data.get("name", ""))[:20]
            game.bump()
        return self.send_json({"ok": True})

    @staticmethod
    def do_takeback(game, asker):
        # atsauc gājienus līdz brīdim, kad atkal ir lūdzēja gājiens (1 vai 2 pusgājieni)
        undo = 1 if game.turn() != asker else 2
        for _ in range(min(undo, len(game.moves))):
            game.board.pop()
            game.moves.pop()
            game.clock, game.last_move_at = game.clock_history.pop()
        if game.last_move_at:
            game.last_move_at = now_ms()
        game.takeback = None
        game.draw_offer = None
        game.bump()


def main():
    threading.Thread(target=cleanup_loop, daemon=True).start()
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    print(f"Gambits darbojas: http://localhost:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
