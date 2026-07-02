"""MeshCore BBS — main class that wires together connection, store, and command router."""

import asyncio
import logging

from meshcore import EventType, MeshCore

from bbs.commands import CommandRouter
from bbs.config import AppConfig
from bbs.connection import create_connection
from bbs.store import BBSStore

_LOGGER = logging.getLogger(__name__)


class MeshCoreBBS:
    def __init__(self, cfg: AppConfig) -> None:
        self._cfg = cfg
        self._mc: MeshCore | None = None
        self._store = BBSStore(cfg.bbs.db_path)
        self._router: CommandRouter | None = None
        # Reference to the task running start()'s main loop, so
        # _on_disconnected() can trigger an orderly shutdown (-> the finally
        # block) instead of killing the process from inside a callback task.
        self._main_task: asyncio.Task | None = None
        self._timeout_task: asyncio.Task | None = None

    async def start(self) -> None:
        self._mc = await create_connection(self._cfg)

        await self._apply_device_name(self._cfg.bbs.name)
        await self._apply_radio_config(self._cfg.radio)

        # Open persistence and make the config-defined rooms available.
        # Rooms are provisioned from config only — users can join them but
        # never create them (create_room is INSERT OR IGNORE, so this is a
        # safe additive sync on every startup; rooms dropped from the config
        # are left intact in the DB along with their existing posts).
        self._store.connect()
        for room in self._cfg.bbs.rooms:
            self._store.create_room(room, created_by="config")
        self._router = CommandRouter(self._store)

        if self._cfg.bbs.room_timeout > 0:
            self._timeout_task = asyncio.create_task(self._room_timeout_task())

        _on_connected = self._mc.subscribe(EventType.CONNECTED, self._on_connected)
        _on_disconnected = self._mc.subscribe(EventType.DISCONNECTED, self._on_disconnected)
        _on_contact_msg_recv = self._mc.subscribe(EventType.CONTACT_MSG_RECV, self._on_contact_msg_recv)

        if self._cfg.bbs.advert:
            await self._mc.commands.send_advert(flood=self._cfg.bbs.advert_flood)

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
            if self._timeout_task is not None:
                self._timeout_task.cancel()
                try:
                    await self._timeout_task
                except asyncio.CancelledError:
                    pass

            self._mc.unsubscribe(_on_connected)
            self._mc.unsubscribe(_on_disconnected)
            self._mc.unsubscribe(_on_contact_msg_recv)

            await self._mc.stop_auto_message_fetching()
            await self._mc.disconnect()
            self._store.close()

            _LOGGER.info(
                "Disconnected."
            )

    async def _on_connected(self, event):
        if event.payload.get('reconnected'):
            _LOGGER.info(
                f"Reconnected."
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

    async def _room_timeout_task(self) -> None:
        """Periodically remove users who have been inactive in a room too long.

        Runs every timeout/4 minutes (minimum 1 min). Only memberships that
        have a last_activity timestamp (set on !join/!post/!read) are
        considered — pre-feature rows with last_activity=0 are skipped.
        """
        timeout_secs = self._cfg.bbs.room_timeout * 60
        check_interval = max(60, timeout_secs // 4)
        _LOGGER.info(
            f"Room timeout active: {self._cfg.bbs.room_timeout}m "
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
                    f"(inactive >{self._cfg.bbs.room_timeout}m)."
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
        result = self._router.handle(contact["public_key"], sender_name, text)

        all_sent = True
        for msg in result.messages:
            if not await self._send_dm(contact, msg):
                all_sent = False
                break

        if all_sent and result.on_delivered is not None:
            result.on_delivered()

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
        result = await self._mc.commands.send_msg(contact, text)
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

    async def _apply_device_name(self, name: str) -> None:
        result = await self._mc.commands.set_name(name)

        if result.type == EventType.ERROR:
            _LOGGER.warning(
                f"Could not set BBS name to '{name}': {result.payload}"
            )
        else:
            _LOGGER.info(
                f"BBS name set to '{name}'."
            )

    async def _apply_radio_config(self, radio) -> None:
        params = (radio.frequency, radio.bandwidth, radio.spreading_factor, radio.coding_rate)

        if all(p is not None for p in params):
            result = await self._mc.commands.set_radio(
                freq=radio.frequency,
                bw=radio.bandwidth,
                sf=radio.spreading_factor,
                cr=radio.coding_rate,
                repeat=None
            )

            if result.type == EventType.ERROR:
                _LOGGER.warning(
                    f"Could not apply radio params: {result.payload}"
                )
            else:
                _LOGGER.info(
                    f"Radio set: freq={radio.frequency} kHz, "
                    f"bw={radio.bandwidth} Hz, "
                    f"sf={radio.spreading_factor}, "
                    f"cr={radio.coding_rate}."
                )

        elif any(p is not None for p in params):
            _LOGGER.warning(
                "Radio config incomplete (frequency, bandwidth, spreading_factor, "
                "coding_rate must all be set). Skipping set_radio()."
            )

        if radio.tx_power is not None:
            result = await self._mc.commands.set_tx_power(radio.tx_power)

            if result.type == EventType.ERROR:
                _LOGGER.warning(
                    f"Could not set TX power to {radio.tx_power} dBm: {result.payload}"
                )
            else:
                _LOGGER.info(
                    f"TX power set to {radio.tx_power} dBm."
                )