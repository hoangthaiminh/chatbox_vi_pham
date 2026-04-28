import json

from channels.generic.websocket import AsyncWebsocketConsumer

from .ws_events import LIVE_GROUP_NAME


class LiveDashboardConsumer(AsyncWebsocketConsumer):
    """WebSocket endpoint that fans out dashboard / candidate events.

    Five event types are forwarded:

    * ``live_event`` — generic "incidents changed" ping; clients respond by
      reloading the message stream.
    * ``candidates_lock`` — a super-admin started or finished a candidate
      mutation. The owner's browser shows the blocking "Đang cập nhật"
      modal; other browsers refuse competing mutation attempts client-side.
    * ``candidates_changed`` — a candidate row was created / edited /
      deleted; clients invalidate their SBD-name cache and re-fetch
      tooltips + stats for the affected rows.
    * ``incidents_lock`` — an admin started or finished a bulk-delete pass
      against the message log. While busy, every client hides the composer
      so a freshly-typed message can't collide with the deletion pass.
    * ``incidents_changed`` — one or more incidents were removed; clients
      drop the matching rows from the DOM.

    All five are broadcast unconditionally to every connected socket
    (anon viewers included), matching the public-by-default dashboard.
    """

    async def connect(self):
        await self.channel_layer.group_add(LIVE_GROUP_NAME, self.channel_name)
        await self.accept()
        await self.send_live_event()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(LIVE_GROUP_NAME, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        # The server drives updates; clients do not need to send messages.
        pass

    async def live_update(self, event):
        await self.send_live_event()

    async def send_live_event(self):
        payload = {"type": "live_event"}
        await self.send(text_data=json.dumps(payload))

    async def candidates_lock(self, event):
        """Forward a candidate-mutation lock state change to the client."""
        payload = {
            "type": "candidates_lock",
            "busy": bool(event.get("busy")),
            "owner_user_id": event.get("owner_user_id"),
            "owner_username": event.get("owner_username") or "",
            "operation": event.get("operation") or "",
        }
        await self.send(text_data=json.dumps(payload))

    async def candidates_changed(self, event):
        """Forward a candidate-row mutation event to the client."""
        payload = {
            "type": "candidates_changed",
            "kind": event.get("kind") or "",
            "candidate_id": event.get("candidate_id"),
            "sbd": event.get("sbd") or "",
            "old_sbd": event.get("old_sbd") or "",
            "affected_sbds": list(event.get("affected_sbds") or []),
        }
        await self.send(text_data=json.dumps(payload))

    async def incidents_lock(self, event):
        """Forward an incident bulk-delete lock state change."""
        payload = {
            "type": "incidents_lock",
            "busy": bool(event.get("busy")),
            "owner_user_id": event.get("owner_user_id"),
            "owner_username": event.get("owner_username") or "",
            "operation": event.get("operation") or "",
        }
        await self.send(text_data=json.dumps(payload))

    async def incidents_changed(self, event):
        """Forward an incident-row delete event so clients can drop the rows."""
        payload = {
            "type": "incidents_changed",
            "kind": event.get("kind") or "",
            "deleted_ids": list(event.get("deleted_ids") or []),
        }
        await self.send(text_data=json.dumps(payload))
