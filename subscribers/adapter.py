import logging

from allauth.account.adapter import DefaultAccountAdapter
from django.conf import settings
from django.contrib import messages

logger = logging.getLogger(__name__)


class NavidromeAccountAdapter(DefaultAccountAdapter):
    """Keeps each user's Navidrome password in sync with their password here.

    Navidrome needs the plaintext password, which we only see while someone is
    typing it, so we hook the places allauth handles it. The Navidrome account is
    created at the first login after the email address is verified, not at
    signup, so bot signups that never confirm their email don't leave working
    Navidrome logins behind.
    """

    def authenticate(self, request, **credentials):
        user = super().authenticate(request, **credentials)
        password = credentials.get('password')
        if user and password:
            profile = _subscriber_profile(user)
            if profile and not profile.navidrome_user_id and _email_verified(user):
                self._sync_navidrome(user, password, request)
        return user

    def set_password(self, user, password):
        # Called by allauth for password change and password reset flows.
        # Note: allauth calls get_adapter() without a request here, so self.request is None.
        super().set_password(user, password)
        profile = _subscriber_profile(user)
        if profile and (profile.navidrome_user_id or _email_verified(user)):
            self._sync_navidrome(user, password, request=None)

    def _sync_navidrome(self, user, password, request):
        from artists.navidrome import create_navidrome_user, update_navidrome_password
        from subscribers.models import SubscriberProfile

        try:
            profile = SubscriberProfile.objects.get(user=user)
        except SubscriberProfile.DoesNotExist:
            logger.warning('No SubscriberProfile for %s during Navidrome sync', user.email)
            return

        is_new_account = not profile.navidrome_user_id
        try:
            navidrome_url = settings.NAVIDROME_BASE_URL
            if not navidrome_url:
                raise RuntimeError('NAVIDROME_BASE_URL is not configured')

            if profile.navidrome_user_id:
                update_navidrome_password(
                    base_url=navidrome_url,
                    admin_user=settings.NAVIDROME_ADMIN_USER,
                    admin_pass=settings.NAVIDROME_ADMIN_PASS,
                    nd_user_id=profile.navidrome_user_id,
                    new_password=password,
                )
            else:
                nd_user_id = create_navidrome_user(
                    base_url=navidrome_url,
                    admin_user=settings.NAVIDROME_ADMIN_USER,
                    admin_pass=settings.NAVIDROME_ADMIN_PASS,
                    username=user.email,
                    display_name=user.email.split('@')[0],
                    email=user.email,
                    password=password,
                )
                profile.navidrome_user_id = nd_user_id
                profile.save(update_fields=['navidrome_user_id'])
        except Exception:
            logger.exception('Failed to sync Navidrome for %s', user.email)
            if is_new_account and request:
                messages.error(
                    request,
                    'You\'re logged in, but we could not set up your streaming account. '
                    'Please contact us so we can sort it out.'
                )


def _subscriber_profile(user):
    from subscribers.models import SubscriberProfile

    return SubscriberProfile.objects.filter(user=user).first()


def _email_verified(user):
    from allauth.account.models import EmailAddress

    return EmailAddress.objects.filter(user=user, verified=True).exists()
