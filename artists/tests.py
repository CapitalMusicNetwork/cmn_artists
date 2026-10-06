from decimal import Decimal

from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase, override_settings

from .agreement import get_artist_agreement_text
from .models import ArtistProfile


class DashboardRedirectTests(TestCase):
    def test_staff_without_artist_profile_is_redirected_to_money(self):
        staff = User.objects.create_user(username='staff@example.com', email='staff@example.com', is_staff=True)
        self.client.force_login(staff)

        response = self.client.get('/dashboard/')

        self.assertRedirects(response, '/subscribe/admin/money/', fetch_redirect_response=False)

    def test_non_staff_without_artist_profile_is_redirected_to_subscriber_dashboard(self):
        user = User.objects.create_user(username='regular@example.com', email='regular@example.com')
        self.client.force_login(user)

        response = self.client.get('/dashboard/')

        self.assertRedirects(response, '/subscribe/dashboard/', fetch_redirect_response=False)

    @override_settings(STATICFILES_STORAGE='django.contrib.staticfiles.storage.StaticFilesStorage')
    def test_staff_who_are_approved_artists_still_see_artist_dashboard(self):
        staff = User.objects.create_user(username='staffartist@example.com', email='staffartist@example.com', is_staff=True)
        ArtistProfile.objects.create(
            user=staff, display_name='Staff Artist',
            status=ArtistProfile.STATUS_APPROVED, tutorial_seen=True,
        )
        self.client.force_login(staff)

        response = self.client.get('/dashboard/')

        self.assertEqual(response.status_code, 200)


class ArtistApplicationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='applicant@example.com', email='applicant@example.com')
        self.client.force_login(self.user)

    @override_settings(STATICFILES_STORAGE='django.contrib.staticfiles.storage.StaticFilesStorage')
    def test_application_page_shows_artist_agreement_text(self):
        response = self.client.get('/profile/apply/')

        self.assertContains(response, get_artist_agreement_text())

    @override_settings(STATICFILES_STORAGE='django.contrib.staticfiles.storage.StaticFilesStorage')
    def test_submitting_without_agreeing_to_terms_is_rejected(self):
        response = self.client.post('/profile/apply/', {
            'display_name': 'Test Artist',
            'note': '',
        })

        self.assertEqual(response.status_code, 200)
        self.assertFalse(ArtistProfile.objects.filter(user=self.user).exists())
        self.assertFormError(response.context['form'], 'agree_to_terms', 'You must agree to the Artist Agreement to submit your application.')

    def test_submitting_with_agreement_creates_pending_application(self):
        response = self.client.post('/profile/apply/', {
            'display_name': 'Test Artist',
            'note': '',
            'agree_to_terms': 'on',
        })

        self.assertRedirects(response, '/dashboard/', fetch_redirect_response=False)
        profile = ArtistProfile.objects.get(user=self.user)
        self.assertEqual(profile.status, ArtistProfile.STATUS_PENDING)


class ArtistApplicationApprovalEmailTests(TestCase):
    def test_approval_email_includes_artist_agreement_text(self):
        staff = User.objects.create_user(username='staff@example.com', email='staff@example.com', is_staff=True)
        applicant = User.objects.create_user(username='applicant2@example.com', email='applicant2@example.com')
        profile = ArtistProfile.objects.create(
            user=applicant, display_name='Test Artist', status=ArtistProfile.STATUS_PENDING,
        )
        self.client.force_login(staff)

        self.client.post(f'/admin-panel/applications/{profile.pk}/approve/')

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(get_artist_agreement_text(), mail.outbox[0].body)


class ApprovedAtTests(TestCase):
    def make_pending(self, email='applicant3@example.com'):
        user = User.objects.create_user(username=email, email=email)
        return ArtistProfile.objects.create(user=user, display_name='Pending', status=ArtistProfile.STATUS_PENDING)

    def test_approve_view_sets_approved_at(self):
        staff = User.objects.create_user(username='staff@example.com', email='staff@example.com', is_staff=True)
        profile = self.make_pending()
        self.assertIsNone(profile.approved_at)
        self.client.force_login(staff)

        self.client.post(f'/admin-panel/applications/{profile.pk}/approve/')

        profile.refresh_from_db()
        self.assertIsNotNone(profile.approved_at)

    def test_resaving_approved_artist_keeps_original_approval_date(self):
        profile = self.make_pending()
        profile.status = ArtistProfile.STATUS_APPROVED
        profile.save()
        approved_at = profile.approved_at

        profile = ArtistProfile.objects.get(pk=profile.pk)
        profile.bio = 'updated'
        profile.save()

        profile.refresh_from_db()
        self.assertEqual(profile.approved_at, approved_at)

    def test_declining_does_not_set_approved_at(self):
        profile = self.make_pending()
        profile.status = ArtistProfile.STATUS_DECLINED
        profile.save()
        self.assertIsNone(profile.approved_at)

    def test_creating_an_approved_artist_sets_approved_at(self):
        user = User.objects.create_user(username='direct@example.com', email='direct@example.com')
        profile = ArtistProfile.objects.create(user=user, display_name='Direct', status=ArtistProfile.STATUS_APPROVED)
        self.assertIsNotNone(profile.approved_at)


@override_settings(STATICFILES_STORAGE='django.contrib.staticfiles.storage.StaticFilesStorage')
class ProfilePayoutTests(TestCase):
    def test_profile_shows_this_months_owed_amount_and_pool_share(self):
        from subscribers.models import SubscriberProfile

        user = User.objects.create_user(username='artist@example.com', email='artist@example.com')
        artist = ArtistProfile.objects.create(user=user, display_name='Artist', status=ArtistProfile.STATUS_APPROVED)
        other_user = User.objects.create_user(username='other@example.com', email='other@example.com')
        ArtistProfile.objects.create(user=other_user, display_name='Other', status=ArtistProfile.STATUS_APPROVED)

        for email, designated in (('fan@example.com', artist), ('orphan@example.com', None)):
            subscriber = User.objects.create_user(username=email, email=email).subscriber_profile
            subscriber.designated_artist = designated
            subscriber.subscription_status = SubscriberProfile.STATUS_ACTIVE
            subscriber.save()

        self.client.force_login(user)
        response = self.client.get('/profile/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['month_new_count'], 1)
        self.assertEqual(response.context['month_owed'], Decimal('36.00'))
        self.assertEqual(response.context['pool_ytd'], Decimal('18.00'))
