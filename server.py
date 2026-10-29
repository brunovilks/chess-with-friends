"""Gambīts — šaha serveris spēlēm ar draugu pa saiti.

Tikai Python standarta bibliotēka, nekas nav jāinstalē.
Palaišana:  python server.py   (ports no vides mainīgā PORT, citādi 8000)
"""
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import urllib.request
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
        self.moves = []
        self.clock = {"w": self.minutes * 60000, "b": self.minutes * 60000}
        self.last_move_at = None
        self.result = None   # '1-0', '0-1', '1/2-1/2'
        self.reason = None
        self.draw_offer = None   # krāsa, kas piedāvā
        self.rematch = set()

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
                self.finish("0-1" if t == "w" else "1-0", "laiks")
                return True
        return False

    def finish(self, result, reason):
        self.result = result
        self.reason = reason
        self.draw_offer = None
        self.bump()
        with lobby:
            bump_lobby()

    def bump(self):
        self.version += 1
        self.touched = time.time()
        self.cond.notify_all()

    def state(self, token=None):
        t = time.time()
        return {
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
            "rematch": sorted(self.rematch),
            "players": {
                c: {
                    "pid": next((q for q, pl in list(players.items()) if self.seats[c] and pl["secret"] == self.seats[c]), None),
                    "taken": self.seats[c] is not None,
                    "name": self.names[c],
                    "online": t - self.seen[c] < ONLINE_WINDOW,
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


accounts = {}       # Google spēlētāja pid -> {"friends": {pid: vārds}, "picture": url}
ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


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


def merge_friends(pid, add, remove):
    acc = accounts.setdefault(pid, {"friends": {}, "picture": ""})
    if isinstance(add, dict):
        for q, n in list(add.items())[:500]:
            q = str(q)
            if q != pid and len(q) == 8 and set(q) <= PID_OK:
                acc["friends"][q] = str(n)[:20] or acc["friends"].get(q, "Friend")
    if isinstance(remove, list):
        for q in remove[:500]:
            acc["friends"].pop(str(q), None)
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

    def send_sound(self, name):
        # tikai skaņu faili ar drošu nosaukumu: snd_<vārds>.ogg vai .wav
        base, _, ext = name.rpartition(".")
        if ext not in ("ogg", "wav") or not base.startswith("snd_") or not base[4:].isalnum():
            return self.send_json({"error": "Not found"}, 404)
        for folder in (os.path.join(ROOT, "sounds"), ROOT):
            path = os.path.join(folder, name)
            if os.path.isfile(path):
                with open(path, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "audio/ogg" if ext == "ogg" else "audio/wav")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "public, max-age=604800")
                self.end_headers()
                self.wfile.write(body)
                return
        self.send_json({"error": "Not found"}, 404)

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
        if url.path.startswith("/s/"):
            return self.send_sound(url.path[3:])
        if url.path == "/health":
            return self.send_json({"ok": True, "games": len(games)})
        if url.path == "/api/config":
            return self.send_json({"googleClientId": GOOGLE_CLIENT_ID})
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
            minutes = int(data.get("minutes", 10))
            inc = int(data.get("inc", 0))
            color = data.get("color", "r")
            if minutes < 0 or minutes > 180 or inc < 0 or inc > 60 or color not in ("w", "b", "r"):
                return self.send_json({"error": "Invalid settings"}, 400)
            game = Game(minutes, inc, color)
            with games_lock:
                games[game.id] = game
            return self.send_json({"id": game.id})

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
        if not (4 <= len(uci) <= 5):
            return self.send_json({"error": "Invalid move"}, 400)

        t = now_ms()
        if game.clocks_running() and game.last_move_at:
            game.clock[me] = max(0, game.clock[me] - (t - game.last_move_at)) + game.inc * 1000
        game.moves.append(uci)
        game.last_move_at = t
        game.draw_offer = None

        end = data.get("end")
        if isinstance(end, dict) and end.get("result") in ("1-0", "0-1", "1/2-1/2"):
            game.finish(end["result"], str(end.get("reason", ""))[:40])
        else:
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


def main():
    threading.Thread(target=cleanup_loop, daemon=True).start()
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    print(f"Gambits darbojas: http://localhost:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
