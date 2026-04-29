import subprocess
import schedule
import datetime
import math
import time
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
timelapseLengthSeconds = int(os.getenv("TIMELAPSE_LENGTH_SECONDS", "10"))

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

    try:
        if timelapseLengthSeconds <= 0:
            print("TIMELAPSE_LENGTH_SECONDS must be > 0")
            return False
    except:
        print("TIMELAPSE_LENGTH_SECONDS invalid")
        return False
    return True

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

# Grabs snapshot from the RTSP source
def save_snapshot():
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

    result = subprocess.run(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=60,
        text=True
    )

    if result.returncode == 0:
        print(f"File saved: {filename}")
        return True
    else:
        print("ERROR: ")
        print(result.stderr)
        return False

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
        "-pix_fmt", "yuv420p",
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

# Runs snapshot and (if successful) timelapse
def trigger():
    print("Trigger has been executed at " + str(datetime.datetime.now()))
    success = save_snapshot()
    if success:
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
    print(f"Timelapse directory: {timelapseDir}")

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
