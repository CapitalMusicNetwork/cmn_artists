import logging

from allauth.account.forms import SignupForm as AllauthSignupForm
from django import forms

from artists.models import ArtistProfile

from . import signup_protection

logger = logging.getLogger(__name__)


class SignupForm(AllauthSignupForm):
    """allauth's signup form plus two bot checks: a honeypot field that's hidden
    from people (so anything that fills it in is a bot), and the ALTCHA
    proof-of-work solution the widget on the signup page submits as `altcha`."""

    HONEYPOT_FIELD = 'website'

    website = forms.CharField(
        required=False,
        label='Leave this field empty',
        widget=forms.TextInput(attrs={'autocomplete': 'off', 'tabindex': '-1'}),
    )

    def clean(self):
        email = self.data.get('email', '')
        if self.cleaned_data.get(self.HONEYPOT_FIELD):
            logger.warning('Blocked signup for %s: honeypot field filled in', email)
            raise forms.ValidationError('Something went wrong. Please try again.')
        if not signup_protection.verify(self.data.get('altcha')):
            logger.warning('Blocked signup for %s: missing or invalid ALTCHA solution', email)
            raise forms.ValidationError(
                'Please wait for the "I\'m not a robot" check to finish, then try again.'
            )
        return super().clean()


class DesignatedArtistForm(forms.Form):
    artist_name = forms.CharField(
        required=False,
        label='Artist you want to support',
        widget=forms.TextInput(attrs={'placeholder': 'search for an artist'}),
    )

    def __init__(self, *args, subscriber, **kwargs):
        self.subscriber = subscriber
        super().__init__(*args, **kwargs)
        if not self.is_bound and subscriber.designated_artist:
            self.fields['artist_name'].initial = subscriber.designated_artist.display_name

    def clean_artist_name(self):
        name = self.cleaned_data['artist_name'].strip()
        if not name:
            return None
        try:
            return ArtistProfile.objects.get(display_name__iexact=name, status=ArtistProfile.STATUS_APPROVED)
        except ArtistProfile.DoesNotExist:
            raise forms.ValidationError('Please select an artist from the list.')

    def save(self):
        self.subscriber.designated_artist = self.cleaned_data['artist_name']
        self.subscriber.save(update_fields=['designated_artist'])
        return self.subscriber
