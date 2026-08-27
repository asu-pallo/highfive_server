from django.contrib import admin

from .models import Profile


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ('nickname', 'loginProvider', 'deviceType', 'deviceModel',
                    'osVersion', 'appVersion', 'createdAt', 'lastLoginAt')
    list_filter = ('loginProvider', 'deviceType')
    search_fields = ('nickname', 'nicknameKey', 'user__username', 'user__email')
    readonly_fields = ('nicknameKey', 'createdAt', 'lastLoginAt')
    raw_id_fields = ('user',)
