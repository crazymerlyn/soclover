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
        "nw": {"words": ["OCEAN","REEF","WAVE","SHELL"], "flipped": false, "card_idx": 0},
        "ne": {"words": ["CASTLE","MOAT","DUNGEON","TOWER"], "flipped": true,  "card_idx": 1},
        "sw": {"words": ["LIGHTNING","THUNDER","STORM","CLOUD"], "flipped": false, "card_idx": 2},
        "se": {"words": ["CRYSTAL","JEWEL","DIAMOND","RING"], "flipped": false, "card_idx": 3}
      },
      "clues": {"n": "weather", "e": "precious", "s": "water", "w": "fortress"},
      "cards": [           <-- filled after clues submitted, includes 2 red herrings
        {"idx": 0, "words": ["OCEAN","REEF","WAVE","SHELL"]},
        ...6 total, shuffled
      ]
    }

    Board layout (2×2 grid of square cards, each with 4 words):
      words[0]=top, words[1]=right, words[2]=bottom, words[3]=left

      [ N clue  ]
      [NW card]      [NE card]
        [ W clue ] [ center ] [ E clue ]
      [SW card]      [SE card]
      [ S clue  ]

    Edge word derivation (flipped rotates card 180°: top<->bottom, left<->right):
      N = NW.word_facing_N   + NE.word_facing_N
      E = NE.word_facing_E   + SE.word_facing_E
      S = SW.word_facing_S   + SE.word_facing_S
      W = NW.word_facing_W   + SW.word_facing_W

    word_facing(card, direction):
      normal:  words[{n:0,e:1,s:2,w:3}[direction]]
      flipped: words[{n:0,e:1,s:2,w:3}[direction] + 2) % 4]
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
      "nw": {"idx": 0},
      "ne": {"idx": 5},
      "sw": {"idx": 16},
      "se": {"idx": 14}
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
