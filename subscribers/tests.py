from datetime import datetime
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone

from artists.models import ArtistProfile
from .forms import DesignatedArtistForm
from .models import ArtistDesignationChange, SubscriberProfile, SubscriptionPayment, SubscriptionStatusChange
from .payouts import attribution_status
from .views import StripeWebhookView


def make_artist(email='artist@example.com', display_name='QA Test Artist', status=ArtistProfile.STATUS_APPROVED):
    user = User.objects.create(email=email, username=email)
    return ArtistProfile.objects.create(user=user, display_name=display_name, status=status)


def make_subscriber(email='subscriber@example.com', is_staff=False):
    user = User.objects.create(email=email, username=email, is_staff=is_staff)
    return user.subscriber_profile


@override_settings(STATICFILES_STORAGE='django.contrib.staticfiles.storage.StaticFilesStorage')
class AdminUserListViewTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(username='staff@example.com', email='staff@example.com', is_staff=True)
        self.client.force_login(self.staff)

    def test_active_subscriber_not_renewing_is_flagged(self):
        profile = make_subscriber('notrenewing@example.com')
        profile.subscription_status = SubscriberProfile.STATUS_ACTIVE
        profile.cancel_at_period_end = True
        profile.save()

        response = self.client.get('/subscribe/admin/users/')

        self.assertContains(response, 'not renewing')

    def test_active_subscriber_renewing_is_not_flagged(self):
        profile = make_subscriber('renewing@example.com')
        profile.subscription_status = SubscriberProfile.STATUS_ACTIVE
        profile.save()

        response = self.client.get('/subscribe/admin/users/')

        self.assertNotContains(response, 'not renewing')


class StaffLibraryAccessTests(TestCase):
    def test_staff_have_library_access_regardless_of_subscription_status(self):
        profile = make_subscriber('staffer@example.com', is_staff=True)
        self.assertEqual(profile.subscription_status, SubscriberProfile.STATUS_PENDING)
        self.assertTrue(profile.should_have_library_access)

    def test_non_staff_pending_subscriber_has_no_library_access(self):
        profile = make_subscriber('regular@example.com', is_staff=False)
        self.assertFalse(profile.should_have_library_access)


class SubscriptionStatusHistoryTests(TestCase):
    def test_status_change_is_recorded(self):
        profile = make_subscriber()
        profile.subscription_status = SubscriberProfile.STATUS_ACTIVE
        profile.save()
        profile.subscription_status = SubscriberProfile.STATUS_CANCELED
        profile.save()

        self.assertEqual(
            list(profile.status_history.order_by('changed_at').values_list('status', flat=True)),
            [SubscriberProfile.STATUS_ACTIVE, SubscriberProfile.STATUS_CANCELED],
        )

    def test_saving_without_status_change_does_not_record(self):
        profile = make_subscriber()
        profile.subscription_status = SubscriberProfile.STATUS_ACTIVE
        profile.save()
        profile.save()  # no-op change

        self.assertEqual(profile.status_history.count(), 1)

    def test_creating_a_profile_does_not_record_history(self):
        profile = make_subscriber()
        self.assertEqual(profile.status_history.count(), 0)


class ArtistDesignationHistoryTests(TestCase):
    def test_designating_an_artist_is_recorded(self):
        profile = make_subscriber()
        artist = make_artist()

        profile.designated_artist = artist
        profile.save()

        self.assertEqual(
            list(profile.artist_designation_history.values_list('artist', flat=True)),
            [artist.pk],
        )

    def test_clearing_designation_is_recorded_with_null_artist(self):
        profile = make_subscriber()
        artist = make_artist()
        profile.designated_artist = artist
        profile.save()

        profile.designated_artist = None
        profile.save()

        self.assertEqual(
            list(profile.artist_designation_history.order_by('changed_at').values_list('artist', flat=True)),
            [artist.pk, None],
        )

    def test_deleting_designated_artist_nulls_the_fk_without_error(self):
        profile = make_subscriber()
        artist = make_artist()
        profile.designated_artist = artist
        profile.save()

        artist.delete()

        profile.refresh_from_db()
        self.assertIsNone(profile.designated_artist)


class DesignatedArtistFormTests(TestCase):
    def test_valid_case_insensitive_artist_name_is_accepted(self):
        profile = make_subscriber()
        make_artist(display_name='QA Test Artist')

        form = DesignatedArtistForm({'artist_name': 'qa test artist'}, subscriber=profile)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()

        profile.refresh_from_db()
        self.assertEqual(profile.designated_artist.display_name, 'QA Test Artist')

    def test_unknown_artist_name_is_rejected(self):
        profile = make_subscriber()
        form = DesignatedArtistForm({'artist_name': 'Not A Real Artist'}, subscriber=profile)
        self.assertFalse(form.is_valid())

    def test_unapproved_artist_name_is_rejected(self):
        profile = make_subscriber()
        make_artist(display_name='Pending Artist', status=ArtistProfile.STATUS_PENDING)

        form = DesignatedArtistForm({'artist_name': 'Pending Artist'}, subscriber=profile)
        self.assertFalse(form.is_valid())

    def test_blank_name_clears_designation(self):
        profile = make_subscriber()
        artist = make_artist()
        profile.designated_artist = artist
        profile.save()

        form = DesignatedArtistForm({'artist_name': ''}, subscriber=profile)
        self.assertTrue(form.is_valid())
        form.save()

        profile.refresh_from_db()
        self.assertIsNone(profile.designated_artist)


class ArtistSupporterCountTests(TestCase):
    def test_only_active_subscribers_are_counted(self):
        artist = make_artist()

        active = make_subscriber('active@example.com')
        active.designated_artist = artist
        active.subscription_status = SubscriberProfile.STATUS_ACTIVE
        active.save()

        canceled = make_subscriber('canceled@example.com')
        canceled.designated_artist = artist
        canceled.subscription_status = SubscriberProfile.STATUS_CANCELED
        canceled.save()

        self.assertEqual(artist.supporter_count, 1)

    def test_no_supporters(self):
        artist = make_artist()
        self.assertEqual(artist.supporter_count, 0)


def at(year, month, day):
    return timezone.make_aware(datetime(year, month, day, 12))


def backdate_history(subscriber, when):
    """Move every history record created so far for this subscriber to `when`."""
    SubscriptionStatusChange.objects.filter(subscriber=subscriber, changed_at__gt=when).update(changed_at=when)
    ArtistDesignationChange.objects.filter(subscriber=subscriber, changed_at__gt=when).update(changed_at=when)


def subscribe(email, when, artist=None):
    subscriber = make_subscriber(email)
    if artist is not None:
        subscriber.designated_artist = artist
    subscriber.subscription_status = SubscriberProfile.STATUS_ACTIVE
    subscriber.save()
    backdate_history(subscriber, when)
    return subscriber


_invoice_counter = 0


def renew(subscriber, when, amount_paid=4000):
    global _invoice_counter
    _invoice_counter += 1
    return SubscriptionPayment.objects.create(
        subscriber=subscriber,
        stripe_invoice_id=f'in_test_{_invoice_counter}',
        billing_reason=SubscriptionPayment.BILLING_REASON_CYCLE,
        amount_paid=amount_paid,
        paid_at=when,
    )


@override_settings(
    STATICFILES_STORAGE='django.contrib.staticfiles.storage.StaticFilesStorage',
    STRIPE_SUBSCRIPTION_PRICE_ID='',  # keep these tests off the network; price tests opt back in
)
class AdminMoneyViewTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(username='staff@example.com', email='staff@example.com', is_staff=True)
        self.artist_a = make_artist('artist_a@example.com', 'Artist A')
        self.artist_b = make_artist('artist_b@example.com', 'Artist B')
        ArtistProfile.objects.update(approved_at=at(2025, 1, 1))

    def get_month(self, month=None):
        self.client.force_login(self.staff)
        url = '/subscribe/admin/money/' + (f'?month={month}' if month else '')
        return self.client.get(url)

    def rows_by_name(self, response):
        return {row['artist'].display_name: row for row in response.context['artist_rows']}

    def test_requires_staff(self):
        non_staff = make_subscriber('nonstaff@example.com').user
        self.client.force_login(non_staff)
        response = self.client.get('/subscribe/admin/money/')
        self.assertEqual(response.status_code, 403)

    def test_attributed_subscribers_pay_full_36_in_their_month(self):
        subscribe('s1@example.com', at(2026, 3, 5), self.artist_a)
        subscribe('s2@example.com', at(2026, 3, 20), self.artist_a)
        subscribe('s3@example.com', at(2026, 4, 2), self.artist_b)  # different month

        response = self.get_month('2026-03')
        ctx = response.context
        self.assertEqual(ctx['new_count'], 2)
        self.assertEqual(ctx['unattributed_count'], 0)
        self.assertEqual(ctx['gross_total'], Decimal('80.00'))
        self.assertEqual(ctx['cmn_total'], Decimal('8.00'))
        self.assertEqual(ctx['attributed_total'], Decimal('72.00'))

        rows = self.rows_by_name(response)
        self.assertEqual(rows['Artist A']['new_count'], 2)
        self.assertEqual(rows['Artist A']['attributed_amount'], Decimal('72.00'))
        self.assertEqual(rows['Artist B']['attributed_amount'], Decimal('0.00'))

    def test_unattributed_subscribers_pooled_across_artists(self):
        subscribe('s1@example.com', at(2026, 3, 5), self.artist_a)
        subscribe('orphan@example.com', at(2026, 3, 6))

        ctx = self.get_month('2026-03').context
        self.assertEqual(ctx['new_count'], 2)
        self.assertEqual(ctx['unattributed_count'], 1)
        self.assertEqual(ctx['pool_total'], Decimal('36.00'))
        self.assertEqual(ctx['pool_share'], Decimal('18.00'))

        rows = {row['artist'].display_name: row for row in ctx['artist_rows']}
        self.assertEqual(rows['Artist A']['attributed_amount'], Decimal('36.00'))
        self.assertEqual(rows['Artist A']['pool_share'], Decimal('18.00'))
        self.assertEqual(rows['Artist B']['pool_share'], Decimal('18.00'))

    def test_pool_only_includes_artists_approved_by_month_end(self):
        late = make_artist('late@example.com', 'Late Artist')
        ArtistProfile.objects.filter(pk=late.pk).update(created_at=at(2025, 1, 1), approved_at=at(2026, 5, 1))
        make_artist('pending@example.com', 'Pending Artist', status=ArtistProfile.STATUS_PENDING)
        subscribe('orphan@example.com', at(2026, 3, 6))

        ctx = self.get_month('2026-03').context
        self.assertEqual(ctx['pool_artist_count'], 2)
        self.assertEqual(ctx['pool_share'], Decimal('18.00'))
        self.assertNotIn('Late Artist', {row['artist'].display_name for row in ctx['artist_rows']})

    def test_pool_share_accumulates_year_to_date(self):
        subscribe('jan@example.com', at(2026, 1, 10))
        subscribe('mar@example.com', at(2026, 3, 10))
        subscribe('lastyear@example.com', at(2025, 12, 10))

        rows = self.rows_by_name(self.get_month('2026-03'))
        self.assertEqual(rows['Artist A']['pool_share'], Decimal('18.00'))
        self.assertEqual(rows['Artist A']['pool_ytd'], Decimal('36.00'))

    def test_designation_at_month_end_is_used(self):
        subscriber = subscribe('later@example.com', at(2026, 3, 5))
        subscriber.designated_artist = self.artist_b
        subscriber.save()
        ArtistDesignationChange.objects.filter(subscriber=subscriber).update(changed_at=at(2026, 3, 25))

        ctx = self.get_month('2026-03').context
        self.assertEqual(ctx['unattributed_count'], 0)
        self.assertEqual(self.rows_by_name(self.get_month('2026-03'))['Artist B']['attributed_amount'], Decimal('36.00'))

    def test_designation_after_month_end_counts_as_unattributed(self):
        subscriber = subscribe('later@example.com', at(2026, 3, 5))
        subscriber.designated_artist = self.artist_b
        subscriber.save()
        ArtistDesignationChange.objects.filter(subscriber=subscriber).update(changed_at=at(2026, 4, 2))

        ctx = self.get_month('2026-03').context
        self.assertEqual(ctx['unattributed_count'], 1)

    def test_past_due_recovery_is_not_a_new_subscriber(self):
        subscriber = subscribe('s1@example.com', at(2026, 1, 5), self.artist_a)
        subscriber.subscription_status = SubscriberProfile.STATUS_PAST_DUE
        subscriber.save()
        backdate_history(subscriber, at(2026, 3, 1))
        subscriber.subscription_status = SubscriberProfile.STATUS_ACTIVE
        subscriber.save()
        backdate_history(subscriber, at(2026, 3, 3))

        self.assertEqual(self.get_month('2026-03').context['new_count'], 0)

    def test_resubscribe_after_cancel_is_a_new_subscriber(self):
        subscriber = subscribe('s1@example.com', at(2025, 1, 5), self.artist_a)
        subscriber.subscription_status = SubscriberProfile.STATUS_CANCELED
        subscriber.save()
        backdate_history(subscriber, at(2026, 1, 5))
        subscriber.subscription_status = SubscriberProfile.STATUS_ACTIVE
        subscriber.save()
        backdate_history(subscriber, at(2026, 3, 3))

        self.assertEqual(self.get_month('2026-03').context['new_count'], 1)

    def test_attributed_renewal_is_owed_in_renewal_month(self):
        subscriber = subscribe('s1@example.com', at(2025, 3, 5), self.artist_a)
        renew(subscriber, at(2026, 3, 5))

        response = self.get_month('2026-03')
        ctx = response.context
        self.assertEqual(ctx['new_count'], 0)
        self.assertEqual(ctx['renewal_count'], 1)
        self.assertEqual(ctx['gross_total'], Decimal('40.00'))
        self.assertEqual(ctx['attributed_total'], Decimal('36.00'))

        row = self.rows_by_name(response)['Artist A']
        self.assertEqual(row['new_count'], 0)
        self.assertEqual(row['renewal_count'], 1)
        self.assertEqual(row['attributed_amount'], Decimal('36.00'))

    def test_unattributed_renewal_goes_to_pool(self):
        subscriber = subscribe('orphan@example.com', at(2025, 3, 5))
        renew(subscriber, at(2026, 3, 5))

        ctx = self.get_month('2026-03').context
        self.assertEqual(ctx['renewal_unattributed_count'], 1)
        self.assertEqual(ctx['pool_total'], Decimal('36.00'))
        self.assertEqual(ctx['pool_share'], Decimal('18.00'))

    def test_first_payment_and_zero_dollar_renewal_are_not_counted_as_renewals(self):
        subscriber = subscribe('s1@example.com', at(2025, 3, 5), self.artist_a)
        SubscriptionPayment.objects.create(
            subscriber=subscriber, stripe_invoice_id='in_first',
            billing_reason=SubscriptionPayment.BILLING_REASON_CREATE, amount_paid=4000, paid_at=at(2026, 3, 5),
        )
        renew(subscriber, at(2026, 3, 6), amount_paid=0)

        self.assertEqual(self.get_month('2026-03').context['renewal_count'], 0)

    def test_shows_stripe_price_and_cadence(self):
        import unittest.mock as mock
        price = {'unit_amount': 4000, 'recurring': {'interval': 'year', 'interval_count': 1}}
        with override_settings(STRIPE_SUBSCRIPTION_PRICE_ID='price_test'), \
                mock.patch('subscribers.views.stripe.Price.retrieve', return_value=price):
            response = self.get_month()
        self.assertContains(response, '$40.00 / year')

    def test_stripe_price_failure_still_renders(self):
        import stripe
        import unittest.mock as mock
        with override_settings(STRIPE_SUBSCRIPTION_PRICE_ID='price_test'), \
                mock.patch('subscribers.views.stripe.Price.retrieve', side_effect=stripe.error.APIConnectionError('down')):
            response = self.get_month()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "couldn't load from Stripe")

    def test_month_navigation(self):
        subscribe('s1@example.com', at(2026, 2, 5), self.artist_a)

        ctx = self.get_month('2026-03').context
        self.assertEqual(ctx['prev_month'], '2026-02')
        self.assertEqual(ctx['next_month'], '2026-04')

        ctx = self.get_month('2026-02').context
        self.assertIsNone(ctx['prev_month'])

        current = self.get_month().context
        self.assertTrue(current['is_current_month'])
        self.assertIsNone(current['next_month'])

    def test_future_or_invalid_month_404s(self):
        self.assertEqual(self.get_month('2999-01').status_code, 404)
        self.assertEqual(self.get_month('garbage').status_code, 404)

    def test_no_approved_artists_gives_zero_share(self):
        ArtistProfile.objects.all().delete()
        subscribe('lonely@example.com', at(2026, 3, 6))

        ctx = self.get_month('2026-03').context
        self.assertEqual(ctx['pool_share'], Decimal('0.00'))
        self.assertEqual(ctx['artist_rows'], [])


class InvoicePaidWebhookTests(TestCase):
    def invoice(self, **overrides):
        invoice = {
            'id': 'in_123',
            'customer': 'cus_123',
            'subscription': 'sub_123',
            'billing_reason': 'subscription_cycle',
            'amount_paid': 4000,
            'status_transitions': {'paid_at': 1767225600},  # 2026-01-01 00:00 UTC
            'lines': {'data': [{'period': {'end': 1798761600}}]},
        }
        invoice.update(overrides)
        return invoice

    def setUp(self):
        self.profile = make_subscriber('payer@example.com')
        self.profile.stripe_customer_id = 'cus_123'
        self.profile.stripe_subscription_id = 'sub_123'
        self.profile.subscription_status = SubscriberProfile.STATUS_ACTIVE
        self.profile.save()

    def test_records_renewal_payment(self):
        StripeWebhookView()._handle_invoice_paid(self.invoice())

        payment = SubscriptionPayment.objects.get()
        self.assertEqual(payment.subscriber, self.profile)
        self.assertEqual(payment.stripe_invoice_id, 'in_123')
        self.assertTrue(payment.is_renewal)
        self.assertEqual(payment.amount_paid, 4000)
        self.assertEqual(payment.paid_at, timezone.make_aware(datetime(2026, 1, 1), timezone.utc))

    def test_redelivered_webhook_is_recorded_once(self):
        StripeWebhookView()._handle_invoice_paid(self.invoice())
        StripeWebhookView()._handle_invoice_paid(self.invoice())
        self.assertEqual(SubscriptionPayment.objects.count(), 1)

    def test_first_invoice_before_checkout_completed_is_matched_by_customer(self):
        self.profile.stripe_subscription_id = ''
        self.profile.save()

        StripeWebhookView()._handle_invoice_paid(
            self.invoice(id='in_first', subscription='sub_new', billing_reason='subscription_create')
        )

        payment = SubscriptionPayment.objects.get()
        self.assertEqual(payment.subscriber, self.profile)
        self.assertFalse(payment.is_renewal)

    def test_past_due_recovery_reactivates_and_records_payment(self):
        self.profile.subscription_status = SubscriberProfile.STATUS_PAST_DUE
        self.profile.save()

        StripeWebhookView()._handle_invoice_paid(self.invoice())

        self.profile.refresh_from_db()
        self.assertEqual(self.profile.subscription_status, SubscriberProfile.STATUS_ACTIVE)
        self.assertEqual(SubscriptionPayment.objects.count(), 1)


class AttributionStatusTests(TestCase):
    def setUp(self):
        self.artist_a = make_artist('artist_a@example.com', 'Artist A')
        self.artist_b = make_artist('artist_b@example.com', 'Artist B')

    def test_no_payment_has_no_status(self):
        self.assertIsNone(attribution_status(make_subscriber()))

    def test_open_until_end_of_payment_month(self):
        subscriber = subscribe('s1@example.com', at(2026, 3, 5), self.artist_a)

        status = attribution_status(subscriber, now=at(2026, 3, 31))
        self.assertFalse(status.is_locked)
        self.assertEqual(status.paid_month, at(2026, 3, 1).replace(hour=0))
        self.assertEqual(status.last_changeable_day.date(), datetime(2026, 3, 31).date())

    def test_locked_to_artist_designated_at_month_end(self):
        subscriber = subscribe('s1@example.com', at(2026, 3, 5), self.artist_a)
        subscriber.designated_artist = self.artist_b
        subscriber.save()
        ArtistDesignationChange.objects.filter(subscriber=subscriber, artist=self.artist_b).update(changed_at=at(2026, 4, 10))

        status = attribution_status(subscriber, now=at(2026, 4, 15))
        self.assertTrue(status.is_locked)
        self.assertEqual(status.locked_artist, self.artist_a)

    def test_renewal_reopens_attribution(self):
        subscriber = subscribe('s1@example.com', at(2025, 3, 5), self.artist_a)
        renew(subscriber, at(2026, 3, 5))

        status = attribution_status(subscriber, now=at(2026, 3, 20))
        self.assertFalse(status.is_locked)
        self.assertEqual(status.paid_month.month, 3)
        self.assertEqual(status.paid_month.year, 2026)


@override_settings(STATICFILES_STORAGE='django.contrib.staticfiles.storage.StaticFilesStorage')
class AccountSettingsAttributionTests(TestCase):
    def test_shows_change_deadline_for_this_months_payment(self):
        artist = make_artist()
        subscriber = make_subscriber('s1@example.com')
        subscriber.designated_artist = artist
        subscriber.subscription_status = SubscriberProfile.STATUS_ACTIVE
        subscriber.save()
        self.client.force_login(subscriber.user)

        response = self.client.get('/account/')

        self.assertContains(response, 'You can change this until the end of')
        self.assertFalse(response.context['attribution'].is_locked)

    def test_locked_payment_shows_next_renewal(self):
        artist = make_artist()
        subscriber = subscribe('s1@example.com', at(2025, 3, 5), artist)
        subscriber.current_period_end = at(2027, 3, 5)
        subscriber.save()
        self.client.force_login(subscriber.user)

        response = self.client.get('/account/')

        self.assertContains(response, 'locked in')
        self.assertContains(response, 'Your next payment on <strong>March 5, 2027</strong>')


class BackfillSubscriptionHistoryTests(TestCase):
    JUL_10 = 1783692991  # 2026-07-10
    AUG_10 = 1786375058  # 2026-08-10

    def setUp(self):
        self.profile = make_subscriber('old@example.com')
        self.profile.stripe_customer_id = 'cus_old'
        self.profile.subscription_status = SubscriberProfile.STATUS_ACTIVE
        self.profile.save()
        SubscriptionStatusChange.objects.all().delete()

    def run_command(self, subscriptions, invoices, *args):
        import io
        import unittest.mock as mock
        from django.core.management import call_command

        def paging(items):
            listing = mock.Mock()
            listing.auto_paging_iter.return_value = iter(items)
            return listing

        with mock.patch('stripe.Subscription.list', return_value=paging(subscriptions)), \
                mock.patch('stripe.Invoice.list', side_effect=lambda **kw: paging(invoices.get(kw['subscription'], []))):
            call_command('backfill_subscription_history', *args, stdout=io.StringIO())

    def stripe_data(self):
        subscriptions = [{'id': 'sub_old', 'status': 'active', 'start_date': self.JUL_10}]
        invoices = {'sub_old': [
            {'id': 'in_1', 'billing_reason': 'subscription_create', 'amount_paid': 4000, 'status_transitions': {'paid_at': self.JUL_10}},
            {'id': 'in_2', 'billing_reason': 'subscription_cycle', 'amount_paid': 4000, 'status_transitions': {'paid_at': self.AUG_10}},
        ]}
        return subscriptions, invoices

    def test_records_activation_and_payments_and_is_idempotent(self):
        self.run_command(*self.stripe_data())
        self.run_command(*self.stripe_data())

        activation = self.profile.status_history.get()
        self.assertEqual(activation.status, SubscriberProfile.STATUS_ACTIVE)
        self.assertEqual(timezone.localtime(activation.changed_at).date(), datetime(2026, 7, 10).date())
        self.assertEqual(self.profile.payments.count(), 2)

        from .payouts import payments_in_month
        self.assertEqual(len(payments_in_month(2026, 7)), 1)
        self.assertTrue(payments_in_month(2026, 8)[0].is_renewal)

    def test_records_cancellation_so_resubscribe_counts_as_new(self):
        subscriptions = [
            {'id': 'sub_new', 'status': 'active', 'start_date': self.AUG_10},
            {'id': 'sub_old', 'status': 'canceled', 'start_date': self.JUL_10, 'ended_at': self.JUL_10 + 86400 * 5},
        ]
        self.run_command(subscriptions, {})

        from .payouts import payments_in_month
        self.assertEqual(len(payments_in_month(2026, 8)), 1)
        self.assertFalse(payments_in_month(2026, 8)[0].is_renewal)

    def test_dry_run_saves_nothing(self):
        self.run_command(*self.stripe_data(), '--dry-run')
        self.assertFalse(self.profile.status_history.exists())
        self.assertFalse(self.profile.payments.exists())

    def test_fallback_to_created_at_when_stripe_has_nothing(self):
        self.run_command([], {}, '--fallback-to-created-at')
        activation = self.profile.status_history.get()
        self.assertEqual(activation.changed_at, self.profile.created_at)

    def test_no_fallback_by_default(self):
        self.run_command([], {})
        self.assertFalse(self.profile.status_history.exists())


class CancelAtPeriodEndTests(TestCase):
    def test_cancel_view_sets_flag(self):
        profile = make_subscriber('canceler@example.com')
        profile.stripe_subscription_id = 'sub_test123'
        profile.subscription_status = SubscriberProfile.STATUS_ACTIVE
        profile.save()

        self.client.force_login(profile.user)

        import unittest.mock as mock
        with mock.patch('subscribers.views.stripe.Subscription.modify') as modify:
            self.client.post('/subscribe/cancel/')
            modify.assert_called_once_with('sub_test123', cancel_at_period_end=True)

        profile.refresh_from_db()
        self.assertTrue(profile.cancel_at_period_end)
