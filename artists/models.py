from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
from django.utils.text import slugify


class ArtistProfile(models.Model):
    STATUS_PENDING = 'pending'
    STATUS_APPROVED = 'approved'
    STATUS_DECLINED = 'declined'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_APPROVED, 'Approved'),
        (STATUS_DECLINED, 'Declined'),
    ]

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='artist_profile')
    display_name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=200, unique=True, blank=True)
    bio = models.TextField(max_length=500, blank=True)
    photo = models.ImageField(upload_to='artist_photos/', blank=True, null=True)
    note = models.TextField(blank=True, help_text='Tell us about yourself and your music')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    approved_at = models.DateTimeField(
        null=True, blank=True,
        help_text='When this artist was (most recently) approved. Artists share the pool of '
                  'unattributed subscriber money starting from the month they were approved.',
    )
    tutorial_seen = models.BooleanField(default=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Read from __dict__ so deferred instances don't trigger a refresh_from_db().
        self._original_status = self.__dict__.get('status')

    @property
    def is_approved(self):
        return self.status == self.STATUS_APPROVED

    @property
    def supporter_count(self):
        """Count of registered subscribers currently designating this artist for support."""
        from subscribers.models import SubscriberProfile
        return self.supporters.filter(subscription_status=SubscriberProfile.STATUS_ACTIVE).count()

    def save(self, *args, **kwargs):
        if self.status == self.STATUS_APPROVED and (
            (self._state.adding and not self.approved_at)
            or (not self._state.adding and self._original_status != self.STATUS_APPROVED)
        ):
            self.approved_at = timezone.now()
            update_fields = kwargs.get('update_fields')
            if update_fields is not None and 'approved_at' not in update_fields:
                kwargs['update_fields'] = [*update_fields, 'approved_at']
        if self.display_name and not self.slug:
            base_slug = slugify(self.display_name)
            slug = base_slug
            counter = 1
            while ArtistProfile.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug = f'{base_slug}-{counter}'
                counter += 1
            self.slug = slug
        super().save(*args, **kwargs)
        self._original_status = self.status

    def __str__(self):
        return self.display_name

    class Meta:
        verbose_name = 'Artist Profile'
        verbose_name_plural = 'Artist Profiles'
