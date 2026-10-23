"""Redis pub/sub bus between the orchestrator and the WhatsApp bridge
(SPEC.md §4.5, §6)."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

import redis.asyncio as redis

from .config import RedisConfig
from .models import IncomingMessage, OutgoingMessage

logger = logging.getLogger(__name__)


class RedisBus:
    def __init__(self, config: RedisConfig) -> None:
        self._config = config
        self._client: redis.Redis = redis.Redis(
            host=config.host, port=config.port, decode_responses=True
        )

    async def publish_outgoing(self, message: OutgoingMessage) -> None:
        await self._client.publish(
            self._config.outgoing_channel, json.dumps(message.to_dict())
        )

    async def incoming_messages(self) -> AsyncIterator[IncomingMessage]:
        async for data in self._listen(self._config.incoming_channel):
            try:
                yield IncomingMessage.from_dict(data)
            except KeyError:
                logger.warning("dropping malformed incoming message: %r", data)

    async def hook_events(self) -> AsyncIterator[dict]:
        """Hook events from the live sessions (hook_relay.py)."""
        async for data in self._listen(self._config.hooks_channel):
            yield data

    async def _listen(self, channel: str) -> AsyncIterator[dict]:
        pubsub = self._client.pubsub()
        await pubsub.subscribe(channel)
        try:
            async for raw in pubsub.listen():
                if raw["type"] != "message":
                    continue
                try:
                    data = json.loads(raw["data"])
                except json.JSONDecodeError:
                    logger.warning("dropping malformed message on %s: %r", channel, raw["data"])
                    continue
                if isinstance(data, dict):
                    yield data
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()

    async def close(self) -> None:
        await self._client.aclose()
