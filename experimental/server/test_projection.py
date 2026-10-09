#!/usr/bin/env python3
"""
Test -P projection handling for the minicap APK (exact size + --fit-projection).

Verifies on a device/emulator:
  1. exact mode (default): lazy-mode JPEG frames and the banner report exactly the
     requested -P target size, even when its aspect ratio differs from the display's
  2. --fit-projection: the legacy rewrite to the display aspect ratio
  3. push mode, -s screenshots and a rotated display (airtest-style -P convention)

The display is expected to be an upright 720x1280 for the non-rotated cases
(adjust PROJ_DISPLAY below when running against another resolution).
"""

import socket
import struct
import subprocess
import sys
import time

ADB_DEVICE = "127.0.0.1:16384"

REPO_ROOT = subprocess.run(
    ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
).stdout.strip()
APK_PATH = REPO_ROOT + r"\experimental\app\prebuild\minicap-debug.apk"
DEVICE_APK = "/data/local/tmp/minicap-debug.apk"
LOCAL_PORT = 13191
failures = []


def adb_shell(cmd, check=True, binary=False):
    result = subprocess.run(
        ["adb", "-s", ADB_DEVICE, "shell", cmd], capture_output=True, check=check
    )
    return result.stdout if binary else result.stdout.decode(errors="replace")


def check(label, expected, actual):
    ok = expected == actual
    print(f"{'PASS' if ok else 'FAIL'}: {label}: expected {expected}, got {actual}")
    if not ok:
        failures.append(label)


def jpeg_size(data):
    # minimal JPEG SOF parser
    i = 2
    while i < len(data) - 9:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            height = struct.unpack(">H", data[i + 5:i + 7])[0]
            width = struct.unpack(">H", data[i + 7:i + 9])[0]
            return width, height
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    raise ValueError("no SOF marker found")


def wait_server_ready(name):
    for _ in range(50):
        if name in adb_shell("cat /proc/net/unix", check=False):
            return
        time.sleep(0.1)
    raise RuntimeError(f"server {name} not ready")


def read_frame(sock):
    header = b""
    while len(header) < 4:
        chunk = sock.recv(4 - len(header))
        if not chunk:
            raise EOFError("no frame header")
        header += chunk
    size = struct.unpack("<I", header)[0]
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise EOFError("truncated frame")
        data += chunk
    return data


def start_server(name, proj, extra=""):
    return subprocess.Popen(
        ["adb", "-s", ADB_DEVICE, "shell",
         f"CLASSPATH={DEVICE_APK} app_process /system/bin "
         f"io.devicefarmer.minicap.Main -n {name} -P {proj} {extra}"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )


def lazy_case(label, proj, expected_jpeg, extra=""):
    """Starts a server (any mode via extra), checks banner + one lazy frame size."""
    name = f"proj_{abs(hash(label)) % 100000}"
    proc = start_server(name, proj, extra)
    try:
        wait_server_ready(name)
        subprocess.run(["adb", "-s", ADB_DEVICE, "forward", f"tcp:{LOCAL_PORT}",
                        f"localabstract:{name}"], capture_output=True, check=True)
        sock = socket.create_connection(("127.0.0.1", LOCAL_PORT), timeout=10)
        sock.settimeout(10)
        try:
            banner = struct.unpack("<2B5I2B", sock.recv(24))
            check(f"{label} banner desired", expected_jpeg, (banner[5], banner[6]))
            sock.send(b"1")
            check(f"{label} jpeg size", expected_jpeg, jpeg_size(read_frame(sock)))
        finally:
            sock.close()
    finally:
        proc.terminate()
        adb_shell(f"pkill -f 'minicap.Main.*{name}'", check=False)


def screenshot_case(proj, expected_jpeg):
    out = adb_shell(
        f"CLASSPATH={DEVICE_APK} app_process /system/bin "
        f"io.devicefarmer.minicap.Main -P {proj} -s", binary=True
    )
    check("screenshot jpeg size", expected_jpeg, jpeg_size(out))


def rotation_case():
    adb_shell("settings put system accelerometer_rotation 0")
    adb_shell("settings put system user_rotation 1")
    time.sleep(1.5)  # let the rotation settle
    try:
        # airtest first-stream convention: natural-frame real, upright target, rot 90
        lazy_case("rotated exact", "720x1280@640x360/90", (640, 360))
    finally:
        adb_shell("settings put system user_rotation 0")


def main():
    subprocess.run(["adb", "-s", ADB_DEVICE, "push", APK_PATH, DEVICE_APK],
                   capture_output=True, check=True)

    wm = adb_shell("wm size")
    if "720x1280" not in wm:
        print(f"unexpected display size {wm.strip()}, adjust the test projections")
        sys.exit(2)

    # exact mode, aspect-matching request: fit is a no-op, size unchanged
    lazy_case("exact aspect-match", "720x1280@360x640/0", (360, 640), extra="-l")
    # exact mode, aspect-mismatched request (the issue's core complaint):
    # fit would rewrite 480x640 -> 360x640, exact must keep 480x640
    lazy_case("exact mismatch", "720x1280@480x640/0", (480, 640), extra="-l")
    # exact mode, landscape target on a portrait display:
    # fit would rewrite 640x360 -> 203x360, exact must keep 640x360
    lazy_case("exact landscape", "720x1280@640x360/0", (640, 360), extra="-l")
    # fit mode opt-in restores the legacy rewrite (480x640 -> 360x640)
    lazy_case("fit mismatch", "720x1280@480x640/0", (360, 640),
              extra="-l --fit-projection")
    # push mode and one-shot screenshots honor the exact size too
    lazy_case("push exact", "720x1280@480x640/0", (480, 640), extra="-r 5")
    screenshot_case("720x1280@480x640/0", (480, 640))
    # rotated display, airtest-style -P
    rotation_case()

    print()
    if failures:
        print("FAILED:", failures)
        sys.exit(1)
    print("all projection tests passed")


if __name__ == "__main__":
    main()
