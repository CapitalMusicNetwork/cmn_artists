"""The scheduler process.

Runs recurring maintenance tasks in a long-lived process instead of a shell
`while true; sleep; python manage.py ...` loop in docker-compose, so Django
only boots once instead of paying startup cost on every tick.

Everything it calls is independently runnable and idempotent, so a restart
never double-runs anything and a missed tick is picked up by the next one.
"""
import logging
import time

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone as dj_timezone

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Run the recurring scheduler loop (minute resolution).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--once', action='store_true',
            help='Run a single tick and exit, instead of looping.',
        )

    def handle(self, *args, **options):
        if options['once']:
            self._tick(dj_timezone.now())
            return

        self.stdout.write('Scheduler started (minute resolution)')
        while True:
            self._sleep_to_next_minute()
            try:
                self._tick(dj_timezone.now())
            except Exception:
                # One bad tick must never take the loop down -- the next minute
                # sweeps up whatever this one failed to do.
                logger.exception('scheduler tick failed')

    @staticmethod
    def _sleep_to_next_minute():
        now = time.time()
        time.sleep(max(1.0, 60.0 - (now % 60.0)))

    def _tick(self, now):
        # This is a long-lived process outside the request cycle, so nothing
        # recycles its DB connections for it.
        close_old_connections()

        if now.minute != 0:
            return

        # ── Hourly ──
        # Auto-expires staff-granted temporary sample access; harmless to run
        # more or less often since it's just a filtered query + revoke.
        call_command('revoke_expired_access')

        # ── Daily, 4am local ──
        # Removes bot signups that never verified their email. Only touches
        # accounts at least a week old, and (by default) without a Navidrome user.
        if dj_timezone.localtime(now).hour == 4:
            call_command('delete_unverified_accounts')
