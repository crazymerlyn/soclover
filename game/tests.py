import datetime
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone
from game.models import Room, Player, Clover, Guess

class SoCloverTests(TestCase):
    def setUp(self):
        self.client = Client()

    def test_create_and_join_room(self):
        # Create a room
        response = self.client.post(reverse("home"), {"action": "create", "name": "HostPlayer"})
        self.assertEqual(response.status_code, 302)
        room = Room.objects.first()
        self.assertIsNotNone(room)
        
        # Verify host was created
        host = Player.objects.filter(room=room, is_host=True).first()
        self.assertEqual(host.name, "HostPlayer")

        # Join room with another player using a separate client/session
        other_client = Client()
        response = other_client.post(reverse("home"), {"action": "join", "code": room.code, "name": "GuestPlayer"})
        self.assertEqual(response.status_code, 302)
        
        guest = Player.objects.filter(room=room, name="GuestPlayer").first()
        self.assertIsNotNone(guest)

    def test_duplicate_name_prevention(self):
        # Create room
        self.client.post(reverse("home"), {"action": "create", "name": "Alex"})
        room = Room.objects.first()

        # Join with same name
        other_client = Client()
        response = other_client.post(reverse("home"), {"action": "join", "code": room.code, "name": "alex"})
        self.assertContains(response, "That name is already taken in this room")
        self.assertEqual(Player.objects.filter(room=room).count(), 1)

    def test_host_handover_when_inactive(self):
        # Setup room with host and guest
        room = Room.objects.create(code="TESTXX")
        host = Player.objects.create(room=room, name="Host", session_key="s1", is_host=True)
        guest = Player.objects.create(room=room, name="Guest", session_key="s2", is_host=False, order=1)

        # Set host last_active to 20 seconds ago
        host.last_active = timezone.now() - datetime.timedelta(seconds=20)
        host.save()

        # Poll state as Guest
        guest_client = Client()
        session = guest_client.session
        session.save()
        guest.session_key = guest_client.session.session_key
        guest.save()

        response = guest_client.get(reverse("get_state", args=[room.code]))
        self.assertEqual(response.status_code, 200)

        # Verify host has changed
        host.refresh_from_db()
        guest.refresh_from_db()
        self.assertFalse(host.is_host)
        self.assertTrue(guest.is_host)

    def test_kick_player_by_host(self):
        # Setup room
        room = Room.objects.create(code="KICKXX")
        host = Player.objects.create(room=room, name="Host", session_key="sh", is_host=True)
        guest = Player.objects.create(room=room, name="Guest", session_key="sg", is_host=False, order=1)

        # Set session on host client
        host_client = Client()
        session = host_client.session
        session.save()
        host.session_key = host_client.session.session_key
        host.save()

        # Try to kick guest as host
        response = host_client.post(reverse("kick_player", args=[room.code, guest.id]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Player.objects.filter(id=guest.id).exists())
