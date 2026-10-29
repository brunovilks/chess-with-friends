# ♞ Gambit — chess with friends

Play chess with a friend through a link, challenge friends from your list, or play two people on one device. No sign-up needed; optional Google sign-in keeps your name and friends on every device.

**Features:** clocks (1 min to 30 min, with increment, or no clock), drag or tap to move, legal-move hints, premoves, right-click arrows and circles, last-move and check highlights, promotion picker, captured pieces and material count, move list with replay (← →), recorded sound effects, chat, draw offers, resign, rematch with swapped colors, spectators, board themes, pass-and-play on one device with optional auto-rotating board.

The server uses only the Python standard library.

## Run locally

```
python server.py
```
Then open http://localhost:8000

## Settings (environment variables)

- `PORT`: port to listen on (default 8000)
- `GOOGLE_CLIENT_ID`: Google OAuth web client ID, enables "Sign in with Google"
- `SERVER_KEY`: long random secret; keep it fixed so Google players keep their identity after restarts

## Credits

- Pieces: "cburnett" set (CC BY-SA 3.0)
- Sounds: "Chess Puzzle Blitz SFX" by [el_boss](https://freesound.org/people/el_boss/packs/30764/) (CC0)
- Rules: [chess.js](https://github.com/jhlywa/chess.js) in the browser, [python-chess](https://github.com/niklasf/python-chess) on the server (the server is the referee)
