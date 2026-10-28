import logging

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from artists.navidrome import revoke_library_access
from subscribers.models import SubscriberProfile

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Revoke Navidrome library access for users whose temporary access has expired.'

    def handle(self, *args, **options):
        navidrome_url = getattr(settings, 'NAVIDROME_BASE_URL', '')
        expired = SubscriberProfile.objects.filter(
            temporary_access_until__lte=timezone.now(),
            temporary_access_until__isnull=False,
        ).exclude(
            subscription_status__in=(SubscriberProfile.STATUS_ACTIVE, SubscriberProfile.STATUS_ARTIST)
        ).exclude(user__is_staff=True).select_related('user')

        count = 0
        for profile in expired:
            if navidrome_url and profile.navidrome_user_id:
                try:
                    revoke_library_access(
                        base_url=navidrome_url,
                        admin_user=settings.NAVIDROME_ADMIN_USER,
                        admin_pass=settings.NAVIDROME_ADMIN_PASS,
                        nd_user_id=profile.navidrome_user_id,
                        sample_library_id=settings.NAVIDROME_SAMPLE_LIBRARY_ID,
                    )
                except Exception:
                    logger.exception('Failed to revoke Navidrome access for %s', profile.user.email)
                    profile.navidrome_access_error = True
                    profile.save(update_fields=['navidrome_access_error'])
                    continue
                else:
                    profile.navidrome_access_error = False
                    profile.library_access_granted = False
                    profile.save(update_fields=['navidrome_access_error', 'library_access_granted'])

            profile.temporary_access_until = None
            profile.save(update_fields=['temporary_access_until'])
            count += 1
            self.stdout.write(f'Revoked temporary access for {profile.user.email}')

        self.stdout.write(self.style.SUCCESS(f'Done. Revoked {count} expired temporary access grant(s).'))
