# Lazy Mode Implementation

## Overview

Lazy mode allows the minicap APK to work in a pull-based model where the server sends a frame only when explicitly requested by the client, rather than continuously pushing frames at a fixed frame rate.

## Motivation

In certain use cases (e.g., low-bandwidth connections, battery-saving scenarios, or infrequent screen updates), continuously pushing frames is unnecessary. Lazy mode enables:

- Client controls frame rate by sending requests
- Server sends the latest captured frame upon request
- Reduces unnecessary frame encoding and network traffic

## Usage

### Server (APK) Side

Start minicap with `-l` flag:

```bash
CLASSPATH=/data/local/tmp/minicap.apk app_process /system/bin io.devicefarmer.minicap.Main \
    -n minicap -P 1080x1920@1080x1920/0 -l
```

### Client (Python) Side

The `minicap_apk.py` already supports lazy mode via the `lazy=True` parameter in `get_stream()`:

```python
device = adb.connect()
minicap = MinicapApk(device.adb)
frame_gen = minicap.get_stream(lazy=True)  # Uses -l flag

# Request frames on demand
for frame in frame_gen:
    # Process frame
    pass
```

## Implementation Details

### Modified Files

| File | Changes |
|------|---------|
| `Main.kt` | Added `-l` flag parsing and help text |
| `Parameters.kt` | Added `lazyMode: Boolean` field |
| `MinicapClientOutput.kt` | Added `requestFrame()` method to read client request |
| `BaseProvider.kt` | Added lazy mode worker thread and frame buffering |

### Architecture

**Normal Mode (Push)**:
```
onImageAvailable → encode → clientOutput.send() → repeat (continuous)
```

**Lazy Mode (Pull)**:
```
onImageAvailable → encode → save to latestFrameData

Worker Thread:
while running:
    requestFrame() → wait for 1 byte from client
    send(latestFrameData) → send latest captured frame
```

### Key Logic

1. **Main.kt**: Parses `-l` flag and passes `lazyMode=true` to SurfaceProvider
2. **BaseProvider**:
   - If `lazyMode=true`: starts a worker thread that blocks on `requestFrame()`
   - `onImageAvailable` still captures frames but stores them in `latestFrameData` instead of sending immediately
3. **MinicapClientOutput**:
   - `requestFrame()` blocks on `socket.inputStream.read()` waiting for client request

## Protocol

The protocol remains unchanged from the standard minicap protocol. In lazy mode:

1. Server sends banner (24 bytes) on connection
2. Client sends `b"1"` (1 byte) to request a frame
3. Server responds with frame (4-byte size header + JPEG data)

## Testing

Verify lazy mode works:

```bash
# Build APK
cd experimental && ./gradlew assembleDebug

# Install on device
adb install app/build/outputs/apk/debug/app-debug.apk

# Start in lazy mode
adb shell "CLASSPATH=/data/local/tmp/minicap-debug.apk app_process /system/bin io.devicefarmer.minicap.Main -n minicap -P 1080x1920@1080x1920/0 -l"

# From client, send requests with delay to verify behavior
```

## Compatibility

- Lazy mode is compatible with existing clients that use `get_stream(lazy=True)` from `minicap_apk.py`
- Non-lazy mode remains the default behavior when `-l` is not specified