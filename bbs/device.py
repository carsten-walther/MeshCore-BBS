"""Device setup helpers for the MeshCore BBS.

Standalone async functions that apply configuration to the MeshCore device
and query device information. They depend only on a connected MeshCore
instance plus config values passed as arguments — no store, router, or MQTT
dependency — making them independently testable.
"""

import logging

from meshcore import EventType, MeshCore

from bbs.config import RadioConfig

_LOGGER = logging.getLogger(__name__)


async def apply_device_name(mc: MeshCore, name: str) -> None:
    result = await mc.commands.set_name(name)
    if result.type == EventType.ERROR:
        _LOGGER.warning(f"Could not set BBS name to '{name}': {result.payload}")
    else:
        _LOGGER.info(f"BBS name set to '{name}'.")


async def apply_device_loc(mc: MeshCore, lat: float, lon: float) -> None:
    if lat == 0.0 and lon == 0.0:
        _LOGGER.info("BBS location not configured — skipping set_coords.")
        return

    result = await mc.commands.set_coords(lat, lon)
    if result.type == EventType.ERROR:
        _LOGGER.warning(f"Could not set BBS location ({lat}, {lon}): {result.payload}")
        return
    _LOGGER.info(f"BBS location set to ({lat}, {lon}).")

    policy_result = await mc.commands.set_advert_loc_policy(1)
    if policy_result.type == EventType.ERROR:
        _LOGGER.warning(f"Could not enable location in adverts: {policy_result.payload}")


async def apply_radio_config(mc: MeshCore, radio: RadioConfig) -> None:
    params = (radio.frequency, radio.bandwidth, radio.spreading_factor, radio.coding_rate)

    if all(p is not None for p in params):
        result = await mc.commands.set_radio(
            freq=radio.frequency,
            bw=radio.bandwidth,
            sf=radio.spreading_factor,
            cr=radio.coding_rate,
            repeat=None,
        )
        if result.type == EventType.ERROR:
            _LOGGER.warning(f"Could not apply radio params: {result.payload}")
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
        result = await mc.commands.set_tx_power(radio.tx_power)
        if result.type == EventType.ERROR:
            _LOGGER.warning(f"Could not set TX power to {radio.tx_power} dBm: {result.payload}")
        else:
            _LOGGER.info(f"TX power set to {radio.tx_power} dBm.")


async def query_device_info(mc: MeshCore) -> dict:
    """Build the device_info dict for the MQTT status payload.

    Queries DEVICE_INFO, STATS_CORE, STATS_RADIO, and STATS_PACKETS.
    Radio parameters are taken from self_info (populated at connect time).
    Missing fields are silently omitted so the payload stays valid even on
    older firmware.
    """
    info: dict = {}
    stats: dict = {}

    try:
        result = await mc.commands.send_device_query()
        if result.type != EventType.ERROR:
            p = result.payload
            if p.get("model"):
                info["model"] = p["model"].strip()
            ver = (p.get("ver") or "").strip()
            if ver:
                info["firmware_version"] = ver
                info["client_version"] = f"meshcore/{ver}"
            if "repeat" in p:
                info["repeat"] = "on" if p["repeat"] else "off"
    except Exception as e:
        _LOGGER.debug(f"Could not query device info: {e}")

    si = mc.self_info
    freq, bw, sf, cr = si.get("radio_freq"), si.get("radio_bw"), si.get("radio_sf"), si.get("radio_cr")
    if all(v is not None for v in (freq, bw, sf, cr)):
        info["radio"] = f"{freq},{bw},{sf},{cr}"

    try:
        r = await mc.commands.get_stats_core()
        if r.type != EventType.ERROR:
            for key in ("battery_mv", "uptime_secs", "errors", "queue_len"):
                if r.payload.get(key) is not None:
                    stats[key] = r.payload[key]
    except Exception as e:
        _LOGGER.debug(f"Could not query core stats: {e}")

    try:
        r = await mc.commands.get_stats_radio()
        if r.type != EventType.ERROR:
            for key in ("noise_floor", "tx_air_secs", "rx_air_secs"):
                if r.payload.get(key) is not None:
                    stats[key] = r.payload[key]
    except Exception as e:
        _LOGGER.debug(f"Could not query radio stats: {e}")

    try:
        r = await mc.commands.get_stats_packets()
        if r.type != EventType.ERROR:
            p = r.payload
            if p.get("sent") is not None:
                stats["packets_sent"] = p["sent"]
            if p.get("recv") is not None:
                stats["packets_received"] = p["recv"]
            if p.get("recv_errors") is not None:
                stats["recv_errors"] = p["recv_errors"]
    except Exception as e:
        _LOGGER.debug(f"Could not query packet stats: {e}")

    if stats:
        info["stats"] = stats
    return info
