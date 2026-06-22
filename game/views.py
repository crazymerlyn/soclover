import json
import secrets
import datetime

from django.db import transaction
from django.shortcuts import render, redirect
from django.urls import reverse
from django.http import JsonResponse, Http404
from django.views.decorators.http import require_POST, require_GET
from django.utils import timezone
from django.db.models import Max

from .models import MAX_PLAYERS, HOST_TIMEOUT_SECONDS, Room, Player, Clover, Guess, create_room_with_retry
from .words import WORD_CARDS

# Cryptographically secure random number generator
_csprng = secrets.SystemRandom()


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
                    room = create_room_with_retry()
                except RuntimeError:
                    return render(request, "game/home.html", {"error": "Could not create room. Please try again."})
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
                    # Upsert player for this session with proper ordering
                    with transaction.atomic():
                        locked_room = Room.objects.select_for_update().get(id=room.id)
                        # Check if player with same name exists (rejoin after session rotation)
                        existing = locked_room.players.filter(name__iexact=player_name).first()
                        if existing:
                            # Update session key for existing player
                            existing.session_key = sk
                            existing.save(update_fields=['session_key'])
                            player = existing
                        else:
                            # Use Max aggregate to get safe order value
                            max_order = locked_room.players.aggregate(
                                max_order=Max('order')
                            )['max_order'] or 0
                            player, created = Player.objects.get_or_create(
                                room=locked_room,
                                session_key=sk,
                                defaults={
                                    "name": player_name,
                                    "is_host": False,
                                    "order": max_order + 1,
                                },
                            )
                    return redirect("lobby", code=room.code)

    code_preset = request.GET.get("code", "").strip().upper()
    return render(request, "game/home.html", {
        "error": error,
        "max_players": MAX_PLAYERS,
        "code_preset": code_preset,
    })


def lobby(request, code):
    try:
        room = get_room_or_json404(code)
    except Http404:
        return redirect("home")
    player = get_player(request, room)

    if not player:
        return redirect(f"{reverse('home')}?code={code}")
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
        return redirect(f"{reverse('home')}?code={code}")
    if room.status == Room.STATUS_LOBBY:
        return redirect("lobby", code=code)

    return render(request, "game/game.html", {"room": room, "player": player})


# ─────────────────────────── API endpoints ───────────────────────────────────


@require_POST
def start_game(request, code):
    with transaction.atomic():
        try:
            room = Room.objects.select_for_update().get(code=code)
        except Room.DoesNotExist:
            return JsonResponse({"error": "Room not found."}, status=404)
        player = get_player(request, room)

        if not player or not player.is_host:
            return JsonResponse({"error": "Only the host can start the game."}, status=403)
        if room.status != Room.STATUS_LOBBY:
            return JsonResponse({"error": "Game already started."}, status=400)

        players = list(room.players.order_by("order"))
        if len(players) < 2:
            return JsonResponse({"error": "Need at least 2 players."}, status=400)

        needed = len(players) * 4
        if needed > len(WORD_CARDS):
            return JsonResponse({"error": "Not enough word cards for this many players."}, status=400)

        # Assign 4 unique cards to each player (unique across the room for variety)
        all_indices = list(range(len(WORD_CARDS)))
        _csprng.shuffle(all_indices)
        used = 0

        for p in players:
            chosen = all_indices[used : used + 4]
            used += 4
            arrangement = {}
            for pos, idx in zip(["n", "e", "s", "w"], chosen):
                arrangement[pos] = {
                    "words": list(WORD_CARDS[idx]),
                    "flipped": _csprng.choice([True, False]),
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

    # Update player's last active timestamp
    Player.objects.filter(id=player.id).update(last_active=timezone.now())

    # Host handover check
    host = room.players.filter(is_host=True).first()
    if host is None:
        with transaction.atomic():
            if not Player.objects.select_for_update().filter(room=room, is_host=True).exists():
                next_host = Player.objects.select_for_update().filter(room=room).order_by('joined_at').first()
                if next_host:
                    next_host.is_host = True
                    next_host.save(update_fields=['is_host'])
                    if player.id == next_host.id:
                        player.is_host = True
                    # Re-fetch player to ensure consistency
                    player.refresh_from_db()
    else:
        # Re-fetch host inside transaction to get fresh last_active
        with transaction.atomic():
            locked_host = Player.objects.select_for_update().filter(room=room, is_host=True).first()
            if locked_host and timezone.now() - locked_host.last_active > datetime.timedelta(seconds=HOST_TIMEOUT_SECONDS):
                next_host = Player.objects.select_for_update().filter(
                    room=room,
                    last_active__gt=timezone.now() - datetime.timedelta(seconds=HOST_TIMEOUT_SECONDS)
                ).exclude(id=locked_host.id).order_by('order').first()
                if next_host:
                    locked_host.is_host = False
                    locked_host.save(update_fields=['is_host'])
                    next_host.is_host = True
                    next_host.save(update_fields=['is_host'])
                    # Refresh player's is_host status from database
                    if player.id == locked_host.id:
                        player.is_host = False
                    elif player.id == next_host.id:
                        player.is_host = True
                    # Re-fetch player to ensure consistency
                    player.refresh_from_db()

    state = {
        "status": room.status,
        "room_code": room.code,
        "my_player_id": player.id,
        "is_host": player.is_host,
    }

    include_submitted = False
    include_guess_submitted = None

    # ── Writing phase ───────────────────────────────────────────────────────
    if room.status == Room.STATUS_WRITING:
        include_submitted = True
        try:
            clover = player.clover
            arrangement = clover.data.get("arrangement")
            if not arrangement:
                state["writing"] = {"error": "Clover not yet assigned."}
            else:
                edges = get_edge_words(arrangement)
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
        if idx >= len(ordered):
            idx = 0

        owner = ordered[idx]
        try:
            clover = owner.clover
        except AttributeError:
            return JsonResponse({"error": "Game state error."}, status=500)
        is_owner = player.id == owner.id

        submitted_count = Guess.objects.filter(clover=clover, submitted=True).count()
        total_guessers = len(ordered) - 1

        my_guess_data = None
        if not is_owner:
            g = Guess.objects.filter(guesser=player, clover=clover).first()
            if g:
                my_guess_data = {
                    "arrangement": g.data,
                    "submitted": g.submitted,
                    "score": g.score,
                }

        include_guess_submitted = clover
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
            {"id": p.id, "name": p.name, "score": p.score}
            for p in room.players.order_by("-score")
        ]

    # Finally, query the players list exactly once
    state["players"] = players_list(
        room,
        include_submitted=include_submitted,
        include_guess_submitted=include_guess_submitted,
    )

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

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON."}, status=400)

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
        player = Player.objects.get(id=player.id)

        # Check if clues already submitted
        try:
            clover = player.clover
            if clover.clues_submitted:
                return JsonResponse({"error": "Clues already submitted."}, status=400)
        except AttributeError:
            return JsonResponse({"error": "Clover not found."}, status=400)

        clover.data["clues"] = clues

        # Build shuffled card list (4 real + 2 red herrings)
        arrangement = clover.data["arrangement"]
        used_indices = {arrangement[p]["card_idx"] for p in ("n", "e", "s", "w")}
        real_cards = [
            {"idx": arrangement[p]["card_idx"], "words": arrangement[p]["words"]}
            for p in ("n", "e", "s", "w")
        ]
        available = [i for i in range(len(WORD_CARDS)) if i not in used_indices]
        if len(available) < 2:
            return JsonResponse({"error": "Not enough word cards available."}, status=500)
        herrings = _csprng.sample(available, 2)
        for hi in herrings:
            real_cards.append({"idx": hi, "words": list(WORD_CARDS[hi])})
        _csprng.shuffle(real_cards)
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

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON."}, status=400)

    guess_arr = body.get("arrangement", {})
    client_clover_idx = body.get("clover_index")

    # Validate arrangement structure and card indices early (pure Python, no DB)
    valid_positions = {"n", "e", "s", "w"}
    max_idx = len(WORD_CARDS) - 1
    for p in valid_positions:
        entry = guess_arr.get(p)
        if not isinstance(entry, dict) or not isinstance(entry.get("idx"), int):
            return JsonResponse({"error": "Invalid arrangement data."}, status=400)
        if not (0 <= entry["idx"] <= max_idx):
            return JsonResponse({"error": "Invalid card index."}, status=400)

    with transaction.atomic():
        # Lock player row AND room row to prevent races with next_clover
        player = Player.objects.select_for_update().get(id=player.id)
        room = Room.objects.select_for_update().get(id=room.id)

        # Re-verify state after acquiring locks
        if room.status not in (Room.STATUS_GUESSING, Room.STATUS_SCORING):
            return JsonResponse({"error": "Invalid state."}, status=400)

        if client_clover_idx is None or client_clover_idx != room.current_clover_index:
            return JsonResponse({
                "error": "Stale guess submission. Please refresh and try again.",
            }, status=409)

        ordered = list(room.players.order_by("order"))
        owner = ordered[room.current_clover_index]

        if player.id == owner.id:
            return JsonResponse({"error": "Can't guess your own clover."}, status=400)

        clover = owner.clover

        # Validate guessed cards are from the dealt set (4 real + 2 herrings)
        dealt_indices = {c["idx"] for c in clover.data.get("cards", [])}
        if not dealt_indices:
            return JsonResponse({"error": "No cards dealt for this clover."}, status=500)
        for p in valid_positions:
            if guess_arr[p]["idx"] not in dealt_indices:
                return JsonResponse({"error": "Invalid card in guess."}, status=400)

        # Score: 1 point per position with correct card_idx
        correct_arr = clover.data["arrangement"]
        score = sum(
            1
            for pos in valid_positions
            if guess_arr[pos].get("idx") == correct_arr[pos]["card_idx"]
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

        # Check if all non-owners have submitted (serialized by room lock)
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

    with transaction.atomic():
        room = Room.objects.select_for_update().get(id=room.id)
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


@require_POST
def kick_player(request, code, player_id):
    try:
        room = get_room_or_json404(code)
    except Http404:
        return JsonResponse({"error": "Room not found."}, status=404)

    requester = get_player(request, room)
    if not requester or not requester.is_host:
        return JsonResponse({"error": "Only the host can kick players."}, status=403)

    try:
        player_to_kick = room.players.get(id=player_id)
    except Player.DoesNotExist:
        return JsonResponse({"error": "Player not found in this room."}, status=404)

    if player_to_kick.is_host:
        return JsonResponse({"error": "Host cannot be kicked."}, status=400)

    with transaction.atomic():
        room = Room.objects.select_for_update().get(id=room.id)
        ordered_players = list(room.players.order_by("order"))

        try:
            kicked_idx = ordered_players.index(player_to_kick)
        except ValueError:
            return JsonResponse({"error": "Player not found."}, status=404)

        # Fetch clover and guesses BEFORE deleting player (cascade deletes them)
        kicked_clover = None
        kicked_guesses_qs = None
        if room.status in (Room.STATUS_GUESSING, Room.STATUS_SCORING):
            try:
                kicked_clover = player_to_kick.clover
                kicked_guesses_qs = Guess.objects.filter(clover=kicked_clover)
            except Clover.DoesNotExist:
                pass

        player_to_kick.delete()

        remaining_players = list(room.players.order_by("order"))

        if len(remaining_players) < 2 and room.status != Room.STATUS_LOBBY:
            room.status = Room.STATUS_FINISHED
            room.save(update_fields=["status"])
            return JsonResponse({"success": True})

        for i, p in enumerate(remaining_players):
            p.order = i
            p.save(update_fields=["order"])

        if room.status == Room.STATUS_WRITING:
            total = len(remaining_players)
            done = Clover.objects.filter(player__room=room, clues_submitted=True).count()
            if done >= total:
                room.status = Room.STATUS_GUESSING
                room.current_clover_index = 0
                room.save(update_fields=["status", "current_clover_index"])

        elif room.status in (Room.STATUS_GUESSING, Room.STATUS_SCORING):
            if room.current_clover_index == kicked_idx:
                # Current clover owner was kicked - clean up their clover and guesses
                if kicked_guesses_qs is not None:
                    kicked_guesses_qs.delete()
                if kicked_clover is not None:
                    kicked_clover.delete()
                
                # Adjust index and check if we should finish
                if room.current_clover_index >= len(remaining_players):
                    room.status = Room.STATUS_FINISHED
                    room.save(update_fields=["status"])
                    return JsonResponse({"success": True})
                else:
                    room.status = Room.STATUS_GUESSING
                    room.save(update_fields=["status", "current_clover_index"])
            elif room.current_clover_index > kicked_idx:
                room.current_clover_index = max(0, room.current_clover_index - 1)
                room.save(update_fields=["current_clover_index"])

            if room.status in (Room.STATUS_GUESSING, Room.STATUS_SCORING):
                current_owner = remaining_players[room.current_clover_index]
                clover = current_owner.clover
                submitted = Guess.objects.filter(clover=clover, submitted=True).count()
                total_g = len(remaining_players) - 1

                if submitted >= total_g:
                    room.status = Room.STATUS_SCORING
                    room.save(update_fields=["status"])
                else:
                    if room.status == Room.STATUS_SCORING:
                        room.status = Room.STATUS_GUESSING
                        room.save(update_fields=["status"])

    return JsonResponse({"success": True})
