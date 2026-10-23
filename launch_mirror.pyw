#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
三星手机防闪退一键投屏启动器 (USB + 5G Wi-Fi 双模自愈投屏)
自动通过 USB 激活手机的 5GHz Wi-Fi 高速 ADB 通道 (tcpip:5555)，
彻底解决电脑外接 USB 集线器供电跌落导致的投屏闪退问题。
"""

import os
import re
import sys
import time
import subprocess
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SCRCPY_DIR = BASE_DIR / "scrcpy"
ADB_EXE = SCRCPY_DIR / "adb.exe"
SCRCPY_EXE = SCRCPY_DIR / "scrcpy.exe"
CREATION_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def run_adb(args, timeout=12):
    cmd = [str(ADB_EXE)] + list(args)
    try:
        res = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            creationflags=CREATION_FLAGS,
        )
        return res.returncode, res.stdout.decode("utf-8", errors="ignore")
    except Exception as e:
        return -1, str(e)


def get_pc_current_wifi_credentials():
    """动态读取当前电脑连接的 Wi-Fi SSID 与密码（不硬编码任何敏感信息）"""
    try:
        res = subprocess.run(
            ["netsh", "wlan", "show", "interfaces"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
            creationflags=CREATION_FLAGS,
        )
        out = res.stdout.decode("utf-8", errors="ignore")
        m_ssid = re.search(r"^\s*SSID\s*:\s*(.+)$", out, re.MULTILINE)
        if not m_ssid:
            return None, None
        ssid = m_ssid.group(1).strip()
        res_prof = subprocess.run(
            ["netsh", "wlan", "show", "profile", f"name={ssid}", "key=clear"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
            creationflags=CREATION_FLAGS,
        )
        prof_out = res_prof.stdout.decode("utf-8", errors="ignore")
        m_key = re.search(r"(?:Key Content|关键内容)\s*:\s*(.+)$", prof_out, re.MULTILINE)
        key = m_key.group(1).strip() if m_key else None
        return ssid, key
    except Exception:
        return None, None


def ensure_best_mirror_target():
    """
    优先确保手机连接到局域网 Wi-Fi 并开启 tcpip:5555，返回最稳定的投屏 target (优先 IP:5555)
    """
    _, out = run_adb(["devices"])
    usb_serial = None
    tcp_serial = None
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            dev = parts[0]
            if ":" in dev:
                tcp_serial = dev
            else:
                usb_serial = dev

    # 如果已经有在线的 tcp_serial，验证是否可达
    if tcp_serial:
        code, echo_out = run_adb(["-s", tcp_serial, "shell", "echo ok"], timeout=3)
        if code == 0 and "ok" in echo_out:
            return tcp_serial

    # 如果有 USB 连接，通过 USB 自动确保手机连上同局域网 Wi-Fi 并激活 tcpip 5555
    if usb_serial:
        _, ip_out = run_adb(["-s", usb_serial, "shell", "ip -4 addr show wlan0"], timeout=5)
        m = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", ip_out)
        if not m:
            ssid, key = get_pc_current_wifi_credentials()
            run_adb(["-s", usb_serial, "shell", "cmd wifi set-wifi-enabled enabled"], timeout=5)
            if ssid and key:
                run_adb(["-s", usb_serial, "shell", f"cmd wifi connect-network {ssid} wpa2 {key}"], timeout=6)
            for _ in range(8):
                time.sleep(0.8)
                _, ip_out = run_adb(["-s", usb_serial, "shell", "ip -4 addr show wlan0"], timeout=4)
                m = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", ip_out)
                if m:
                    break

        if m:
            phone_ip = m.group(1)
            target_tcp = f"{phone_ip}:5555"
            run_adb(["-s", usb_serial, "tcpip", "5555"], timeout=6)
            time.sleep(1.2)
            run_adb(["connect", target_tcp], timeout=6)
            code, echo_out = run_adb(["-s", target_tcp, "shell", "echo ok"], timeout=4)
            if code == 0 and "ok" in echo_out:
                return target_tcp

        return usb_serial

    # 如果既没有检测到，尝试直连常见局域网 IP
    for ip in ["192.168.2.5:5555", "192.168.2.2:5555", "192.168.2.3:5555"]:
        run_adb(["connect", ip], timeout=3)
        code, echo_out = run_adb(["-s", ip, "shell", "echo ok"], timeout=3)
        if code == 0 and "ok" in echo_out:
            return ip

    return None


def launch():
    target = ensure_best_mirror_target()
    if not target:
        return
    cmd = [
        str(SCRCPY_EXE),
        "-s", target,
        "--video-codec=h265",
        "--max-size=1280",
        "--video-bit-rate=4M",
        "--max-fps=60",
        "--stay-awake",
        "--window-title", f"Samsung 实时投屏 ({target})",
    ]
    env = os.environ.copy()
    env["ADB"] = str(ADB_EXE)
    subprocess.Popen(cmd, cwd=str(SCRCPY_DIR), env=env, creationflags=CREATION_FLAGS)


if __name__ == "__main__":
    launch()
