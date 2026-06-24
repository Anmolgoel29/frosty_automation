import json
from django.shortcuts import render
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Count
from crm.models.lead import Lead
from crm.models.deal import Deal
from chat.models import ChatMessage
from linkedin.models import ActionLog
from linkedin.enums import ProfileState

@staff_member_required
def report_view(request):
    total_leads = Lead.objects.count()
    total_deals = Deal.objects.count()
    
    # Action Logs
    connect_requests_sent = ActionLog.objects.filter(action_type=ActionLog.ActionType.CONNECT).count()
    
    # Deal States (CONNECTED or COMPLETED means they accepted the request at some point)
    accepted_connections = Deal.objects.filter(state__in=[ProfileState.CONNECTED, ProfileState.COMPLETED]).count()
    acceptance_rate = round((accepted_connections / connect_requests_sent * 100), 1) if connect_requests_sent > 0 else 0
    
    # Chat Messages
    messages_sent = ChatMessage.objects.filter(is_outgoing=True).count()
    replies_received = ChatMessage.objects.filter(is_outgoing=False).count()
    
    # Outcomes
    converted_deals = Deal.objects.filter(outcome="converted").count()
    conversion_rate = round((converted_deals / total_deals * 100), 1) if total_deals > 0 else 0
    
    # Outcome Distribution for Pie Chart
    outcomes = Deal.objects.exclude(outcome="").values('outcome').annotate(count=Count('outcome'))
    outcome_labels = [o['outcome'].replace('_', ' ').title() for o in outcomes]
    outcome_data = [o['count'] for o in outcomes]
    
    context = {
        'total_leads': total_leads,
        'total_deals': total_deals,
        'connect_requests_sent': connect_requests_sent,
        'accepted_connections': accepted_connections,
        'acceptance_rate': acceptance_rate,
        'messages_sent': messages_sent,
        'replies_received': replies_received,
        'converted_deals': converted_deals,
        'conversion_rate': conversion_rate,
        'outcome_labels': json.dumps(outcome_labels),
        'outcome_data': json.dumps(outcome_data),
    }
    
    return render(request, "report.html", context)
