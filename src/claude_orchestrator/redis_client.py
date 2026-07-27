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
        pubsub = self._client.pubsub()
        await pubsub.subscribe(self._config.incoming_channel)
        try:
            async for raw in pubsub.listen():
                if raw["type"] != "message":
                    continue
                try:
                    data = json.loads(raw["data"])
                    yield IncomingMessage.from_dict(data)
                except (json.JSONDecodeError, KeyError):
                    logger.warning("dropping malformed incoming message: %r", raw["data"])
        finally:
            await pubsub.unsubscribe(self._config.incoming_channel)
            await pubsub.aclose()

    async def close(self) -> None:
        await self._client.aclose()
