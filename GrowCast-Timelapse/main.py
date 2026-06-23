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

ENV_PATH = Path(__file__).resolve().parent / ".env"
PLUGIN_ID = "growcast.timelapse"
SYNC_INTERVAL_MINUTES = 10

# Import Settings
load_dotenv(ENV_PATH)
apiURL = os.getenv("API_URL")
apiToken = os.getenv("API_TOKEN")
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

api_sync_enabled = bool(apiURL and apiToken)
paused = False
deferred_trigger_pending = False
catchup_on_next_tick = False
last_settings_version = None


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

    parsed_timelapse_length = parse_int_setting(
        "TIMELAPSE_LENGTH_SECONDS",
        env_values.get("TIMELAPSE_LENGTH_SECONDS", "10"),
        minimum=0,
        exclusive_minimum=True,
    )
    return parsed_timelapse_length is not None


def update_env_file(updates):
    lines = []
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines(keepends=True)

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

    ENV_PATH.write_text("".join(new_lines), encoding="utf-8")


def snapshot_runtime_settings():
    return {
        "time1": time1,
        "time2": time2,
        "time3": time3,
        "snapshotMinuteInterval": snapshotMinuteInterval,
        "timelapseLengthSecondsRaw": timelapseLengthSecondsRaw,
        "timelapseQuality": timelapseQuality,
        "timelapseLengthSeconds": timelapseLengthSeconds,
        "retryMaxSeconds": retryMaxSeconds,
        "retryDelaySeconds": retryDelaySeconds,
        "paused": paused,
    }


def restore_runtime_settings(previous):
    global time1, time2, time3, snapshotMinuteInterval
    global timelapseLengthSecondsRaw, timelapseQuality
    global timelapseLengthSeconds, retryMaxSeconds, retryDelaySeconds, paused

    time1 = previous["time1"]
    time2 = previous["time2"]
    time3 = previous["time3"]
    snapshotMinuteInterval = previous["snapshotMinuteInterval"]
    timelapseLengthSecondsRaw = previous["timelapseLengthSecondsRaw"]
    timelapseQuality = previous["timelapseQuality"]
    timelapseLengthSeconds = previous["timelapseLengthSeconds"]
    retryMaxSeconds = previous["retryMaxSeconds"]
    retryDelaySeconds = previous["retryDelaySeconds"]
    paused = previous["paused"]


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


def fetch_mesh_settings():
    base_url = apiURL.rstrip("/")
    url = f"{base_url}/api/mesh/{PLUGIN_ID}"
    log_api(f"Fetching settings from {url}")

    response = requests.get(
        url,
        headers={"Authorization": f"Bearer {apiToken}"},
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


def apply_api_settings(settings, settings_version):
    global paused, last_settings_version

    env_values = api_settings_to_env_values(settings)
    if not validate_env_values(env_values):
        log_api("Rejected API settings: validation failed")
        return False

    was_paused = paused
    previous = snapshot_runtime_settings()
    new_paused = bool(settings.get("paused", False))

    if not apply_env_values_to_runtime(env_values):
        log_api("Rejected API settings: failed to apply numeric settings")
        restore_runtime_settings(previous)
        return False

    paused = new_paused
    update_env_file(env_values)
    last_settings_version = settings_version
    log_api(
        f"Applied settings to .env (version={settings_version}, paused={paused}, "
        f"TIME_1={time1 or '-'}, TIME_2={time2 or '-'}, TIME_3={time3 or '-'}, "
        f"INTERVAL={snapshotMinuteInterval or '-'}, "
        f"TIMELAPSE_LENGTH_SECONDS={timelapseLengthSeconds}, "
        f"TIMELAPSE_QUALITY={timelapseQuality})"
    )

    reschedule_jobs()

    if was_paused and not paused:
        handle_pause_disabled()

    return True


def handle_pause_disabled():
    global catchup_on_next_tick

    if deferred_trigger_pending:
        log_api("Pause disabled with deferred trigger pending - catch-up scheduled for next tick")
        catchup_on_next_tick = True


def run_pending_catchup():
    global catchup_on_next_tick

    if catchup_on_next_tick:
        catchup_on_next_tick = False
        trigger(force=True)


def sync_from_api():
    global last_settings_version, paused

    try:
        payload = fetch_mesh_settings()
    except Exception as e:
        log_api(f"Failed to fetch settings: {e}")
        return False

    settings_version = payload.get("settingsVersion")
    settings = payload.get("settings", {})
    new_paused = bool(settings.get("paused", False))

    if last_settings_version is not None and settings_version == last_settings_version:
        was_paused = paused
        paused = new_paused
        if was_paused and not paused:
            handle_pause_disabled()
        return False

    return apply_api_settings(settings, settings_version)


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

    if api_sync_enabled:
        schedule.every(SYNC_INTERVAL_MINUTES).minutes.do(sync_from_api)


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

# Validating User input
def validate_inputs() :
    if not rtsp_url:
        print("RTSP_STREAM is required")
        return False

    times = [time1, time2, time3]
    # Filter out falsy stuff
    times = [t for t in times if t]

    def is_valid_time(t):
        try:
            datetime.datetime.strptime(t, "%H:%M")
            return True
        except ValueError:
            return False

    for t in times:
        if not is_valid_time(t):
            print(f"Invalid time format: {t} (expected HH:MM)")
            return False

    interval = None
    if snapshotMinuteInterval:
        try:
            interval = int(snapshotMinuteInterval)
            if interval <= 0:
                print("INTERVAL must be > 0")
                return False
        except ValueError:
            print("INTERVAL must be an integer")
            return False

    if not times and not interval:
        print("You must define TIME_X or INTERVAL")
        return False

    if not parse_numeric_settings():
        return False

    return True

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
    global deferred_trigger_pending

    if paused and not force:
        deferred_trigger_pending = True
        print(
            f"Paused - skipping snapshot at "
            f"{datetime.datetime.now().strftime('%d - %m - %Y // %H : %M')} "
            f"(one catch-up will run when pause is disabled)"
        )
        return

    deferred_trigger_pending = False
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
    if api_sync_enabled:
        print(f"API sync: enabled ({apiURL}, every {SYNC_INTERVAL_MINUTES} min)")
        print(f"Paused: {paused}")
    else:
        print("API sync: disabled")
    print("---------------------------")

if "--test" in sys.argv:
    if not validate_inputs():
        raise ValueError("Invalid .env configuration")
    trigger()
    sys.exit(0)

if api_sync_enabled:
    sync_from_api()

if not validate_inputs():
    raise ValueError("Invalid .env configuration")

welcome()
reschedule_jobs()

while True:
    schedule.run_pending()
    run_pending_catchup()
    time.sleep(1)
