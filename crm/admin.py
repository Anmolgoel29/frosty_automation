from django.contrib import admin
from unfold.admin import ModelAdmin
from .models.lead import Lead
from .models.deal import Deal

@admin.register(Lead)
class LeadAdmin(ModelAdmin):
    list_display = ('public_identifier', 'linkedin_url', 'disqualified', 'human_takeover')
    list_filter = ('disqualified', 'human_takeover')
    search_fields = ('public_identifier', 'linkedin_url', 'urn')
    actions = ['mark_human_takeover', 'resume_ai']

    @admin.action(description="Human Takeover (Stop AI messages)")
    def mark_human_takeover(self, request, queryset):
        updated = queryset.update(human_takeover=True)
        self.message_user(request, f"Successfully marked {updated} lead(s) for Human Takeover. AI will stop sending messages.")

    @admin.action(description="Resume AI messages")
    def resume_ai(self, request, queryset):
        updated = queryset.update(human_takeover=False)
        self.message_user(request, f"Successfully resumed AI for {updated} lead(s).")

@admin.register(Deal)
class DealAdmin(ModelAdmin):
    list_display = ('lead', 'campaign', 'state', 'outcome', 'connect_attempts')
    list_filter = ('state', 'outcome', 'campaign')
    search_fields = ('lead__public_identifier', 'reason')
