import logging


logger = logging.getLogger(__name__)


def _get_lead_and_ct(public_identifier: str):
    """Return (lead, content_type) for a public identifier."""
    from django.contrib.contenttypes.models import ContentType
    from crm.models import Lead

    lead = Lead.objects.get(public_identifier=public_identifier)
    ct = ContentType.objects.get_for_model(lead)
    return lead, ct


def sync_conversation(session, public_identifier: str) -> list[dict]:
    """Fetch messages from Voyager API and upsert into ChatMessage.

    Returns messages as a list of {sender, text, timestamp, is_outgoing} dicts
    from the DB (always the source of truth after sync). No derived summary is
    built: the ``ChatMessage`` rows written here *are* the conversation memory,
    read back verbatim by the follow-up agent.
    """
    lead, ct = _get_lead_and_ct(public_identifier)
    _sync_from_api(session, public_identifier, lead, ct)

    return _read_from_db(public_identifier)


def _sync_from_api(session, public_identifier: str, lead, ct) -> list:
    """Fetch messages from Voyager API and upsert into DB.

    Returns the list of newly-created ``ChatMessage`` rows (in arrival order).
    """
    from chat.models import ChatMessage
    from linkedin.actions.conversations import (
        find_conversation_urn, find_conversation_urn_via_navigation, parse_message_element,
    )
    from linkedin.api.client import PlaywrightLinkedinAPI
    from linkedin.api.messaging import fetch_messages

    session.ensure_browser()
    api = PlaywrightLinkedinAPI(session=session)

    target_urn = lead.get_urn(session)
    mailbox_urn = session.self_profile["urn"]

    # Find conversation URN
    conversation_urn = find_conversation_urn(api, target_urn, mailbox_urn)
    if not conversation_urn:
        conversation_urn = find_conversation_urn_via_navigation(session, target_urn)
    if not conversation_urn:
        logger.debug("sync: no conversation found for %s", public_identifier)
        return []

    # Fetch messages
    raw = fetch_messages(api, conversation_urn)
    elements = raw.get("data", {}).get("messengerMessagesBySyncToken", {}).get("elements", [])

    self_urn = session.self_profile["urn"]

    parsed_messages = [
        p for p in (parse_message_element(m) for m in elements)
        if p and p["entityUrn"]
    ]

    # Every sync re-fetches the whole thread, so most of these rows already
    # exist. One query tells us which, instead of an upsert round-trip per
    # message — and LinkedIn messages are immutable once sent, so there is
    # nothing to update on the ones we already have.
    known_urns = set(
        ChatMessage.objects.filter(
            linkedin_urn__in=[p["entityUrn"] for p in parsed_messages],
        ).values_list("linkedin_urn", flat=True)
    )

    to_create = []
    for parsed in parsed_messages:
        if parsed["entityUrn"] in known_urns:
            continue
        row = ChatMessage(
            linkedin_urn=parsed["entityUrn"],
            content_type=ct,
            object_id=lead.pk,
            content=parsed["text"],
            is_outgoing=parsed["sender_host_urn"] == self_urn,
            owner=session.django_user,
        )
        if parsed["delivered_at"]:
            row.creation_date = parsed["delivered_at"]
        to_create.append(row)
        logger.debug("sync: new message from %s for %s", parsed["sender_name"], public_identifier)

    new_messages = ChatMessage.objects.bulk_create(to_create) if to_create else []

    # Sort new messages chronologically so the LLM sees them in order.
    new_messages.sort(key=lambda m: m.creation_date or m.pk)
    logger.debug("sync: processed %d messages for %s (%d new)",
                 len(elements), public_identifier, len(new_messages))
    return new_messages


def tag_last_outgoing(public_identifier: str, authored_by: str) -> None:
    """Stamp the most recent outgoing ChatMessage with who composed it.

    Best-effort: if the row isn't there yet (the sync that should have
    created it raced or was mocked out), this is a no-op rather than an
    error — provenance is a nice-to-have for the admin UI, not something
    worth crashing a task over.
    """
    from chat.models import ChatMessage

    lead, ct = _get_lead_and_ct(public_identifier)
    last_outgoing = (
        ChatMessage.objects.filter(content_type=ct, object_id=lead.pk, is_outgoing=True)
        .order_by("-creation_date")
        .first()
    )
    if last_outgoing is None or last_outgoing.authored_by:
        return
    last_outgoing.authored_by = authored_by
    last_outgoing.save(update_fields=["authored_by"])


def _read_from_db(public_identifier: str) -> list[dict]:
    """Read all ChatMessages for a lead, sorted chronologically."""
    from chat.models import ChatMessage

    lead, ct = _get_lead_and_ct(public_identifier)
    lead_name = lead.public_identifier or "them"

    messages = ChatMessage.objects.filter(
        content_type=ct, object_id=lead.pk,
    ).select_related("owner").order_by("creation_date")

    result = []
    for msg in messages:
        if not msg.content:
            continue
        if msg.is_outgoing:
            owner = msg.owner
            sender = f"{owner.first_name or ''} {owner.last_name or ''}".strip() if owner else "me"
        else:
            sender = lead_name
        result.append({
            "sender": sender or "me",
            "text": msg.content,
            "timestamp": msg.creation_date.strftime("%Y-%m-%d %H:%M") if msg.creation_date else "",
            "is_outgoing": msg.is_outgoing,
        })
    return result
