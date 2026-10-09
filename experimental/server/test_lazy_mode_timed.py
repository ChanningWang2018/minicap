#!/usr/bin/env python3
"""
Improved test script for minicap APK lazy mode.
Tests with precise timing and saves frames for verification.
"""

import subprocess
import time
import socket
import struct
import os
import sys
import json

ADB_DEVICE = "emulator-5554"

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
        
        start_time = time.time()
        while time.time() - start_time < 5:
            line = self.proc.stderr.readline()
            if line:
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
        self.sock.settimeout(30)
    
    def recv_header(self):
        data = self.sock.recv(24)
        if len(data) != 24:
            raise Exception(f"Invalid header: expected 24 bytes, got {len(data)}")
        
        header = struct.unpack("<2B5I2B", data)
        print(f"Header: version={header[0]}, pid={header[2]}, "
              f"size={header[3]}x{header[4]}, quirks={header[8]}")
        return header
    
    def request_frame(self):
        self.sock.send(b"1")
    
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


def test_lazy_mode_timed(interval=2, count=5, output_dir=os.path.join(REPO_ROOT, "output")):
    """
    测试 lazy mode，精确计时并保存帧。
    """
    client = MinicapTestClient()
    
    results = {
        "interval": interval,
        "count": count,
        "frames": []
    }
    
    frames_data = []  # 保存实际帧数据
    
    try:
        width, height = client.get_display_size()
        print(f"Display size: {width}x{height}")
        
        client.push_apk()
        client.setup_forward()
        client.start_server(width, height, lazy=True)
        
        time.sleep(1)
        
        client.connect()
        client.recv_header()
        
        print("=" * 60)
        print(f"Testing Lazy Mode: {count} frames at {interval}s intervals")
        print("=" * 60)
        
        for i in range(count):
            send_time = time.time()
            send_timestamp = time.strftime("%H:%M:%S.%%03d") % ((send_time % 1) * 1000)
            
            client.request_frame()
            
            frame_data = client.recv_frame()
            
            recv_time = time.time()
            recv_timestamp = time.strftime("%H:%M:%S.%%03d") % ((recv_time % 1) * 1000)
            
            if frame_data:
                round_trip = recv_time - send_time
                frame_result = {
                    "frame_num": i + 1,
                    "send_time": send_time,
                    "recv_time": recv_time,
                    "send_timestamp": send_timestamp,
                    "recv_timestamp": recv_timestamp,
                    "round_trip": round_trip,
                    "size": len(frame_data)
                }
                results["frames"].append(frame_result)
                frames_data.append(frame_data)  # 保存帧数据
                
                # 保存帧图片
                filename = os.path.join(output_dir, f"frame_{i+1:03d}.jpg")
                with open(filename, "wb") as f:
                    f.write(frame_data)
                print(f"Frame {i+1}: RT={round_trip:.3f}s, saved to {filename}")
            else:
                print(f"Frame {i+1}: FAILED")
                results["frames"].append({"frame_num": i + 1, "failed": True})
            
            if i < count - 1:
                time.sleep(interval)
        
        # 分析结果
        print("\n" + "=" * 60)
        print("Analysis:")
        print("=" * 60)
        
        valid_frames = [f for f in results["frames"] if not f.get("failed")]
        
        if len(valid_frames) >= 2:
            print("\nFrame intervals:")
            for i in range(1, len(valid_frames)):
                delta = valid_frames[i]["send_time"] - valid_frames[i-1]["send_time"]
                print(f"  Frame {i} -> Frame {i+1}: {delta:.3f}s")
            
            round_trips = [f["round_trip"] for f in valid_frames]
            print(f"\nRound trip time: min={min(round_trips):.3f}s, max={max(round_trips):.3f}s, avg={sum(round_trips)/len(round_trips):.3f}s")
            
            sizes = [f["size"] for f in valid_frames]
            size_changes = [abs(sizes[i+1] - sizes[i]) for i in range(len(sizes)-1)]
            avg_change = sum(size_changes) / len(size_changes) if size_changes else 0
            
            print(f"\nFrame sizes: {sizes}")
            print(f"Avg size change: {avg_change:.0f} bytes")
            
            if avg_change < 1000:
                print("WARNING: Frame sizes very similar!")
            else:
                print("OK: Frame sizes vary significantly")
        
        return results, frames_data, valid_frames
    
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return results, [], []
    
    finally:
        client.disconnect()
        client.cleanup_server()
        client.cleanup_forward()


def main():
    print("=" * 60)
    print("Minicap Lazy Mode - Precise Timing Test")
    print("=" * 60)
    
    if not os.path.exists(APK_PATH):
        print(f"Error: APK not found at {APK_PATH}")
        sys.exit(1)
    
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    output_dir = os.path.join(REPO_ROOT, "output", f"timed_test_{timestamp}")
    os.makedirs(output_dir, exist_ok=True)
    
    print(f"\nFrames will be saved to: {output_dir}/")
    print("Please check the jpg files to verify the stopwatch timestamps.")
    print()
    
    results, frames_data, valid_frames = test_lazy_mode_timed(interval=2, count=5, output_dir=output_dir)
    
    if valid_frames:
        # 保存 JSON 结果
        with open(os.path.join(output_dir, "results.json"), "w") as f:
            json.dump(results, f, indent=2)
        
        print(f"\nResults saved to: {output_dir}/results.json")
        print(f"Frames saved to: {output_dir}/frame_*.jpg")


if __name__ == "__main__":
    main()
