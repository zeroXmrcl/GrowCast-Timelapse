import datetime
import os
from enum import Enum
from pathlib import Path

import requests

PLUGIN_ID = "growcast.timelapse"
SYNC_INTERVAL_MINUTES = 10


class SyncResult(Enum):
    APPLIED = "applied"
    NO_CHANGE = "no_change"
    FAILED = "failed"


class PauseGate:
    def __init__(self):
        self.paused = False

    def should_run_trigger(self):
        if self.paused:
            return False
        return True

    def set_paused(self, new_paused):
        self.paused = new_paused


def log_api(message):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[API {timestamp}] {message}")


def parse_int_setting(name, value, *, minimum=None, exclusive_minimum=False):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        print(f"{name} must be an integer")
        return None

    if minimum is not None:
        if exclusive_minimum and parsed <= minimum:
            print(f"{name} must be > {minimum}")
            return None
        if not exclusive_minimum and parsed < minimum:
            print(f"{name} must be >= {minimum}")
            return None

    return parsed


def validate_env_values(env_values):
    times = [env_values.get("TIME_1"), env_values.get("TIME_2"), env_values.get("TIME_3")]
    times = [t for t in times if t]

    for t in times:
        try:
            datetime.datetime.strptime(t, "%H:%M")
        except ValueError:
            print(f"Invalid time format: {t} (expected HH:MM)")
            return False

    interval_raw = env_values.get("INTERVAL", "")
    if interval_raw:
        try:
            interval = int(interval_raw)
            if interval <= 0:
                print("INTERVAL must be > 0")
                return False
        except ValueError:
            print("INTERVAL must be an integer")
            return False

    if not times and not interval_raw:
        print("You must define TIME_X or INTERVAL")
        return False

    quality = env_values.get("TIMELAPSE_QUALITY", "medium")
    if quality not in ("low", "medium", "high"):
        print(f"Invalid TIMELAPSE_QUALITY: {quality}")
        return False

    if parse_int_setting(
        "TIMELAPSE_LENGTH_SECONDS",
        env_values.get("TIMELAPSE_LENGTH_SECONDS", "10"),
        minimum=0,
        exclusive_minimum=True,
    ) is None:
        return False

    for key, minimum, exclusive in (
        ("RETRY_MAX_SECONDS", 0, False),
        ("RETRY_DELAY_SECONDS", 0, True),
    ):
        if key not in env_values:
            continue
        raw = env_values[key]
        if not raw:
            continue
        if parse_int_setting(key, raw, minimum=minimum, exclusive_minimum=exclusive) is None:
            return False

    return True


def api_settings_to_env_values(settings):
    interval = settings.get("intervalMinutes")
    return {
        "TZ": settings.get("timezone") or "UTC",
        "TIME_1": settings.get("time1") or "",
        "TIME_2": settings.get("time2") or "",
        "TIME_3": settings.get("time3") or "",
        "INTERVAL": "" if interval is None else str(interval),
        "TIMELAPSE_LENGTH_SECONDS": str(settings.get("timelapseLengthSeconds", 10)),
        "TIMELAPSE_QUALITY": settings.get("timelapseQuality") or "medium",
    }


def env_values_key(env_values):
    return tuple(sorted(env_values.items()))


def update_env_file(env_path: Path, updates):
    lines = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines(keepends=True)

    updated_keys = set()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            new_lines.append(line if line.endswith("\n") else line + "\n")
            continue
        if "=" in stripped:
            key, _, _ = stripped.partition("=")
            key = key.strip()
            if key in updates:
                new_lines.append(f"{key}={updates[key]}\n")
                updated_keys.add(key)
                continue
        new_lines.append(line if line.endswith("\n") else line + "\n")

    for key, value in updates.items():
        if key not in updated_keys:
            new_lines.append(f"{key}={value}\n")

    env_path.write_text("".join(new_lines), encoding="utf-8")


class SettingsSync:
    def __init__(self, env_path, api_url, api_token, pause_gate, on_apply, on_reschedule):
        self.env_path = env_path
        self.api_url = api_url or ""
        self.api_token = api_token or ""
        self.pause_gate = pause_gate
        self.on_apply = on_apply
        self.on_reschedule = on_reschedule
        self.last_settings_version = None
        self.last_applied_env_key = None

    @property
    def enabled(self):
        return bool(self.api_url and self.api_token)

    def fetch_mesh_settings(self):
        base_url = self.api_url.rstrip("/")
        url = f"{base_url}/api/mesh/{PLUGIN_ID}"
        log_api(f"Fetching settings from {url}")

        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {self.api_token}"},
            timeout=30,
            allow_redirects=False,
        )
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError:
            preview = (response.text or "")[:200]
            raise ValueError(
                f"API returned non-JSON response (status={response.status_code}): {preview!r}"
            ) from None
        log_api(
            f"Fetched settings (version={payload.get('settingsVersion')}, "
            f"paused={payload.get('settings', {}).get('paused')})"
        )
        return payload

    def settings_changed(self, settings, settings_version):
        env_values = api_settings_to_env_values(settings)
        env_key = env_values_key(env_values)

        if settings_version is not None:
            return self.last_settings_version != settings_version

        if self.last_applied_env_key is None:
            return True

        if env_key != self.last_applied_env_key:
            log_api("settingsVersion missing; applying because settings content changed")
            return True

        return False

    def apply_settings(self, settings, settings_version, runtime_snapshot):
        env_values = api_settings_to_env_values(settings)
        if not validate_env_values(env_values):
            log_api("Rejected API settings: validation failed")
            return SyncResult.FAILED

        self.on_apply(env_values)
        update_env_file(self.env_path, env_values)
        self.last_settings_version = settings_version
        self.last_applied_env_key = env_values_key(env_values)

        snap = runtime_snapshot()
        log_api(
            f"Applied settings to .env (version={settings_version}, paused={self.pause_gate.paused}, "
            f"TIME_1={snap['TIME_1'] or '-'}, TIME_2={snap['TIME_2'] or '-'}, "
            f"TIME_3={snap['TIME_3'] or '-'}, INTERVAL={snap['INTERVAL'] or '-'}, "
            f"TIMELAPSE_LENGTH_SECONDS={snap['TIMELAPSE_LENGTH_SECONDS']}, "
            f"TIMELAPSE_QUALITY={snap['TIMELAPSE_QUALITY']})"
        )

        self.on_reschedule()
        return SyncResult.APPLIED

    def sync(self, runtime_snapshot):
        try:
            payload = self.fetch_mesh_settings()
        except Exception as e:
            log_api(f"Failed to fetch settings: {e}")
            return SyncResult.FAILED

        settings_version = payload.get("settingsVersion")
        settings = payload.get("settings", {})
        new_paused = bool(settings.get("paused", False))

        self.pause_gate.set_paused(new_paused)

        if not self.settings_changed(settings, settings_version):
            return SyncResult.NO_CHANGE

        return self.apply_settings(settings, settings_version, runtime_snapshot)