import subprocess
import schedule
import requests
import datetime
import math
import time
import sys
import os
from dotenv import load_dotenv

# Import Settings
load_dotenv()
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
def trigger():
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

if "--test" in sys.argv:
    if not validate_inputs():
        raise ValueError("Invalid .env configuration")
    trigger()
    sys.exit(0)

if not validate_inputs():
    raise ValueError("Invalid .env configuration")
else:
    welcome()

# Set triggers on specified times
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

while True:
    schedule.run_pending()
    time.sleep(1)
