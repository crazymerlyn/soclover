import secrets
import string
from django.db import models, IntegrityError
from django.utils import timezone


MAX_PLAYERS = 8
ROOM_CODE_LENGTH = 6
HOST_TIMEOUT_SECONDS = 15
RED_HERRING_COUNT = 2

_SAFE_CHARS = "ABCDEFGHJKLMNPRTUVWXYZ"  # no I, O, Q, S (avoid ambiguity)

def generate_room_code():
    return ''.join(secrets.choice(_SAFE_CHARS) for _ in range(ROOM_CODE_LENGTH))


def create_room_with_retry(max_attempts=10):
    for _ in range(max_attempts):
        code = generate_room_code()
        try:
            room = Room.objects.create(code=code)
            return room
        except IntegrityError:
            continue
    raise RuntimeError("Could not generate unique room code")


class Room(models.Model):
    STATUS_LOBBY    = 'lobby'
    STATUS_WRITING  = 'writing'
    STATUS_GUESSING = 'guessing'
    STATUS_SCORING  = 'scoring'
    STATUS_FINISHED = 'finished'
    
    STATUS_CHOICES = [
        (STATUS_LOBBY, 'Lobby'),
        (STATUS_WRITING, 'Writing'),
        (STATUS_GUESSING, 'Guessing'),
        (STATUS_SCORING, 'Scoring'),
        (STATUS_FINISHED, 'Finished'),
    ]

    code = models.CharField(max_length=6, unique=True, default=generate_room_code)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_LOBBY)
    # Index into players.order_by('order') — whose clover is currently being guessed
    current_clover_index = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    def clean(self):
        from django.core.exceptions import ValidationError
        ordered_players = list(self.players.order_by('order'))
        if self.current_clover_index < 0 or self.current_clover_index >= len(ordered_players):
            if ordered_players:  # Only validate if there are players
                raise ValidationError('current_clover_index is out of range')

    def __str__(self):
        return f"Room {self.code} ({self.status})"


class Player(models.Model):
    room = models.ForeignKey(Room, on_delete=models.CASCADE, related_name='players')
    name = models.CharField(max_length=50)
    session_key = models.CharField(max_length=128)
    is_host = models.BooleanField(default=False)
    is_visitor = models.BooleanField(default=False)
    score = models.IntegerField(default=0)
    order = models.IntegerField(default=0)
    joined_at = models.DateTimeField(auto_now_add=True)
    last_active = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = [('room', 'session_key')]

    def __str__(self):
        return f"{self.name} in {self.room.code}"


class Clover(models.Model):
    """
    One player's clover board for the whole game.

    data layout (JSONField):
    {
      "arrangement": {
        "n": {"words": ["OCEAN","WAVE"], "flipped": false, "card_idx": 0},
        "e": {"words": ["WOLF","HOWL"],  "flipped": true,  "card_idx": 5},
        "s": {"words": ["CROWN","SCEPTER"],"flipped":false,"card_idx":16},
        "w": {"words": ["SPIDER","SILK"],"flipped": false, "card_idx": 14}
      },
      "clues": {"ne": "sea", "se": "royalty", "sw": "web", "nw": "night"},
      "cards": [           <-- filled after clues submitted, includes 2 red herrings
        {"idx": 0, "words": ["OCEAN","WAVE"]},
        ...6 total, shuffled
      ]
    }

    Edge word derivation (flipped swaps which word faces which edge):
      NE = arrangement.n primary   + arrangement.e secondary
      SE = arrangement.e primary   + arrangement.s secondary
      SW = arrangement.s primary   + arrangement.w secondary
      NW = arrangement.w primary   + arrangement.n secondary

    where primary  = words[0] if not flipped else words[1]
          secondary= words[1] if not flipped else words[0]
    """
    player = models.OneToOneField(Player, on_delete=models.CASCADE, related_name='clover')
    data = models.JSONField(default=dict)
    clues_submitted = models.BooleanField(default=False)

    def __str__(self):
        return f"Clover({self.player.name})"


class Guess(models.Model):
    """
    One player's card-placement guess for a given clover.

    data layout:
    {
      "n": {"idx": 0},
      "e": {"idx": 5},
      "s": {"idx": 16},
      "w": {"idx": 14}
    }
    """
    guesser = models.ForeignKey(Player, on_delete=models.CASCADE, related_name='guesses_made')
    clover  = models.ForeignKey(Clover, on_delete=models.CASCADE, related_name='guesses')
    data    = models.JSONField(default=dict)
    score   = models.IntegerField(default=0)
    submitted = models.BooleanField(default=False)

    class Meta:
        unique_together = [('guesser', 'clover')]

    def __str__(self):
        return f"Guess by {self.guesser.name} for {self.clover.player.name} (score={self.score})"
