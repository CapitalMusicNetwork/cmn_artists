from django.contrib import admin

from .models import ArtistDesignationChange, SubscriberProfile, SubscriptionPayment, SubscriptionStatusChange


class SubscriptionStatusChangeInline(admin.TabularInline):
    model = SubscriptionStatusChange
    extra = 0
    readonly_fields = ['status', 'changed_at']
    can_delete = False
    ordering = ['-changed_at']


class ArtistDesignationChangeInline(admin.TabularInline):
    model = ArtistDesignationChange
    extra = 0
    readonly_fields = ['artist', 'changed_at']
    can_delete = False
    ordering = ['-changed_at']


class SubscriptionPaymentInline(admin.TabularInline):
    model = SubscriptionPayment
    extra = 0
    readonly_fields = ['stripe_invoice_id', 'billing_reason', 'amount_paid', 'paid_at']
    can_delete = False
    ordering = ['-paid_at']


@admin.register(SubscriberProfile)
class SubscriberProfileAdmin(admin.ModelAdmin):
    list_display = [
        'user', 'subscription_status', 'cancel_at_period_end', 'current_period_end',
        'designated_artist', 'navidrome_user_id', 'created_at',
    ]
    list_filter = ['subscription_status', 'cancel_at_period_end']
    search_fields = ['user__email', 'stripe_customer_id', 'stripe_subscription_id']
    readonly_fields = ['created_at', 'stripe_customer_id', 'stripe_subscription_id', 'navidrome_user_id']
    raw_id_fields = ['user', 'designated_artist']
    inlines = [SubscriptionPaymentInline, SubscriptionStatusChangeInline, ArtistDesignationChangeInline]


@admin.register(SubscriptionStatusChange)
class SubscriptionStatusChangeAdmin(admin.ModelAdmin):
    list_display = ['subscriber', 'status', 'changed_at']
    list_filter = ['status']
    search_fields = ['subscriber__user__email']
    raw_id_fields = ['subscriber']


@admin.register(ArtistDesignationChange)
class ArtistDesignationChangeAdmin(admin.ModelAdmin):
    list_display = ['subscriber', 'artist', 'changed_at']
    search_fields = ['subscriber__user__email', 'artist__display_name']
    raw_id_fields = ['subscriber', 'artist']


@admin.register(SubscriptionPayment)
class SubscriptionPaymentAdmin(admin.ModelAdmin):
    list_display = ['subscriber', 'billing_reason', 'amount_paid', 'paid_at', 'stripe_invoice_id']
    list_filter = ['billing_reason']
    search_fields = ['subscriber__user__email', 'stripe_invoice_id']
    raw_id_fields = ['subscriber']
