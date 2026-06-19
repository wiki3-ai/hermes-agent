"""Zulip Platform Adapter for Hermes Agent.

Connects to a Zulip organization via the Zulip Python client library.
Supports streams, topics, and DMs.  Uses Zulip's event queue API for
real-time message reception.

Requires the ``zulip`` package (``pip install zulip``).

Configuration via environment variables::

    ZULIP_EMAIL             Bot email (e.g. hermes-bot@chat.example.com)
    ZULIP_API_KEY           Bot API key
    ZULIP_SITE              Server URL (e.g. https://chat.example.com)
    ZULIP_ALLOWED_USERS     Comma-separated user emails
    ZULIP_ALLOW_ALL_USERS   Allow all users (set to "true" to disable auth)
    ZULIP_HOME_CHANNEL      Stream name or numeric ID for cron delivery
    ZULIP_HOME_TOPIC        Default topic (default: hermes)

Or via ``config.yaml``::

    gateway:
      platforms:
        zulip:
          enabled: true
          extra:
            email: hermes-bot@chat.example.com
            site: https://chat.example.com
          home_channel:
            platform: zulip
            chat_id: "stream:4:hermes"
            name: general
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.platforms.helpers import MessageDeduplicator
from gateway.config import Platform, PlatformConfig

logger = logging.getLogger(__name__)

# Zulip message length limit (server default is 10000 characters).
MAX_MESSAGE_LENGTH = 10000

# Reconnect parameters (exponential backoff).
_RECONNECT_BASE_DELAY = 2.0
_RECONNECT_MAX_DELAY = 60.0
_RECONNECT_JITTER = 0.2

# Event queue idle timeout (Zulip default is 600s).  We re-register
# at 500s to avoid "Bad event queue ID" errors on quiet servers.
_QUEUE_REFRESH_SECONDS = 500


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _strip_zulip_markdown(text: str) -> str:
    """Convert markdown that doesn't render in Zulip's flavor.

    Zulip uses a variant of Markdown.  Most standard markdown works, but
    image markdown should be converted to plain links since files are
    uploaded separately.
    """
    # Images: ![alt](url) → url
    text = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r"\2", text)
    return text


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class ZulipAdapter(BasePlatformAdapter):
    """Gateway adapter for Zulip (streams, topics, DMs).

    Instantiated by the adapter_factory passed to register_platform().
    """

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("zulip"))

        extra = getattr(config, "extra", {}) or {}

        self._email: str = (
            os.getenv("ZULIP_EMAIL", "")
            or extra.get("email", "")
        )
        self._api_key: str = (
            config.token
            or os.getenv("ZULIP_API_KEY", "")
            or extra.get("api_key", "")
        )
        self._site: str = (
            os.getenv("ZULIP_SITE", "")
            or extra.get("site", "")
        ).rstrip("/")

        # Auth
        self._allowed_users_raw = os.getenv("ZULIP_ALLOWED_USERS", "")
        self._allow_all = os.getenv("ZULIP_ALLOW_ALL_USERS", "").lower() in {"1", "true", "yes"}
        self._allowed_users: set = {
            u.strip().lower() for u in self._allowed_users_raw.split(",") if u.strip()
        }

        # Zulip client (lazily created on connect)
        self._client: Any = None

        # Background event-polling task
        self._poll_task: Optional[asyncio.Task] = None
        self._closing: bool = False

        # Dedup cache
        self._dedup = MessageDeduplicator()

        # Queue state
        self._queue_id: Optional[str] = None
        self._last_event_id: Optional[int] = None
        self._queue_created_at: float = 0.0

        # Bot identity
        self._bot_user_id: Optional[int] = None
        self._bot_email: str = self._email

    @property
    def name(self) -> str:
        return "Zulip"

    # ------------------------------------------------------------------
    # Required overrides
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """Connect to Zulip and start the event-polling listener."""
        if not self._email or not self._api_key or not self._site:
            logger.error("Zulip: ZULIP_EMAIL, ZULIP_API_KEY, and ZULIP_SITE must be configured")
            self._set_fatal_error(
                "config_missing",
                "ZULIP_EMAIL, ZULIP_API_KEY, and ZULIP_SITE must be set",
                retryable=False,
            )
            return False

        try:
            import zulip  # noqa: F401
        except ImportError:
            logger.error("Zulip: 'zulip' package not installed (pip install zulip)")
            self._set_fatal_error(
                "missing_dependency",
                "pip install zulip",
                retryable=False,
            )
            return False

        # Prevent two profiles from using the same bot identity
        try:
            from gateway.status import acquire_scoped_lock
            lock_key = f"{self._site}:{self._email}"
            if not acquire_scoped_lock("zulip", lock_key):
                logger.error("Zulip: %s@%s already in use by another profile", self._email, self._site)
                self._set_fatal_error("lock_conflict", "Zulip identity in use by another profile", retryable=False)
                return False
            self._lock_key = lock_key
        except ImportError:
            self._lock_key = None

        try:
            import zulip
            self._client = zulip.Client(
                email=self._email,
                api_key=self._api_key,
                site=self._site,
            )
        except Exception as e:
            logger.error("Zulip: failed to create client: %s", e)
            self._set_fatal_error("client_init_failed", str(e), retryable=True)
            return False

        # Verify credentials
        try:
            result = await asyncio.to_thread(self._client.get_profile)
            if result.get("result") != "success":
                logger.error("Zulip: authentication failed: %s", result.get("msg", "unknown"))
                self._set_fatal_error("auth_failed", result.get("msg", "auth failed"), retryable=False)
                return False
            self._bot_user_id = result.get("user_id")
            self._bot_email = result.get("email", self._email)
            logger.info(
                "Zulip: authenticated as %s (user_id=%s) on %s",
                self._bot_email, self._bot_user_id, self._site,
            )
        except Exception as e:
            logger.error("Zulip: failed to get profile: %s", e)
            self._set_fatal_error("auth_failed", str(e), retryable=True)
            return False

        # Register event queue
        if not await self._register_queue():
            return False

        # Start background polling
        self._closing = False
        self._poll_task = asyncio.create_task(self._event_poll_loop())
        self._mark_connected()
        logger.info("Zulip: connected to %s as %s", self._site, self._bot_email)
        return True

    async def disconnect(self) -> None:
        """Disconnect from Zulip."""
        self._closing = True

        # Release scoped lock
        if getattr(self, "_lock_key", None):
            try:
                from gateway.status import release_scoped_lock
                release_scoped_lock("zulip", self._lock_key)
            except Exception:
                pass

        self._mark_disconnected()

        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):
                pass

        self._queue_id = None
        self._client = None
        logger.info("Zulip: disconnected")

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a message to a Zulip stream or DM.

        chat_id formats:
            - ``stream:<stream_id>:<topic>`` — stream message
            - ``dm:<user_id>`` — direct message by user ID
            - ``dm:<email>`` — direct message by email
            - Fallback: treated as stream name with default topic
        """
        if not self._client:
            return SendResult(success=False, error="Not connected")

        formatted = self._strip_markdown(content)
        chunks = self._truncate(formatted, MAX_MESSAGE_LENGTH)

        last_id = None
        for chunk in chunks:
            msg: Dict[str, Any] = {}

            if chat_id.startswith("stream:"):
                parts = chat_id.split(":", 2)
                msg["type"] = "stream"
                msg["to"] = parts[1] if len(parts) > 1 else chat_id
                msg["topic"] = parts[2] if len(parts) > 2 else "hermes"
            elif chat_id.startswith("dm:"):
                recipient = chat_id[3:]
                msg["type"] = "direct"
                msg["to"] = [int(recipient) if recipient.isdigit() else recipient]
            else:
                # Fallback: treat as stream name
                msg["type"] = "stream"
                msg["to"] = chat_id
                msg["topic"] = (metadata or {}).get("topic", "hermes")

            msg["content"] = chunk

            try:
                result = await asyncio.to_thread(self._client.send_message, msg)
                if result.get("result") == "success":
                    last_id = str(result.get("id", ""))
                else:
                    logger.error("Zulip: send failed: %s", result.get("msg"))
                    return SendResult(success=False, error=result.get("msg", "send failed"))
            except Exception as e:
                logger.error("Zulip: send error: %s", e)
                return SendResult(success=False, error=str(e))

        return SendResult(success=True, message_id=last_id)

    async def send_typing(
        self, chat_id: str, metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """Zulip has no typing indicator API — no-op."""
        pass

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Return chat name and type."""
        if chat_id.startswith("dm:"):
            return {"name": chat_id[3:], "type": "dm"}
        elif chat_id.startswith("stream:"):
            parts = chat_id.split(":", 2)
            return {"name": parts[1] if len(parts) > 1 else chat_id, "type": "channel"}
        return {"name": chat_id, "type": "channel"}

    async def edit_message(
        self, chat_id: str, message_id: str, content: str, *, finalize: bool = False
    ) -> SendResult:
        """Edit an existing Zulip message (supports streaming)."""
        if not self._client:
            return SendResult(success=False, error="Not connected")

        formatted = self._strip_markdown(content)
        try:
            result = await asyncio.to_thread(
                self._client.edit_message,
                {"message_id": int(message_id), "content": formatted},
            )
            if result.get("result") == "success":
                return SendResult(success=True, message_id=message_id)
            return SendResult(success=False, error=result.get("msg", "edit failed"))
        except Exception as e:
            return SendResult(success=False, error=str(e))

    def format_message(self, content: str) -> str:
        """Zulip uses standard Markdown — mostly pass through."""
        return self._strip_markdown(content)

    # ------------------------------------------------------------------
    # Message formatting
    # ------------------------------------------------------------------

    @staticmethod
    def _strip_markdown(text: str) -> str:
        """Convert markdown that doesn't render in Zulip."""
        return _strip_zulip_markdown(text)

    @staticmethod
    def _truncate(text: str, max_len: int) -> List[str]:
        """Split text into chunks of max_len characters."""
        if len(text) <= max_len:
            return [text]
        chunks = []
        while text:
            if len(text) <= max_len:
                chunks.append(text)
                break
            # Try to split at a newline boundary
            split_at = text.rfind("\n", 0, max_len)
            if split_at < max_len // 2:
                split_at = max_len
            chunks.append(text[:split_at])
            text = text[split_at:].lstrip("\n")
        return chunks if chunks else [""]

    # ------------------------------------------------------------------
    # Event queue management
    # ------------------------------------------------------------------

    async def _register_queue(self) -> bool:
        """Register a Zulip event queue for message events."""
        try:
            result = await asyncio.to_thread(
                self._client.register,
                ["message"],
            )
            if result.get("result") != "success":
                logger.error("Zulip: failed to register event queue: %s", result.get("msg"))
                self._set_fatal_error(
                    "queue_failed",
                    result.get("msg", "queue registration failed"),
                    retryable=True,
                )
                return False
            self._queue_id = result.get("queue_id")
            self._last_event_id = result.get("last_event_id", -1)
            self._queue_created_at = time.monotonic()
            logger.info("Zulip: event queue registered (queue_id=%s)", self._queue_id)
            return True
        except Exception as e:
            logger.error("Zulip: failed to register event queue: %s", e)
            self._set_fatal_error("queue_failed", str(e), retryable=True)
            return False

    async def _maybe_refresh_queue(self) -> None:
        """Re-register the event queue if it's approaching the idle timeout."""
        if not self._queue_id:
            return
        age = time.monotonic() - self._queue_created_at
        if age >= _QUEUE_REFRESH_SECONDS:
            logger.debug("Zulip: refreshing event queue (age=%.0fs)", age)
            await self._register_queue()

    # ------------------------------------------------------------------
    # Event polling (long-poll via zulip client)
    # ------------------------------------------------------------------

    async def _event_poll_loop(self) -> None:
        """Long-poll Zulip event queue for new messages.

        Uses the zulip client's built-in long-polling (90s HTTP timeout).
        The queue is periodically refreshed before the server-side idle
        timeout (600s) to avoid "Bad event queue ID" errors.
        """
        import random

        delay = _RECONNECT_BASE_DELAY
        while not self._closing:
            try:
                # Refresh queue before server-side timeout
                await self._maybe_refresh_queue()

                # Long-poll for events (blocks up to ~90s)
                result = await asyncio.wait_for(
                    asyncio.to_thread(
                        self._client.get_events,
                        queue_id=self._queue_id,
                        last_event_id=self._last_event_id,
                        dont_block=False,
                        timeout=90,
                    ),
                    timeout=120.0,
                )

                if result.get("result") != "success":
                    msg = result.get("msg", "")
                    # Queue expired — re-register instead of failing
                    if "Bad event queue ID" in msg:
                        logger.warning("Zulip: event queue expired, re-registering")
                        if await self._register_queue():
                            delay = _RECONNECT_BASE_DELAY
                            continue
                    logger.warning("Zulip: event poll failed: %s", msg)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, _RECONNECT_MAX_DELAY)
                    continue

                # Reset backoff on success
                delay = _RECONNECT_BASE_DELAY

                events = result.get("events", [])
                for event in events:
                    self._last_event_id = event.get("id", self._last_event_id)
                    if event.get("type") == "message":
                        await self._handle_message_event(event)

            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                logger.warning("Zulip: event poll timed out (120s), re-registering queue")
                await self._register_queue()
                await asyncio.sleep(delay)
                delay = min(delay * 2, _RECONNECT_MAX_DELAY)
            except Exception as e:
                if self._closing:
                    break
                logger.warning("Zulip: event poll error: %s", e)
                jitter = random.uniform(0, _RECONNECT_JITTER * delay)
                await asyncio.sleep(delay + jitter)
                delay = min(delay * 2, _RECONNECT_MAX_DELAY)

        if self.is_connected and not self._closing:
            logger.warning("Zulip: event poll loop exited unexpectedly")
            self._set_fatal_error(
                "connection_lost",
                "Zulip event polling stopped",
                retryable=True,
            )
            await self._notify_fatal_error()

    # ------------------------------------------------------------------
    # Message handling
    # ------------------------------------------------------------------

    async def _handle_message_event(self, event: Dict[str, Any]) -> None:
        """Handle an incoming message event from Zulip."""
        msg = event.get("message", {})
        if not msg:
            return

        # Dedup by message ID
        msg_id = str(msg.get("id", ""))
        if self._dedup.is_duplicate(msg_id):
            return

        # Ignore our own messages
        sender_email = msg.get("sender_email", "")
        if sender_email == self._bot_email:
            return

        text = msg.get("content", "")
        if not text:
            return

        # Determine chat type and build chat_id
        msg_type = msg.get("type", "")
        if msg_type == "stream":
            stream_id = str(msg.get("stream_id", ""))
            stream_name = msg.get("display_recipient", stream_id)
            topic = msg.get("subject", "hermes")
            chat_id = f"stream:{stream_id}:{topic}"
            chat_name = f"{stream_name} > {topic}"
            chat_type = "channel"
        elif msg_type == "direct":
            sender_id = str(msg.get("sender_id", ""))
            chat_id = f"dm:{sender_id}"
            chat_name = sender_email
            chat_type = "dm"
        else:
            logger.debug("Zulip: ignoring message type %r", msg_type)
            return

        # Auth check
        if not self._is_user_allowed(sender_email):
            logger.debug("Zulip: ignoring message from unauthorized user %s", sender_email)
            return

        # Build source and dispatch
        source = self.build_source(
            chat_id=chat_id,
            chat_name=chat_name,
            chat_type=chat_type,
            user_id=sender_email,
            user_name=sender_email,
            chat_topic=msg.get("subject") if msg_type == "stream" else None,
        )

        message_event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            message_id=msg_id,
            timestamp=datetime.now(),
        )

        await self.handle_message(message_event)

    def _is_user_allowed(self, sender_email: str) -> bool:
        """Check if sender is authorized."""
        if self._allow_all:
            return True
        if not self._allowed_users:
            return False
        return sender_email.lower() in self._allowed_users


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

def check_requirements() -> bool:
    """Check if Zulip is configured."""
    return bool(
        os.getenv("ZULIP_EMAIL", "").strip()
        and os.getenv("ZULIP_API_KEY", "").strip()
        and os.getenv("ZULIP_SITE", "").strip()
    )


def is_connected(config) -> bool:
    """Check whether Zulip is configured (env vars set)."""
    return check_requirements()


def validate_config(config) -> bool:
    """Validate that the platform config has enough to connect."""
    extra = getattr(config, "extra", {}) or {}
    email = os.getenv("ZULIP_EMAIL") or extra.get("email", "")
    api_key = config.token or os.getenv("ZULIP_API_KEY") or extra.get("api_key", "")
    site = os.getenv("ZULIP_SITE") or extra.get("site", "")
    return bool(email and api_key and site)


def _env_enablement() -> Optional[dict]:
    """Seed PlatformConfig.extra from env vars during config load."""
    email = os.getenv("ZULIP_EMAIL", "").strip()
    api_key = os.getenv("ZULIP_API_KEY", "").strip()
    site = os.getenv("ZULIP_SITE", "").strip()

    if not (email and api_key and site):
        return None

    seed = {
        "enabled": True,
        "token": api_key,
        "email": email,
        "site": site,
    }

    # Home channel for cron delivery
    home_channel = os.getenv("ZULIP_HOME_CHANNEL", "").strip()
    home_channel_name = os.getenv("ZULIP_HOME_CHANNEL_NAME", "").strip()
    home_topic = os.getenv("ZULIP_HOME_TOPIC", "hermes").strip()

    if home_channel:
        # If it looks like a stream name (not numeric), use it directly.
        # The gateway's home_channel.chat_id format is "stream:<id>:<topic>"
        # but we can also use "stream:<name>:<topic>" — the adapter's send()
        # method passes the value to Zulip's "to" field which accepts both.
        if home_channel.isdigit():
            chat_id = f"stream:{home_channel}:{home_topic}"
        else:
            chat_id = f"stream:{home_channel}:{home_topic}"
        seed["home_channel"] = {
            "chat_id": chat_id,
            "name": home_channel_name or home_channel,
        }

    return seed


# ---------------------------------------------------------------------------
# Standalone sender (for cron delivery without a live gateway adapter)
# ---------------------------------------------------------------------------

async def _standalone_send(
    pconfig,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[list] = None,
    force_document: bool = False,
) -> Dict[str, Any]:
    """Send via the Zulip REST API without a live gateway adapter.

    Used by ``tools/send_message_tool._send_via_adapter`` when the gateway
    runner is not in this process (typical for cron jobs running out-of-process).
    """
    extra = getattr(pconfig, "extra", {}) or {}
    email = os.getenv("ZULIP_EMAIL", "") or extra.get("email", "")
    api_key = pconfig.token or os.getenv("ZULIP_API_KEY", "") or extra.get("api_key", "")
    site = (os.getenv("ZULIP_SITE", "") or extra.get("site", "")).rstrip("/")

    if not (email and api_key and site):
        return {"error": "ZULIP_EMAIL, ZULIP_API_KEY, and ZULIP_SITE must be configured"}

    try:
        import zulip
    except ImportError:
        return {"error": "zulip package not installed. Run: pip install zulip"}

    client = zulip.Client(email=email, api_key=api_key, site=site)

    msg: Dict[str, Any] = {}
    if chat_id.startswith("stream:"):
        parts = chat_id.split(":", 2)
        msg["type"] = "stream"
        msg["to"] = parts[1] if len(parts) > 1 else chat_id
        msg["topic"] = parts[2] if len(parts) > 2 else "hermes"
    elif chat_id.startswith("dm:"):
        recipient = chat_id[3:]
        msg["type"] = "direct"
        msg["to"] = [int(recipient) if recipient.isdigit() else recipient]
    else:
        msg["type"] = "stream"
        msg["to"] = chat_id
        msg["topic"] = "hermes"

    msg["content"] = message

    result = client.send_message(msg)
    if result.get("result") == "success":
        return {"message_id": str(result.get("id", ""))}
    return {"error": result.get("msg", "send failed")}


def _build_adapter(config) -> ZulipAdapter:
    """Factory wrapper that constructs ZulipAdapter from a PlatformConfig."""
    return ZulipAdapter(config)


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system."""
    ctx.register_platform(
        name="zulip",
        label="Zulip",
        adapter_factory=_build_adapter,
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["ZULIP_EMAIL", "ZULIP_API_KEY", "ZULIP_SITE"],
        install_hint="pip install zulip",
        # Env-driven auto-configuration: seeds PlatformConfig.extra with
        # email/api_key/site + home_channel so env-only setups show up
        # in gateway status without instantiating the adapter.
        env_enablement_fn=_env_enablement,
        # Auth env vars for _is_user_authorized() integration.
        allowed_users_env="ZULIP_ALLOWED_USERS",
        allow_all_env="ZULIP_ALLOW_ALL_USERS",
        # Cron home-channel delivery.
        cron_deliver_env_var="ZULIP_HOME_CHANNEL",
        # Out-of-process cron delivery via Zulip REST API.
        standalone_sender_fn=_standalone_send,
        # Display
        emoji="💬",
        max_message_length=MAX_MESSAGE_LENGTH,
        # LLM guidance
        platform_hint=(
            "You are chatting via Zulip. Zulip supports standard Markdown "
            "with some differences. Messages support streams (channels) with "
            "topics for organization. Keep responses concise. Use topic names "
            "to organize conversations. Code blocks with triple backticks "
            "are supported. Links render automatically."
        ),
    )
