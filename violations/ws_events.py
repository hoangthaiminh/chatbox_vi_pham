"""WebSocket broadcast helpers.

Five broadcast flavours feed the realtime dashboard:

* ``notify_live_update`` — generic "incidents changed, please refresh" ping
  used by the existing incident pipeline.
* ``notify_candidates_lock`` — informs every connected client that a
  super-admin has begun (or finished) a candidate-roster mutation. The
  owner's browser shows a blocking "Đang cập nhật cơ sở dữ liệu" modal so
  they can't fire another mutation; other admins use it to know that a
  competing attempt would just bounce with HTTP 409.
* ``notify_candidates_changed`` — fires after a candidate Add / Edit /
  Delete (or bulk delete / CSV reload) commits, so every client refreshes
  tooltips, stats, and the in-memory SBD-name cache for the affected rows.
* ``notify_incidents_lock`` — same shape as ``notify_candidates_lock`` but
  for the incident bulk-delete flow. Clients use it to disable the
  composer and (for the owner) to keep selection-mode UI active until the
  server releases the lock.
* ``notify_incidents_changed`` — fires after one or more incidents are
  deleted (single delete OR bulk-delete). Clients drop the matching
  ``.chat-row[data-incident-id]`` elements from the DOM so the listing
  converges to the post-delete state without a full reload.

These events are intentionally broadcast to the *entire* live group
(authenticated or not) so anonymous viewers also see up-to-date listings —
matches the rest of the public-by-default dashboard model.
"""
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

LIVE_GROUP_NAME = "violations_live_updates"


def notify_live_update():
    channel_layer = get_channel_layer()
    if not channel_layer:
        return

    async_to_sync(channel_layer.group_send)(
        LIVE_GROUP_NAME,
        {
            "type": "live.update",
        },
    )


def notify_candidates_lock(
    *,
    busy: bool,
    owner_user_id=None,
    owner_username: str = "",
    operation: str = "",
):
    """Broadcast a candidate-mutation lock state change.

    ``busy=True`` is sent when a super-admin acquires the lock; ``busy=False``
    when the lock is released (or after a manual force-release). Owner info
    is included so the holder's browser can recognise itself and render the
    blocking modal, while other clients can show a passive indicator.
    """
    channel_layer = get_channel_layer()
    if not channel_layer:
        return

    async_to_sync(channel_layer.group_send)(
        LIVE_GROUP_NAME,
        {
            "type": "candidates.lock",
            "busy": bool(busy),
            "owner_user_id": owner_user_id,
            "owner_username": owner_username or "",
            "operation": operation or "",
        },
    )


def notify_candidates_changed(
    *,
    kind: str,
    candidate_id=None,
    sbd: str = "",
    old_sbd: str = "",
    affected_sbds=None,
):
    """Broadcast that one or more candidate rows have changed.

    ``kind`` is one of: ``"create"``, ``"update"``, ``"delete"``,
    ``"bulk_delete"``, ``"csv_reload"``. For single-row mutations the
    ``candidate_id`` / ``sbd`` (and ``old_sbd`` on rename) identify the
    affected row precisely so clients can do narrow refreshes; for bulk
    operations ``affected_sbds`` carries the full list (or ``None`` when
    the set is too large and clients should refresh broadly).
    """
    channel_layer = get_channel_layer()
    if not channel_layer:
        return

    async_to_sync(channel_layer.group_send)(
        LIVE_GROUP_NAME,
        {
            "type": "candidates.changed",
            "kind": kind,
            "candidate_id": candidate_id,
            "sbd": sbd or "",
            "old_sbd": old_sbd or "",
            "affected_sbds": list(affected_sbds) if affected_sbds else [],
        },
    )


def notify_incidents_lock(
    *,
    busy: bool,
    owner_user_id=None,
    owner_username: str = "",
    operation: str = "",
):
    """Broadcast incident bulk-delete lock state changes.

    Sent before a bulk-delete starts (``busy=True``) and after it commits
    (``busy=False``). All connected clients use it to swap the composer
    bar for an "đang xoá" placeholder so a posted incident cannot collide
    with the deletion pass mid-flight.
    """
    channel_layer = get_channel_layer()
    if not channel_layer:
        return

    async_to_sync(channel_layer.group_send)(
        LIVE_GROUP_NAME,
        {
            "type": "incidents.lock",
            "busy": bool(busy),
            "owner_user_id": owner_user_id,
            "owner_username": owner_username or "",
            "operation": operation or "",
        },
    )


def notify_incidents_changed(
    *,
    kind: str,
    deleted_ids=None,
    incident_id=None,
):
    """Broadcast that one or more incidents have been removed.

    ``kind`` is one of ``"delete"`` (single) or ``"bulk_delete"`` (many).
    ``deleted_ids`` is the canonical list every client should drop from
    the DOM. ``incident_id`` is kept for backwards-compat single-delete
    callers — it is folded into ``deleted_ids`` if not already present.
    """
    channel_layer = get_channel_layer()
    if not channel_layer:
        return

    ids = list(deleted_ids or [])
    if incident_id is not None and incident_id not in ids:
        ids.append(incident_id)

    async_to_sync(channel_layer.group_send)(
        LIVE_GROUP_NAME,
        {
            "type": "incidents.changed",
            "kind": kind,
            "deleted_ids": ids,
        },
    )
