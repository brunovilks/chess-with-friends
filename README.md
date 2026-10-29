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

- Piece sets (files `pc_<set>_<piece>.svg`) from [lichess](https://github.com/lichess-org/lila/blob/master/COPYING.md): Staunty, Maestro, Cardinal by sadsnake1 and California by Jerry S. (CC BY-NC-SA 4.0, non-commercial), Cburnett by Colin M.L. Burnett and Merida by Armando Hernandez Marroquin (GPLv2+), Chessnut by Alexis Luengas (Apache 2.0)
- Sounds (all CC0, Freesound): "Wood" and "Crisp" sets trimmed and normalized from recordings by [JJTaynos](https://freesound.org/people/JJTaynos/sounds/733927/) and [Amatsuuu](https://freesound.org/people/Amatsuuu/sounds/629182/); "Classic" set by [el_boss](https://freesound.org/people/el_boss/packs/30764/)
- Data: ratings, friends and game history are stored in SQLite under `DATA_DIR` (default `./data`)
- Rules: [chess.js](https://github.com/jhlywa/chess.js) in the browser, [python-chess](https://github.com/niklasf/python-chess) on the server (the server is the referee)
