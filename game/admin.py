from django.contrib import admin

from .models import Room, Player, Clover, Guess


@admin.register(Room)
class RoomAdmin(admin.ModelAdmin):
    list_display = ("code", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("code",)


@admin.register(Player)
class PlayerAdmin(admin.ModelAdmin):
    list_display = ("name", "room", "is_host", "score", "order")
    list_filter = ("is_host", "room__status")
    search_fields = ("name", "room__code")


@admin.register(Clover)
class CloverAdmin(admin.ModelAdmin):
    list_display = ("player", "clues_submitted")
    list_filter = ("clues_submitted",)


@admin.register(Guess)
class GuessAdmin(admin.ModelAdmin):
    list_display = ("guesser", "clover", "score", "submitted")
    list_filter = ("submitted",)
