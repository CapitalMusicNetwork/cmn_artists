from datetime import datetime, timezone as dt_timezone

from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone


class SubscriberProfile(models.Model):
    STATUS_PENDING = 'pending'
    STATUS_ACTIVE = 'active'
    STATUS_ARTIST = 'artist'
    STATUS_PAST_DUE = 'past_due'
    STATUS_CANCELED = 'canceled'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_ACTIVE, 'Active'),
        (STATUS_ARTIST, 'Artist (free access)'),
        (STATUS_PAST_DUE, 'Past Due'),
        (STATUS_CANCELED, 'Canceled'),
    ]

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='subscriber_profile')
    stripe_customer_id = models.CharField(max_length=100, blank=True)
    stripe_subscription_id = models.CharField(max_length=100, blank=True)
    subscription_status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    current_period_end = models.DateTimeField(null=True, blank=True)
    cancel_at_period_end = models.BooleanField(
        default=False,
        help_text='Whether Stripe is set to cancel this subscription at current_period_end '
                  'instead of automatically renewing it.',
    )
    designated_artist = models.ForeignKey(
        'accounts.ArtistProfile', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='supporters',
        help_text='The artist who receives 90% of this subscriber\'s fee.',
    )
    navidrome_user_id = models.CharField(max_length=100, blank=True)
    navidrome_access_error = models.BooleanField(default=False)
    library_access_granted = models.BooleanField(
        default=False,
        help_text='Whether we\'ve actually granted this user Navidrome library access. '
                  'Tracked separately from subscription_status so staff can grant/revoke manually.',
    )
    temporary_access_until = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Read straight from __dict__ (not the field descriptors) so that
        # deferred/partial instances (e.g. built by the on_delete=SET_NULL
        # cascade collector) don't trigger a refresh_from_db() here.
        self._original_subscription_status = self.__dict__.get('subscription_status')
        self._original_designated_artist_id = self.__dict__.get('designated_artist_id')

    def save(self, *args, **kwargs):
        is_new = self._state.adding
        status_changed = not is_new and self.subscription_status != self._original_subscription_status
        artist_changed = not is_new and self.designated_artist_id != self._original_designated_artist_id

        super().save(*args, **kwargs)

        if status_changed:
            SubscriptionStatusChange.objects.create(subscriber=self, status=self.subscription_status)
        if artist_changed:
            ArtistDesignationChange.objects.create(subscriber=self, artist=self.designated_artist)

        self._original_subscription_status = self.subscription_status
        self._original_designated_artist_id = self.designated_artist_id

    @property
    def should_have_library_access(self):
        """Whether the subscriber's Stripe/temporary-access status entitles them to library access."""
        if self.user.is_staff:
            return True
        if self.subscription_status in (self.STATUS_ACTIVE, self.STATUS_ARTIST):
            return True
        if self.temporary_access_until and self.temporary_access_until > timezone.now():
            return True
        return False

    @property
    def temporary_access_active(self):
        return bool(self.temporary_access_until and self.temporary_access_until > timezone.now())

    def __str__(self):
        return f'{self.user.email} [{self.subscription_status}]'

    class Meta:
        verbose_name = 'Subscriber Profile'
        verbose_name_plural = 'Subscriber Profiles'


class SubscriptionStatusChange(models.Model):
    """A record of every subscription_status transition a subscriber has gone through."""

    subscriber = models.ForeignKey(SubscriberProfile, on_delete=models.CASCADE, related_name='status_history')
    status = models.CharField(max_length=20, choices=SubscriberProfile.STATUS_CHOICES)
    changed_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.subscriber.user.email} -> {self.status} @ {self.changed_at}'

    class Meta:
        verbose_name = 'Subscription Status Change'
        verbose_name_plural = 'Subscription Status Changes'
        ordering = ['-changed_at']


class ArtistDesignationChange(models.Model):
    """A record of every designated-artist change a subscriber has made."""

    subscriber = models.ForeignKey(SubscriberProfile, on_delete=models.CASCADE, related_name='artist_designation_history')
    artist = models.ForeignKey(
        'accounts.ArtistProfile', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='designation_changes',
    )
    changed_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        artist_name = self.artist.display_name if self.artist else '(none)'
        return f'{self.subscriber.user.email} -> {artist_name} @ {self.changed_at}'

    class Meta:
        verbose_name = 'Artist Designation Change'
        verbose_name_plural = 'Artist Designation Changes'
        ordering = ['-changed_at']


class SubscriptionPayment(models.Model):
    """A paid Stripe invoice (new subscription or annual renewal), recorded from the invoice.paid webhook."""

    BILLING_REASON_CREATE = 'subscription_create'
    BILLING_REASON_CYCLE = 'subscription_cycle'

    subscriber = models.ForeignKey(SubscriberProfile, on_delete=models.CASCADE, related_name='payments')
    stripe_invoice_id = models.CharField(max_length=100, unique=True)
    billing_reason = models.CharField(
        max_length=50, blank=True,
        help_text="Stripe's billing_reason: 'subscription_create' for a first payment, "
                  "'subscription_cycle' for a renewal.",
    )
    amount_paid = models.PositiveIntegerField(help_text='In cents.')
    paid_at = models.DateTimeField()

    @classmethod
    def record_from_invoice(cls, subscriber, invoice):
        """Record a paid Stripe invoice; safe to call more than once for the same invoice."""
        paid_at = (invoice.get('status_transitions') or {}).get('paid_at')
        return cls.objects.get_or_create(
            stripe_invoice_id=invoice['id'],
            defaults={
                'subscriber': subscriber,
                'billing_reason': invoice.get('billing_reason') or '',
                'amount_paid': invoice.get('amount_paid') or 0,
                'paid_at': datetime.fromtimestamp(paid_at, tz=dt_timezone.utc) if paid_at else timezone.now(),
            },
        )

    @property
    def is_renewal(self):
        return self.billing_reason == self.BILLING_REASON_CYCLE

    def __str__(self):
        return f'{self.subscriber.user.email} ${self.amount_paid / 100:.2f} ({self.billing_reason}) @ {self.paid_at}'

    class Meta:
        verbose_name = 'Subscription Payment'
        verbose_name_plural = 'Subscription Payments'
        ordering = ['-paid_at']
