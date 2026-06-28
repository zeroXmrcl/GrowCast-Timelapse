import subprocess
import schedule
import requests
import datetime
import math
import time
import sys
import os
from pathlib import Path
from dotenv import load_dotenv

from api import (
    SYNC_INTERVAL_MINUTES,
    PauseGate,
    SettingsSync,
    parse_int_setting,
    validate_env_values,
)

ENV_PATH = Path(__file__).resolve().parent / ".env"

# Import Settings
load_dotenv(ENV_PATH)
API_URL = os.getenv("API_URL")
API_TOKEN = os.getenv("API_TOKEN")
time1 = os.getenv("TIME_1")
time2 = os.getenv("TIME_2")
time3 = os.getenv("TIME_3")
rtsp_url = os.getenv("RTSP_STREAM") or ""
snapshotDir = os.getenv("SNAPSHOT_DIR_OUT") or "./snapshots"
timelapseDir = os.getenv("TIMELAPSE_DIR_OUT") or "./timelapse"
snapshotMinuteInterval = os.getenv("INTERVAL")
timelapseLengthSecondsRaw = os.getenv("TIMELAPSE_LENGTH_SECONDS", "10")
timelapseQuality = os.getenv("TIMELAPSE_QUALITY", "medium")
webHookURL = os.getenv("WH_URL") or ""
retryMaxSecondsRaw = os.getenv("RETRY_MAX_SECONDS", "3600")
retryDelaySecondsRaw = os.getenv("RETRY_DELAY_SECONDS", "60")

timelapseLengthSeconds = 10
retryMaxSeconds = 3600
retryDelaySeconds = 60

pause_gate = PauseGate()


def build_env_from_globals():
    return {
        "TZ": os.getenv("TZ", "UTC"),
        "TIME_1": time1 or "",
        "TIME_2": time2 or "",
        "TIME_3": time3 or "",
        "INTERVAL": snapshotMinuteInterval or "",
        "TIMELAPSE_LENGTH_SECONDS": timelapseLengthSecondsRaw,
        "TIMELAPSE_QUALITY": timelapseQuality,
        "RETRY_MAX_SECONDS": retryMaxSecondsRaw,
        "RETRY_DELAY_SECONDS": retryDelaySecondsRaw,
    }


def runtime_snapshot():
    return {
        "TIME_1": time1,
        "TIME_2": time2,
        "TIME_3": time3,
        "INTERVAL": snapshotMinuteInterval,
        "TIMELAPSE_LENGTH_SECONDS": timelapseLengthSeconds,
        "TIMELAPSE_QUALITY": timelapseQuality,
    }


def parse_numeric_settings():
    global timelapseLengthSeconds, retryMaxSeconds, retryDelaySeconds

    parsed_timelapse_length = parse_int_setting(
        "TIMELAPSE_LENGTH_SECONDS",
        timelapseLengthSecondsRaw,
        minimum=0,
        exclusive_minimum=True,
    )
    parsed_retry_max = parse_int_setting(
        "RETRY_MAX_SECONDS",
        retryMaxSecondsRaw,
        minimum=0,
    )
    parsed_retry_delay = parse_int_setting(
        "RETRY_DELAY_SECONDS",
        retryDelaySecondsRaw,
        minimum=0,
        exclusive_minimum=True,
    )

    if any(value is None for value in (parsed_timelapse_length, parsed_retry_max, parsed_retry_delay)):
        return False

    timelapseLengthSeconds = parsed_timelapse_length
    retryMaxSeconds = parsed_retry_max
    retryDelaySeconds = parsed_retry_delay
    return True


def apply_env_values_to_runtime(env_values):
    global time1, time2, time3, snapshotMinuteInterval
    global timelapseLengthSecondsRaw, timelapseQuality

    time1 = env_values.get("TIME_1", "")
    time2 = env_values.get("TIME_2", "")
    time3 = env_values.get("TIME_3", "")
    snapshotMinuteInterval = env_values.get("INTERVAL", "")
    timelapseLengthSecondsRaw = env_values.get("TIMELAPSE_LENGTH_SECONDS", "10")
    timelapseQuality = env_values.get("TIMELAPSE_QUALITY", "medium")

    tz = env_values.get("TZ")
    if tz:
        os.environ["TZ"] = tz
        if hasattr(time, "tzset"):
            time.tzset()

    return parse_numeric_settings()


def reschedule_jobs():
    schedule.clear()

    if time1:
        schedule.every().day.at(time1).do(trigger)
    if time2:
        schedule.every().day.at(time2).do(trigger)
    if time3:
        schedule.every().day.at(time3).do(trigger)
    if snapshotMinuteInterval:
        minutes = int(snapshotMinuteInterval)
        if minutes > 0:
            schedule.every(minutes).minutes.do(trigger)

    if settings_sync.enabled:
        schedule.every(SYNC_INTERVAL_MINUTES).minutes.do(sync_from_api)


settings_sync = SettingsSync(
    ENV_PATH,
    API_URL,
    API_TOKEN,
    pause_gate,
    apply_env_values_to_runtime,
    reschedule_jobs,
)


def sync_from_api():
    settings_sync.sync(runtime_snapshot)


# Sends new snapshots to Webhook
def webhook(file_path, message="New snapshot!"):
    if not webHookURL:
        print("WH_URL is not set, skipping upload.")
        return False

    if not os.path.exists(file_path):
        print(f"File not found: {file_path}")
        return False

    try:
        with open(file_path, "rb") as file:
            files = {
                "file": (os.path.basename(file_path), file)
            }

            data = {
                "content": message
            }

            response = requests.post(
                webHookURL,
                data=data,
                files=files,
                timeout=30
            )

        if response.status_code in [200, 204]:
            print(f"Webhook snapshot uploaded: {file_path}.")
            return True
        else:
            print(f"Webhook request failed: {response.status_code}")
            print(response.text)
            return False

    except Exception as e:
        print("Upload error:")
        print(e)
        return False


def validate_inputs():
    if not rtsp_url:
        print("RTSP_STREAM is required")
        return False

    if not validate_env_values(build_env_from_globals()):
        return False

    return parse_numeric_settings()


if "--validate" in sys.argv:
    print("input valid:", validate_inputs())
    sys.exit(0)

# Translate quality setting to ffmpeg CRF value
def get_quality():
    if timelapseQuality == "low":
        return "28"
    elif timelapseQuality == "medium":
        return "23"
    elif timelapseQuality == "high":
        return "18"
    else:
        return "23"

# Generates filename
def create_filename():
    os.makedirs(snapshotDir, exist_ok=True)

    existing = []
    for name in os.listdir(snapshotDir):
        if name.lower().endswith(".webp"):
            base = os.path.splitext(name)[0]
            if base.isdigit():
                existing.append(int(base))

    next_number = max(existing, default=0) + 1
    return os.path.join(snapshotDir, f"{next_number:04d}.webp")

# Tries to grab one snapshot from the RTSP source
def grab_snapshot():
    print("Taking snapshot...")
    filename = create_filename()
    print(filename)

    cmd = [
        "ffmpeg",
        "-y",
        "-rtsp_transport", "tcp",
        "-i", rtsp_url,
        "-frames:v", "1",
        "-q:v", "80",
        filename,
    ]

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=60,
            text=True
        )
    except subprocess.TimeoutExpired as e:
        print("ERROR: snapshot attempt timed out")
        if e.stderr:
            print(e.stderr)
        if os.path.exists(filename):
            os.remove(filename)
        return False

    if result.returncode == 0:
        print(f"File saved: {filename}")
        return filename
    else:
        print("ERROR: ")
        print(result.stderr)
        if os.path.exists(filename):
            os.remove(filename)
        return False

# Grabs snapshot or waits if camera is not reachable
def save_snapshot():
    deadline = time.monotonic() + retryMaxSeconds

    while True:
        snapshot = grab_snapshot()
        if snapshot:
            return snapshot

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print(f"Could not take snapshot within {retryMaxSeconds} seconds - Giving up.")
            return False

        wait_seconds = min(retryDelaySeconds, remaining)
        print(f"Camera unavailable. Retrying in {int(wait_seconds)} seconds...")
        time.sleep(wait_seconds)

if "--snapshot" in sys.argv:
    if not parse_numeric_settings():
        sys.exit(1)
    if not rtsp_url:
        print("RTSP_STREAM is required")
        sys.exit(1)
    save_snapshot()
    sys.exit(0)

# Renders timelapse from all NUMERIC.webp files in ./snapshots
def create_timelapse():
    print("Creating timelapse...")
    os.makedirs(timelapseDir, exist_ok=True)

    image_files = []
    for name in os.listdir(snapshotDir):
        if name.lower().endswith(".webp"):
            base = os.path.splitext(name)[0]
            if base.isdigit():
                image_files.append(name)

    image_files.sort()

    if not image_files:
        print("No images found.")
        return False

    image_count = len(image_files)
    fps = max(1, math.ceil(image_count / timelapseLengthSeconds))

    output_file = os.path.join(
        timelapseDir,
        "latest_timelapse.mp4"
    )

    input_pattern = os.path.join(snapshotDir, "%04d.webp")

    cmd = [
        "ffmpeg",
        "-y",
        "-framerate", str(fps),
        "-i", input_pattern,
        "-c:v", "libx264",
        "-crf", get_quality(),
        "-preset", "slow",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        output_file,
    ]

    print(f"Creating timelapse, found {image_count} images, {fps} fps ...")

    result = subprocess.run(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True
    )

    if result.returncode == 0:
        print(f"Timelapse saved: {output_file}")
        return True
    else:
        print("ERROR: ")
        print(result.stderr)
        return False

if "--render" in sys.argv:
    if not parse_numeric_settings():
        sys.exit(1)
    create_timelapse()
    sys.exit(0)

# Runs snapshot and (if successful) timelapse
def trigger(force=False):
    if not pause_gate.should_run_trigger(force=force):
        print(
            f"Paused - skipping snapshot at "
            f"{datetime.datetime.now().strftime('%d - %m - %Y // %H : %M')} "
            f"(one catch-up will run when pause is disabled)"
        )
        return

    print(f"Trigger has been executed at {datetime.datetime.now().strftime('%d - %m - %Y // %H : %M')}")
    success = save_snapshot()
    if success:
        webhook(success, datetime.datetime.now().strftime("%d - %m - %Y // %H : %M"))
        create_timelapse()

# Prints configuration
def welcome():
    print("Configuration looks good!")
    print("GrowCast Timelapse started!")
    print("---------------------------")
    if time1 or time2 or time3:
        print(f"Times set: {time1} {time2} {time3}")
    print(f"Snapshot interval: {snapshotMinuteInterval} minutes")
    print(f"Snapshot directory: {snapshotDir}")
    print("---------------------------")
    print(f"Timelapse directory: {timelapseDir}")
    print(f"Timelapse length: {timelapseLengthSeconds} seconds")
    print(f"Timelapse quality: {timelapseQuality}")
    print("---------------------------")
    if settings_sync.enabled:
        print(f"API sync: enabled ({API_URL}, every {SYNC_INTERVAL_MINUTES} min)")
        print(f"Paused: {pause_gate.paused}")
    else:
        print("API sync: disabled")
    print("---------------------------")

if "--test" in sys.argv:
    if not validate_inputs():
        raise ValueError("Invalid .env configuration")
    trigger()
    sys.exit(0)

if settings_sync.enabled:
    sync_from_api()

if not validate_inputs():
    raise ValueError("Invalid .env configuration")

welcome()
reschedule_jobs()

while True:
    schedule.run_pending()
    pause_gate.run_pending_catchup(trigger)
    time.sleep(1)