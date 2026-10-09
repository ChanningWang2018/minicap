#!/usr/bin/env python3
"""
Test lazy mode for minicap APK.
This script pushes the APK, starts the server in lazy mode, and captures frames at 2-second intervals.
"""

import subprocess
import time
import socket
import struct
import os
import sys
import threading
import signal
import shutil

ADB_DEVICE = "127.0.0.1:16384"

def adb(args, check=True):
    cmd = ["adb"]
    if ADB_DEVICE:
        cmd.extend(["-s", ADB_DEVICE])
    cmd.extend(args)
    return subprocess.run(cmd, check=check, capture_output=True, text=True)

def adb_shell(cmd, check=True):
    full_cmd = ["adb"]
    if ADB_DEVICE:
        full_cmd.extend(["-s", ADB_DEVICE])
    full_cmd.extend(["shell", cmd])
    return subprocess.run(full_cmd, check=check, capture_output=True, text=True)

def adb_push(local, remote):
    cmd = ["adb"]
    if ADB_DEVICE:
        cmd.extend(["-s", ADB_DEVICE])
    cmd.extend(["push", local, remote])
    return subprocess.run(cmd, check=True)

def adb_forward(local, remote):
    cmd = ["adb"]
    if ADB_DEVICE:
        cmd.extend(["-s", ADB_DEVICE])
    cmd.extend(["forward", local, remote])
    return subprocess.run(cmd, check=True)

def adb_forward_remove(local):
    cmd = ["adb"]
    if ADB_DEVICE:
        cmd.extend(["-s", ADB_DEVICE])
    cmd.extend(["forward", "--remove", local])
    return subprocess.run(cmd, check=True)

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
APK_PATH = os.path.join(REPO_ROOT, "experimental", "app", "prebuild", "minicap-debug.apk")
DEVICE_APK_PATH = "/data/local/tmp/minicap-debug.apk"
SERVER_CMD = "CLASSPATH={} app_process /system/bin io.devicefarmer.minicap.Main".format(DEVICE_APK_PATH)
LOCAL_PORT = 13131
SOCKET_NAME = "minicap_test"

class MinicapTestClient:
    def __init__(self):
        self.sock = None
        self.proc = None
        self.forward_port = None
    
    def get_display_size(self):
        result = adb_shell(f"dumpsys window | grep -Eo 'init=[0-9]+x[0-9]+' | head -1", check=False)
        output = result.stdout.strip()
        
        if not output:
            result = adb_shell("wm size", check=False)
            output = result.stdout.strip()
        
        if not output:
            return 1080, 1920
        
        if "=" in output:
            size = output.split("=")[-1].strip()
        else:
            size = output.strip()
        
        if "x" in size:
            w, h = map(int, size.split("x"))
            return w, h
        return 1080, 1920
    
    def push_apk(self):
        print(f"Pushing APK to device: {APK_PATH}")
        adb_push(APK_PATH, DEVICE_APK_PATH)
        adb_shell(f"chmod 755 {DEVICE_APK_PATH}")
        print("APK pushed successfully")
    
    def setup_forward(self):
        print(f"Setting up forward for {SOCKET_NAME}")
        adb_forward(f"tcp:{LOCAL_PORT}", f"localabstract:{SOCKET_NAME}")
        self.forward_port = LOCAL_PORT
    
    def cleanup_forward(self):
        if self.forward_port:
            print(f"Cleaning up forward tcp:{self.forward_port}")
            adb_forward_remove(f"tcp:{self.forward_port}")
    
    def start_server(self, width, height, lazy=True):
        print(f"Starting minicap server (lazy={lazy}) with size {width}x{height}")
        
        socket_arg = SOCKET_NAME
        lazy_arg = "-l" if lazy else "-r 30"
        
        cmd = f"{SERVER_CMD} -n '{socket_arg}' -P {width}x{height}@{width}x{height}/0 {lazy_arg}"
        
        adb_cmd = ["adb"]
        if ADB_DEVICE:
            adb_cmd.extend(["-s", ADB_DEVICE])
        adb_cmd.extend(["shell", cmd])
        
        self.proc = subprocess.Popen(
            adb_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        
        time.sleep(0.5)
        
        output_lines = []
        start_time = time.time()
        while time.time() - start_time < 5:
            line = self.proc.stderr.readline()
            if line:
                output_lines.append(line.decode('utf-8', errors='ignore').strip())
                print(f"Server: {line.decode('utf-8', errors='ignore').strip()}")
                if f"Listening on socket : {socket_arg}" in line.decode():
                    break
            if self.proc.poll() is not None:
                break
            time.sleep(0.1)
    
    def cleanup_server(self):
        if self.proc:
            print("Stopping server...")
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2)
            except:
                self.proc.kill()
    
    def connect(self):
        print(f"Connecting to localhost:{self.forward_port}")
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.connect(("localhost", self.forward_port))
        self.sock.settimeout(30)  # 增加超时时间
    
    def recv_header(self):
        data = self.sock.recv(24)
        if len(data) != 24:
            raise Exception(f"Invalid header: expected 24 bytes, got {len(data)}")
        
        header = struct.unpack("<2B5I2B", data)
        print(f"Header: version={header[0]}, header_size={header[1]}, pid={header[2]}, "
              f"real_size={header[3]}x{header[4]}, virtual_size={header[5]}x{header[6]}, "
              f"orientation={header[7]}, quirks={header[8]}")
        return header
    
    def request_frame(self):
        print("  Sending '1'...")
        self.sock.send(b"1")
        print("  Sent")
    
    def recv_frame(self):
        try:
            self.sock.settimeout(10)
            
            header = self.sock.recv(4)
            if len(header) != 4:
                return None
            
            frame_size = struct.unpack("<I", header)[0]
            
            data = b""
            remaining = frame_size
            while remaining > 0:
                chunk = self.sock.recv(min(remaining, 65536))
                if not chunk:
                    break
                data += chunk
                remaining -= len(chunk)
            
            return data
        except socket.timeout:
            return None
        except Exception as e:
            print(f"  Error: {e}")
            return None
    
    def disconnect(self):
        if self.sock:
            self.sock.close()
            self.sock = None

def test_max_fps(client, count=10):
    """测试无延迟的最大 FPS"""
    print("=" * 60)
    print(f"Testing MAX FPS (no delay), {count} frames")
    print("=" * 60)
    
    frames = []
    
    # 预热
    client.request_frame()
    time.sleep(0.5)
    frame_data = client.recv_frame()
    
    if not frame_data:
        time.sleep(0.5)
        client.request_frame()
        time.sleep(0.5)
        frame_data = client.recv_frame()
    
    if frame_data:
        frames.append(frame_data)
        print(f"Frame 1: {len(frame_data)} bytes (warmup)")
    else:
        print("Frame 1: FAILED")
        return frames
    
    start_time = time.time()
    
    for i in range(1, count):
        client.request_frame()
        
        # 无延迟 - 尽可能快地请求
        frame_data = client.recv_frame()
        
        if frame_data:
            frames.append(frame_data)
            elapsed = time.time() - start_time
            print(f"Frame {i+1}: {len(frame_data)} bytes, {elapsed:.3f}s")
        else:
            print(f"Frame {i+1}: FAILED")
            break
    
    total_time = time.time() - start_time
    frame_count = len(frames)
    
    print("=" * 60)
    if frame_count > 1:
        fps = (frame_count - 1) / total_time if total_time > 0 else 0
        print(f"Results:")
        print(f"  Frames: {frame_count}")
        print(f"  Time: {total_time:.3f}s")
        print(f"  MAX FPS: {fps:.2f}")
    else:
        print("Failed to capture frames")
    
    return frames

def run_fps_test(count=5):
    client = MinicapTestClient()
    
    try:
        width, height = client.get_display_size()
        print(f"Display size: {width}x{height}")
        
        client.push_apk()
        client.setup_forward()
        client.start_server(width, height, lazy=True)
        
        time.sleep(1)
        
        client.connect()
        header = client.recv_header()
        print(f"Connected, header: {header}")
        
        frames = test_max_fps(client, count=count)
        
        if len(frames) >= 2:
            print(f"\nTotal frames: {len(frames)}")
        
        return frames
        
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return []
    
    finally:
        client.disconnect()
        client.cleanup_server()
        client.cleanup_forward()

def capture_test(client, interval=2, count=5, output_prefix=os.path.join(REPO_ROOT, "output", "frame")):
    """Capture frames at specified interval and analyze timing"""
    
    print("=" * 60)
    print(f"Capturing {count} frames at {interval}-second intervals")
    print("=" * 60)
    
    frames = []
    
    client.request_frame()
    time.sleep(0.5)
    
    for i in range(count):
        start_time = time.time()
        
        client.request_frame()
        
        frame_data = client.recv_frame()
        elapsed = time.time() - start_time
        
        if frame_data:
            frame_size = len(frame_data)
            frames.append(frame_data)
            print(f"Frame {i+1}: size={frame_size} bytes, elapsed={elapsed:.3f}s")
            
            filename = f"{output_prefix}_{i+1:03d}.jpg"
            with open(filename, "wb") as f:
                f.write(frame_data)
            print(f"  Saved: {filename}")
        else:
            print(f"Frame {i+1}: FAILED to receive")
        
        if i < count - 1:
            sleep_time = interval - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)
    
    print("=" * 60)
    print("Analysis:")
    if len(frames) >= 2:
        print(f"  First frame size: {len(frames[0])} bytes")
        print(f"  Last frame size: {len(frames[-1])} bytes")
        
        if len(frames) >= 3:
            sizes = [len(f) for f in frames]
            avg_diff = sum(abs(sizes[i+1] - sizes[i]) for i in range(len(sizes)-1)) / (len(sizes)-1)
            print(f"  Avg frame size change: {avg_diff:.0f} bytes")
            
            if avg_diff < 1000:
                print("  WARNING: Frame sizes very similar - possible frame buffering detected!")
                print("  Lazy mode may not be working correctly.")
            else:
                print("  Frame sizes vary significantly - lazy mode appears to be working.")
    
    return frames

def run_test(lazy=True, interval=2, count=5, output_prefix=os.path.join(REPO_ROOT, "output", "frame")):
    client = MinicapTestClient()
    
    try:
        width, height = client.get_display_size()
        print(f"Detected display size: {width}x{height}")
        
        client.push_apk()
        client.setup_forward()
        client.start_server(width, height, lazy=lazy)
        
        time.sleep(1)
        
        client.connect()
        client.recv_header()
        
        frames = capture_test(client, interval=interval, count=count, output_prefix=output_prefix)
        
        return frames
        
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return []
    
    finally:
        client.disconnect()
        client.cleanup_server()
        client.cleanup_forward()

def main():
    print("Minicap Lazy Mode MAX FPS Test")
    print("=" * 60)
    
    if not os.path.exists(APK_PATH):
        print(f"Error: APK not found at {APK_PATH}")
        sys.exit(1)
    
    os.makedirs(os.path.join(REPO_ROOT, "output"), exist_ok=True)
    
    client = MinicapTestClient()
    
    try:
        width, height = client.get_display_size()
        print(f"Display size: {width}x{height}")
        
        client.push_apk()
        client.setup_forward()
        client.start_server(width, height, lazy=True)
        
        time.sleep(1)
        
        client.connect()
        client.recv_header()
        
        frames = test_max_fps(client, count=10)
        
        if len(frames) >= 2:
            sizes = [len(f) for f in frames]
            print(f"\nFrame sizes: min={min(sizes)}, max={max(sizes)}, avg={sum(sizes)//len(sizes)}")
        
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
    
    finally:
        client.disconnect()
        client.cleanup_server()
        client.cleanup_forward()
    
    if len(frames) >= 2:
        print(f"\nTotal frames: {len(frames)}")

if __name__ == "__main__":
    main()