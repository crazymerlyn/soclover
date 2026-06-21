from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta
from game.models import Room, Player, Clover, Guess


class Command(BaseCommand):
    help = 'Clean up old rooms and stale game data'

    def add_arguments(self, parser):
        parser.add_argument(
            '--hours',
            type=int,
            default=24,
            help='Delete rooms older than this many hours (default: 24)',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be deleted without actually deleting',
        )

    def handle(self, *args, **options):
        hours = options['hours']
        dry_run = options['dry_run']
        cutoff = timezone.now() - timedelta(hours=hours)

        # Find old rooms
        old_rooms = Room.objects.filter(created_at__lt=cutoff)
        count = old_rooms.count()

        if count == 0:
            self.stdout.write(self.style.SUCCESS(f'No rooms older than {hours} hours found.'))
            return

        if dry_run:
            self.stdout.write(self.style.WARNING(f'DRY RUN: Would delete {count} rooms:'))
            for room in old_rooms[:10]:
                self.stdout.write(f'  - {room.code} ({room.status}, created {room.created_at})')
            if count > 10:
                self.stdout.write(f'  ... and {count - 10} more')
            return

        # Delete old rooms (CASCADE will delete related objects)
        deleted_count = old_rooms.delete()[0]
        self.stdout.write(self.style.SUCCESS(f'Successfully deleted {deleted_count} old rooms.'))
