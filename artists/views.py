import logging
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.mail import send_mail
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.views import View
from django.views.generic import ListView, TemplateView, UpdateView

from artists.navidrome import grant_library_access
from subscribers.payouts import ARTIST_PER_SUBSCRIPTION, month_payouts
from .agreement import get_artist_agreement_text
from .forms import ArtistApplicationForm, ArtistProfileForm
from .models import ArtistProfile

logger = logging.getLogger(__name__)


class ArtistProfileRequiredMixin(LoginRequiredMixin):
    """Restricts a view to users with an approved artist profile.

    Authenticated users who aren't approved yet (or haven't applied) are sent
    to the profile page, which shows them their application status instead of
    a login prompt.
    """

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        profile = getattr(request.user, 'artist_profile', None)
        if not profile or not profile.is_approved:
            return redirect('profile')
        return super().dispatch(request, *args, **kwargs)


class ProfileView(LoginRequiredMixin, TemplateView):
    template_name = 'artists/profile.html'

    def get(self, request, *args, **kwargs):
        profile = getattr(request.user, 'artist_profile', None)
        if not profile:
            return redirect('artist-application')
        if not profile.is_approved:
            return render(request, 'artists/application_status.html', {
                'profile': profile,
                'support_email': settings.SUPPORT_EMAIL,
            })
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        profile = getattr(self.request.user, 'artist_profile', None)
        context['profile'] = profile
        context['albums'] = profile.albums.prefetch_related('tracks').order_by('-created_at')

        now = timezone.localtime()
        payouts = month_payouts(now.year, now.month)
        attributed = payouts.attributed_to(profile)
        context['month_start'] = now.replace(day=1)
        context['month_new_count'] = sum(1 for p in attributed if not p.is_renewal)
        context['month_renewal_count'] = sum(1 for p in attributed if p.is_renewal)
        context['month_owed'] = len(attributed) * ARTIST_PER_SUBSCRIPTION
        context['pool_ytd'] = payouts.artist_pool_ytd.get(profile.pk, Decimal('0.00'))
        return context


class ProfileEditView(ArtistProfileRequiredMixin, UpdateView):
    model = ArtistProfile
    form_class = ArtistProfileForm
    template_name = 'artists/profile_edit.html'
    success_url = reverse_lazy('profile')

    def get_object(self, queryset=None):
        profile, _ = ArtistProfile.objects.get_or_create(
            user=self.request.user,
            defaults={'display_name': self.request.user.email.split('@')[0]}
        )
        return profile

    def form_valid(self, form):
        messages.success(self.request, 'Profile updated.')
        return super().form_valid(form)

    def form_invalid(self, form):
        messages.error(self.request, 'Please correct the errors below.')
        return super().form_invalid(form)


class ArtistApplicationView(LoginRequiredMixin, View):
    template_name = 'artists/artist_application.html'

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        profile = getattr(request.user, 'artist_profile', None)
        if profile:
            if profile.status == ArtistProfile.STATUS_APPROVED:
                messages.info(request, 'You already have artist access.')
                return redirect('dashboard')
            if profile.status == ArtistProfile.STATUS_PENDING:
                messages.info(request, 'Your application is under review.')
                return redirect('dashboard')
            # STATUS_DECLINED falls through to allow re-application
        return super().dispatch(request, *args, **kwargs)

    def get(self, request):
        profile = getattr(request.user, 'artist_profile', None)
        form = ArtistApplicationForm(instance=profile)
        return render(request, self.template_name, {
            'form': form,
            'artist_agreement_text': get_artist_agreement_text(),
        })

    def post(self, request):
        profile = getattr(request.user, 'artist_profile', None)
        form = ArtistApplicationForm(request.POST, instance=profile)
        if form.is_valid():
            artist_profile = form.save(commit=False)
            artist_profile.user = request.user
            artist_profile.status = ArtistProfile.STATUS_PENDING
            artist_profile.save()
            messages.success(request,
                             "Thanks for submitting your application! " \
                             "We will review and will send you an email once " \
                             "we've made a decision. If you need help in the " \
                             "meantime, you can reach out to " + settings.SUPPORT_EMAIL)
            return redirect('dashboard')
        return render(request, self.template_name, {
            'form': form,
            'artist_agreement_text': get_artist_agreement_text(),
        })


class AdminApplicationListView(LoginRequiredMixin, UserPassesTestMixin, ListView):
    model = ArtistProfile
    template_name = 'artists/admin_application_list.html'
    context_object_name = 'applications'

    def test_func(self):
        return self.request.user.is_staff

    def get_queryset(self):
        return ArtistProfile.objects.filter(
            status__in=[ArtistProfile.STATUS_PENDING, ArtistProfile.STATUS_DECLINED]
        ).order_by('-created_at')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['pending_count'] = ArtistProfile.objects.filter(status=ArtistProfile.STATUS_PENDING).count()
        return context


class AdminApplicationApproveView(LoginRequiredMixin, UserPassesTestMixin, View):
    def test_func(self):
        return self.request.user.is_staff

    def post(self, request, pk):
        artist_profile = get_object_or_404(ArtistProfile, pk=pk, status=ArtistProfile.STATUS_PENDING)
        artist_profile.status = ArtistProfile.STATUS_APPROVED
        artist_profile.save(update_fields=['status'])

        user = artist_profile.user
        subscriber_profile = user.subscriber_profile
        subscriber_profile.subscription_status = 'artist'
        subscriber_profile.save(update_fields=['subscription_status'])

        navidrome_url = getattr(settings, 'NAVIDROME_BASE_URL', '')
        if navidrome_url and subscriber_profile.navidrome_user_id:
            try:
                grant_library_access(
                    base_url=navidrome_url,
                    admin_user=settings.NAVIDROME_ADMIN_USER,
                    admin_pass=settings.NAVIDROME_ADMIN_PASS,
                    nd_user_id=subscriber_profile.navidrome_user_id,
                    library_id=settings.NAVIDROME_LIBRARY_ID,
                    sample_library_id=settings.NAVIDROME_SAMPLE_LIBRARY_ID,
                )
            except Exception:
                logger.exception('Failed to grant Navidrome library access for %s', user.email)
                subscriber_profile.navidrome_access_error = True
                subscriber_profile.save(update_fields=['navidrome_access_error'])
            else:
                subscriber_profile.navidrome_access_error = False
                subscriber_profile.library_access_granted = True
                subscriber_profile.save(update_fields=['navidrome_access_error', 'library_access_granted'])

        try:
            send_mail(
                subject='Your artist application was approved — Capital Music Network',
                message=(
                    f"Hi {artist_profile.display_name},\n\n"
                    f"Your application to submit music to Capital Music Network has been approved.\n\n"
                    f"Log in to get started: {request.build_absolute_uri('/dashboard/')}\n\n"
                    f"— Capital Music Network\n\n"
                    f"{'=' * 60}\n"
                    f"ARTIST AGREEMENT\n"
                    f"{'=' * 60}\n\n"
                    f"{get_artist_agreement_text()}"
                ),
                from_email=None,
                recipient_list=[user.email],
                fail_silently=False,
            )
            messages.success(request, f'Application approved and email sent to {user.email}.')
        except Exception:
            logger.exception('Failed to send approval email to %s', user.email)
            messages.warning(request, f'Approved, but approval email failed to send to {user.email}.')

        return redirect('admin-application-list')


class AdminApplicationDeclineView(LoginRequiredMixin, UserPassesTestMixin, View):
    def test_func(self):
        return self.request.user.is_staff

    def post(self, request, pk):
        artist_profile = get_object_or_404(ArtistProfile, pk=pk, status=ArtistProfile.STATUS_PENDING)
        artist_profile.status = ArtistProfile.STATUS_DECLINED
        artist_profile.save(update_fields=['status'])

        try:
            send_mail(
                subject='Your artist application — Capital Music Network',
                message=(
                    f"Hi {artist_profile.display_name},\n\n"
                    f"Thank you for applying to Capital Music Network. Unfortunately we're not able to "
                    f"approve your application at this time.\n\n"
                    f"— Capital Music Network"
                ),
                from_email=None,
                recipient_list=[artist_profile.user.email],
                fail_silently=False,
            )
        except Exception:
            logger.exception('Failed to send decline email to %s', artist_profile.user.email)

        messages.success(request, f'Application from {artist_profile.user.email} declined.')
        return redirect('admin-application-list')


class TutorialView(LoginRequiredMixin, View):
    template_name = 'artists/tutorial.html'

    def get(self, request):
        return render(request, self.template_name)

    def post(self, request):
        try:
            profile = request.user.artist_profile
            profile.tutorial_seen = True
            profile.save()
        except ArtistProfile.DoesNotExist:
            pass
        return redirect('dashboard')


class DashboardView(LoginRequiredMixin, View):
    def get(self, request):
        artist_profile = getattr(request.user, 'artist_profile', None)
        if artist_profile and artist_profile.is_approved:
            if not artist_profile.tutorial_seen:
                return redirect('tutorial')
            albums = artist_profile.albums.prefetch_related('tracks')
            total_tracks = sum(a.track_count for a in albums)
            return render(request, 'dashboard.html', {
                'albums': albums,
                'total_tracks': total_tracks,
            })
        if request.user.is_staff:
            return redirect('admin-money')
        return redirect('subscriber-dashboard')


class TermsView(TemplateView):
    template_name = 'artists/terms.html'

class PrivacyView(TemplateView):
    template_name = 'artists/privacy.html'

class MoneyTransparencyView(TemplateView):
    template_name = 'artists/money_transparency.html'
