"""Tests for the Zulip platform adapter plugin."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

from tests.gateway._plugin_adapter_loader import load_plugin_adapter

# Load plugins/platforms/zulip/adapter.py under a unique module name.
_zulip_mod = load_plugin_adapter("zulip")

ZulipAdapter = _zulip_mod.ZulipAdapter
check_requirements = _zulip_mod.check_requirements
validate_config = _zulip_mod.validate_config
_env_enablement = _zulip_mod._env_enablement
_strip_zulip_markdown = _zulip_mod._strip_zulip_markdown


# ── Helpers ──────────────────────────────────────────────────────────────


class TestStripZulipMarkdown:

    def test_image_to_link(self):
        assert _strip_zulip_markdown("![alt](https://img.png)") == "https://img.png"

    def test_plain_text_unchanged(self):
        assert _strip_zulip_markdown("hello world") == "hello world"

    def test_bold_preserved(self):
        """Zulip supports **bold**, so we keep it."""
        assert _strip_zulip_markdown("**bold**") == "**bold**"


# ── Configuration checks ────────────────────────────────────────────────


class TestCheckRequirements:

    def test_all_set(self, monkeypatch):
        monkeypatch.setenv("ZULIP_EMAIL", "bot@test.com")
        monkeypatch.setenv("ZULIP_API_KEY", "key123")
        monkeypatch.setenv("ZULIP_SITE", "https://chat.test.com")
        assert check_requirements() is True

    def test_missing_email(self, monkeypatch):
        monkeypatch.delenv("ZULIP_EMAIL", raising=False)
        monkeypatch.setenv("ZULIP_API_KEY", "key123")
        monkeypatch.setenv("ZULIP_SITE", "https://chat.test.com")
        assert check_requirements() is False

    def test_missing_api_key(self, monkeypatch):
        monkeypatch.setenv("ZULIP_EMAIL", "bot@test.com")
        monkeypatch.delenv("ZULIP_API_KEY", raising=False)
        monkeypatch.setenv("ZULIP_SITE", "https://chat.test.com")
        assert check_requirements() is False

    def test_missing_site(self, monkeypatch):
        monkeypatch.setenv("ZULIP_EMAIL", "bot@test.com")
        monkeypatch.setenv("ZULIP_API_KEY", "key123")
        monkeypatch.delenv("ZULIP_SITE", raising=False)
        assert check_requirements() is False


class TestEnvEnablement:

    def test_returns_none_without_credentials(self, monkeypatch):
        monkeypatch.delenv("ZULIP_EMAIL", raising=False)
        monkeypatch.delenv("ZULIP_API_KEY", raising=False)
        monkeypatch.delenv("ZULIP_SITE", raising=False)
        assert _env_enablement() is None

    def test_returns_seed_with_credentials(self, monkeypatch):
        monkeypatch.setenv("ZULIP_EMAIL", "bot@test.com")
        monkeypatch.setenv("ZULIP_API_KEY", "key123")
        monkeypatch.setenv("ZULIP_SITE", "https://chat.test.com")
        monkeypatch.delenv("ZULIP_HOME_CHANNEL", raising=False)
        monkeypatch.delenv("ZULIP_HOME_TOPIC", raising=False)

        seed = _env_enablement()
        assert seed is not None
        assert seed["enabled"] is True
        assert seed["token"] == "key123"
        assert seed["email"] == "bot@test.com"
        assert seed["site"] == "https://chat.test.com"
        assert "home_channel" not in seed

    def test_home_channel_numeric(self, monkeypatch):
        monkeypatch.setenv("ZULIP_EMAIL", "bot@test.com")
        monkeypatch.setenv("ZULIP_API_KEY", "key123")
        monkeypatch.setenv("ZULIP_SITE", "https://chat.test.com")
        monkeypatch.setenv("ZULIP_HOME_CHANNEL", "4")
        monkeypatch.setenv("ZULIP_HOME_TOPIC", "notifications")

        seed = _env_enablement()
        assert seed["home_channel"]["chat_id"] == "stream:4:notifications"

    def test_home_channel_name(self, monkeypatch):
        monkeypatch.setenv("ZULIP_EMAIL", "bot@test.com")
        monkeypatch.setenv("ZULIP_API_KEY", "key123")
        monkeypatch.setenv("ZULIP_SITE", "https://chat.test.com")
        monkeypatch.setenv("ZULIP_HOME_CHANNEL", "general")
        monkeypatch.setenv("ZULIP_HOME_TOPIC", "hermes")

        seed = _env_enablement()
        assert seed["home_channel"]["chat_id"] == "stream:general:hermes"

    def test_site_trailing_slash_stripped(self, monkeypatch):
        monkeypatch.setenv("ZULIP_EMAIL", "bot@test.com")
        monkeypatch.setenv("ZULIP_API_KEY", "key123")
        monkeypatch.setenv("ZULIP_SITE", "https://chat.test.com/")

        seed = _env_enablement()
        assert seed["site"] == "https://chat.test.com"


# ── Adapter init ────────────────────────────────────────────────────────


class TestZulipAdapterInit:

    def _make_adapter(self, monkeypatch, env=None, extra=None):
        """Create a ZulipAdapter with controlled env/extra."""
        for key in ("ZULIP_EMAIL", "ZULIP_API_KEY", "ZULIP_SITE",
                     "ZULIP_ALLOWED_USERS", "ZULIP_ALLOW_ALL_USERS"):
            monkeypatch.delenv(key, raising=False)
        if env:
            for k, v in env.items():
                monkeypatch.setenv(k, v)
        from gateway.config import PlatformConfig
        cfg = PlatformConfig(enabled=True, extra=extra or {})
        return ZulipAdapter(cfg)

    def test_init_from_env(self, monkeypatch):
        adapter = self._make_adapter(monkeypatch, env={
            "ZULIP_EMAIL": "bot@test.com",
            "ZULIP_API_KEY": "key123",
            "ZULIP_SITE": "https://chat.test.com",
        })
        assert adapter._email == "bot@test.com"
        assert adapter._api_key == "key123"
        assert adapter._site == "https://chat.test.com"

    def test_init_from_extra(self, monkeypatch):
        adapter = self._make_adapter(monkeypatch, extra={
            "email": "bot@test.com",
            "api_key": "key123",
            "site": "https://chat.test.com",
        })
        assert adapter._email == "bot@test.com"
        assert adapter._api_key == "key123"
        assert adapter._site == "https://chat.test.com"

    def test_env_overrides_extra(self, monkeypatch):
        adapter = self._make_adapter(monkeypatch,
            env={"ZULIP_EMAIL": "env@test.com"},
            extra={"email": "extra@test.com"},
        )
        assert adapter._email == "env@test.com"

    def test_site_trailing_slash_stripped(self, monkeypatch):
        adapter = self._make_adapter(monkeypatch, env={
            "ZULIP_EMAIL": "bot@test.com",
            "ZULIP_API_KEY": "key123",
            "ZULIP_SITE": "https://chat.test.com/",
        })
        assert adapter._site == "https://chat.test.com"

    def test_allow_all_users(self, monkeypatch):
        adapter = self._make_adapter(monkeypatch, env={
            "ZULIP_EMAIL": "bot@test.com",
            "ZULIP_API_KEY": "key123",
            "ZULIP_SITE": "https://chat.test.com",
            "ZULIP_ALLOW_ALL_USERS": "true",
        })
        assert adapter._allow_all is True
        assert adapter._is_user_allowed("anyone@test.com") is True

    def test_allowed_users_list(self, monkeypatch):
        adapter = self._make_adapter(monkeypatch, env={
            "ZULIP_EMAIL": "bot@test.com",
            "ZULIP_API_KEY": "key123",
            "ZULIP_SITE": "https://chat.test.com",
            "ZULIP_ALLOWED_USERS": "alice@test.com,bob@test.com",
        })
        assert adapter._is_user_allowed("alice@test.com") is True
        assert adapter._is_user_allowed("bob@test.com") is True
        assert adapter._is_user_allowed("eve@test.com") is False

    def test_allowed_users_case_insensitive(self, monkeypatch):
        adapter = self._make_adapter(monkeypatch, env={
            "ZULIP_EMAIL": "bot@test.com",
            "ZULIP_API_KEY": "key123",
            "ZULIP_SITE": "https://chat.test.com",
            "ZULIP_ALLOWED_USERS": "Alice@Test.COM",
        })
        assert adapter._is_user_allowed("alice@test.com") is True


# ── Adapter connect/disconnect ──────────────────────────────────────────


class TestZulipAdapterConnect:

    def _make_adapter(self, monkeypatch):
        for key in ("ZULIP_EMAIL", "ZULIP_API_KEY", "ZULIP_SITE",
                     "ZULIP_ALLOWED_USERS", "ZULIP_ALLOW_ALL_USERS"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("ZULIP_EMAIL", "bot@test.com")
        monkeypatch.setenv("ZULIP_API_KEY", "key123")
        monkeypatch.setenv("ZULIP_SITE", "https://chat.test.com")
        monkeypatch.setenv("ZULIP_ALLOW_ALL_USERS", "true")
        from gateway.config import PlatformConfig
        cfg = PlatformConfig(enabled=True)
        return ZulipAdapter(cfg)

    @pytest.mark.asyncio
    async def test_connect_missing_credentials(self, monkeypatch):
        """connect() returns False with no credentials."""
        for key in ("ZULIP_EMAIL", "ZULIP_API_KEY", "ZULIP_SITE"):
            monkeypatch.delenv(key, raising=False)
        from gateway.config import PlatformConfig
        cfg = PlatformConfig(enabled=True)
        adapter = ZulipAdapter(cfg)
        result = await adapter.connect()
        assert result is False

    @pytest.mark.asyncio
    async def test_connect_missing_zulip_package(self, monkeypatch):
        """connect() returns False when zulip package is not installed."""
        adapter = self._make_adapter(monkeypatch)
        with patch.dict("sys.modules", {"zulip": None}):
            result = await adapter.connect()
        assert result is False

    @pytest.mark.asyncio
    async def test_connect_auth_failure(self, monkeypatch):
        """connect() returns False when authentication fails."""
        adapter = self._make_adapter(monkeypatch)

        mock_client = MagicMock()
        mock_client.get_profile.return_value = {"result": "error", "msg": "Invalid API key"}
        mock_zulip = MagicMock()
        mock_zulip.Client.return_value = mock_client

        with patch.dict("sys.modules", {"zulip": mock_zulip}):
            result = await adapter.connect()
        assert result is False

    @pytest.mark.asyncio
    async def test_connect_success(self, monkeypatch):
        """connect() returns True and starts poll task on success."""
        adapter = self._make_adapter(monkeypatch)

        mock_client = MagicMock()
        mock_client.get_profile.return_value = {
            "result": "success",
            "user_id": 9,
            "email": "bot@test.com",
        }
        mock_client.register.return_value = {
            "result": "success",
            "queue_id": "test-queue-id",
            "last_event_id": 42,
        }
        mock_zulip = MagicMock()
        mock_zulip.Client.return_value = mock_client

        with patch.dict("sys.modules", {"zulip": mock_zulip}):
            result = await adapter.connect()

        assert result is True
        assert adapter._bot_user_id == 9
        assert adapter._queue_id == "test-queue-id"
        assert adapter._last_event_id == 42
        assert adapter._poll_task is not None
        assert not adapter._poll_task.done()

        # Cleanup
        await adapter.disconnect()


# ── Adapter send ────────────────────────────────────────────────────────


class TestZulipAdapterSend:

    @pytest.fixture
    def adapter(self, monkeypatch):
        for key in ("ZULIP_EMAIL", "ZULIP_API_KEY", "ZULIP_SITE",
                     "ZULIP_ALLOWED_USERS", "ZULIP_ALLOW_ALL_USERS"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("ZULIP_EMAIL", "bot@test.com")
        monkeypatch.setenv("ZULIP_API_KEY", "key123")
        monkeypatch.setenv("ZULIP_SITE", "https://chat.test.com")
        monkeypatch.setenv("ZULIP_ALLOW_ALL_USERS", "true")
        from gateway.config import PlatformConfig
        cfg = PlatformConfig(enabled=True)
        return ZulipAdapter(cfg)

    @pytest.mark.asyncio
    async def test_send_not_connected(self, adapter):
        result = await adapter.send("stream:1:general", "hello")
        assert result.success is False
        assert "Not connected" in result.error

    @pytest.mark.asyncio
    async def test_send_stream_message(self, adapter):
        mock_client = MagicMock()
        mock_client.send_message.return_value = {"result": "success", "id": 123}
        adapter._client = mock_client

        result = await adapter.send("stream:4:hermes", "Hello from Hermes")
        assert result.success is True
        assert result.message_id == "123"

        call_args = mock_client.send_message.call_args[0][0]
        assert call_args["type"] == "stream"
        assert call_args["to"] == "4"
        assert call_args["topic"] == "hermes"
        assert call_args["content"] == "Hello from Hermes"

    @pytest.mark.asyncio
    async def test_send_dm_by_id(self, adapter):
        mock_client = MagicMock()
        mock_client.send_message.return_value = {"result": "success", "id": 456}
        adapter._client = mock_client

        result = await adapter.send("dm:42", "Hello DM")
        assert result.success is True

        call_args = mock_client.send_message.call_args[0][0]
        assert call_args["type"] == "direct"
        assert call_args["to"] == [42]

    @pytest.mark.asyncio
    async def test_send_dm_by_email(self, adapter):
        mock_client = MagicMock()
        mock_client.send_message.return_value = {"result": "success", "id": 789}
        adapter._client = mock_client

        result = await adapter.send("dm:user@test.com", "Hello DM")
        assert result.success is True

        call_args = mock_client.send_message.call_args[0][0]
        assert call_args["type"] == "direct"
        assert call_args["to"] == ["user@test.com"]

    @pytest.mark.asyncio
    async def test_send_fallback_stream(self, adapter):
        """Bare chat_id falls back to stream with default topic."""
        mock_client = MagicMock()
        mock_client.send_message.return_value = {"result": "success", "id": 1}
        adapter._client = mock_client

        result = await adapter.send("general", "hello")
        assert result.success is True

        call_args = mock_client.send_message.call_args[0][0]
        assert call_args["type"] == "stream"
        assert call_args["to"] == "general"
        assert call_args["topic"] == "hermes"

    @pytest.mark.asyncio
    async def test_send_api_failure(self, adapter):
        mock_client = MagicMock()
        mock_client.send_message.return_value = {"result": "error", "msg": "rate limited"}
        adapter._client = mock_client

        result = await adapter.send("stream:1:general", "hello")
        assert result.success is False
        assert "rate limited" in result.error


# ── Message handling ────────────────────────────────────────────────────


class TestMessageHandling:

    @pytest.fixture
    def adapter(self, monkeypatch):
        for key in ("ZULIP_EMAIL", "ZULIP_API_KEY", "ZULIP_SITE",
                     "ZULIP_ALLOWED_USERS", "ZULIP_ALLOW_ALL_USERS"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("ZULIP_EMAIL", "bot@test.com")
        monkeypatch.setenv("ZULIP_API_KEY", "key123")
        monkeypatch.setenv("ZULIP_SITE", "https://chat.test.com")
        monkeypatch.setenv("ZULIP_ALLOW_ALL_USERS", "true")
        from gateway.config import PlatformConfig
        cfg = PlatformConfig(enabled=True)
        adapter = ZulipAdapter(cfg)
        adapter._bot_email = "bot@test.com"
        return adapter

    @pytest.mark.asyncio
    async def test_handle_stream_message(self, adapter):
        """Stream messages build correct chat_id and dispatch."""
        dispatched = []
        adapter._message_handler = AsyncMock(side_effect=lambda evt: dispatched.append(evt))

        event = {
            "type": "message",
            "message": {
                "id": 100,
                "type": "stream",
                "sender_email": "user@test.com",
                "sender_id": 5,
                "stream_id": 4,
                "display_recipient": "general",
                "subject": "test-topic",
                "content": "Hello bot!",
            },
        }
        await adapter._handle_message_event(event)

        assert len(dispatched) == 1
        msg = dispatched[0]
        assert msg.text == "Hello bot!"
        assert msg.source.chat_id == "stream:4:test-topic"
        assert msg.source.user_id == "user@test.com"
        assert msg.source.chat_topic == "test-topic"

    @pytest.mark.asyncio
    async def test_handle_dm_message(self, adapter):
        """DM messages build correct dm: chat_id."""
        dispatched = []
        adapter._message_handler = AsyncMock(side_effect=lambda evt: dispatched.append(evt))

        event = {
            "type": "message",
            "message": {
                "id": 101,
                "type": "direct",
                "sender_email": "user@test.com",
                "sender_id": 5,
                "content": "DM hello",
            },
        }
        await adapter._handle_message_event(event)

        assert len(dispatched) == 1
        assert dispatched[0].source.chat_id == "dm:5"
        assert dispatched[0].source.chat_type == "dm"

    @pytest.mark.asyncio
    async def test_ignore_own_messages(self, adapter):
        """Bot's own messages are ignored."""
        dispatched = []
        adapter._message_handler = AsyncMock(side_effect=lambda evt: dispatched.append(evt))

        event = {
            "type": "message",
            "message": {
                "id": 102,
                "type": "stream",
                "sender_email": "bot@test.com",
                "sender_id": 9,
                "stream_id": 4,
                "display_recipient": "general",
                "subject": "hermes",
                "content": "I said this",
            },
        }
        await adapter._handle_message_event(event)
        assert len(dispatched) == 0

    @pytest.mark.asyncio
    async def test_ignore_unauthorized_users(self, monkeypatch):
        """Unauthorized users are filtered out."""
        for key in ("ZULIP_EMAIL", "ZULIP_API_KEY", "ZULIP_SITE",
                     "ZULIP_ALLOWED_USERS", "ZULIP_ALLOW_ALL_USERS"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("ZULIP_EMAIL", "bot@test.com")
        monkeypatch.setenv("ZULIP_API_KEY", "key123")
        monkeypatch.setenv("ZULIP_SITE", "https://chat.test.com")
        monkeypatch.setenv("ZULIP_ALLOWED_USERS", "allowed@test.com")

        from gateway.config import PlatformConfig
        cfg = PlatformConfig(enabled=True)
        adapter = ZulipAdapter(cfg)
        adapter._bot_email = "bot@test.com"
        dispatched = []
        adapter._message_handler = AsyncMock(side_effect=lambda evt: dispatched.append(evt))

        event = {
            "type": "message",
            "message": {
                "id": 103,
                "type": "stream",
                "sender_email": "notallowed@test.com",
                "sender_id": 99,
                "stream_id": 4,
                "display_recipient": "general",
                "subject": "hermes",
                "content": "should be ignored",
            },
        }
        await adapter._handle_message_event(event)
        assert len(dispatched) == 0

    @pytest.mark.asyncio
    async def test_dedup_repeated_messages(self, adapter):
        """Duplicate message IDs are processed only once."""
        dispatched = []
        adapter._message_handler = AsyncMock(side_effect=lambda evt: dispatched.append(evt))

        event = {
            "type": "message",
            "message": {
                "id": 200,
                "type": "stream",
                "sender_email": "user@test.com",
                "sender_id": 5,
                "stream_id": 4,
                "display_recipient": "general",
                "subject": "hermes",
                "content": "once",
            },
        }
        await adapter._handle_message_event(event)
        await adapter._handle_message_event(event)
        assert len(dispatched) == 1


# ── Register function ───────────────────────────────────────────────────


class TestRegister:

    def test_register_calls_register_platform(self):
        mock_ctx = MagicMock()
        register(mock_ctx)
        mock_ctx.register_platform.assert_called_once()
        call_kwargs = mock_ctx.register_platform.call_args
        assert call_kwargs[1]["name"] == "zulip"
        assert call_kwargs[1]["label"] == "Zulip"
        assert call_kwargs[1]["max_message_length"] == 10000
