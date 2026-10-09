# Lazy Mode Implementation

## Overview

Lazy mode allows the minicap APK to work in a pull-based model where the server sends a frame only when explicitly requested by the client, rather than continuously pushing frames at a fixed frame rate.

## Motivation

In certain use cases (e.g., low-bandwidth connections, battery-saving scenarios, or infrequent screen updates), continuously pushing frames is unnecessary. Lazy mode enables:

- Client controls frame delivery by sending requests
- Server sends the latest captured frame upon request
- Reduced network traffic: no frame data is written to the socket unless the client asks for one (e.g., while the screen is static, nothing is sent)
- The screen-refresh callback no longer performs JPEG encoding; it only caches the latest frame as a bitmap
- JPEG encoding happens on demand, once per client request, on the lazy mode worker thread (the display thread never encodes in lazy mode)

Note: lazy mode trades continuous streaming for per-request latency. Each request pays for one JPEG encoding, so it is most beneficial when the screen changes slowly or frames are needed infrequently.

## Usage

### Server (APK) Side

Start minicap with `-l` flag:

```bash
CLASSPATH=/data/local/tmp/minicap.apk app_process /system/bin io.devicefarmer.minicap.Main \
    -n minicap -P 1080x1920@1080x1920/0 -l
```

`-l` is listed as its own entry in the `-h` help output:

```
-l:            Lazy mode: send frame only when recv request from client.
```

## Projection Handling (`-P`)

By default the requested `-P` target size is honored **exactly**: every encoded frame (lazy mode, push mode and `-s` screenshots alike) is exactly the requested `width x height`, matching what clients such as airtest's `snapshot(projection=(w, h))` need for cross-device template matching. The `INFO: <real>@<target>/<rot>` startup line and the banner's virtual display size both report the requested size verbatim.

Internally the capture buffer keeps the display's aspect ratio (the display projection would otherwise stretch the content into a skewed frame), and the captured bitmap is rescaled to the exact requested size right before JPEG encoding. When the requested size already has the display's aspect ratio nothing is rescaled — the buffer is the requested size itself.

Pass `--fit-projection` to restore the legacy behavior: the requested target is rewritten to the display aspect ratio before any capture happens (the startup `INFO` line then shows the fitted size, like the native binary does), and frames are delivered at that fitted size with no rescaling. This flag affects all frame-producing modes the same way.

```bash
# exact size: JPEG frames are exactly 360x640, even on a 16:9 display
adb shell CLASSPATH=/data/local/tmp/minicap.apk app_process /system/bin \
    io.devicefarmer.minicap.Main -l -P 1280x720@360x640/0 -n repro

# legacy fit: a 360x640 request on a 16:9 display yields 360x203 frames
adb shell CLASSPATH=/data/local/tmp/minicap.apk app_process /system/bin \
    io.devicefarmer.minicap.Main -l --fit-projection -P 1280x720@360x640/0 -n repro
```

### Client (Python) Side

The Python client lives at `experimental/server/minicap_apk.py` and supports lazy mode via the `lazy=True` parameter of `get_stream()` (this is the default). When `lazy=True` the server is started with `-l`, and the client sends a 1-byte request (`b"1"`) before reading each frame:

```python
device = adb.connect()
minicap = MinicapApk(device.adb)
frame_gen = minicap.get_stream(lazy=True)  # starts the server with -l

# Each iteration sends one request and returns the latest frame
for frame in frame_gen:
    # Process frame
    pass
```

## Implementation Details

### Modified Files

| File | Changes |
|------|---------|
| `Main.kt` | Added `-l` flag parsing, `-l` entry in the help text, and the `lazyMode` field of `Parameters` |
| `MinicapClientOutput.kt` | Added `requestFrame()` to block on a 1-byte client request (returns `false` on end of stream) and `sendFrame()` to write a frame with its 4-byte little-endian size header |
| `BaseProvider.kt` | Added lazy mode worker thread (`lazy-mode-worker`), bitmap frame cache, and bounded first-frame wait |

### Architecture

**Push mode (default, unchanged)**:

```
onImageAvailable → encode JPEG into output buffer → clientOutput.send() → repeat (continuous)
```

Every screen refresh is encoded and pushed to the client as fast as `-r` allows.

**Lazy mode (pull)**:

```
Display callback thread (onImageAvailable):
    cache the latest frame as a bitmap (no JPEG encoding)
    refresh rate throttled by -r

Worker thread ("lazy-mode-worker"):
    while client connected:
        requestFrame()  → block until 1 byte arrives from the client
        encode the latest cached bitmap to JPEG (on demand)
        sendFrame()     → 4-byte little-endian length header + JPEG data
    on end of stream (client disconnected) → exit cleanly
```

The two sides are decoupled through a `latestFrame: Bitmap` cache: the display callback only refreshes the cache, and encoding happens on the worker thread only when a request arrives.

### Key Logic

1. **Main.kt**: Parses the `-l` flag and passes `lazyMode=true` to the provider
2. **BaseProvider**:
   - `onImageAvailable` in lazy mode copies the image into the `latestFrame` bitmap cache and closes the image; no encoding. Like push mode, this is throttled by `-r`, so in lazy mode `-r` limits how often the cache is refreshed — it does not affect when frames are sent
   - On connection, starts the `lazy-mode-worker` thread, which loops over `requestFrame()` and `sendLatestFrame()`
   - When `requestFrame()` returns `false` (end of stream), the client has disconnected and the worker exits cleanly
   - If a request arrives before any frame has been cached, the worker waits up to 2 seconds (polling every 50 ms, below the client's 3-second receive timeout) for the display's initial frame; if no frame appears, it logs a warning and drops the request without sending anything
3. **MinicapClientOutput**:
   - `requestFrame()` blocks on `socket.inputStream.read()` waiting for the 1-byte client request
   - `sendFrame()` writes the 4-byte little-endian length header followed by the JPEG data, matching the push mode wire format

## Protocol

The wire protocol is the same as the standard minicap protocol. In lazy mode:

1. Server sends the banner (24 bytes) on connection
2. Client sends `b"1"` (1 byte) to request a frame
3. Server responds with one frame (4-byte little-endian size header + JPEG data)

Frame delivery guarantees in lazy mode:

- **Initial frame**: the display produces an initial frame shortly after connection, even if the screen is static, so the first request can be served immediately in the common case
- **Request before first frame**: if a request arrives before the display has produced any frame, the server waits up to 2 seconds for it (intentionally below the client's 3-second receive timeout); if no frame shows up, the request is dropped, a warning is logged, and no data is sent (the client's receive times out)
- **Disconnect**: when the client closes the connection, the worker reads end of stream and exits cleanly; no resources are left blocked on the socket

## Testing

Two test scripts live next to the client in `experimental/server/`:

- `test_lazy_mode.py`: pushes the prebuilt APK, starts the server in lazy mode, and captures frames at 2-second intervals
- `test_lazy_mode_timed.py`: measures frame timing precisely and saves frames for verification

```bash
# Build the APK from source
cd experimental && ./gradlew assembleDebug

# Install it on the device (or rely on the scripts pushing experimental/app/prebuild/minicap-debug.apk)
adb install app/build/outputs/apk/debug/app-debug.apk

# Run the bundled lazy mode tests (adjust the ADB_DEVICE constant in each script to your device serial)
python server/test_lazy_mode.py
python server/test_lazy_mode_timed.py
```

## Compatibility

- Lazy mode is compatible with existing clients that use `get_stream(lazy=True)` from `experimental/server/minicap_apk.py`
- Push mode remains the default behavior when `-l` is not specified; its wire format is unchanged
- The exact `-P` output applies to every mode; clients that depended on the fitted size must pass `--fit-projection`
