import json

from channels.generic.websocket import AsyncWebsocketConsumer

from .ws_events import LIVE_GROUP_NAME


class LiveDashboardConsumer(AsyncWebsocketConsumer):
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
