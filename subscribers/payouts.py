"""
How subscription money is owed to artists (see templates/artists/money_transparency.html):

- Every paid subscription year (a new subscriber or an annual renewal) is $40: $4 to CMN, $36 to artists.
- If the subscriber designates an artist, that artist is owed the full $36 at the end of the month
  the payment came in.
- If not, the $36 goes into a pool split evenly across every artist approved by the end of that
  month. Pooled shares accrue month by month and are paid out at the end of the year.

Attribution uses whichever artist the subscriber was designating at the end of the payment's month,
so subscribers can set or change their artist until then; later changes apply to their next payment.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal

from django.utils import timezone

from artists.models import ArtistProfile
from .models import SubscriberProfile, SubscriptionPayment, SubscriptionStatusChange

SUBSCRIPTION_PRICE = Decimal('40.00')
CMN_PER_SUBSCRIPTION = Decimal('4.00')
ARTIST_PER_SUBSCRIPTION = Decimal('36.00')
CENT = Decimal('0.01')


def month_start(year, month):
    return timezone.make_aware(datetime(year, month, 1))


def next_month(year, month):
    return (year + 1, 1) if month == 12 else (year, month + 1)


def prev_month(year, month):
    return (year - 1, 12) if month == 1 else (year, month - 1)


@dataclass
class Payment:
    subscriber: SubscriberProfile
    artist: ArtistProfile | None
    is_renewal: bool


@dataclass
class MonthPayouts:
    payments: list
    pool_artists: list
    pool_share: Decimal
    artist_pool_ytd: dict = field(default_factory=dict)

    @property
    def unattributed(self):
        return [p for p in self.payments if p.artist is None]

    @property
    def pool_total(self):
        return len(self.unattributed) * ARTIST_PER_SUBSCRIPTION

    def attributed_to(self, artist):
        return [p for p in self.payments if p.artist is not None and p.artist.pk == artist.pk]

    def pool_share_for(self, artist):
        return self.pool_share if any(a.pk == artist.pk for a in self.pool_artists) else Decimal('0.00')


def _designated_artist_at(subscriber, moment):
    history = subscriber.artist_designation_history.select_related('artist')
    if not history.exists():
        return subscriber.designated_artist
    last = history.filter(changed_at__lt=moment).order_by('-changed_at').first()
    return last.artist if last else None


def _new_subscribers_in_month(start, end):
    """
    Subscribers whose status changed to active in [start, end), unless they were already active or
    past_due right before it (a past_due -> active recovery is a renewal payment, not a new signup).
    """
    activations = (
        SubscriptionStatusChange.objects
        .filter(status=SubscriberProfile.STATUS_ACTIVE, changed_at__gte=start, changed_at__lt=end)
        .select_related('subscriber__user', 'subscriber__designated_artist')
        .order_by('changed_at')
    )
    new_subscribers = {}
    for change in activations:
        if change.subscriber.pk not in new_subscribers and _is_new_subscription(change):
            new_subscribers[change.subscriber.pk] = change.subscriber
    return list(new_subscribers.values())


def _is_new_subscription(activation):
    previous = (
        SubscriptionStatusChange.objects
        .filter(subscriber_id=activation.subscriber_id, changed_at__lt=activation.changed_at)
        .exclude(pk=activation.pk)
        .order_by('-changed_at')
        .first()
    )
    return not (previous and previous.status in (SubscriberProfile.STATUS_ACTIVE, SubscriberProfile.STATUS_PAST_DUE))


def _renewing_subscribers_in_month(start, end):
    payments = (
        SubscriptionPayment.objects
        .filter(
            billing_reason=SubscriptionPayment.BILLING_REASON_CYCLE,
            amount_paid__gt=0,
            paid_at__gte=start,
            paid_at__lt=end,
        )
        .select_related('subscriber__user', 'subscriber__designated_artist')
        .order_by('paid_at')
    )
    return [payment.subscriber for payment in payments]


def payments_in_month(year, month):
    start = month_start(year, month)
    end = month_start(*next_month(year, month))
    return [
        Payment(subscriber, _designated_artist_at(subscriber, end), is_renewal)
        for is_renewal, subscribers in (
            (False, _new_subscribers_in_month(start, end)),
            (True, _renewing_subscribers_in_month(start, end)),
        )
        for subscriber in subscribers
    ]


def _latest_payment_at(subscriber):
    """When the subscriber last paid for a subscription year (new subscription or renewal), or None."""
    activations = subscriber.status_history.filter(status=SubscriberProfile.STATUS_ACTIVE).order_by('-changed_at')
    latest_new = next((a.changed_at for a in activations if _is_new_subscription(a)), None)
    latest_renewal = (
        subscriber.payments
        .filter(billing_reason=SubscriptionPayment.BILLING_REASON_CYCLE, amount_paid__gt=0)
        .order_by('-paid_at')
        .values_list('paid_at', flat=True)
        .first()
    )
    return max(filter(None, (latest_new, latest_renewal)), default=None)


@dataclass
class AttributionStatus:
    paid_month: datetime        # first day of the month the latest payment came in
    locks_at: datetime          # first moment of the following month
    is_locked: bool
    locked_artist: ArtistProfile | None = None  # only meaningful once is_locked

    @property
    def last_changeable_day(self):
        return self.locks_at - timedelta(days=1)


def attribution_status(subscriber, now=None):
    """
    Where the subscriber's most recent payment is (or was) attributed. A payment is credited to
    whichever artist they designate at the end of the month it came in; after that it's locked in
    and any change only affects their next payment.
    """
    paid_at = _latest_payment_at(subscriber)
    if paid_at is None:
        return None
    paid_at = timezone.localtime(paid_at)
    locks_at = month_start(*next_month(paid_at.year, paid_at.month))
    is_locked = (now or timezone.now()) >= locks_at
    return AttributionStatus(
        paid_month=month_start(paid_at.year, paid_at.month),
        locks_at=locks_at,
        is_locked=is_locked,
        locked_artist=_designated_artist_at(subscriber, locks_at) if is_locked else None,
    )


def pool_artists_for_month(year, month):
    """Artists approved by the end of the given month (and still approved)."""
    end = month_start(*next_month(year, month))
    return ArtistProfile.objects.filter(status=ArtistProfile.STATUS_APPROVED, approved_at__lt=end)


def _pool_share(unattributed_count, artist_count):
    if not artist_count:
        return Decimal('0.00')
    # Round down so the shares never add up to more than was collected.
    return (unattributed_count * ARTIST_PER_SUBSCRIPTION / artist_count).quantize(CENT, rounding=ROUND_DOWN)


def month_payouts(year, month):
    """Everything owed for one month, plus each artist's accrued pool share for the year up to it."""
    result = None
    artist_pool_ytd = {}
    for m in range(1, month + 1):
        payments = payments_in_month(year, m)
        pool_artists = list(pool_artists_for_month(year, m).order_by('display_name'))
        share = _pool_share(sum(1 for p in payments if p.artist is None), len(pool_artists))
        for artist in pool_artists:
            artist_pool_ytd[artist.pk] = artist_pool_ytd.get(artist.pk, Decimal('0.00')) + share
        if m == month:
            result = MonthPayouts(payments, pool_artists, share)
    result.artist_pool_ytd = artist_pool_ytd
    return result
