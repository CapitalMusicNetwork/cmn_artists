import logging
from datetime import timedelta

from allauth.account.models import EmailAddress
from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.utils import timezone

from artists.navidrome import delete_navidrome_user

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        'Delete accounts that never verified their email address, along with their '
        'Navidrome users. These are almost always bot signups.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--days', type=int, default=7,
            help='Only delete accounts that signed up at least this many days ago (default 7). '
                 'Confirmation links expire after 3 days, so 7 leaves real people time to confirm.',
        )
        parser.add_argument(
            '--include-navidrome', action='store_true',
            help='Also delete unverified accounts that have a Navidrome user, and that Navidrome user. '
                 'Before Navidrome accounts were deferred until email verification, every signup got one, '
                 'and someone could be streaming with it without ever having logged in here, so these '
                 'are skipped unless you ask for them.',
        )
        parser.add_argument('--dry-run', action='store_true', help='List the accounts without deleting them.')

    def handle(self, *args, days, include_navidrome, dry_run, **options):
        candidates = (
            User.objects
            .filter(date_joined__lte=timezone.now() - timedelta(days=days), last_login__isnull=True)
            .filter(is_staff=False, is_superuser=False, artist_profile__isnull=True)
            .exclude(pk__in=EmailAddress.objects.filter(verified=True).values('user_id'))
            # Anyone who has been to Stripe checkout is a real person; leave them alone.
            .exclude(subscriber_profile__stripe_customer_id__gt='')
            .select_related('subscriber_profile')
            .order_by('date_joined')
        )

        if not include_navidrome:
            candidates = candidates.filter(subscriber_profile__navidrome_user_id='')

        deleted = failed = 0
        for user in candidates:
            if dry_run:
                nd_user_id = getattr(getattr(user, 'subscriber_profile', None), 'navidrome_user_id', '')
                navidrome = f', Navidrome user {nd_user_id}' if nd_user_id else ''
                self.stdout.write(f'[dry run] would delete {user.email} (joined {user.date_joined:%Y-%m-%d}{navidrome})')
                deleted += 1
                continue
            if not self._delete_navidrome_user(user):
                failed += 1
                continue
            user.delete()
            deleted += 1
            logger.info('Deleted unverified account %s (joined %s)', user.email, user.date_joined)
            self.stdout.write(f'Deleted {user.email}')

        prefix = '[dry run] would delete' if dry_run else 'Deleted'
        summary = f'{prefix} {deleted} unverified account(s).'
        if failed:
            summary += f' Skipped {failed} whose Navidrome user could not be deleted; they will be retried next run.'
        self.stdout.write(self.style.SUCCESS(summary))

    def _delete_navidrome_user(self, user):
        """Returns False if the account must be kept so the Navidrome user isn't orphaned."""
        profile = getattr(user, 'subscriber_profile', None)
        nd_user_id = profile.navidrome_user_id if profile else ''
        if not nd_user_id:
            return True
        if not settings.NAVIDROME_BASE_URL:
            self.stdout.write(self.style.WARNING(
                f'{user.email}: has Navidrome user {nd_user_id} but NAVIDROME_BASE_URL is not set'
            ))
            return False
        try:
            delete_navidrome_user(
                base_url=settings.NAVIDROME_BASE_URL,
                admin_user=settings.NAVIDROME_ADMIN_USER,
                admin_pass=settings.NAVIDROME_ADMIN_PASS,
                nd_user_id=nd_user_id,
            )
        except Exception:
            logger.exception('Failed to delete Navidrome user %s for %s', nd_user_id, user.email)
            return False
        return True
