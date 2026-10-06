from datetime import datetime, timedelta, timezone as dt_timezone

import stripe
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from subscribers.models import SubscriberProfile, SubscriptionPayment, SubscriptionStatusChange


class Command(BaseCommand):
    help = (
        'Back-fill subscription start/cancel history and paid invoices from Stripe, for subscribers '
        'who signed up before that history was recorded. Safe to run more than once.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Show what would be recorded without saving it.')
        parser.add_argument(
            '--fallback-to-created-at', action='store_true',
            help='For active subscribers with no activation history that Stripe has no subscriptions for, '
                 'record their activation at the date their account was created.',
        )

    def handle(self, *args, dry_run, fallback_to_created_at, **options):
        stripe.api_key = settings.STRIPE_SECRET_KEY
        self.stats = {'activations': 0, 'cancellations': 0, 'payments': 0}

        with transaction.atomic():
            for profile in SubscriberProfile.objects.select_related('user').order_by('pk'):
                found_in_stripe = self._backfill_from_stripe(profile)
                if not found_in_stripe and fallback_to_created_at:
                    self._fallback(profile)
            if dry_run:
                transaction.set_rollback(True)

        prefix = '[dry run] would record' if dry_run else 'Recorded'
        self.stdout.write(self.style.SUCCESS(
            f"{prefix} {self.stats['activations']} activation(s), {self.stats['cancellations']} "
            f"cancellation(s) and {self.stats['payments']} payment(s)."
        ))

    def _backfill_from_stripe(self, profile):
        if not profile.stripe_customer_id:
            return False
        try:
            subscriptions = list(
                stripe.Subscription.list(customer=profile.stripe_customer_id, status='all', limit=100).auto_paging_iter()
            )
        except stripe.error.StripeError as e:
            self.stdout.write(self.style.WARNING(f'{profile.user.email}: Stripe lookup failed ({e.user_message})'))
            return False

        for subscription in subscriptions:
            if subscription.get('start_date'):
                self._record_status(profile, SubscriberProfile.STATUS_ACTIVE, _from_timestamp(subscription['start_date']))
            if subscription.get('status') == 'canceled' and subscription.get('ended_at'):
                self._record_status(profile, SubscriberProfile.STATUS_CANCELED, _from_timestamp(subscription['ended_at']))
            for invoice in stripe.Invoice.list(subscription=subscription['id'], status='paid', limit=100).auto_paging_iter():
                payment, created = SubscriptionPayment.record_from_invoice(profile, invoice)
                if created:
                    self.stats['payments'] += 1
                    self._log(profile, f'payment ({payment.billing_reason}) @ {_local(payment.paid_at)}')
        return bool(subscriptions)

    def _fallback(self, profile):
        if profile.subscription_status not in (SubscriberProfile.STATUS_ACTIVE, SubscriberProfile.STATUS_PAST_DUE):
            return
        if profile.status_history.filter(status=SubscriberProfile.STATUS_ACTIVE).exists():
            return
        self._record_status(profile, SubscriberProfile.STATUS_ACTIVE, profile.created_at, note=' (account creation date)')

    def _record_status(self, profile, status, when, note=''):
        already_recorded = profile.status_history.filter(
            status=status, changed_at__gte=when - timedelta(days=1), changed_at__lte=when + timedelta(days=1),
        ).exists()
        if already_recorded:
            return
        change = SubscriptionStatusChange.objects.create(subscriber=profile, status=status)
        # changed_at is auto_now_add, so backdate it with an update.
        SubscriptionStatusChange.objects.filter(pk=change.pk).update(changed_at=when)
        self.stats['activations' if status == SubscriberProfile.STATUS_ACTIVE else 'cancellations'] += 1
        self._log(profile, f'{status} @ {_local(when)}{note}')

    def _log(self, profile, message):
        self.stdout.write(f'{profile.user.email}: {message}')


def _from_timestamp(value):
    return datetime.fromtimestamp(value, tz=dt_timezone.utc)


def _local(value):
    return timezone.localtime(value).strftime('%Y-%m-%d %H:%M')
