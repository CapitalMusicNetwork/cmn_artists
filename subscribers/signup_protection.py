"""Self-hosted ALTCHA proof-of-work check for the signup form.

The browser fetches a challenge from `challenge_view`, the ALTCHA widget
(static/vendor/altcha/) solves it in a web worker while the person fills in the
form, and the solution comes back in the form's `altcha` field. Nothing here
talks to an outside service: challenges are HMAC-signed with a key derived from
SECRET_KEY, so any gunicorn worker can verify one another worker issued.
"""
import logging
from datetime import timedelta

import altcha
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.views.decorators.cache import never_cache

logger = logging.getLogger(__name__)

ALGORITHM = 'PBKDF2/SHA-256'
# PBKDF2 iterations per attempt. With the default 1-byte key prefix the widget
# needs ~256 attempts on average: well under a second on a desktop browser,
# a few seconds on a slow phone (done while they type), and one cheap
# derivation for us to verify.
COST = 5000
# Long enough to fill in the form; the widget fetches a fresh challenge itself
# when one expires.
EXPIRES_AFTER = timedelta(minutes=20)


def _hmac_secret():
    return salted_hmac('subscribers.altcha', 'challenge-signing').hexdigest()


def create_challenge():
    challenge = altcha.create_challenge(
        ALGORITHM,
        COST,
        expires_at=timezone.now() + EXPIRES_AFTER,
        hmac_secret=_hmac_secret(),
    )
    return challenge.to_dict()


@never_cache
def challenge_view(request):
    return JsonResponse(create_challenge())


def verify(payload):
    """Return True if `payload` (the widget's base64 form value) solves a challenge
    we issued, hasn't expired, and hasn't been used before."""
    if not payload:
        return False
    try:
        result = altcha.verify_solution(payload, _hmac_secret())
    except Exception:
        # Garbage that gets past the library's own parsing (e.g. non-hex nonce).
        logger.warning('Unparseable ALTCHA payload', exc_info=True)
        return False
    if not result.verified:
        return False

    # A solved payload stays valid until it expires, so record each one we accept
    # to stop a bot solving once and replaying it for every signup.
    from .models import UsedAltchaChallenge

    nonce = altcha.Payload.from_base64(payload).challenge.parameters.nonce
    now = timezone.now()
    UsedAltchaChallenge.objects.filter(expires_at__lt=now).delete()
    try:
        with transaction.atomic():
            UsedAltchaChallenge.objects.create(nonce=nonce, expires_at=now + EXPIRES_AFTER)
    except IntegrityError:
        return False
    return True
