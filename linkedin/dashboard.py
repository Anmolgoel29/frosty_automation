# linkedin/dashboard.py
from chat.models import ChatMessage
from crm.models.deal import Deal
from linkedin.enums import ProfileState
from linkedin.models import ActionLog


def dashboard_callback(request, context):
    """Injects outreach stats into the admin index page (unfold DASHBOARD_CALLBACK)."""
    context.update({
        "messages_sent": ChatMessage.objects.filter(is_outgoing=True).count(),
        "replies_received": ChatMessage.objects.filter(is_outgoing=False).count(),
        "connect_requests_sent": ActionLog.objects.filter(
            action_type=ActionLog.ActionType.CONNECT,
        ).count(),
        "connect_requests_accepted": Deal.objects.filter(
            state__in=[ProfileState.CONNECTED, ProfileState.COMPLETED],
        ).count(),
    })
    return context
