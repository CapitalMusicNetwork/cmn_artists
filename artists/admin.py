from django.contrib import admin

from .models import ArtistProfile


@admin.register(ArtistProfile)
class ArtistProfileAdmin(admin.ModelAdmin):
    list_display = ['display_name', 'user', 'status', 'slug', 'created_at', 'approved_at']
    list_filter = ['status', 'created_at']
    search_fields = ['display_name', 'user__email', 'slug']
    prepopulated_fields = {'slug': ('display_name',)}
    readonly_fields = ['created_at']
    raw_id_fields = ['user']
