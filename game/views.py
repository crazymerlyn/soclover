import json
import random

from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse
from django.views.decorators.http import require_POST, require_GET
from django.views.decorators.csrf import csrf_exempt

from .models import Room, Player, Clover, Guess
from .words import WORD_CARDS


# ─────────────────────────── helpers ────────────────────────────────────────


def ensure_session(request):
    if not request.session.session_key:
        request.session.create()
    return request.session.session_key


def get_player(request, room):
    sk = request.session.session_key
    if not sk:
        return None
    return Player.objects.filter(room=room, session_key=sk).first()


def primary(card_entry):
    """The word that faces the clockwise-adjacent edge."""
    w = card_entry["words"]
    return w[1] if card_entry.get("flipped") else w[0]


def secondary(card_entry):
    """The word that faces the counter-clockwise-adjacent edge."""
    w = card_entry["words"]
    return w[0] if card_entry.get("flipped") else w[1]


def get_edge_words(arrangement):
    """
    Returns the two words visible at each of the 4 hint spaces.
    NE = N.primary  + E.secondary
    SE = E.primary  + S.secondary
    SW = S.primary  + W.secondary
    NW = W.primary  + N.secondary
    """
    n, e, s, w = (arrangement[p] for p in ("n", "e", "s", "w"))
    return {
        "ne": [primary(n), secondary(e)],
        "se": [primary(e), secondary(s)],
        "sw": [primary(s), secondary(w)],
        "nw": [primary(w), secondary(n)],
    }


def players_list(room, include_submitted=False):
    out = []
    for p in room.players.order_by("order"):
        d = {
            "id": p.id,
            "name": p.name,
            "is_host": p.is_host,
            "score": p.score,
        }
        if include_submitted and hasattr(p, "clover"):
            d["clues_submitted"] = p.clover.clues_submitted
        out.append(d)
    return out


# ─────────────────────────── page views ─────────────────────────────────────


def home(request):
    error = None
    if request.method == "POST":
        action = request.POST.get("action")
        player_name = request.POST.get("name", "").strip()[:50]

        if not player_name:
            error = "Please enter your name."
        else:
            sk = ensure_session(request)

            if action == "create":
                room = Room.objects.create()
                Player.objects.create(
                    room=room,
                    name=player_name,
                    session_key=sk,
                    is_host=True,
                    order=0,
                )
                return redirect("lobby", code=room.code)

            elif action == "join":
                code = request.POST.get("code", "").strip().upper()
                room = Room.objects.filter(code=code).first()
                if not room:
                    error = "Room not found. Check the code and try again."
                elif room.status != Room.STATUS_LOBBY:
                    error = "That game is already in progress."
                else:
                    # Upsert player for this session
                    player, _ = Player.objects.get_or_create(
                        room=room,
                        session_key=sk,
                        defaults={
                            "name": player_name,
                            "is_host": False,
                            "order": room.players.count(),
                        },
                    )
                    return redirect("lobby", code=room.code)

    return render(request, "game/home.html", {"error": error})


def lobby(request, code):
    room = get_object_or_404(Room, code=code)
    player = get_player(request, room)

    if not player:
        return redirect("home")
    if room.status != Room.STATUS_LOBBY:
        return redirect("game_view", code=code)

    return render(request, "game/lobby.html", {"room": room, "player": player})


def game_view(request, code):
    room = get_object_or_404(Room, code=code)
    player = get_player(request, room)

    if not player:
        return redirect("home")
    if room.status == Room.STATUS_LOBBY:
        return redirect("lobby", code=code)

    return render(request, "game/game.html", {"room": room, "player": player})


# ─────────────────────────── API endpoints ───────────────────────────────────


@require_POST
def start_game(request, code):
    room = get_object_or_404(Room, code=code)
    player = get_player(request, room)

    if not player or not player.is_host:
        return JsonResponse({"error": "Only the host can start the game."}, status=403)
    if room.status != Room.STATUS_LOBBY:
        return JsonResponse({"error": "Game already started."}, status=400)

    players = list(room.players.order_by("order"))
    # if len(players) < 2:
    #     return JsonResponse({'error': 'Need at least 2 players.'}, status=400)

    # Assign 4 unique cards to each player (unique across the room for variety)
    all_indices = list(range(len(WORD_CARDS)))
    random.shuffle(all_indices)
    used = 0

    for p in players:
        chosen = all_indices[used : used + 4]
        used += 4
        arrangement = {}
        for pos, idx in zip(["n", "e", "s", "w"], chosen):
            arrangement[pos] = {
                "words": list(WORD_CARDS[idx]),
                "flipped": random.choice([True, False]),
                "card_idx": idx,
            }
        Clover.objects.create(
            player=p,
            data={"arrangement": arrangement, "clues": {}, "cards": []},
        )

    room.status = Room.STATUS_WRITING
    room.save()
    return JsonResponse({"success": True})


@require_GET
def get_state(request, code):
    room = get_object_or_404(Room, code=code)
    player = get_player(request, room)

    if not player:
        return JsonResponse({"error": "Not in room."}, status=403)

    state = {
        "status": room.status,
        "players": players_list(
            room, include_submitted=(room.status == Room.STATUS_WRITING)
        ),
        "my_player_id": player.id,
        "is_host": player.is_host,
        "room_code": room.code,
    }

    # ── Writing phase ───────────────────────────────────────────────────────
    if room.status == Room.STATUS_WRITING:
        clover = getattr(player, "clover", None)
        if clover:
            edges = get_edge_words(clover.data["arrangement"])
            state["writing"] = {
                "edges": edges,
                "clues": clover.data.get("clues", {}),
                "submitted": clover.clues_submitted,
            }

    # ── Guessing / Scoring phase ────────────────────────────────────────────
    elif room.status in (Room.STATUS_GUESSING, Room.STATUS_SCORING):
        ordered = list(room.players.order_by("order"))
        idx = room.current_clover_index
        owner = ordered[idx]
        clover = owner.clover
        is_owner = player.id == owner.id

        submitted_count = Guess.objects.filter(clover=clover, submitted=True).count()
        total_guessers = room.players.count() - 1

        my_guess_data = None
        if not is_owner:
            g = Guess.objects.filter(guesser=player, clover=clover).first()
            if g:
                my_guess_data = {
                    "arrangement": g.data,
                    "submitted": g.submitted,
                    "score": g.score,
                }

        state["guessing"] = {
            "owner_name": owner.name,
            "owner_id": owner.id,
            "is_owner": is_owner,
            "clues": clover.data.get("clues", {}),
            "cards": clover.data.get("cards", []),
            "submitted_count": submitted_count,
            "total_guessers": total_guessers,
            "all_submitted": submitted_count >= total_guessers,
            "clover_index": idx,
            "total_clovers": len(ordered),
            "my_guess": my_guess_data,
        }

        if room.status == Room.STATUS_SCORING:
            arr = clover.data["arrangement"]
            edges = get_edge_words(arr)
            guesses_info = []
            for g in Guess.objects.filter(clover=clover, submitted=True).select_related(
                "guesser"
            ):
                guesses_info.append(
                    {
                        "guesser_name": g.guesser.name,
                        "guesser_id": g.guesser.id,
                        "score": g.score,
                        "arrangement": g.data,
                    }
                )
            state["scoring"] = {
                "correct_arrangement": arr,
                "correct_edges": edges,
                "all_guesses": guesses_info,
            }

    # ── Finished ────────────────────────────────────────────────────────────
    elif room.status == Room.STATUS_FINISHED:
        state["final_scores"] = [
            {"name": p.name, "score": p.score} for p in room.players.order_by("-score")
        ]

    return JsonResponse(state)


@require_POST
def submit_clues(request, code):
    room = get_object_or_404(Room, code=code)
    player = get_player(request, room)

    if not player or room.status != Room.STATUS_WRITING:
        return JsonResponse({"error": "Invalid state."}, status=400)

    body = json.loads(request.body)
    clues = {k: body.get(k, "").strip() for k in ("ne", "se", "sw", "nw")}

    if not all(clues.values()):
        return JsonResponse({"error": "All four clues are required."}, status=400)

    clover = player.clover
    clover.data["clues"] = clues

    # Build shuffled card list (4 real + 2 red herrings)
    arrangement = clover.data["arrangement"]
    used_indices = {arrangement[p]["card_idx"] for p in ("n", "e", "s", "w")}
    real_cards = [
        {"idx": arrangement[p]["card_idx"], "words": arrangement[p]["words"]}
        for p in ("n", "e", "s", "w")
    ]
    available = [i for i in range(len(WORD_CARDS)) if i not in used_indices]
    herrings = random.sample(available, 2)
    for hi in herrings:
        real_cards.append({"idx": hi, "words": list(WORD_CARDS[hi])})
    random.shuffle(real_cards)
    clover.data["cards"] = real_cards
    clover.clues_submitted = True
    clover.save()

    # Auto-advance once every player has submitted
    total = room.players.count()
    done = Clover.objects.filter(player__room=room, clues_submitted=True).count()
    if done >= total:
        room.status = Room.STATUS_GUESSING
        room.current_clover_index = 0
        room.save()

    return JsonResponse({"success": True})


@require_POST
def submit_guess(request, code):
    room = get_object_or_404(Room, code=code)
    player = get_player(request, room)

    if not player or room.status not in (Room.STATUS_GUESSING, Room.STATUS_SCORING):
        return JsonResponse({"error": "Invalid state."}, status=400)

    ordered = list(room.players.order_by("order"))
    owner = ordered[room.current_clover_index]

    if player.id == owner.id:
        return JsonResponse({"error": "Can't guess your own clover."}, status=400)

    clover = owner.clover
    body = json.loads(request.body)
    guess_arr = body.get("arrangement", {})

    # Score: 1 point per position with correct card_idx
    correct_arr = clover.data["arrangement"]
    score = sum(
        1
        for pos in ("n", "e", "s", "w")
        if pos in guess_arr
        and guess_arr[pos].get("idx") == correct_arr[pos]["card_idx"]
    )

    # Upsert guess
    guess, created = Guess.objects.get_or_create(
        guesser=player,
        clover=clover,
        defaults={"data": guess_arr, "score": score, "submitted": True},
    )
    if not created:
        # Undo previous score contribution
        player.score = max(0, player.score - guess.score)
        guess.data = guess_arr
        guess.score = score
        guess.submitted = True
        guess.save()

    player.score += score
    player.save()

    # Check if all non-owners have submitted
    submitted = Guess.objects.filter(clover=clover, submitted=True).count()
    total_g = room.players.count() - 1
    if submitted >= total_g:
        room.status = Room.STATUS_SCORING
        room.save()

    return JsonResponse({"success": True, "score": score})


@require_POST
def next_clover(request, code):
    room = get_object_or_404(Room, code=code)
    player = get_player(request, room)

    if not player or not player.is_host:
        return JsonResponse({"error": "Only the host can advance."}, status=403)
    if room.status != Room.STATUS_SCORING:
        return JsonResponse({"error": "Not in scoring phase."}, status=400)

    next_idx = room.current_clover_index + 1
    if next_idx >= room.players.count():
        room.status = Room.STATUS_FINISHED
    else:
        room.status = Room.STATUS_GUESSING
        room.current_clover_index = next_idx
    room.save()

    return JsonResponse({"success": True})
