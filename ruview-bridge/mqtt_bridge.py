#!/usr/bin/env python3
"""
RuView → Home Assistant MQTT Bridge

Polls the RuView REST API (verified against ruvnet/wifi-densepose 0.6.5)
and publishes sensor data to MQTT with HA auto-discovery. Creates entities
for presence, breathing, heart rate, motion, fall detection, person count,
sleep state, and activity.

API field mapping (validated 2026-05-21 against simulated mode):
  /api/v1/vital-signs:
    vital_signs.breathing_rate_bpm   -> breathing_bpm
    vital_signs.heart_rate_bpm       -> heart_bpm
    vital_signs.breathing_confidence -> breathing_confidence
  /api/v1/sensing/latest:
    classification.presence          -> presence (bool)
    classification.motion_level      -> activity (string: present_still / present_moving / absent / ...)
    estimated_persons                -> person_count
    features.motion_band_power       -> motion_level (normalized via tanh(x/50))
    (no fall field in 0.6.5)         -> fall_flag defaults to False

Known v1 limitations:
  - The high-level API is fused across nodes, so bedroom + living_room
    receive identical breathing/heart/presence payloads. Per-node split
    requires either a future /api/v1/nodes/{id}/vitals endpoint or
    a node-aware aggregation written against /api/v1/sensing/latest:nodes[].
  - RuView 0.6.5 has no fall-detection REST endpoint. fall_confirmed stays
    False; HA automation #4 (Fall Emergency) will never fire until either
    RuView ships /api/v1/falls or we add a heuristic here.

Environment variables:
  RUVIEW_URL      - RuView sensing server URL (default: http://localhost:3000)
  MQTT_HOST       - MQTT broker host (default: 192.168.1.3)
  MQTT_PORT       - MQTT broker port (default: 1883)
  MQTT_USER       - MQTT username
  MQTT_PASS       - MQTT password
  POLL_INTERVAL   - Seconds between API polls (default: 2)
  NODE_MAP        - node_id:room_name pairs (default: 1:bedroom,2:living_room)
"""

import json
import logging
import math
import os
import signal
import sys
import time
from datetime import datetime

import paho.mqtt.client as mqtt
import requests

RUVIEW_URL    = os.getenv("RUVIEW_URL", "http://localhost:3000")
MQTT_HOST     = os.getenv("MQTT_HOST", "192.168.1.3")
MQTT_PORT     = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER     = os.getenv("MQTT_USER", "ruview")
MQTT_PASS     = os.getenv("MQTT_PASS", "ruview123")
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "2"))
NODE_MAP_STR  = os.getenv("NODE_MAP", "1:bedroom,2:living_room")

NODE_MAP = {}
for pair in NODE_MAP_STR.split(","):
    parts = pair.strip().split(":")
    if len(parts) == 2:
        NODE_MAP[parts[0].strip()] = parts[1].strip()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("ruview-bridge")


class RoomState:
    def __init__(self, room_name):
        self.room = room_name
        self.sleeping = False
        self.sleep_start = None
        self.last_presence = False
        self.presence_off_since = None
        self.fall_debounce = []
        self.breathing_history = []
        self.motion_history = []
        self.wakeup_triggered = False

    def update(self, presence, breathing, heart_rate, motion, fall_flag):
        now = time.time()
        result = {
            "sleeping": self.sleeping,
            "wakeup": False,
            "fall_confirmed": False,
            "empty_minutes": 0,
            "activity": "unknown",
        }

        if not presence and self.last_presence:
            self.presence_off_since = now
        if presence:
            self.presence_off_since = None
        self.last_presence = presence

        if self.presence_off_since:
            result["empty_minutes"] = round((now - self.presence_off_since) / 60, 1)

        # Activity preference order:
        #   1. API's classification.motion_level string when available (most accurate)
        #   2. fall back to numeric motion thresholds
        api_motion_level = getattr(self, "_last_motion_level_str", None)
        if api_motion_level in ("absent", "absent_movement", "absent_still"):
            result["activity"] = "empty"
        elif api_motion_level == "present_still":
            result["activity"] = "still"
        elif api_motion_level == "present_moving":
            result["activity"] = "walking" if motion >= 0.3 else "sitting"
        elif not presence:
            result["activity"] = "empty"
        elif motion < 0.05:
            result["activity"] = "still"
        elif motion < 0.3:
            result["activity"] = "sitting"
        elif motion < 0.7:
            result["activity"] = "walking"
        else:
            result["activity"] = "active"

        hour = datetime.now().hour
        is_sleep_hours = hour >= 22 or hour < 8
        self.breathing_history.append(breathing)
        self.motion_history.append(motion)
        if len(self.breathing_history) > 30:
            self.breathing_history.pop(0)
            self.motion_history.pop(0)

        recent_motion = self.motion_history[-15:]
        recent_breathing = self.breathing_history[-15:]
        avg_motion = sum(recent_motion) / max(len(recent_motion), 1)
        avg_breathing = sum(recent_breathing) / max(len(recent_breathing), 1)

        was_sleeping = self.sleeping
        if is_sleep_hours and presence and 6 <= avg_breathing <= 22 and avg_motion < 0.1:
            if not self.sleeping:
                self.sleeping = True
                self.sleep_start = now
                self.wakeup_triggered = False
                log.info(f"[{self.room}] Sleep detected")
        elif self.sleeping and (avg_motion > 0.3 or avg_breathing > 22):
            self.sleeping = False
            log.info(f"[{self.room}] Wakeup detected")

        result["sleeping"] = self.sleeping

        if was_sleeping and not self.sleeping and not self.wakeup_triggered:
            result["wakeup"] = True
            self.wakeup_triggered = True

        if fall_flag:
            self.fall_debounce.append(now)
        self.fall_debounce = [t for t in self.fall_debounce if now - t < 5]
        if len(self.fall_debounce) >= 2:
            result["fall_confirmed"] = True

        return result


DISCOVERY_PREFIX = "homeassistant"


def publish_discovery(client, room):
    device = {
        "identifiers": [f"ruview_{room}"],
        "name": f"RuView {room.replace('_', ' ').title()}",
        "manufacturer": "RuView",
        "model": "ESP32-S3 CSI Node",
        "sw_version": "1.0",
    }
    state_topic = f"ruview/{room}/state"

    sensors = [
        {"type": "binary_sensor", "key": "presence", "name": "Presence",
         "device_class": "occupancy", "value_template": "{{ value_json.presence }}",
         "payload_on": "true", "payload_off": "false"},
        {"type": "binary_sensor", "key": "fall_detected", "name": "Fall Detected",
         "device_class": "safety", "value_template": "{{ value_json.fall_confirmed }}",
         "payload_on": "true", "payload_off": "false"},
        {"type": "binary_sensor", "key": "sleeping", "name": "Sleeping",
         "value_template": "{{ value_json.sleeping }}",
         "payload_on": "true", "payload_off": "false", "icon": "mdi:sleep"},
        {"type": "binary_sensor", "key": "wakeup", "name": "Wakeup",
         "value_template": "{{ value_json.wakeup }}",
         "payload_on": "true", "payload_off": "false", "icon": "mdi:alarm"},
        {"type": "sensor", "key": "breathing_rate", "name": "Breathing Rate",
         "unit": "BPM", "value_template": "{{ value_json.breathing_bpm | round(1) }}",
         "icon": "mdi:lungs", "state_class": "measurement"},
        {"type": "sensor", "key": "heart_rate", "name": "Heart Rate",
         "unit": "BPM", "value_template": "{{ value_json.heart_bpm | round(1) }}",
         "icon": "mdi:heart-pulse", "state_class": "measurement"},
        {"type": "sensor", "key": "motion_energy", "name": "Motion Energy",
         "unit": "%", "value_template": "{{ (value_json.motion_level * 100) | round(1) }}",
         "icon": "mdi:motion-sensor", "state_class": "measurement"},
        {"type": "sensor", "key": "person_count", "name": "Person Count",
         "unit": "persons", "value_template": "{{ value_json.person_count }}",
         "icon": "mdi:account-group", "state_class": "measurement"},
        {"type": "sensor", "key": "activity", "name": "Activity",
         "value_template": "{{ value_json.activity }}", "icon": "mdi:run"},
        {"type": "sensor", "key": "empty_minutes", "name": "Empty Duration",
         "unit": "min", "value_template": "{{ value_json.empty_minutes }}",
         "icon": "mdi:timer-outline", "state_class": "measurement"},
    ]

    for s in sensors:
        uid = f"ruview_{room}_{s['key']}"
        config_topic = f"{DISCOVERY_PREFIX}/{s['type']}/{uid}/config"
        payload = {
            "name": s["name"],
            "unique_id": uid,
            "state_topic": state_topic,
            "value_template": s["value_template"],
            "device": device,
            "availability_topic": f"ruview/{room}/status",
            "payload_available": "online",
            "payload_not_available": "offline",
        }
        if "unit" in s:
            payload["unit_of_measurement"] = s["unit"]
        if "device_class" in s:
            payload["device_class"] = s["device_class"]
        if "icon" in s:
            payload["icon"] = s["icon"]
        if "state_class" in s:
            payload["state_class"] = s["state_class"]
        if "payload_on" in s:
            payload["payload_on"] = s["payload_on"]
            payload["payload_off"] = s["payload_off"]

        client.publish(config_topic, json.dumps(payload), retain=True)
        log.info(f"Discovery: {config_topic}")


def main():
    running = True

    def stop(sig, frame):
        nonlocal running
        running = False
        log.info("Shutting down...")

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    connack_rc = {"value": None}
    _RC_MEANINGS = {
        0: "accepted",
        1: "refused: unacceptable protocol version",
        2: "refused: identifier rejected",
        3: "refused: server unavailable",
        4: "refused: bad username or password",
        5: "refused: not authorized (user missing or ACL denies it)",
    }

    def on_connect(c, userdata, flags, rc):
        connack_rc["value"] = rc
        meaning = _RC_MEANINGS.get(rc, f"unknown rc={rc}")
        if rc == 0:
            log.info(f"MQTT CONNACK ok ({meaning})")
        else:
            log.error(f"MQTT CONNACK failed: {meaning}")

    client = mqtt.Client(client_id="ruview-bridge")
    client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.on_connect = on_connect
    log.info(f"Connecting to MQTT at {MQTT_HOST}:{MQTT_PORT} as user={MQTT_USER}")
    try:
        client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    except Exception as e:
        log.error(f"MQTT TCP connect failed: {e}")
        sys.exit(1)
    client.loop_start()

    # Wait briefly for CONNACK so we can fail-fast on bad creds.
    deadline = time.time() + 5
    while connack_rc["value"] is None and time.time() < deadline:
        time.sleep(0.1)
    if connack_rc["value"] != 0:
        log.error(
            "MQTT broker did not accept the connection within 5s. "
            "Most common cause: the `ruview` user in Mosquitto does not exist yet. "
            "Create the HA user at Settings -> People -> Users with the password from "
            "RUVIEW_MQTT_PASS, or update Mosquitto's logins.json."
        )
        client.loop_stop()
        sys.exit(1)

    # Mapping from RuView's per-node `motion_level` string to a 0..1 motion
    # estimate. RuView 0.6.5 doesn't return a numeric motion-energy per node,
    # so we synthesize one from the classification string. The downstream
    # RoomState uses this for sleep/wakeup detection where the absolute
    # number matters less than the relative ordering.
    MOTION_LEVEL_NUMERIC = {
        "absent": 0.0,
        "absent_still": 0.0,
        "absent_movement": 0.0,
        "present_still": 0.05,
        "present_moving": 0.6,
        "active": 0.8,
    }

    room_states = {}
    room_availability = {}  # room -> "online" | "offline" (last published)
    for node_id, room in NODE_MAP.items():
        publish_discovery(client, room)
        # Defer initial availability — first poll cycle will publish online
        # only for rooms whose node is actually present and active.
        room_states[room] = RoomState(room)
        room_availability[room] = None

    log.info(f"Bridge started. Polling {RUVIEW_URL} every {POLL_INTERVAL}s")
    log.info(f"Node map: {NODE_MAP}")

    while running:
        try:
            vitals = requests.get(f"{RUVIEW_URL}/api/v1/vital-signs", timeout=5).json()
            nodes_resp = requests.get(f"{RUVIEW_URL}/api/v1/nodes", timeout=5).json()

            # Fused vital signs — RuView 0.6.5 returns one combined value
            # across all nodes. Until /api/v1/nodes/{id}/vitals lands, both
            # rooms get the same breathing/heart numbers when both are
            # occupied. Acceptable v1 trade-off; documented limitation.
            vs = vitals.get("vital_signs", {})
            breathing = float(vs.get("breathing_rate_bpm", 0) or 0)
            heart = float(vs.get("heart_rate_bpm", 0) or 0)
            confidence = float(vs.get("breathing_confidence", 0) or 0)

            # Build per-node-id lookup from /api/v1/nodes (keyed by string
            # to match NODE_MAP, which comes from env as "1:bedroom,...").
            nodes_by_id = {
                str(n.get("node_id")): n for n in nodes_resp.get("nodes", [])
            }

            for node_id, room in NODE_MAP.items():
                state = room_states[room]
                node = nodes_by_id.get(node_id)
                is_online = (node is not None) and (node.get("status") == "active")

                # Publish availability transitions (retained) so HA flips the
                # entity to "unavailable" when a board is offline or stale.
                want_avail = "online" if is_online else "offline"
                if room_availability[room] != want_avail:
                    client.publish(f"ruview/{room}/status", want_avail, retain=True)
                    room_availability[room] = want_avail
                    log.info(f"[{room}] availability -> {want_avail}")

                if not is_online:
                    # Don't publish state for a missing/stale node — HA's
                    # availability_topic will mark its entities unavailable.
                    continue

                motion_level_str = node.get("motion_level", "absent")
                person_count = int(node.get("person_count", 0) or 0)
                presence = not motion_level_str.startswith("absent")
                motion = MOTION_LEVEL_NUMERIC.get(motion_level_str, 0.3)
                fall_flag = False  # RuView 0.6.5 has no fall endpoint

                state._last_motion_level_str = motion_level_str
                derived = state.update(presence, breathing, heart, motion, fall_flag)

                payload = {
                    "presence": "true" if presence else "false",
                    "breathing_bpm": breathing,           # fused — see TODO above
                    "heart_bpm": heart,                   # fused — see TODO above
                    "motion_level": motion,
                    "breathing_confidence": confidence,
                    "person_count": person_count,
                    "fall_confirmed": "true" if derived["fall_confirmed"] else "false",
                    "sleeping": "true" if derived["sleeping"] else "false",
                    "wakeup": "true" if derived["wakeup"] else "false",
                    "activity": derived["activity"],
                    "empty_minutes": derived["empty_minutes"],
                    "timestamp": int(time.time()),
                }

                client.publish(f"ruview/{room}/state", json.dumps(payload))
                log.debug(f"[{room}] presence={presence} motion={motion_level_str} "
                          f"persons={person_count} br={breathing:.1f} hr={heart:.1f}")

        except requests.exceptions.ConnectionError:
            log.warning(f"RuView server unreachable at {RUVIEW_URL}")
        except Exception as e:
            log.error(f"Error: {e}")

        time.sleep(POLL_INTERVAL)

    for room in NODE_MAP.values():
        client.publish(f"ruview/{room}/status", "offline", retain=True)
    client.loop_stop()
    client.disconnect()
    log.info("Bridge stopped.")


if __name__ == "__main__":
    main()
