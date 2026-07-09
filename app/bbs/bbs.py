"""MeshCore BBS — main class that wires together connection, store, and command router."""

import asyncio
import datetime
import logging
import time

from collections import deque
from meshcore import EventType, MeshCore

from bbs.commands import CommandRouter
from bbs.config import AppConfig
from bbs.connection import create_connection
from bbs.device import apply_device_loc, apply_device_name, apply_flood_scope, apply_radio_config, query_device_info
from bbs.mqtt import MqttPublisher
from bbs.store import BBSStore
from bbs.weather import WttrInProvider

_LOGGER = logging.getLogger(__name__)

_RX_LOG_MAX_AGE = 5.0       # seconds an RX-log entry stays attributable to a DM
_RX_LOG_BUFFER = 8          # recent packets to keep for matching
_PAYLOAD_TYPE_TXT_MSG = 2   # MeshCore PAYLOAD_TYPE_TXT_MSG (adverts are 4)


def _parse_rx_log_data(rx: dict) -> dict:
    """Parse a raw RX_LOG_DATA event payload into SNR, RSSI, and hop-path info."""
    path_hash_size = int(rx.get("path_hash_size", 2))
    path_len = rx.get("path_len", 0)
    path = rx.get("path", "")
    snr = rx.get("snr", 0)
    rssi = rx.get("rssi", 0)
    recv_time = rx.get("recv_time") or int(time.time())

    char_length = path_hash_size * 2
    nodes = [path[i:i + char_length] for i in range(0, len(path), char_length)] if path else []

    if path and len(path) % char_length != 0:
        _LOGGER.warning(
            f"RX_LOG_DATA path length {len(path)} not a multiple of {char_length} "
            f"(path_hash_size={path_hash_size}) — last hop may be truncated: {path!r}"
        )
    if path_len is not None and len(nodes) != path_len:
        _LOGGER.warning(
            f"RX_LOG_DATA path_len={path_len} does not match parsed hop count "
            f"{len(nodes)} — path: {path!r}"
        )

    return {
        "snr": snr,
        "rssi": rssi,
        "hops": len(nodes),
        "path": nodes,
        "timestamp": recv_time,
    }


def _render_channel_text(template: str, name: str) -> str:
    """Insert the BBS name into the channel advert template.

    Supports the "{name}" placeholder and, for existing configs, the
    legacy "%s". Deliberately NOT %-formatting: a literal "%" in the
    text (e.g. "100% free") must not raise."""
    return template.replace("{name}", name).replace("%s", name)


class MeshCoreBBS:
    def __init__(self, cfg: AppConfig) -> None:
        self._cfg = cfg
        self._mc: MeshCore | None = None
        self._store = BBSStore(cfg.bbs.storage.db_path)
        self._router: CommandRouter | None = None
        # Reference to the task running start()'s main loop, so
        # _on_disconnected() can trigger an orderly shutdown (-> the finally
        # block) instead of killing the process from inside a callback task.
        self._main_task: asyncio.Task | None = None
        self._bg_tasks: list[asyncio.Task] = []
        # Tracks when each user (by pubkey) last received an inbox notification,
        # so the periodic task respects the configured interval.
        self._inbox_notify_last: dict[str, float] = {}
        self._restart_requested: bool = False
        # Recent RX_LOG_DATA payloads with their arrival time. !ping claims
        # the oldest fresh TXT_MSG entry from here — a single "last packet"
        # slot would let any advert or repeater packet arriving between the
        # RX log and the (fetch-delayed) CONTACT_MSG_RECV event overwrite
        # the DM's signal data.
        self._rx_log_recent: deque[tuple[float, dict]] = deque(maxlen=_RX_LOG_BUFFER)
        self._mqtt: MqttPublisher | None = None

    async def start(self) -> bool:
        self._mc = await create_connection(self._cfg)

        await apply_device_name(self._mc, self._cfg.bbs.name)
        await apply_device_loc(self._mc, self._cfg.bbs.latitude, self._cfg.bbs.longitude)
        await apply_radio_config(self._mc, self._cfg.radio)
        await apply_flood_scope(self._mc, self._cfg.bbs.advert.flood_scope)

        active_brokers = [b for b in self._cfg.mqtt.brokers if b.enabled and b.host]
        if active_brokers:
            public_key = self._mc.self_info.get("public_key", "")
            device_info = await query_device_info(self._mc)
            self._mqtt = MqttPublisher(self._cfg.mqtt, self._cfg.bbs.name, public_key, device_info)
            await self._mqtt.start()

        # Open persistence and make the config-defined rooms available.
        # Rooms are provisioned from config only — users can join them but
        # never create them (create_room is INSERT OR IGNORE, so this is a
        # safe additive sync on every startup; rooms dropped from the config
        # are left intact in the DB along with their existing posts).
        self._store.connect()
        for room in self._cfg.bbs.rooms.names:
            self._store.create_room(room, created_by="config")
        self._router = CommandRouter(
            self._store,
            max_message_length=self._cfg.bbs.messaging.max_len,
            user_list_limit=self._cfg.bbs.messaging.user_list_limit,
            weather_provider=WttrInProvider(),
            weather_location=self._cfg.bbs.features.weather_location,
            advert_callback=lambda: self._mc.commands.send_advert(flood=self._cfg.bbs.advert.flood),
            advert_channels_callback=self._send_channel_adverts,
            restart_callback=self._request_restart,
            admin_pubkeys=self._cfg.bbs.admin.pubkeys,
            additional_commands=self._cfg.bbs.features.commands,
        )

        if self._cfg.bbs.rooms.timeout > 0:
            self._spawn(self._room_timeout_task(), "room_timeout")
        if self._cfg.bbs.advert.times:
            self._spawn(self._advert_times_task(), "advert_times")
        if self._cfg.bbs.channels.times:
            self._spawn(self._advert_in_channels_times_task(), "channel_advert_times")
        if self._cfg.bbs.messaging.inbox_notify_interval > 0:
            self._spawn(self._inbox_notify_interval_task(), "inbox_notify")
        if self._cfg.bbs.storage.post_ttl_days > 0:
            self._spawn(self._post_cleanup_task_fn(), "post_cleanup")

        _on_connected = self._mc.subscribe(EventType.CONNECTED, self._on_connected)
        _on_disconnected = self._mc.subscribe(EventType.DISCONNECTED, self._on_disconnected)
        _on_contact_msg_recv = self._mc.subscribe(EventType.CONTACT_MSG_RECV, self._on_contact_msg_recv)
        _on_rx_log_data = self._mc.subscribe(EventType.RX_LOG_DATA, self._on_rx_log_data)

        if self._cfg.bbs.advert.enabled:
            await self._mc.commands.send_advert(flood=self._cfg.bbs.advert.flood)

        await self._mc.start_auto_message_fetching()

        # Captured so _on_disconnected() can cancel this exact task and let
        # the finally block below run the orderly shutdown.
        self._main_task = asyncio.current_task()

        try:
            while True:
                await asyncio.sleep(3600)

        except (KeyboardInterrupt, asyncio.CancelledError):
            _LOGGER.info(
                "Stopping..."
            )

        finally:
            if self._mqtt:
                await self._mqtt.stop()
                self._mqtt = None

            for task in self._bg_tasks:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            self._bg_tasks.clear()

            self._mc.unsubscribe(_on_connected)
            self._mc.unsubscribe(_on_disconnected)
            self._mc.unsubscribe(_on_contact_msg_recv)
            self._mc.unsubscribe(_on_rx_log_data)

            await self._mc.stop_auto_message_fetching()
            await self._mc.disconnect()
            self._store.close()

            _LOGGER.info(
                "Disconnected."
            )

        return self._restart_requested

    def _spawn(self, coro, name: str) -> None:
        """Create a supervised background task: crashes are logged loudly
        instead of disappearing into a never-awaited Task object."""
        task = asyncio.create_task(coro, name=name)
        task.add_done_callback(self._on_bg_task_done)
        self._bg_tasks.append(task)

    def _on_bg_task_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return  # normal shutdown path
        exc = task.exception()
        if exc is not None:
            _LOGGER.error(
                f"Background task '{task.get_name()}' crashed — "
                f"its schedule is now DEAD until restart:",
                exc_info=exc,
            )

    async def _on_connected(self, event):
        if event.payload.get('reconnected'):
            _LOGGER.info(
                "Reconnected."
            )

    async def _on_disconnected(self, event):
        if event.payload.get('max_attempts_exceeded'):
            _LOGGER.error(
                "Disconnected: max attempts exceeded — shutting down. "
                "An external process supervisor should restart the BBS."
            )
            # Cancel the main loop so start()'s finally block runs the
            # orderly teardown (unsubscribe, stop fetching, disconnect,
            # close the store). Calling sys.exit() here would only raise in
            # this callback task and skip that cleanup.
            if self._main_task is not None:
                self._main_task.cancel()

    async def _on_rx_log_data(self, event) -> None:
        payload = event.payload or {}
        self._rx_log_recent.append((time.monotonic(), payload))
        if self._mqtt:
            await self._mqtt.publish_packet(payload)

    def _claim_rx_log_for_dm(self) -> dict | None:
        """Return and remove the RX-log entry most likely belonging to the
        DM being handled right now.

        Candidates must be fresh (≤ _RX_LOG_MAX_AGE) and of type TXT_MSG —
        adverts and other traffic heard in between are skipped. Among the
        candidates the OLDEST wins (FIFO): events arrive in order, so for
        two back-to-back DMs the first message pairs with the first packet.
        Returns None instead of guessing when nothing matches; residual
        ambiguity (a third-party DM overheard in the same window) cannot be
        resolved without a correlation ID from the firmware."""
        now = time.monotonic()
        for i, (ts, payload) in enumerate(self._rx_log_recent):
            if now - ts > _RX_LOG_MAX_AGE:
                continue
            if payload.get("payload_type") == _PAYLOAD_TYPE_TXT_MSG:
                del self._rx_log_recent[i]
                return payload
        return None

    async def _request_restart(self) -> None:
        """Signal the main loop to perform an orderly shutdown and restart."""
        _LOGGER.info("Restart requested via !restart.")
        self._restart_requested = True
        if self._main_task is not None:
            self._main_task.cancel()

    async def _room_timeout_task(self) -> None:
        """Periodically remove users who have been inactive in a room too long.

        Runs every timeout/4 minutes (minimum 1 min). Only memberships that
        have a last_activity timestamp (set on !join/!post/!read) are
        considered — pre-feature rows with last_activity=0 are skipped.
        """
        timeout_secs = self._cfg.bbs.rooms.timeout * 60
        check_interval = max(60, timeout_secs // 4)
        _LOGGER.info(
            f"Room timeout active: {self._cfg.bbs.rooms.timeout}m "
            f"(checking every {check_interval // 60}m)."
        )
        while True:
            await asyncio.sleep(check_interval)
            for row in self._store.inactive_members(timeout_secs):
                pubkey, room = row["pubkey"], row["room"]
                user = self._store.get_user(pubkey)
                name = user["name"] if user else pubkey[:12]
                self._store.leave_room(pubkey, room)
                if user and user["current_room"] == room:
                    self._store.set_current_room(pubkey, None)
                _LOGGER.info(
                    f"Auto-left '{room}': {name} "
                    f"(inactive >{self._cfg.bbs.rooms.timeout}m)."
                )
                contact = self._contact_by_pubkey(pubkey)
                if contact:
                    await self._send_dm(
                        contact,
                        f"You were removed from '{room}' after "
                        f"{self._cfg.bbs.rooms.timeout}m inactivity. "
                        f"Send !join {room} to rejoin.",
                    )

    @staticmethod
    def _next_advert_time(times: list[str]) -> float:
        """Return the UNIX timestamp of the next scheduled fire time (UTC)."""
        now = datetime.datetime.now(datetime.UTC)
        candidates = []
        for t in times:
            h, m = map(int, t.split(":"))
            candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if candidate <= now:
                candidate += datetime.timedelta(days=1)
            candidates.append(candidate)
        return min(candidates).timestamp()

    async def _advert_times_task(self) -> None:
        """Broadcast an advert at the configured UTC times each day."""
        _LOGGER.info(f"Advert times active: {', '.join(self._cfg.bbs.advert.times)} UTC.")
        while True:
            await asyncio.sleep(self._next_advert_time(self._cfg.bbs.advert.times) - time.time())
            try:
                await self._mc.commands.send_advert(flood=self._cfg.bbs.advert.flood)
                _LOGGER.info("Scheduled advert sent.")
            except Exception:
                _LOGGER.exception("Scheduled advert failed — will retry at the next scheduled time.")

    async def _resolve_channel(self, name: str) -> int:
        """Return the index of `name` in the device's channel list, creating it if absent.

        Queries the device for max_channels, then probes each slot. Creates the
        channel in the first empty slot if not found. Raises RuntimeError on failure.
        """
        device_info = await self._mc.commands.send_device_query()
        if device_info.type == EventType.ERROR:
            raise RuntimeError(f"Failed to query device capabilities: {device_info.payload}")

        max_channels: int = device_info.payload.get("max_channels", 8)
        first_empty: int | None = None

        for idx in range(max_channels):
            result = await self._mc.commands.get_channel(idx)
            if result.type == EventType.ERROR:
                _LOGGER.debug(f"Channel slot {idx}: error ({result.payload})")
                continue
            slot_name: str = result.payload.get("channel_name", "").strip("\x00").strip()
            if slot_name == name:
                _LOGGER.info(f"Channel '{name}' found at index {idx}.")
                return idx
            if not slot_name and first_empty is None:
                first_empty = idx

        if first_empty is None:
            raise RuntimeError(
                f"Channel '{name}' not found and no empty slot available "
                f"(checked {max_channels} slots)."
            )

        _LOGGER.info(f"Channel '{name}' not found — creating at index {first_empty}.")
        result = await self._mc.commands.set_channel(first_empty, name)
        if result.type == EventType.ERROR:
            raise RuntimeError(f"Failed to create channel '{name}': {result.payload}")
        return first_empty

    async def _send_channel_adverts(self) -> None:
        """Send the configured advert text to all configured channels."""
        msg = _render_channel_text(self._cfg.bbs.channels.text, self._cfg.bbs.name)
        for chan_name in self._cfg.bbs.channels.names:
            try:
                idx = await self._resolve_channel(chan_name)
            except RuntimeError as e:
                _LOGGER.warning(f"Channel advert skipped for '{chan_name}': {e}")
                continue
            await self._mc.commands.send_chan_msg(idx, msg)
            _LOGGER.info(f"Channel advert sent to '{chan_name}'.")

    async def _advert_in_channels_times_task(self) -> None:
        """Broadcast a channel advert at the configured UTC times each day."""
        _LOGGER.info(f"Advert channel times active: {', '.join(self._cfg.bbs.channels.times)} UTC.")
        while True:
            await asyncio.sleep(self._next_advert_time(self._cfg.bbs.channels.times) - time.time())
            try:
                await self._send_channel_adverts()
            except Exception:
                _LOGGER.exception(
                    "Channel advert failed — will retry at the next scheduled time."
                )

    async def _inbox_notify_interval_task(self) -> None:
        """Periodically remind users who have unread private messages."""
        interval_secs = self._cfg.bbs.messaging.inbox_notify_interval * 60
        _LOGGER.info(
            f"Inbox notify interval active: every {self._cfg.bbs.messaging.inbox_notify_interval}m."
        )
        while True:
            await asyncio.sleep(interval_secs)
            now = asyncio.get_running_loop().time()
            for pubkey in self._store.recipients_with_undelivered_private():
                last = self._inbox_notify_last.get(pubkey, 0.0)
                if now - last >= interval_secs:
                    await self._notify_inbox(pubkey)

    async def _post_cleanup_task_fn(self) -> None:
        """Periodically soft-delete room posts older than post_ttl_days."""
        ttl_secs = self._cfg.bbs.storage.post_ttl_days * 86400
        # Check every ttl/4 days, minimum once per hour.
        check_interval = max(3600, ttl_secs // 4)
        _LOGGER.info(
            f"Post TTL active: {self._cfg.bbs.storage.post_ttl_days}d "
            f"(checking every {check_interval // 3600}h)."
        )
        while True:
            await asyncio.sleep(check_interval)
            count = self._store.expire_posts(ttl_secs)
            if count:
                _LOGGER.info(f"Expired {count} post(s) older than {self._cfg.bbs.storage.post_ttl_days}d.")

    async def _notify_inbox(self, pubkey: str) -> None:
        """Send a 'you have new messages' DM to the given user and record the time."""
        contact = self._contact_by_pubkey(pubkey)
        if contact is None:
            return
        count = len(self._store.undelivered_private(pubkey))
        if count == 0:
            return
        noun = "message" if count == 1 else "messages"
        if await self._send_dm(contact, f"You have {count} new {noun} in your inbox. Send !inbox."):
            self._inbox_notify_last[pubkey] = asyncio.get_running_loop().time()

    def _contact_by_pubkey(self, pubkey: str) -> dict | None:
        """Find a contact by exact full public key in the device's contact cache."""
        if not self._mc or not self._mc.contacts:
            return None
        return next(
            (c for c in self._mc.contacts.values() if c.get("public_key") == pubkey),
            None,
        )

    async def _on_contact_msg_recv(self, event):
        """Handle an incoming direct message.

        The CONTACT_MSG_RECV payload only carries the sender's pubkey_prefix
        (first 12 hex chars of their public key), not the full key needed to
        reply. Resolve it against the device's contact list to get the full
        contact object. Thanks to auto-add being enabled (the firmware
        default), a sender is normally already in the list by the time their
        message arrives — but we handle the not-found / ambiguous cases
        explicitly rather than assuming.
        """
        payload = event.payload or {}
        prefix = payload.get("pubkey_prefix", "")
        text = payload.get("text", "")

        contact = await self._resolve_contact(prefix)
        if contact is None:
            # Either the sender isn't in the contact list yet (auto-add
            # disabled, or their advert hasn't been heard) or the prefix was
            # ambiguous. Without a full public key we can't reliably reply.
            _LOGGER.warning(
                f"DM from unresolved sender (prefix={prefix!r}): {text!r} — cannot reply."
            )
            return

        sender_name = contact.get("adv_name", prefix)
        _LOGGER.info(
            f"DM from '{sender_name}' ({prefix}): {text!r}"
        )

        # Dispatch into the BBS command parser. The router replies only to
        # this sender (the BBS is pull-based). We send every reply message
        # and, only if all of them went out, run the result's on_delivered
        # commit — so a failed radio send doesn't advance the seen/delivered
        # state and silently drop messages the user never received.
        rx = self._claim_rx_log_for_dm()
        signal_info = _parse_rx_log_data(rx) if rx else None

        result = await self._router.handle(contact["public_key"], str(sender_name), text, signal_info=signal_info)

        all_sent = True
        for i, msg in enumerate(result.messages):
            if i > 0:
                await asyncio.sleep(self._cfg.bbs.messaging.inter_delay)
            if not await self._send_dm(contact, msg):
                all_sent = False
                break

        if all_sent and result.on_delivered is not None:
            result.on_delivered()

        if result.inbox_notify_pubkey and self._cfg.bbs.messaging.inbox_notify_interval > 0:
            await self._notify_inbox(result.inbox_notify_pubkey)

    async def _resolve_contact(self, prefix: str) -> dict | None:
        """Resolve a pubkey_prefix to a full contact dict, refreshing the
        contact list from the device once if it's not found in the cache.

        Returns None if no contact matches, or if more than one does (an
        ambiguous prefix — rare, but possible, and unsafe to guess at).
        """
        if not prefix:
            return None

        contact = self._match_prefix(self._mc.contacts, prefix)
        if contact is not None:
            return contact

        # Cache miss — the sender may have been auto-added after the cache
        # was last populated. Refresh once and retry.
        result = await self._mc.commands.get_contacts()
        if result.type == EventType.ERROR:
            _LOGGER.warning(
                f"Could not refresh contacts: {result.payload}"
            )
            return None

        return self._match_prefix(result.payload, prefix)

    @staticmethod
    def _match_prefix(contacts: dict | None, prefix: str) -> dict | None:
        """Find the single contact whose public_key starts with `prefix`.

        `contacts` is the dict returned by get_contacts()/MeshCore.contacts,
        keyed by pubkey prefix. Returns None on no match OR multiple matches,
        since an ambiguous prefix can't be resolved to one sender safely.
        """
        if not contacts:
            return None

        matches = [
            c for c in contacts.values()
            if c.get("public_key", "").startswith(prefix)
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            _LOGGER.warning(
                f"Ambiguous sender prefix {prefix!r} matched {len(matches)} contacts."
            )
        return None

    async def _send_dm(self, contact: dict, text: str) -> bool:
        """Send a direct message to a resolved contact.

        Returns True only if the device accepted the send, so the caller can
        decide whether to commit deferred state (see _on_contact_msg_recv).
        """
        result = await self._mc.commands.send_msg_with_retry(
            dst=contact,
            msg=text,
            max_attempts=5,
            max_flood_attempts=2,
            flood_after=3
        )
        if result is None:
            _LOGGER.error(
                "Error sending DM: no response from device."
            )
            return False
        elif result.type == EventType.ERROR:
            _LOGGER.error(
                f"Error sending DM: {result.payload}"
            )
            return False
        else:
            _LOGGER.info(
                f"DM sent to '{contact.get('adv_name', '?')}'."
            )
            return True
