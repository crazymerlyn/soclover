# 🍀 So Clover Online

A multiplayer online implementation of So Clover built with Django + vanilla JavaScript.

## Quick Start

```bash
# 1. Install Python 3.10+, then:
pip install -r requirements.txt

# 2. Set up the database
python manage.py makemigrations game
python manage.py migrate

# 3. Run the server
python manage.py runserver

# 4. Open http://localhost:8000 in your browser
```

For local multiplayer, other players on the same network can join via your machine's IP:
```
http://YOUR_LOCAL_IP:8000
```

---

## How to Play

### Setup
1. One player creates a room and shares the 6-letter room code.
2. All players join using the code, then the host clicks **Start Game**.

### Writing Phase ✏️
Each player sees their own **4-leaf clover** board. Four cards have been randomly placed
in the N / E / S / W positions. Each card has two words; adjacent cards share a **hint space**
(NE, SE, SW, NW) that shows one word from each card.

- Write **one clue word** per hint space that hints at **both** words shown there.
- Submit when done. Wait for everyone else to finish.

### Guessing Phase 🔍
Each player's clover is guessed in turn. The clover owner watches while everyone else:
1. Sees the 4 clue words and **6 shuffled cards** (4 real + 2 red herrings).
2. **Click** a card from the pool to select it, then click a clover position (N/E/S/W) to place it.
3. Use the **⇄ flip** button to swap which word faces which hint.
4. The hint spaces dynamically show which words your current placement would produce.
5. Submit when all 4 positions are filled.

**Scoring:** 1 point per correctly placed card (position + orientation). Max 4 points per clover.

### Results
After everyone guesses, see the correct arrangement and each player's score. The host
advances to the next player's clover until all have been guessed.

---

## Architecture

```
soclover/
├── manage.py
├── requirements.txt
├── setup.sh
├── soclover/           ← Django project config
│   ├── settings.py
│   └── urls.py
└── game/               ← App
    ├── models.py       ← Room, Player, Clover, Guess
    ├── views.py        ← Pages + REST-style API endpoints
    ├── urls.py
    ├── words.py        ← 70-card word bank
    └── templates/game/
        ├── base.html
        ├── home.html   ← Create/join room
        ├── lobby.html  ← Waiting room
        └── game.html   ← All game phases (polling every 2.5s)
```

### Game State Flow
```
lobby → writing → guessing ⇄ scoring → (next clover) → finished
```

State is shared via a `GET /api/<code>/state/` polling endpoint.
No WebSockets required — each client polls every 2.5 seconds.

### Edge Word Logic
```
Clover layout:       NE = N.primary   + E.secondary
      [N]            SE = E.primary   + S.secondary
   NW    NE          SW = S.primary   + W.secondary
[W]    +    [E]      NW = W.primary   + N.secondary
   SW    SE
      [S]            primary  = words[0] (or words[1] if flipped)
                     secondary= words[1] (or words[0] if flipped)
```

---

## Production Notes
- Change `SECRET_KEY` in `settings.py`
- Set `DEBUG = False` and configure `ALLOWED_HOSTS`
- Use PostgreSQL instead of SQLite
- Add a process manager (gunicorn + nginx)
