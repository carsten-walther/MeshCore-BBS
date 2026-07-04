"""MQTT publisher for the MeshCore BBS.

Publishes radio packet and device status data to all configured MQTT brokers,
using topics compatible with the meshcore-packet-capture schema:

  meshcore/{IATA}/{PUBLIC_KEY}/status   — device online / offline
  meshcore/{IATA}/{PUBLIC_KEY}/packets  — per-packet RF metadata (SNR, RSSI, …)

Each broker runs its own background task that maintains a persistent connection
and drains a per-broker asyncio.Queue. On reconnect the online-status is
re-published automatically. Shutdown sends the offline-status before closing.
"""

import asyncio
import hashlib
import json
import logging
import ssl
from datetime import datetime, timezone

import aiomqtt

from bbs.config import MqttBrokerConfig, MqttConfig

_LOGGER = logging.getLogger(__name__)

_SENTINEL = object()  # signals the broker task to publish offline + exit

_ROUTE_MAP = {
    "FLOOD": "F",
    "TC_FLOOD": "F",       # meshcore_parser naming
    "TRANSPORT_FLOOD": "F",  # packet_capture naming
    "DIRECT": "D",
    "TC_DIRECT": "T",
    "TRANSPORT_DIRECT": "T",
}


class MqttPublisher:
    def __init__(
        self,
        cfg: MqttConfig,
        device_name: str,
        public_key: str,
        device_info: dict | None = None,
    ) -> None:
        self._cfg = cfg
        self._device_name = device_name
        self._public_key = public_key.upper() if public_key else "UNKNOWN"
        self._device_info = device_info or {}
        self._queues: list[asyncio.Queue] = []
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        for broker in self._cfg.brokers:
            if not broker.enabled or not broker.host:
                continue
            q: asyncio.Queue = asyncio.Queue()
            self._queues.append(q)
            self._tasks.append(asyncio.create_task(self._broker_task(broker, q)))
        if self._tasks:
            _LOGGER.info(
                f"MQTT publisher started: {len(self._tasks)} broker(s), "
                f"IATA={self._cfg.iata}, origin_id={self._public_key}."
            )
            _LOGGER.info(f"MQTT topics: {self._topic('status')} / {self._topic('packets')}")

    async def stop(self) -> None:
        for q in self._queues:
            await q.put(_SENTINEL)
        for task in self._tasks:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
            except (asyncio.TimeoutError, Exception):
                pass
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._queues.clear()
        self._tasks.clear()

    async def publish_packet(self, rx: dict) -> None:
        """Queue a packet event (RX_LOG_DATA payload) for all brokers."""
        if not self._queues:
            return
        serialized = json.dumps(self._format_packet(rx))
        for q in self._queues:
            await q.put(("packets", serialized, False))

    def _topic(self, suffix: str) -> str:
        return f"meshcore/{self._cfg.iata}/{self._public_key}/{suffix}"

    def _status_payload(self, status: str) -> dict:
        return {
            "status": status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "origin": self._device_name,
            "origin_id": self._public_key,
            **self._device_info,
        }

    def _format_packet(self, rx: dict) -> dict:
        now = datetime.now(timezone.utc)
        raw_hex = (rx.get("payload") or "").upper()

        route = _ROUTE_MAP.get(rx.get("route_typename", ""), "U")
        packet_type = str(rx.get("payload_type", 0))

        pkt_payload = rx.get("pkt_payload", b"")
        payload_len = str(len(pkt_payload) if isinstance(pkt_payload, bytes) else 0)
        total_len = str(rx.get("payload_length", len(raw_hex) // 2 if raw_hex else 0))

        data: dict = {
            "origin": self._device_name,
            "origin_id": self._public_key,
            "timestamp": now.isoformat(),
            "type": "PACKET",
            "direction": "rx",
            "time": now.strftime("%H:%M:%S"),
            "date": now.strftime("%d/%m/%Y"),
            "len": total_len,
            "packet_type": packet_type,
            "route": route,
            "payload_len": payload_len,
            "SNR": str(rx.get("snr", "Unknown")),
            "RSSI": str(rx.get("rssi", "Unknown")),
            "score": 0
        }

        if raw_hex:
            data["raw"] = raw_hex
            try:
                data["hash"] = hashlib.sha256(bytes.fromhex(raw_hex)).hexdigest()[:16].upper()
            except ValueError:
                pass

        # path only for direct routes, matching packet_capture behaviour
        if route == "D":
            path_raw = rx.get("path", "")
            path_hash_size = int(rx.get("path_hash_size", 2))
            char_len = path_hash_size * 2
            if path_raw:
                hops = [path_raw[i:i + char_len] for i in range(0, len(path_raw), char_len)]
                if hops:
                    data["path"] = ",".join(hops)

        return data

    async def _broker_task(self, broker: MqttBrokerConfig, queue: asyncio.Queue) -> None:
        label = f"{broker.host}:{broker.port}"
        client_kwargs: dict = dict(
            hostname=broker.host,
            port=broker.port,
            keepalive=broker.keepalive,
        )
        if broker.username:
            client_kwargs["username"] = broker.username
        if broker.password:
            client_kwargs["password"] = broker.password
        if broker.tls:
            ctx = ssl.create_default_context()
            if not broker.tls_verify:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            client_kwargs["tls_context"] = ctx

        while True:
            try:
                async with aiomqtt.Client(**client_kwargs) as client:
                    _LOGGER.info(f"MQTT connected to {label}.")
                    status_topic = self._topic("status")
                    await client.publish(
                        status_topic,
                        json.dumps(self._status_payload("online")),
                        qos=broker.qos,
                        retain=True,
                    )
                    while True:
                        item = await queue.get()
                        if item is _SENTINEL:
                            await client.publish(
                                status_topic,
                                json.dumps(self._status_payload("offline")),
                                qos=broker.qos,
                                retain=True,
                            )
                            _LOGGER.info(f"MQTT offline status sent to {label}.")
                            return
                        suffix, payload, retain = item
                        await client.publish(
                            self._topic(suffix), payload, qos=broker.qos, retain=retain
                        )
            except aiomqtt.MqttError as e:
                _LOGGER.warning(f"MQTT {label}: {e} — reconnecting in 30s.")
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                _LOGGER.error(f"MQTT {label}: unexpected error: {e} — reconnecting in 60s.")
                await asyncio.sleep(60)
