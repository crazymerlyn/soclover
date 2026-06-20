import json
import random

from django.db import transaction
from django.shortcuts import render, redirect
from django.http import JsonResponse, Http404
from django.views.decorators.http import require_POST, require_GET

from .models import MAX_PLAYERS, Room, Player, Clover, Guess, create_room_with_retry
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


def get_room_or_json404(code):
    try:
        return Room.objects.get(code=code)
    except Room.DoesNotExist:
        raise Http404("Room not found.")


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


def players_list(room, include_submitted=False, include_guess_submitted=None):
    qs = room.players.select_related("clover").order_by("order")
    out = []
    guess_map = {}
    if include_guess_submitted is not None:
        guesses = Guess.objects.filter(
            guesser__room=room, clover=include_guess_submitted
        ).values_list("guesser_id", "submitted")
        guess_map = {g[0]: g[1] for g in guesses}
    for p in qs:
        d = {
            "id": p.id,
            "name": p.name,
            "is_host": p.is_host,
            "score": p.score,
        }
        if include_submitted:
            try:
                d["clues_submitted"] = p.clover.clues_submitted
            except AttributeError:
                d["clues_submitted"] = False
        if include_guess_submitted is not None:
            d["guess_submitted"] = guess_map.get(p.id, False)
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
                try:
                    code = create_room_with_retry()
                except RuntimeError as e:
                    return render(request, "game/home.html", {"error": str(e)})
                room = Room.objects.create(code=code)
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
                elif room.players.count() >= MAX_PLAYERS:
                    error = "Room is full (max {} players).".format(MAX_PLAYERS)
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

    return render(request, "game/home.html", {"error": error, "max_players": MAX_PLAYERS})


def lobby(request, code):
    try:
        room = get_room_or_json404(code)
    except Http404:
        return redirect("home")
    player = get_player(request, room)

    if not player:
        return redirect("home")
    if room.status != Room.STATUS_LOBBY:
        return redirect("game_view", code=code)

    return render(request, "game/lobby.html", {"room": room, "player": player})


def game_view(request, code):
    try:
        room = get_room_or_json404(code)
    except Http404:
        return redirect("home")
    player = get_player(request, room)

    if not player:
        return redirect("home")
    if room.status == Room.STATUS_LOBBY:
        return redirect("lobby", code=code)

    return render(request, "game/game.html", {"room": room, "player": player})


# ─────────────────────────── API endpoints ───────────────────────────────────


@require_POST
def start_game(request, code):
    try:
        room = get_room_or_json404(code)
    except Http404:
        return JsonResponse({"error": "Room not found."}, status=404)
    player = get_player(request, room)

    if not player or not player.is_host:
        return JsonResponse({"error": "Only the host can start the game."}, status=403)
    if room.status != Room.STATUS_LOBBY:
        return JsonResponse({"error": "Game already started."}, status=400)

    players = list(room.players.order_by("order"))
    if len(players) < 2:
        return JsonResponse({"error": "Need at least 2 players."}, status=400)

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
    try:
        room = get_room_or_json404(code)
    except Http404:
        return JsonResponse({"error": "Room not found."}, status=404)
    player = get_player(request, room)

    if not player:
        return JsonResponse({"error": "Not in room."}, status=403)

    state = {
        "status": room.status,
        "room_code": room.code,
        "players": players_list(room),
        "my_player_id": player.id,
        "is_host": player.is_host,
    }

    # ── Writing phase ───────────────────────────────────────────────────────
    if room.status == Room.STATUS_WRITING:
        state["players"] = players_list(room, include_submitted=True)

        try:
            clover = player.clover
            edges = get_edge_words(clover.data["arrangement"])
            state["writing"] = {
                "edges": edges,
                "clues": clover.data.get("clues", {}),
                "submitted": clover.clues_submitted,
            }
        except AttributeError:
            state["writing"] = {"error": "Clover not yet assigned."}

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

        state["players"] = players_list(
            room, include_guess_submitted=clover
        )
        state["guessing"] = {
            "owner_name": owner.name,
            "owner_id": owner.id,
            "is_owner": is_owner,
            "clues": clover.data.get("clues", {}),
            "cards": clover.data.get("cards", []) if not is_owner else [],
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
    try:
        room = get_room_or_json404(code)
    except Http404:
        return JsonResponse({"error": "Room not found."}, status=404)
    player = get_player(request, room)

    if not player or room.status != Room.STATUS_WRITING:
        return JsonResponse({"error": "Invalid state."}, status=400)

    body = json.loads(request.body)
    clues = {k: body.get(k, "").strip() for k in ("ne", "se", "sw", "nw")}

    if not all(clues.values()):
        return JsonResponse({"error": "All four clues are required."}, status=400)

    for k, v in clues.items():
        if " " in v:
            return JsonResponse(
                {"error": "Clues must be single words (no spaces)."},
                status=400,
            )

    with transaction.atomic():
        room = Room.objects.select_for_update().get(id=room.id)
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
            room.save(update_fields=["status", "current_clover_index"])

    return JsonResponse({"success": True})


@require_POST
def submit_guess(request, code):
    try:
        room = get_room_or_json404(code)
    except Http404:
        return JsonResponse({"error": "Room not found."}, status=404)
    player = get_player(request, room)

    if not player or room.status not in (Room.STATUS_GUESSING, Room.STATUS_SCORING):
        return JsonResponse({"error": "Invalid state."}, status=400)

    ordered = list(room.players.order_by("order"))
    owner = ordered[room.current_clover_index]

    if player.id == owner.id:
        return JsonResponse({"error": "Can't guess your own clover."}, status=400)

    body = json.loads(request.body)

    # Verify client knows which clover it's guessing (prevent stale-client race)
    client_clover_idx = body.get("clover_index")
    if client_clover_idx is None or client_clover_idx != room.current_clover_index:
        return JsonResponse({
            "error": "Stale guess submission. Please refresh and try again.",
        }, status=409)

    clover = owner.clover
    guess_arr = body.get("arrangement", {})

    # Validate arrangement structure
    valid_positions = {"n", "e", "s", "w"}
    if not all(p in guess_arr and isinstance(guess_arr[p], dict) and "idx" in guess_arr[p] for p in valid_positions):
        return JsonResponse({"error": "Invalid arrangement data."}, status=400)

    # Score: 1 point per position with correct card_idx
    correct_arr = clover.data["arrangement"]
    score = sum(
        1
        for pos in valid_positions
        if guess_arr[pos].get("idx") == correct_arr[pos]["card_idx"]
    )

    with transaction.atomic():
        # Upsert guess
        guess, created = Guess.objects.get_or_create(
            guesser=player,
            clover=clover,
            defaults={"data": guess_arr, "score": score, "submitted": True},
        )
        if not created:
            # Undo previous score contribution
            player = Player.objects.select_for_update().get(id=player.id)
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
            room.save(update_fields=["status"])

    return JsonResponse({"success": True, "score": score})


@require_POST
def next_clover(request, code):
    try:
        room = get_room_or_json404(code)
    except Http404:
        return JsonResponse({"error": "Room not found."}, status=404)
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
