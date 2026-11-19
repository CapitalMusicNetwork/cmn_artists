from django.contrib import admin
from django.urls import path, re_path, include
from django.conf import settings
from django.views.static import serve as serve_static

from artists.views import (
    AdminApplicationApproveView,
    AdminApplicationDeclineView,
    AdminApplicationListView,
    DashboardView,
    MoneyTransparencyView,
    TermsView,
    PrivacyView,
    TutorialView,
)
from subscribers.signup_protection import challenge_view as altcha_challenge_view
from subscribers.views import AccountSettingsView, StripeWebhookView


def home_view(request):
    from django.shortcuts import render
    return render(request, 'home.html')


urlpatterns = [
    path('', home_view, name='home'),
    path('accounts/altcha-challenge/', altcha_challenge_view, name='altcha-challenge'),
    path('accounts/', include('allauth.urls')),
    path('hijack/', include('hijack.urls')),
    path('profile/', include('artists.urls')),
    path('music/', include('music.urls')),
    path('subscribe/', include('subscribers.urls')),
    # Artist application flow
    path('admin-panel/applications/', AdminApplicationListView.as_view(), name='admin-application-list'),
    path('admin-panel/applications/<int:pk>/approve/', AdminApplicationApproveView.as_view(), name='admin-application-approve'),
    path('admin-panel/applications/<int:pk>/decline/', AdminApplicationDeclineView.as_view(), name='admin-application-decline'),
    path('money-transparency/', MoneyTransparencyView.as_view(), name='money-transparency'),
    path('terms/', TermsView.as_view(), name='terms'),
    path('privacy/', PrivacyView.as_view(), name='privacy'),
    path('tutorial/', TutorialView.as_view(), name='tutorial'),
    path('dashboard/', DashboardView.as_view(), name='dashboard'),
    path('account/', AccountSettingsView.as_view(), name='account-settings'),
    # Stripe webhook (must be exempt from CSRF — handled by raw request body verification)
    path('stripe/webhook/', StripeWebhookView.as_view(), name='stripe-webhook'),
    path('admin/', admin.site.urls),
]

# Served by Django in production too: there's no reverse proxy in front of
# gunicorn handling /media/ separately, so this is the only thing that serves
# uploaded artist media (cover art, tracks) to the artists app.
# Not using django.conf.urls.static.static() here - it no-ops when DEBUG=False.
urlpatterns += [
    re_path(
        r"^%s(?P<path>.*)$" % settings.MEDIA_URL.lstrip("/"),
        serve_static,
        {"document_root": settings.MEDIA_ROOT},
    ),
]
