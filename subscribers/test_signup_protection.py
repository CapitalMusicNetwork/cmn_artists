import unittest.mock as mock
from datetime import timedelta
from io import StringIO

import altcha
from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from artists.models import ArtistProfile
from . import signup_protection
from .models import UsedAltchaChallenge

STATIC = override_settings(STATICFILES_STORAGE='django.contrib.staticfiles.storage.StaticFilesStorage')
NAVIDROME = override_settings(
    NAVIDROME_BASE_URL='https://navidrome.test', NAVIDROME_ADMIN_USER='admin', NAVIDROME_ADMIN_PASS='pw',
)
PASSWORD = 'correct-horse-battery-staple'


def solved_payload(challenge=None):
    """What the ALTCHA widget would submit after solving a challenge we issued."""
    challenge = altcha.Challenge.from_dict(challenge or signup_protection.create_challenge())
    solution = altcha.solve_challenge(challenge)
    return altcha.Payload(challenge, solution).to_base64()


def signup_data(email='new@example.com', **extra):
    return {'email': email, 'password1': PASSWORD, 'password2': PASSWORD, **extra}


@STATIC
class SignupBotProtectionTests(TestCase):
    def signup(self, **data):
        return self.client.post('/accounts/signup/', signup_data(**data))

    def test_signup_page_includes_widget_and_honeypot(self):
        response = self.client.get('/accounts/signup/')
        self.assertContains(response, '<altcha-widget challenge="/accounts/altcha-challenge/"')
        self.assertContains(response, 'name="website"')

    def test_challenge_endpoint_returns_signed_uncached_challenge(self):
        response = self.client.get('/accounts/altcha-challenge/')
        body = response.json()
        self.assertEqual(body['parameters']['algorithm'], 'PBKDF2/SHA-256')
        self.assertTrue(body['signature'])
        self.assertIn('no-cache', response['Cache-Control'])

    def test_signup_with_solved_challenge_creates_account(self):
        response = self.signup(altcha=solved_payload())
        self.assertRedirects(response, '/accounts/confirm-email/', fetch_redirect_response=False)
        self.assertTrue(User.objects.filter(email='new@example.com').exists())

    def test_signup_without_challenge_is_rejected(self):
        response = self.signup()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'robot')
        self.assertFalse(User.objects.filter(email='new@example.com').exists())

    def test_signup_with_tampered_challenge_is_rejected(self):
        challenge = signup_protection.create_challenge()
        challenge['parameters']['cost'] = 1  # make it trivially cheap; breaks the signature
        self.signup(altcha=solved_payload(challenge))
        self.assertFalse(User.objects.filter(email='new@example.com').exists())

    def test_signup_with_unsigned_challenge_is_rejected(self):
        challenge = altcha.create_challenge('PBKDF2/SHA-256', 1).to_dict()
        self.signup(altcha=solved_payload(challenge))
        self.assertFalse(User.objects.filter(email='new@example.com').exists())

    def test_signup_with_garbage_payload_is_rejected(self):
        self.signup(altcha='not-base64!!')
        self.assertFalse(User.objects.filter(email='new@example.com').exists())

    def test_solved_challenge_cannot_be_replayed(self):
        payload = solved_payload()
        self.signup(email='first@example.com', altcha=payload)
        self.signup(email='second@example.com', altcha=payload)
        self.assertTrue(User.objects.filter(email='first@example.com').exists())
        self.assertFalse(User.objects.filter(email='second@example.com').exists())

    def test_expired_used_challenges_are_pruned(self):
        UsedAltchaChallenge.objects.create(nonce='old', expires_at=timezone.now() - timedelta(minutes=1))
        self.signup(altcha=solved_payload())
        self.assertFalse(UsedAltchaChallenge.objects.filter(nonce='old').exists())

    def test_filled_honeypot_is_rejected(self):
        self.signup(altcha=solved_payload(), website='http://spam.example')
        self.assertFalse(User.objects.filter(email='new@example.com').exists())


@STATIC
@NAVIDROME
class NavidromeAccountTimingTests(TestCase):
    def setUp(self):
        patcher = mock.patch('artists.navidrome.create_navidrome_user', return_value='nd-new')
        self.create_navidrome_user = patcher.start()
        self.addCleanup(patcher.stop)

    def make_user(self, verified):
        user = User.objects.create_user(username='person@example.com', email='person@example.com', password=PASSWORD)
        EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=verified)
        return user

    def log_in(self):
        return self.client.post('/accounts/login/', {'login': 'person@example.com', 'password': PASSWORD})

    def test_signup_does_not_create_navidrome_user(self):
        self.client.post('/accounts/signup/', signup_data(altcha=solved_payload()))
        self.assertTrue(User.objects.filter(email='new@example.com').exists())
        self.create_navidrome_user.assert_not_called()

    def test_first_verified_login_creates_navidrome_user(self):
        user = self.make_user(verified=True)
        self.log_in()
        self.create_navidrome_user.assert_called_once()
        self.assertEqual(self.create_navidrome_user.call_args.kwargs['password'], PASSWORD)
        user.subscriber_profile.refresh_from_db()
        self.assertEqual(user.subscriber_profile.navidrome_user_id, 'nd-new')

    def test_unverified_login_does_not_create_navidrome_user(self):
        self.make_user(verified=False)
        self.log_in()
        self.create_navidrome_user.assert_not_called()

    def test_login_with_existing_navidrome_user_does_nothing(self):
        user = self.make_user(verified=True)
        user.subscriber_profile.navidrome_user_id = 'nd-existing'
        user.subscriber_profile.save()
        with mock.patch('artists.navidrome.update_navidrome_password') as update:
            self.log_in()
        self.create_navidrome_user.assert_not_called()
        update.assert_not_called()

    def test_wrong_password_does_not_create_navidrome_user(self):
        self.make_user(verified=True)
        self.client.post('/accounts/login/', {'login': 'person@example.com', 'password': 'wrong'})
        self.create_navidrome_user.assert_not_called()

    def test_failed_navidrome_creation_still_logs_in_and_warns(self):
        self.make_user(verified=True)
        self.create_navidrome_user.side_effect = RuntimeError('navidrome down')
        response = self.log_in()
        self.assertRedirects(response, '/dashboard/', fetch_redirect_response=False)
        messages = [str(m) for m in response.wsgi_request._messages]
        self.assertTrue(any('could not set up your streaming account' in m for m in messages))


@NAVIDROME
class DeleteUnverifiedAccountsTests(TestCase):
    def setUp(self):
        command = 'subscribers.management.commands.delete_unverified_accounts'
        for name in ('admin_session', 'delete_navidrome_user'):
            patcher = mock.patch(f'{command}.{name}')
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)

    def make_user(self, email, days_ago=10, verified=False, navidrome_user_id='', **fields):
        user = User.objects.create(
            username=email, email=email, date_joined=timezone.now() - timedelta(days=days_ago), **fields,
        )
        EmailAddress.objects.create(user=user, email=email, primary=True, verified=verified)
        if navidrome_user_id:
            user.subscriber_profile.navidrome_user_id = navidrome_user_id
            user.subscriber_profile.save()
        return user

    def run_command(self, *args):
        out = StringIO()
        call_command('delete_unverified_accounts', *args, stdout=out)
        return out.getvalue()

    def remaining(self):
        return set(User.objects.values_list('email', flat=True))

    def test_deletes_only_old_unverified_accounts(self):
        self.make_user('bot@example.com')
        self.make_user('recent@example.com', days_ago=2)
        self.make_user('verified@example.com', verified=True)
        self.make_user('loggedin@example.com', last_login=timezone.now())
        self.make_user('staff@example.com', is_staff=True)
        paying = self.make_user('paying@example.com')
        paying.subscriber_profile.stripe_customer_id = 'cus_123'
        paying.subscriber_profile.save()
        applicant = self.make_user('artist@example.com')
        ArtistProfile.objects.create(user=applicant, display_name='Applicant')

        self.run_command()

        self.assertEqual(self.remaining(), {
            'recent@example.com', 'verified@example.com', 'loggedin@example.com',
            'staff@example.com', 'paying@example.com', 'artist@example.com',
        })

    def test_skips_accounts_with_navidrome_users_by_default(self):
        self.make_user('legacy@example.com', navidrome_user_id='nd1')
        self.run_command()
        self.assertIn('legacy@example.com', self.remaining())
        self.delete_navidrome_user.assert_not_called()

    def test_include_navidrome_deletes_navidrome_user_too(self):
        self.make_user('legacy@example.com', navidrome_user_id='nd1')
        self.run_command('--include-navidrome')
        self.assertNotIn('legacy@example.com', self.remaining())
        self.assertEqual(self.delete_navidrome_user.call_args.kwargs['nd_user_id'], 'nd1')

    def test_keeps_account_if_navidrome_delete_fails(self):
        self.make_user('legacy@example.com', navidrome_user_id='nd1')
        self.delete_navidrome_user.side_effect = RuntimeError('navidrome down')
        output = self.run_command('--include-navidrome')
        self.assertIn('legacy@example.com', self.remaining())
        self.assertIn('Skipped 1', output)

    def test_logs_in_to_navidrome_once_per_run(self):
        self.make_user('bot1@example.com', navidrome_user_id='nd1')
        self.make_user('bot2@example.com', navidrome_user_id='nd2')
        self.make_user('bot3@example.com', navidrome_user_id='nd3')
        self.run_command('--include-navidrome')
        self.admin_session.assert_called_once()
        self.assertEqual(self.delete_navidrome_user.call_count, 3)
        self.assertEqual(self.remaining(), set())

    def test_failed_navidrome_login_is_not_retried_per_account(self):
        self.make_user('bot1@example.com', navidrome_user_id='nd1')
        self.make_user('bot2@example.com', navidrome_user_id='nd2')
        self.make_user('plain@example.com')
        self.admin_session.side_effect = RuntimeError('429 Too Many Requests')
        output = self.run_command('--include-navidrome')
        self.admin_session.assert_called_once()
        self.delete_navidrome_user.assert_not_called()
        self.assertEqual(self.remaining(), {'bot1@example.com', 'bot2@example.com'})
        self.assertIn('Skipped 2', output)

    def test_dry_run_deletes_nothing(self):
        self.make_user('bot@example.com', navidrome_user_id='nd1')
        output = self.run_command('--include-navidrome', '--dry-run')
        self.assertIn('would delete bot@example.com', output)
        self.assertIn('bot@example.com', self.remaining())
        self.delete_navidrome_user.assert_not_called()

    def test_days_option(self):
        self.make_user('bot@example.com', days_ago=2)
        self.run_command('--days', '1')
        self.assertNotIn('bot@example.com', self.remaining())
