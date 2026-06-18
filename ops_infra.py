"""
OPS-Infra v2 - Standalone Infrastructure Monitoring Tool

Single self-contained app:
  1. Enter a Store ID.
  2. Fetch active camera IPs / credentials / RTSP URLs directly from the dashboard API.
  3. Run health checks (cameras ping + RTSP, internet, system, antivirus) locally.
  4. Show results split across tabs:
       - Camera Status : ping health + offline reason per camera
       - RTSP Preview  : live streaming preview per camera (sub-tabs), no VLC needed
       - Wi-Fi History, Sleep/Power, Antivirus

RTSP Preview requires:
    pip install opencv-python Pillow

Build to .exe:
    pip install pyinstaller requests psutil ping3 opencv-python Pillow
    pyinstaller --onefile --noconsole --name ops_infra_v2 Infra/ops_infra_v2.py
"""

import tkinter as tk
from tkinter import ttk, messagebox
import requests
import json
import threading
import queue
import datetime
import socket
import platform
import subprocess
import time
import re
import os
import sys
from concurrent.futures import ThreadPoolExecutor

# Suppress console flashes from subprocess calls when running as a no-console EXE
_NO_WIN = subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:
    import psutil
except ImportError:
    psutil = None

try:
    import ping3
except ImportError:
    ping3 = None

try:
    import cv2
except ImportError:
    cv2 = None

try:
    from PIL import Image, ImageTk
except ImportError:
    Image = None
    ImageTk = None


# ===========================================================================
# DASHBOARD API CONFIG
# Priority: config.json next to EXE  >  config.json bundled inside EXE
# ===========================================================================
def _config_path():
    if getattr(sys, "frozen", False):
        # User can always override by placing config.json next to the EXE
        user_cfg = os.path.join(os.path.dirname(sys.executable), "config.json")
        if os.path.exists(user_cfg):
            return user_cfg
        # Fall back to the config bundled into the EXE by PyInstaller (--add-data)
        return os.path.join(sys._MEIPASS, "config.json")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

_DEFAULT_CONFIG = {
    "dashboard_base_url": "https://dashboard-api.tangoeye.ai",
    "dashboard_token":    "",
    "auth_style":         "bearer",
}

def _load_config():
    path = _config_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                data = json.load(fh)
            merged = dict(_DEFAULT_CONFIG)
            merged.update({k: v for k, v in data.items() if k in _DEFAULT_CONFIG})
            return merged
        except Exception:
            pass
    # Only write a template when running as a plain script (not frozen EXE)
    if not getattr(sys, "frozen", False):
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(_DEFAULT_CONFIG, fh, indent=2)
        except Exception:
            pass
    return dict(_DEFAULT_CONFIG)

_cfg               = _load_config()
DASHBOARD_BASE_URL = _cfg["dashboard_base_url"]
DASHBOARD_TOKEN    = _cfg["dashboard_token"]
AUTH_STYLE         = _cfg["auth_style"]

CAMERA_ENDPOINT = ("/v3/edgeapp/getAllCameraStreamData"
                   "?storeId={store_id}&date={date}"
                   "&searchValue=&filterByStatus=&filterByProduct=&filterByZone=")

REQUEST_TIMEOUT    = 30
APP_EXE            = "TangoEyeStreamer.exe"
STREAM_FOLDER_ROOT = r"C:\ProgramData\Tango_IT\Tango_Eye_Streamer"


def _auth_header():
    if AUTH_STYLE == "bearer":
        return f"Bearer {DASHBOARD_TOKEN}"
    return DASHBOARD_TOKEN


def _pick(d, *keys, default=None):
    if not isinstance(d, dict):
        return default
    lowered = {k.lower(): v for k, v in d.items()}
    for k in keys:
        v = lowered.get(k.lower())
        if v not in (None, "", []):
            return v
    return default


def _extract_camera_list(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("cameras", "data", "result", "results", "items", "payload"):
            val = payload.get(key)
            if isinstance(val, list):
                return val
            if isinstance(val, dict):
                nested = _extract_camera_list(val)
                if nested:
                    return nested
    return []


def _normalize_camera(c):
    ip           = _pick(c, "ip", "cameraIp", "camera_ip", "ipAddress", "ip_address")
    username     = _pick(c, "username", "user", "camUsername", "login", default="admin")
    password     = _pick(c, "password", "pass", "camPassword", "pwd", default="")
    rtsp         = _pick(c, "rtsp", "rtspUrl", "rtsp_url", "streamUrl", "stream_url", "url")
    number       = _pick(c, "cameraNumber", "camera_number", "cameraName", "camera_name",
                         "name", "channel", "id", default="CAM")
    manufacturer = _pick(c, "manufacturer", "make", "brand", "vendor", default="Unknown")
    active       = _pick(c, "isActivated", "active", "isActive", "is_active",
                         "enabled", "status", default=True)
    is_active    = active not in (False, 0, "0", "false", "False", "inactive", "disabled", "DOWN")
    up           = _pick(c, "isUp", "up", default=True)
    is_up        = up not in (False, 0, "0", "false", "False", "DOWN", "down")
    client_id    = _pick(c, "clientId", "client_id", "brandId", "brand_id")
    stream_name  = _pick(c, "streamName", "stream_name", default="")
    stream_id    = _pick(c, "streamId", "stream_id", "streamName", "stream_name", default="")
    return {
        "ip": ip, "username": username, "password": password, "rtsp": rtsp,
        "camera_number": str(number), "manufacturer": manufacturer,
        "active": is_active, "is_up": is_up, "client_id": client_id,
        "stream_name": str(stream_name),
        "stream_id": str(stream_id),
    }


def fetch_store_cameras(store_id):
    today   = datetime.date.today().isoformat()
    url     = DASHBOARD_BASE_URL.rstrip("/") + CAMERA_ENDPOINT.format(
        store_id=store_id, date=today)
    headers = {
        "Authorization": _auth_header(),
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT, verify=True)
        if resp.status_code != 200:
            return [], f"Dashboard API returned {resp.status_code}: {resp.text[:200]}"
        payload = resp.json()
    except requests.exceptions.RequestException as e:
        return [], f"Request failed: {e}"
    except ValueError:
        return [], "Dashboard API did not return valid JSON."

    raw     = _extract_camera_list(payload)
    if not raw:
        return [], "No cameras found in the dashboard response for this store."
    cameras = [_normalize_camera(c) for c in raw if isinstance(c, dict)]
    cameras = [c for c in cameras if c["active"] and (c["ip"] or c["rtsp"])]
    if not cameras:
        return [], "Cameras were returned but none are active / have an IP."
    return cameras, None


# ===========================================================================
# HEALTH CHECKS
# ===========================================================================
def check_internet():
    results = []
    for _ in range(3):
        latency, ok = None, False
        if ping3 is not None:
            try:
                r = ping3.ping("8.8.8.8", timeout=3)
                if r:
                    ok, latency = True, round(r * 1000, 2)
            except Exception:
                pass
        else:
            try:
                start = time.perf_counter()
                with socket.create_connection(("8.8.8.8", 53), timeout=3):
                    latency, ok = round((time.perf_counter() - start) * 1000, 2), True
            except Exception:
                pass
        results.append({"success": ok, "latency": latency})
        time.sleep(0.5)
    success = sum(1 for r in results if r["success"])
    lat     = [r["latency"] for r in results if r["latency"] is not None]
    return {
        "connected":      success >= 2,
        "packet_loss":    round((3 - success) * 100 / 3, 1),
        "avg_latency_ms": round(sum(lat) / len(lat), 2) if lat else None,
    }


def check_system():
    if psutil is None:
        return {"error": "psutil not installed"}
    try:
        cpu          = psutil.cpu_percent(interval=1)
        ram          = psutil.virtual_memory().percent
        disk_path    = "C:\\" if platform.system() == "Windows" else "/"
        disk         = psutil.disk_usage(disk_path).percent
        uptime_hours = round((time.time() - psutil.boot_time()) / 3600, 1)
        return {
            "cpu_percent":      cpu,
            "ram_percent":      ram,
            "disk_percent":     disk,
            "uptime_hours":     uptime_hours,
            "high_utilization": cpu > 80 or ram > 85 or disk > 90,
        }
    except Exception as e:
        return {"error": str(e)}


def check_antivirus():
    try:
        if platform.system() == "Windows":
            result = subprocess.run(
                ["wmic", "/namespace:\\\\root\\SecurityCenter2", "path",
                 "AntivirusProduct", "get", "displayName"],
                capture_output=True, text=True, timeout=10,
                creationflags=_NO_WIN,
            )
            lines = [l.strip() for l in result.stdout.strip().split("\n")[1:] if l.strip()]
            return {"antivirus_name": lines[0] if lines else "None detected"}
        return {"antivirus_name": "Check only available on Windows"}
    except Exception:
        return {"antivirus_name": "Unable to detect"}


def get_network_info():
    network_name = "Unknown"
    try:
        if platform.system() == "Windows":
            result = subprocess.run(["netsh", "wlan", "show", "interfaces"],
                                    capture_output=True, text=True, timeout=5,
                                    creationflags=_NO_WIN)
            m = re.search(r"^\s*SSID\s*:\s*(.+)$", result.stdout, re.MULTILINE)
            network_name = m.group(1).strip() if m else "Ethernet/Wired"
    except Exception:
        network_name = "Unknown"
    try:
        hostname = socket.gethostname()
        local_ip = socket.gethostbyname(hostname)
    except Exception:
        hostname, local_ip = "Unknown", "Unknown"
    return {"network_name": network_name, "hostname": hostname, "local_ip": local_ip}


# ===========================================================================
# RTSP HELPERS
# ===========================================================================
def _build_rtsp_url(cam):
    if cam.get("rtsp"):
        return cam["rtsp"]
    user  = cam.get("username") or ""
    pwd   = cam.get("password") or ""
    creds = f"{user}:{pwd}@" if user else ""
    return f"rtsp://{creds}{cam['ip']}:554/cam/realmonitor?channel=1&subtype=0"


def _ensure_rtsp_credentials(url, username, password):
    if not username:
        return url
    m = re.match(r"(rtsp://)(.*)", url, re.IGNORECASE)
    if not m:
        return url
    scheme, rest = m.groups()
    if "@" in rest.split("/", 1)[0]:
        return url
    cred = username + (":" + password if password else "")
    return f"{scheme}{cred}@{rest}"


def _grab_frame(url, result):
    cap = None
    try:
        cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
        if not cap.isOpened():
            result["status"] = "FAILED"
            return
        ok, frame = cap.read()
        if ok and frame is not None and getattr(frame, "size", 0) > 0:
            result["status"] = "WORKING"
        else:
            result["status"] = "NO_VIDEO"
    except Exception:
        result["status"] = "FAILED"
    finally:
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass


def check_rtsp(rtsp_url, username=None, password=None, timeout=8):
    if cv2 is None:
        return _check_rtsp_options(rtsp_url, timeout)
    url = _ensure_rtsp_credentials(rtsp_url, username, password)
    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = (
        f"rtsp_transport;tcp|stimeout;{int(timeout * 1_000_000)}"
    )
    result = {"status": "TIMEOUT"}
    worker = threading.Thread(target=_grab_frame, args=(url, result), daemon=True)
    worker.start()
    worker.join(timeout + 4)
    return result["status"]


def _check_rtsp_options(rtsp_url, timeout=5):
    m = re.match(r"rtsp://(?:[^@/]+@)?([^:/]+)(?::(\d+))?", rtsp_url)
    if not m:
        return "INVALID_URL"
    host = m.group(1)
    port = int(m.group(2)) if m.group(2) else 554
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            req = (f"OPTIONS {rtsp_url} RTSP/1.0\r\nCSeq: 1\r\n"
                   f"User-Agent: OPS-Infra\r\n\r\n")
            sock.sendall(req.encode())
            data = sock.recv(1024).decode(errors="ignore")
        if "RTSP/1.0 200" in data or "RTSP/1.0 401" in data or "RTSP/2.0 200" in data:
            return "REACHABLE"
        return "REACHABLE" if data else "NO_RESPONSE"
    except socket.timeout:
        return "TIMEOUT"
    except OSError:
        return "FAILED"


# ===========================================================================
# PING DIAGNOSIS
# ===========================================================================
def _diagnose_ping_failure(ip):
    """Probe common TCP ports to explain WHY a camera does not respond to ICMP ping."""
    for port, label in [
        (554,  "RTSP/554"),
        (80,   "HTTP/80"),
        (8080, "HTTP/8080"),
        (443,  "HTTPS/443"),
    ]:
        try:
            with socket.create_connection((ip, port), timeout=2):
                return (f"ICMP responses disabled on camera — "
                        f"host IS reachable on {label}")
        except ConnectionRefusedError:
            return (f"Host is up — TCP {label} refused "
                    f"(RTSP service may be disabled or using a non-standard port)")
        except socket.timeout:
            continue
        except OSError as e:
            msg = str(e).lower()
            if "no route" in msg or "unreachable" in msg:
                return "No network route to this IP — check VLAN / switch / cable"
            continue
    return "All TCP ports timed out — camera is offline or blocked by firewall"


# ===========================================================================
# CAMERA CHECK
# ===========================================================================
def check_camera(cam):
    ip           = cam.get("ip")
    ping_status  = "NOT_CHECKED"
    ping_latency = None
    ping_reason  = ""

    if not ip:
        return {
            "camera_number":   cam.get("camera_number", "CAM"),
            "stream_name":     cam.get("stream_name", ""),
            "client_id":       cam.get("client_id"),
            "ip":              None,
            "manufacturer":    cam.get("manufacturer", "Unknown"),
            "is_up":           cam.get("is_up", True),
            "ping":            "NO_IP",
            "ping_latency_ms": None,
            "ping_reason":     "No IP address assigned to this camera",
            "rtsp":            "SKIPPED",
            "rtsp_url":        _build_rtsp_url(cam) if cam.get("rtsp") else "",
        }

    if ping3 is not None:
        # Pre-warm the OS ARP cache with a TCP touch before ICMP.
        # On a cold first run, ARP resolution consumes the entire ICMP timeout,
        # causing a false FAILED even when the camera is online.  The TCP
        # handshake resolves the MAC address so the ping packet goes out
        # immediately on the first attempt.
        _tcp_ok  = False
        _tcp_lat = None
        try:
            _t0 = time.perf_counter()
            with socket.create_connection((ip, 554), timeout=1.0):
                _tcp_lat = round((time.perf_counter() - _t0) * 1000, 2)
                _tcp_ok  = True
        except Exception:
            pass

        try:
            r = ping3.ping(ip, timeout=2)
            if r:
                ping_status  = "OK"
                ping_latency = round(r * 1000, 2)
            elif _tcp_ok:
                # ICMP still blocked / disabled but TCP/554 confirms host is up.
                ping_status  = "OK"
                ping_latency = _tcp_lat
            else:
                ping_status = "FAILED"
                ping_reason = _diagnose_ping_failure(ip)
        except PermissionError:
            # No raw-socket access (not running as admin).
            # Reuse the TCP/554 pre-warm result — no admin needed for TCP.
            if _tcp_ok:
                ping_status  = "OK"
                ping_latency = _tcp_lat
            else:
                ping_status = "FAILED"
                ping_reason = _diagnose_ping_failure(ip)
        except Exception:
            ping_status = "FAILED"
            ping_reason = _diagnose_ping_failure(ip)
    else:
        try:
            start = time.perf_counter()
            with socket.create_connection((ip, 554), timeout=2):
                ping_status  = "OK"
                ping_latency = round((time.perf_counter() - start) * 1000, 2)
        except ConnectionRefusedError:
            ping_status = "FAILED"
            ping_reason = "Port 554 refused — camera is reachable but RTSP service is closed"
        except socket.timeout:
            ping_status = "FAILED"
            ping_reason = _diagnose_ping_failure(ip)
        except Exception:
            ping_status = "FAILED"
            ping_reason = _diagnose_ping_failure(ip)

    rtsp_url    = _build_rtsp_url(cam)
    rtsp_status = (check_rtsp(rtsp_url, cam.get("username"), cam.get("password"))
                   if ping_status == "OK" else "SKIPPED")

    return {
        "camera_number":   cam.get("camera_number", "CAM"),
        "stream_name":     cam.get("stream_name", ""),
        "client_id":       cam.get("client_id"),
        "ip":              ip,
        "manufacturer":    cam.get("manufacturer", "Unknown"),
        "is_up":           cam.get("is_up", True),
        "ping":            ping_status,
        "ping_latency_ms": ping_latency,
        "ping_reason":     ping_reason,
        "rtsp":            rtsp_status,
        "rtsp_url":        rtsp_url,
    }


# ===========================================================================
# EVENT LOG HELPERS
# ===========================================================================
def get_wifi_change_logs(hours=24):
    if platform.system() != "Windows":
        return []
    ps = r'''
$ErrorActionPreference='SilentlyContinue'
$start=(Get-Date).AddHours(-HOURS)
$events = Get-WinEvent -FilterHashtable @{LogName='Microsoft-Windows-WLAN-AutoConfig/Operational';Id=8001,8003;StartTime=$start}
$list = foreach($e in $events){
  $ssid=''
  if($e.Message -match '\bSSID:\s*(.+)'){ $ssid=$Matches[1].Trim() }
  [PSCustomObject]@{ time=$e.TimeCreated.ToString('o'); id=$e.Id; ssid=$ssid }
}
$list | ConvertTo-Json -Compress
'''.replace("HOURS", str(int(hours)))
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=40, creationflags=_NO_WIN)
        raw = (result.stdout or "").strip()
        if not raw:
            return []
        data = json.loads(raw)
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]
    events = []
    for d in data:
        try:
            evid = int(d.get("id"))
        except (TypeError, ValueError):
            continue
        events.append({
            "time":  d.get("time", ""),
            "event": "Connected" if evid == 8001 else "Disconnected",
            "ssid":  (d.get("ssid") or "").strip() or "(unknown)",
        })
    events.sort(key=lambda e: e["time"], reverse=True)
    return events


def get_sleep_wake_logs(hours=24):
    if platform.system() != "Windows":
        return []
    ps = r'''
$ErrorActionPreference='SilentlyContinue'
$start=(Get-Date).AddHours(-HOURS)
$f1=@{LogName='System';ProviderName='Microsoft-Windows-Kernel-Power';Id=42,107,109,41;StartTime=$start}
$f2=@{LogName='System';ProviderName='Microsoft-Windows-Power-Troubleshooter';Id=1;StartTime=$start}
$events=@(Get-WinEvent -FilterHashtable $f1) + @(Get-WinEvent -FilterHashtable $f2)
$list = foreach($e in $events){
  $line = ($e.Message -split "`r?`n" | Where-Object { $_.Trim() } | Select-Object -First 1)
  [PSCustomObject]@{ time=$e.TimeCreated.ToString('o'); id=$e.Id; msg=$line }
}
$list | ConvertTo-Json -Compress
'''.replace("HOURS", str(int(hours)))
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=40, creationflags=_NO_WIN)
        raw = (result.stdout or "").strip()
        if not raw:
            return []
        data = json.loads(raw)
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]
    mapping = {
        42:  ("Sleep",      "System entering sleep"),
        107: ("Wake",       "System resumed from sleep"),
        1:   ("Wake",       "System returned from a low-power state"),
        41:  ("Power loss", "Rebooted without a clean shutdown"),
        109: ("Shutdown",   "Kernel initiated a power transition"),
    }
    events = []
    for d in data:
        try:
            evid = int(d.get("id"))
        except (TypeError, ValueError):
            continue
        event, default_detail = mapping.get(evid, ("Power event", ""))
        detail = (d.get("msg") or "").strip() or default_detail
        events.append({"time": d.get("time", ""), "event": event, "detail": detail})
    events.sort(key=lambda e: e["time"], reverse=True)
    return events


def _decode_av_state(state):
    try:
        s = int(state)
    except (TypeError, ValueError):
        return None, None
    hexs       = format(s & 0xFFFFFF, "06x")
    enabled    = hexs[2:4] in ("10", "11")
    up_to_date = hexs[4:6] == "00"
    return enabled, up_to_date


def get_antivirus_details():
    if platform.system() != "Windows":
        return []
    ps = r'''
$ErrorActionPreference='SilentlyContinue'
$av = Get-CimInstance -Namespace root/SecurityCenter2 -ClassName AntivirusProduct
$list = foreach($a in $av){
  [PSCustomObject]@{ name=$a.displayName; state=$a.productState; ts=$a.timestamp }
}
$list | ConvertTo-Json -Compress
'''
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=20, creationflags=_NO_WIN)
        raw = (result.stdout or "").strip()
        if not raw:
            return []
        data = json.loads(raw)
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]
    products = []
    for d in data:
        enabled, up_to_date = _decode_av_state(d.get("state"))
        products.append({
            "name":       (d.get("name") or "Unknown").strip(),
            "enabled":    enabled,
            "up_to_date": up_to_date,
            "updated":    (d.get("ts") or "").strip(),
        })
    return products


def _check_process(exe_name):
    for proc in psutil.process_iter(['name', 'pid', 'status', 'create_time']):
        try:
            if proc.info['name'].lower() == exe_name.lower():
                started  = datetime.datetime.fromtimestamp(proc.info['create_time']).isoformat()
                uptime_h = round((time.time() - proc.info['create_time']) / 3600, 1)
                return {'running': True, 'pid': proc.info['pid'],
                        'status': proc.info['status'],
                        'started_at': started, 'uptime_hours': uptime_h}
        except Exception:
            pass
    return {'running': False, 'pid': None, 'status': 'not found',
            'started_at': None, 'uptime_hours': None}


def _get_crash_events(exe_name, days=2):
    if platform.system() != "Windows":
        return []
    app = exe_name.replace(".exe", "")
    ps = (
        "$ErrorActionPreference='SilentlyContinue'\n"
        f"$start=(Get-Date).AddDays(-{days})\n"
        "$ev=Get-WinEvent -FilterHashtable @{LogName='Application';Id=1000,1001,1002;StartTime=$start}"
        " -ErrorAction SilentlyContinue\n"
        f"$filt=$ev|Where-Object{{$_.Message -like '*{app}*'}}\n"
        "$list=foreach($e in $filt){"
        "[PSCustomObject]@{time=$e.TimeCreated.ToString('o');id=$e.Id;level=$e.LevelDisplayName;"
        "msg=(($e.Message -split \"`r?`n\"|Where-Object{$_.Trim()}|Select-Object -First 3)-join' | ')}}\n"
        "$list|ConvertTo-Json -Compress"
    )
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, text=True, timeout=15, creationflags=_NO_WIN)
        data = json.loads((r.stdout or "").strip())
        if isinstance(data, dict):
            data = [data]
        events = [{"time": d.get("time", ""), "id": str(d.get("id", "")),
                   "level": d.get("level", ""), "msg": (d.get("msg") or "").strip()}
                  for d in data]
        events.sort(key=lambda e: e["time"], reverse=True)
        return events
    except Exception:
        return []


def _check_stream_folders(cameras):
    results = []
    seen   = set()
    cutoff = time.time() - 3600

    try:
        all_folders = [e for e in os.listdir(STREAM_FOLDER_ROOT)
                       if os.path.isdir(os.path.join(STREAM_FOLDER_ROOT, e))]
    except Exception:
        all_folders = []

    def _mtime(fname):
        try:
            return os.path.getmtime(os.path.join(STREAM_FOLDER_ROOT, fname))
        except Exception:
            return 0

    for cam in cameras:
        sid = (cam.get("stream_id") or cam.get("stream_name") or "").strip()
        if not sid or sid in seen:
            continue
        seen.add(sid)

        matching = [f for f in all_folders if f.startswith(sid)]

        if not matching:
            results.append({
                "stream_id":        sid,
                "camera_number":    cam.get("camera_number", "—"),
                "ip":               cam.get("ip", "—"),
                "folder_exists":    False,
                "last_modified":    None,
                "recent_images_1h": 0,
                "total_images":     0,
                "status":           "missing",
            })
            continue

        best   = max(matching, key=_mtime)
        folder = os.path.join(STREAM_FOLDER_ROOT, best)

        last_modified = None
        recent        = 0
        total         = 0

        try:
            last_modified = datetime.datetime.fromtimestamp(
                os.path.getmtime(folder)).isoformat()
        except Exception:
            pass
        try:
            for fn in os.listdir(folder):
                if fn.lower().endswith(('.jpg', '.jpeg', '.png', '.bmp', '.webp')):
                    total += 1
                    try:
                        if os.path.getmtime(os.path.join(folder, fn)) > cutoff:
                            recent += 1
                    except Exception:
                        pass
        except Exception:
            pass

        status = "active" if recent > 0 else ("stale" if total > 0 else "empty")

        results.append({
            "stream_id":        sid,
            "camera_number":    cam.get("camera_number", "—"),
            "ip":               cam.get("ip", "—"),
            "folder_exists":    True,
            "last_modified":    last_modified,
            "recent_images_1h": recent,
            "total_images":     total,
            "status":           status,
        })
    return results


def check_app_status(cameras):
    process        = _check_process(APP_EXE)
    crashes        = _get_crash_events(APP_EXE, days=2)
    stream_folders = _check_stream_folders(cameras)
    return {
        "app_exe":        APP_EXE,
        "process":        process,
        "crashes":        crashes,
        "stream_folders": stream_folders,
        "summary": {
            "process_running": process.get("running", False),
            "crash_count_2d":  len(crashes),
            "streams_total":   len(stream_folders),
            "streams_active":  sum(1 for s in stream_folders if s["status"] == "active"),
            "streams_stale":   sum(1 for s in stream_folders if s["status"] == "stale"),
            "streams_missing": sum(1 for s in stream_folders if s["status"] == "missing"),
        },
    }


def _parse_network_status_logs(days=2):
    entries = []
    today = datetime.date.today()
    for i in range(days):
        d = today - datetime.timedelta(days=i)
        path = os.path.join(STREAM_FOLDER_ROOT, f"NetworkStatus_{d.strftime('%d-%m-%Y')}.txt")
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    line = line.strip()
                    m = re.match(r"Network is (ONLINE|OFFLINE)\s*-\s*(\d{2}:\d{2}:\d{2})\s*,(\d{2}-\d{2}-\d{4})", line)
                    if m:
                        state, hms, dmy = m.groups()
                        try:
                            dt = datetime.datetime.strptime(f"{dmy} {hms}", "%d-%m-%Y %H:%M:%S")
                            entries.append({"time": dt.isoformat(), "status": state})
                        except Exception:
                            entries.append({"time": f"{dmy} {hms}", "status": state})
        except Exception:
            pass
    entries.sort(key=lambda x: x.get("time", ""), reverse=True)
    return entries


def _parse_camera_status_logs(days=2):
    entries = []
    today = datetime.date.today()
    for i in range(days):
        d = today - datetime.timedelta(days=i)
        path = os.path.join(STREAM_FOLDER_ROOT, f"CameraStatusLogs_{d.strftime('%d-%m-%Y')}.txt")
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    line = line.strip()
                    m = re.match(
                        r"Camera IP for the stream (rtsp://[^\s]+)\s+is\s+(UP|DOWN)\s+at\s*:(\d{2}:\d{2}:\d{2})\s*,(\d{2}-\d{2}-\d{4})",
                        line, re.IGNORECASE)
                    if m:
                        rtsp_url, state, hms, dmy = m.groups()
                        ip_m = re.search(r"@([\d.]+)", rtsp_url)
                        cam_ip = ip_m.group(1) if ip_m else rtsp_url.split("//")[-1][:30]
                        try:
                            dt = datetime.datetime.strptime(f"{dmy} {hms}", "%d-%m-%Y %H:%M:%S")
                            entries.append({"time": dt.isoformat(), "ip": cam_ip, "status": state.upper()})
                        except Exception:
                            entries.append({"time": f"{dmy} {hms}", "ip": cam_ip, "status": state.upper()})
        except Exception:
            pass
    entries.sort(key=lambda x: x.get("time", ""), reverse=True)
    return entries


def _parse_speed_test_logs(days=2):
    entries = []
    today = datetime.date.today()
    for i in range(days):
        d = today - datetime.timedelta(days=i)
        path = os.path.join(STREAM_FOLDER_ROOT, f"speedTestLogs_{d.strftime('%d%m%Y')}.txt")
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    line = line.strip()
                    m = re.match(r"(\d{2}:\d{2}:\d{2})\s+Upload Speed\s*:\s*([\d.]+)\s*bytes", line)
                    if m:
                        hms, speed_bytes = m.groups()
                        speed_mbps = round(float(speed_bytes) / 1_000_000, 2)
                        try:
                            dt = datetime.datetime.combine(d, datetime.time.fromisoformat(hms))
                            entries.append({"time": dt.isoformat(), "upload_mbps": speed_mbps})
                        except Exception:
                            entries.append({"time": f"{d.isoformat()} {hms}", "upload_mbps": speed_mbps})
        except Exception:
            pass
    entries.sort(key=lambda x: x.get("time", ""), reverse=True)
    return entries


def _parse_power_event_logs(days=2):
    entries = []
    today = datetime.date.today()
    for i in range(days):
        d = today - datetime.timedelta(days=i)
        path = os.path.join(STREAM_FOLDER_ROOT, f"SystemPowerEvents_{d.strftime('%Y-%m-%d')}.log")
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    line = line.strip()
                    m = re.match(r"(\d{2}-\d{2}-\d{4})\s+(\d{2}:\d{2}:\d{2})\s+(\w+)\s+(.*)", line)
                    if m:
                        dmy, hms, event, detail = m.groups()
                        try:
                            dt = datetime.datetime.strptime(f"{dmy} {hms}", "%d-%m-%Y %H:%M:%S")
                            entries.append({"time": dt.isoformat(), "event": event.strip(), "detail": detail.strip()})
                        except Exception:
                            entries.append({"time": f"{dmy} {hms}", "event": event.strip(), "detail": detail.strip()})
        except Exception:
            pass
    entries.sort(key=lambda x: x.get("time", ""), reverse=True)
    return entries


def check_system_logs():
    return {
        "internet":  _parse_network_status_logs(days=2),
        "cameras":   _parse_camera_status_logs(days=2),
        "speedtest": _parse_speed_test_logs(days=2),
        "power":     _parse_power_event_logs(days=2),
    }


def run_complete_monitoring(store_id, cameras):
    with ThreadPoolExecutor(max_workers=max(12, len(cameras) + 8)) as ex:
        cam_futures  = [ex.submit(check_camera, c) for c in cameras]
        f_internet   = ex.submit(check_internet)
        f_network    = ex.submit(get_network_info)
        f_system     = ex.submit(check_system)
        f_antivirus  = ex.submit(check_antivirus)
        f_av_list    = ex.submit(get_antivirus_details)
        f_wifi       = ex.submit(get_wifi_change_logs, 24)
        f_sleep      = ex.submit(get_sleep_wake_logs, 24)
        f_app        = ex.submit(check_app_status, cameras)
        f_syslogs    = ex.submit(check_system_logs)

        camera_results = [f.result() for f in cam_futures]
        internet       = f_internet.result()
        network        = f_network.result()
        system         = f_system.result()
        antivirus      = f_antivirus.result()
        antivirus_list = f_av_list.result()
        wifi_changes   = f_wifi.result()
        sleep_logs     = f_sleep.result()
        app_status     = f_app.result()
        system_logs    = f_syslogs.result()

    return {
        "store_id":       store_id,
        "timestamp":      datetime.datetime.now().isoformat(),
        "internet":       internet,
        "network":        network,
        "system":         system,
        "antivirus":      antivirus,
        "antivirus_list": antivirus_list,
        "wifi_changes":   wifi_changes,
        "sleep_logs":     sleep_logs,
        "cameras":        camera_results,
        "app_status":     app_status,
        "system_logs":    system_logs,
        "summary": {
            "total_cameras":   len(camera_results),
            "cameras_passing": sum(1 for c in camera_results if c["ping"] == "OK"),
            "rtsp_working":    sum(1 for c in camera_results if c["rtsp"] == "WORKING"),
        },
    }


# ===========================================================================
# UI CONSTANTS
# ===========================================================================
COL_BG     = "#f4f6f8"
COL_HEADER = "#1f2a37"
COL_CARD   = "#ffffff"
COL_BORDER = "#e2e8f0"
COL_TEXT   = "#1f2937"
COL_MUTED  = "#6b7280"
COL_ACCENT = "#2563eb"
COL_GREEN  = "#16a34a"
COL_AMBER  = "#d97706"
COL_RED    = "#dc2626"
COL_DARK   = "#0f172a"

STATE_COLORS = {"ok": COL_GREEN, "warn": COL_AMBER, "bad": COL_RED, "muted": COL_MUTED}

RTSP_INFO = {
    "WORKING":     ("🟢", "Stream Working",  ""),
    "REACHABLE":   ("🟡", "RTSP Reachable",  "Port responded but frame capture not confirmed.\n"
                                              "Install opencv-python for full verification."),
    "NO_VIDEO":    ("🟡", "No Video",        "Stream opened but returned no frames.\n"
                                              "Camera shutter may be closed or encoding issue."),
    "FAILED":      ("🔴", "Stream Failed",   "Could not open the RTSP stream.\n"
                                              "Camera may be misconfigured or port is blocked."),
    "TIMEOUT":     ("🟡", "RTSP Timeout",    "Stream did not respond within timeout.\n"
                                              "Camera may be overloaded or stream URL incorrect."),
    "SKIPPED":     ("🔴", "Not Checked",     "Camera is offline — RTSP check was skipped."),
    "NO_RESPONSE": ("🟡", "No Response",     "Connected to RTSP port but received no valid reply."),
    "INVALID_URL": ("🔴", "Invalid URL",     "The RTSP URL is malformed and cannot be used."),
    "NOT_CHECKED": ("⚪", "Not Checked",      "Monitoring has not been run yet."),
}


def _camera_health(cam):
    if cam.get("ping") != "OK":
        return "bad", "Offline"
    rtsp = cam.get("rtsp", "")
    if rtsp == "WORKING":
        return "ok", "Online"
    if rtsp == "REACHABLE":
        return "warn", "RTSP reachable"
    if rtsp == "NO_VIDEO":
        return "bad", "No video"
    return "bad", "RTSP down"


# ===========================================================================
# LOCAL NETWORK CAMERA SCANNER
# ===========================================================================
_CAMERA_OUIS = {
    # Hikvision
    "28:57:be": "Hikvision",   "ac:cc:8e": "Hikvision",   "4c:bd:8f": "Hikvision",
    "c0:56:e3": "Hikvision",   "e8:0b:06": "Hikvision",   "84:92:15": "Hikvision",
    "44:19:b6": "Hikvision",   "18:68:cb": "Hikvision",   "a4:14:37": "Hikvision",
    "3c:e8:24": "Hikvision",   "bc:ad:28": "Hikvision",   "f0:1c:13": "Hikvision",
    # Dahua
    "4c:11:bf": "Dahua",       "90:02:a9": "Dahua",       "e0:50:8b": "Dahua",
    "34:fe:56": "Dahua",       "e8:6d:52": "Dahua",       "b8:59:9f": "Dahua",
    # CP Plus (Aditya Infotech)
    "e0:e8:5d": "CP Plus",  "d4:a1:48": "CP Plus",  "00:0f:99": "CP Plus",
    "5c:35:48": "CP Plus",  "f8:20:97": "CP Plus",  "ec:c8:9c": "CP Plus",
    "00:12:34": "CP Plus",  "a8:57:4e": "CP Plus",  "54:27:58": "CP Plus",
    "d0:c5:d3": "CP Plus",  "1c:c0:e1": "CP Plus",
    # Axis
    "00:40:8c": "Axis",        "b8:a4:4f": "Axis",
    # Hanwha / Samsung Techwin
    "00:09:18": "Hanwha",      "58:e8:76": "Hanwha",      "00:16:6c": "Hanwha",
    # Bosch
    "00:04:63": "Bosch",
    # Pelco
    "00:07:71": "Pelco",
    # Vivotek
    "00:02:d1": "Vivotek",
    # Reolink
    "ec:71:db": "Reolink",
    # Uniview (UNV)
    "c8:f7:42": "Uniview",
    # Honeywell
    "00:0e:8f": "Honeywell",
    # FLIR
    "00:40:7f": "FLIR",
    # Tiandy
    "c0:39:37": "Tiandy",
    # Milesight
    "70:b3:d5": "Milesight",
}


def _get_local_subnet():
    """Return the /24 subnet prefix (e.g. '192.168.1') used for outbound traffic."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if not ip.startswith("127.") and not ip.startswith("169.254."):
            p = ip.split(".")
            return f"{p[0]}.{p[1]}.{p[2]}"
    except Exception:
        pass
    if psutil:
        for _, addrs in psutil.net_if_addrs().items():
            for addr in addrs:
                if (addr.family == socket.AF_INET
                        and not addr.address.startswith("127.")
                        and not addr.address.startswith("169.254.")):
                    p = addr.address.split(".")
                    return f"{p[0]}.{p[1]}.{p[2]}"
    return None


def _get_arp_table():
    """Return {ip: mac} from the OS ARP cache."""
    table = {}
    try:
        r = subprocess.run(["arp", "-a"],
                           capture_output=True, text=True,
                           timeout=5, creationflags=_NO_WIN)
        for line in r.stdout.splitlines():
            # Match full 6-octet MAC (aa-bb-cc-dd-ee-ff or aa:bb:cc:dd:ee:ff)
            m = re.search(
                r"(\d+\.\d+\.\d+\.\d+)\s+"
                r"([0-9a-fA-F]{2}(?:[-:][0-9a-fA-F]{2}){5})",
                line)
            if m:
                table[m.group(1)] = m.group(2).replace("-", ":").lower()
    except Exception:
        pass
    return table


def _oui_lookup(mac):
    if not mac or len(mac) < 8:
        return ""
    return _CAMERA_OUIS.get(mac[:8].lower(), "")


_BRAND_KEYWORDS = [
    ("hikvision",        "Hikvision"),
    ("hik-",             "Hikvision"),
    ("cp plus",          "CP Plus"),
    ("cpplus",           "CP Plus"),
    ("cp-plus",          "CP Plus"),
    ("aditya infotech",  "CP Plus"),
    ("ipcam",            "CP Plus"),   # CP Plus web-UI string
    ("dahua",            "Dahua"),
    ("dh-",              "Dahua"),
    ("axis communications", "Axis"),
    ("axis",             "Axis"),
    ("hanwha",           "Hanwha"),
    ("samsung",          "Hanwha"),
    ("vivotek",          "Vivotek"),
    ("reolink",          "Reolink"),
    ("uniview",          "Uniview"),
    ("unv",              "Uniview"),
    ("bosch",            "Bosch"),
    ("pelco",            "Pelco"),
    ("tiandy",           "Tiandy"),
    ("milesight",        "Milesight"),
    ("honeywell",        "Honeywell"),
]


def _scan_text_for_brand(text):
    t = text.lower()
    for kw, brand in _BRAND_KEYWORDS:
        if kw in t:
            return brand
    return ""


def _probe_manufacturer(ip, open_ports):
    """Try HTTP paths then RTSP OPTIONS to identify camera manufacturer."""
    http_paths = ["/", "/doc/page/login.asp", "/login.asp", "/index.html"]
    for port in [p for p in [80, 8080] if p in open_ports]:
        for path in http_paths:
            try:
                r = requests.get(
                    f"http://{ip}:{port}{path}", timeout=3, verify=False,
                    allow_redirects=True,
                    headers={"User-Agent": "OPS-Infra/2"})
                combined = (r.headers.get("Server", "")
                            + " " + r.headers.get("X-Powered-By", "")
                            + " " + r.text[:4000])
                brand = _scan_text_for_brand(combined)
                if brand:
                    return brand
            except Exception:
                pass

    if 554 in open_ports:
        try:
            with socket.create_connection((ip, 554), timeout=3) as sock:
                sock.settimeout(3)
                sock.sendall(
                    f"OPTIONS rtsp://{ip}:554/ RTSP/1.0\r\n"
                    f"CSeq: 1\r\nUser-Agent: OPS-Infra\r\n\r\n".encode())
                data = sock.recv(1024).decode(errors="ignore")
                brand = _scan_text_for_brand(data)
                if brand:
                    return brand
        except Exception:
            pass
    return ""


def scan_local_cameras(progress_cb=None):
    """
    Scan the local /24 subnet for camera-like devices.
    Returns (results_list, error_str).

    Two-phase design so the first run produces the same results as subsequent
    runs:
      Phase 1 — concurrent TCP port scan across all 254 hosts.  The TCP
                 handshakes populate the OS ARP cache as a side-effect.
      Phase 2 — read the now-warm ARP table, then identify the manufacturer
                 for every host that had an open camera port.
    """
    prefix = _get_local_subnet()
    if not prefix:
        return [], "Could not determine local subnet"

    hosts     = [f"{prefix}.{i}" for i in range(1, 255)]
    total     = len(hosts)
    done      = [0]
    lock      = threading.Lock()
    cam_ports = [554, 80, 8080, 37777]   # 37777 = Dahua SDK

    # ── Phase 1: concurrent port scan ────────────────────────────────────────
    def _scan_ports(ip):
        found = []
        for port in cam_ports:
            try:
                with socket.create_connection((ip, port), timeout=1.0):
                    found.append(port)
            except Exception:
                pass
        with lock:
            done[0] += 1
            if progress_cb:
                try:
                    progress_cb(done[0], total)
                except Exception:
                    pass
        return (ip, found) if found else None

    with ThreadPoolExecutor(max_workers=60) as ex:
        phase1 = [r for r in ex.map(_scan_ports, hosts) if r]

    # ── Phase 2: fresh ARP + manufacturer identification ─────────────────────
    # ARP cache is now warm from Phase 1 TCP connections — OUI lookup works.
    arp_table = _get_arp_table()

    results = []
    for ip, open_ports in phase1:
        mac    = arp_table.get(ip, "")
        mfr    = _oui_lookup(mac) or _probe_manufacturer(ip, open_ports)
        is_cam = (554 in open_ports) or (37777 in open_ports) or bool(mfr)
        results.append({
            "ip":          ip,
            "open_ports":  open_ports,
            "ports_str":   ", ".join(str(p) for p in open_ports),
            "manufacturer": mfr or ("Unknown" if is_cam else "—"),
            "mac":         mac or "—",
            "is_camera":   is_cam,
        })

    results.sort(key=lambda x: tuple(int(p) for p in x["ip"].split(".")))
    return results, None


# ===========================================================================
# MAIN APP
# ===========================================================================
class OPSInfraApp:
    def __init__(self, root):
        self.root = root
        self.root.title("OPS-Infra v2 · Infrastructure Monitoring")
        self.root.configure(bg=COL_BG)

        # Compute scale factor from actual screen resolution vs reference 1920×1080
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        raw = min(sw / 1920, sh / 1080)
        self.S = max(0.75, min(round(raw, 2), 2.0))   # clamp 0.75 – 2.0

        # Size the window to 88 % of the screen and centre it
        ww = int(sw * 0.88)
        wh = int(sh * 0.88)
        cx = (sw - ww) // 2
        cy = (sh - wh) // 2
        root.geometry(f"{ww}x{wh}+{cx}+{cy}")
        root.minsize(self._s(900), self._s(560))

        self.current_store_id  = None
        self.current_results   = None
        self.tiles             = {}
        self.info              = {}

        # RTSP stream state — one entry per camera number
        # viewer = {"canvas", "overlay", "rtsp_url", "running", "thread", "fq", "photo", "error"}
        self._stream_viewers  = {}
        self._rtsp_tab_frames = []       # tk.Frame objects added to rtsp_notebook

        self._init_style()
        self.setup_ui()
        self._tick()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    def _bind_col_resize(self, tree, proportions):
        """Redistribute treeview column widths proportionally on every resize."""
        def _resize(event):
            w = max(100, event.width - 4)
            for col, pct in proportions.items():
                tree.column(col, width=max(40, int(w * pct)))
        tree.bind("<Configure>", _resize)

    # ------------------------------------------------------------------
    def _s(self, n):
        """Scale a pixel value by the display scale factor."""
        return max(1, int(n * self.S))

    def _f(self, n):
        """Scale a font size — tiny fonts (≤9 pt) are never shrunk further."""
        if n <= 9:
            return n
        return max(9, int(n * self.S))

    # ------------------------------------------------------------------
    def _init_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Treeview", background=COL_CARD, fieldbackground=COL_CARD,
                        foreground=COL_TEXT, rowheight=self._s(30),
                        font=("Segoe UI", self._f(10)), borderwidth=0)
        style.configure("Treeview.Heading", background="#eef2f7", foreground=COL_TEXT,
                        font=("Segoe UI", self._f(10), "bold"),
                        padding=self._s(6), relief="flat")
        style.map("Treeview",
                  background=[("selected", "#dbeafe")],
                  foreground=[("selected", COL_TEXT)])
        style.configure("Accent.Horizontal.TProgressbar", background=COL_ACCENT)
        style.configure("TNotebook", background=COL_BG, borderwidth=0)
        style.configure("TNotebook.Tab", font=("Segoe UI", self._f(10), "bold"),
                        padding=(self._s(14), self._s(8)))
        style.map("TNotebook.Tab",
                  background=[("selected", COL_CARD)],
                  foreground=[("selected", COL_ACCENT)])

    # ------------------------------------------------------------------
    def setup_ui(self):
        header = tk.Frame(self.root, bg=COL_HEADER, height=self._s(64))
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(header, text="OPS-Infra v2", bg=COL_HEADER, fg="white",
                 font=("Segoe UI", self._f(18), "bold")).pack(
                 side="left", padx=(self._s(20), self._s(8)), pady=self._s(14))
        tk.Label(header, text="Infrastructure Monitoring", bg=COL_HEADER, fg="#9ca3af",
                 font=("Segoe UI", self._f(11))).pack(side="left", pady=(self._s(20), 0))
        self.header_clock = tk.Label(header, text="", bg=COL_HEADER, fg="#9ca3af",
                                     font=("Segoe UI", self._f(10)))
        self.header_clock.pack(side="right", padx=self._s(20))

        body = tk.Frame(self.root, bg=COL_BG)
        body.pack(fill="both", expand=True, padx=self._s(18), pady=self._s(6))

        self._build_controls(body)
        self._build_info_strip(body)
        self._build_tiles(body)
        self._build_tabs(body)
        self._build_footer(body)

    # ------------------------------------------------------------------
    def _build_controls(self, parent):
        bar = tk.Frame(parent, bg=COL_BG)
        bar.pack(fill="x", pady=(0, self._s(6)))
        tk.Label(bar, text="Store ID", bg=COL_BG, fg=COL_TEXT,
                 font=("Segoe UI", self._f(11), "bold")).pack(side="left")
        self.store_entry = tk.Entry(bar, font=("Segoe UI", self._f(12)), width=22, bg=COL_CARD,
                                    fg=COL_TEXT, relief="solid", bd=1, highlightthickness=1,
                                    highlightbackground=COL_BORDER, insertbackground=COL_TEXT)
        self.store_entry.pack(side="left", padx=(self._s(10), self._s(10)), ipady=self._s(4))
        self.store_entry.bind("<Return>", lambda _e: self.run_monitoring())
        self.store_entry.focus_set()
        self.run_button = tk.Button(bar, text="▶  Run Monitoring",
                                    font=("Segoe UI", self._f(11), "bold"),
                                    bg=COL_ACCENT, fg="white", activebackground="#1d4ed8",
                                    activeforeground="white", relief="flat", bd=0,
                                    padx=self._s(18), pady=self._s(8), cursor="hand2",
                                    command=self.run_monitoring)
        self.run_button.pack(side="left")
        self.progress = ttk.Progressbar(bar, mode="indeterminate", length=self._s(180),
                                        style="Accent.Horizontal.TProgressbar")
        self.controls_status = tk.Label(bar, text="Ready", bg=COL_BG, fg=COL_MUTED,
                                        font=("Segoe UI", self._f(10)))
        self.controls_status.pack(side="right")

    # ------------------------------------------------------------------
    def _build_info_strip(self, parent):
        card  = tk.Frame(parent, bg=COL_CARD,
                         highlightbackground=COL_BORDER, highlightthickness=1)
        card.pack(fill="x", pady=(0, self._s(6)))
        inner = tk.Frame(card, bg=COL_CARD)
        inner.pack(fill="x", padx=self._s(16), pady=self._s(6))
        for key, label in [("store", "STORE ID"), ("brand", "BRAND / CLIENT"),
                            ("cameras", "CAMERAS"), ("host", "MONITOR HOST"),
                            ("network", "NETWORK"), ("checked", "LAST CHECKED")]:
            col = tk.Frame(inner, bg=COL_CARD)
            col.pack(side="left", expand=True, anchor="w")
            tk.Label(col, text=label, bg=COL_CARD, fg=COL_MUTED,
                     font=("Segoe UI", 8, "bold")).pack(anchor="w")
            v = tk.Label(col, text="—", bg=COL_CARD, fg=COL_TEXT,
                         font=("Segoe UI", self._f(12), "bold"))
            v.pack(anchor="w")
            self.info[key] = v

    # ------------------------------------------------------------------
    def _build_tiles(self, parent):
        wrap  = tk.Frame(parent, bg=COL_BG)
        wrap.pack(fill="x", pady=(0, self._s(6)))
        specs = [("internet", "INTERNET"), ("cpu", "CPU"), ("ram", "RAM"),
                 ("disk", "DISK"), ("cameras_ok", "CAMERAS ONLINE"), ("rtsp_ok", "RTSP WORKING")]
        for i, (key, caption) in enumerate(specs):
            wrap.grid_columnconfigure(i, weight=1, uniform="tiles")
            card = tk.Frame(wrap, bg=COL_CARD,
                            highlightbackground=COL_BORDER, highlightthickness=1)
            card.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else self._s(8), 0))
            tk.Label(card, text=caption, bg=COL_CARD, fg=COL_MUTED,
                     font=("Segoe UI", 8, "bold")).pack(
                     anchor="w", padx=self._s(12), pady=(self._s(5), 0))
            val = tk.Label(card, text="—", bg=COL_CARD, fg=COL_TEXT,
                           font=("Segoe UI", self._f(16), "bold"))
            val.pack(anchor="w", padx=self._s(12))
            sub = tk.Label(card, text="", bg=COL_CARD, fg=COL_MUTED,
                           font=("Segoe UI", 8))
            sub.pack(anchor="w", padx=self._s(12), pady=(0, self._s(5)))
            self.tiles[key] = {"val": val, "sub": sub}

    # ------------------------------------------------------------------
    def _build_tabs(self, parent):
        nb = ttk.Notebook(parent)
        nb.pack(fill="both", expand=True)
        self.notebook = nb

        cam_tab = tk.Frame(nb, bg=COL_CARD)
        nb.add(cam_tab, text="Camera Status")
        self._build_camera_table(cam_tab)

        rtsp_tab = tk.Frame(nb, bg=COL_CARD)
        nb.add(rtsp_tab, text="RTSP Preview")
        self._build_rtsp_preview_tab(rtsp_tab)

        net_tab = tk.Frame(nb, bg=COL_CARD)
        nb.add(net_tab, text="Other Camera IPs")
        self._build_other_cameras_tab(net_tab)

        wifi_tab = tk.Frame(nb, bg=COL_CARD)
        nb.add(wifi_tab, text="Wi-Fi History (24h)")
        self._build_wifi_table(wifi_tab)

        sleep_tab = tk.Frame(nb, bg=COL_CARD)
        nb.add(sleep_tab, text="Sleep / Power Logs")
        self._build_sleep_table(sleep_tab)

        av_tab = tk.Frame(nb, bg=COL_CARD)
        nb.add(av_tab, text="Antivirus")
        self._build_av_table(av_tab)

        app_tab = tk.Frame(nb, bg=COL_CARD)
        nb.add(app_tab, text="App Status")
        self._build_appstatus_tab(app_tab)

        syslogs_tab = tk.Frame(nb, bg=COL_CARD)
        nb.add(syslogs_tab, text="System Logs (2d)")
        self._build_syslogs_tab(syslogs_tab)

    # ------------------------------------------------------------------
    # TAB 0: Camera Status — ping only + offline reason
    # ------------------------------------------------------------------
    def _build_camera_table(self, parent):
        cols     = ("status", "camera", "ip", "mfr", "ping", "lat", "reason")
        headings = {
            "status": "Status", "camera": "Camera", "ip": "IP Address",
            "mfr": "Manufacturer", "ping": "Ping", "lat": "Latency",
            "reason": "Offline Reason",
        }
        widths  = {"status": self._s(110), "camera": self._s(80), "ip": self._s(145),
                   "mfr": self._s(120), "ping": self._s(80), "lat": self._s(90),
                   "reason": self._s(330)}
        anchors = {"status": "w", "camera": "w", "ip": "w", "mfr": "w",
                   "ping": "center", "lat": "center", "reason": "w"}

        wrap = tk.Frame(parent, bg=COL_CARD)
        wrap.pack(fill="both", expand=True, padx=self._s(14), pady=self._s(14))
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings", selectmode="browse")
        for c in cols:
            self.tree.heading(c, text=headings[c], anchor=anchors[c])
            self.tree.column(c, width=widths[c], anchor=anchors[c], stretch=True, minwidth=40)
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.tag_configure("ok",   background="#f0fdf4", foreground="#15803d")
        self.tree.tag_configure("warn", background="#fffbeb", foreground="#92400e")
        self.tree.tag_configure("bad",  background="#fef2f2", foreground="#b91c1c")
        self.tree.insert("", "end",
                         values=("", "", "Enter a Store ID and click Run Monitoring",
                                 "", "", "", ""))
        self._bind_col_resize(self.tree, {
            "status": 0.13, "camera": 0.09, "ip": 0.15,
            "mfr":   0.17,  "ping":   0.07, "lat": 0.09, "reason": 0.30,
        })

    # ------------------------------------------------------------------
    # TAB 1: RTSP Preview — cv2 + Pillow canvas per camera
    # ------------------------------------------------------------------
    def _build_rtsp_preview_tab(self, parent):
        # Control bar
        ctrl_card = tk.Frame(parent, bg=COL_CARD,
                             highlightbackground=COL_BORDER, highlightthickness=1)
        ctrl_card.pack(fill="x", padx=self._s(14), pady=(self._s(14), self._s(6)))
        ctrl = tk.Frame(ctrl_card, bg=COL_CARD)
        ctrl.pack(fill="x", padx=self._s(14), pady=self._s(10))

        tk.Label(ctrl, text="RTSP Live Preview", bg=COL_CARD, fg=COL_TEXT,
                 font=("Segoe UI", self._f(11), "bold")).pack(side="left")

        can_preview = cv2 is not None and Image is not None
        prev_text   = ("Preview engine: OpenCV + Pillow  ✓"
                       if can_preview else
                       "Preview engine: Not available  —  pip install opencv-python Pillow")
        prev_color  = COL_GREEN if can_preview else COL_AMBER
        tk.Label(ctrl, text=prev_text, bg=COL_CARD, fg=prev_color,
                 font=("Segoe UI", 9)).pack(side="left", padx=self._s(16))

        self._rtsp_stop_btn = tk.Button(
            ctrl, text="■  Stop All", font=("Segoe UI", self._f(10)),
            bg="#e5e7eb", fg=COL_TEXT, relief="flat", bd=0,
            padx=self._s(12), pady=self._s(5), cursor="hand2", command=self._stop_all_rtsp)
        self._rtsp_stop_btn.pack(side="right")

        self._rtsp_play_btn = tk.Button(
            ctrl, text="▶  Play All", font=("Segoe UI", self._f(10), "bold"),
            bg=COL_ACCENT, fg="white", activebackground="#1d4ed8",
            activeforeground="white", relief="flat", bd=0,
            padx=self._s(12), pady=self._s(5), cursor="hand2", command=self._play_all_rtsp)
        self._rtsp_play_btn.pack(side="right", padx=(0, self._s(8)))

        # Sub-notebook (one tab per camera)
        self.rtsp_notebook = ttk.Notebook(parent)
        self.rtsp_notebook.pack(fill="both", expand=True,
                                padx=self._s(14), pady=(0, self._s(14)))

        ph = tk.Frame(self.rtsp_notebook, bg=COL_CARD)
        self.rtsp_notebook.add(ph, text="No cameras loaded")
        self._rtsp_tab_frames.append(ph)
        tk.Label(ph, text="Run monitoring to load RTSP previews",
                 bg=COL_CARD, fg=COL_MUTED, font=("Segoe UI", self._f(12))).pack(expand=True)

    # ------------------------------------------------------------------
    def _rebuild_rtsp_tabs(self, camera_results):
        """Called after each monitoring run to rebuild per-camera RTSP sub-tabs."""
        self._stop_all_rtsp()
        self._stream_viewers.clear()

        for frame in self._rtsp_tab_frames:
            try:
                self.rtsp_notebook.forget(frame)
                frame.destroy()
            except Exception:
                pass
        self._rtsp_tab_frames.clear()

        if not camera_results:
            ph = tk.Frame(self.rtsp_notebook, bg=COL_CARD)
            self.rtsp_notebook.add(ph, text="No cameras")
            self._rtsp_tab_frames.append(ph)
            tk.Label(ph, text="No cameras found for this store.",
                     bg=COL_CARD, fg=COL_MUTED, font=("Segoe UI", 12)).pack(expand=True)
            self.notebook.tab(1, text="RTSP Preview")
            return

        can_preview = cv2 is not None and Image is not None
        working     = sum(1 for c in camera_results if c.get("rtsp") == "WORKING")
        total       = len(camera_results)
        self.notebook.tab(1, text=f"RTSP Preview  ({working}/{total} working)")

        dots = {"ok": "●", "warn": "◑", "bad": "○"}
        for cam in camera_results:
            state, _    = _camera_health(cam)
            dot         = dots.get(state, "○")
            cam_num     = cam.get("camera_number", "CAM")
            rtsp_status = cam.get("rtsp", "SKIPPED")

            use_player  = rtsp_status in ("WORKING", "REACHABLE") and can_preview
            bg          = "black" if use_player else COL_CARD

            tab_frame = tk.Frame(self.rtsp_notebook, bg=bg)
            self.rtsp_notebook.add(tab_frame, text=f"{dot} CAM {cam_num}")
            self._rtsp_tab_frames.append(tab_frame)

            if use_player:
                self._build_rtsp_player_panel(tab_frame, cam)
            elif rtsp_status in ("WORKING", "REACHABLE"):
                self._build_rtsp_no_preview_panel(tab_frame, cam)
            else:
                self._build_rtsp_error_panel(tab_frame, cam)

    # ------------------------------------------------------------------
    def _build_rtsp_player_panel(self, parent, cam):
        """
        OpenCV + Pillow canvas-based RTSP player.
        Frames are decoded in a background thread and painted onto a tk.Canvas.
        No external software required.
        """
        cam_num  = cam.get("camera_number", "CAM")
        rtsp_url = cam.get("rtsp_url", "")

        # ── top control bar ──────────────────────────────────────────
        top = tk.Frame(parent, bg="#1e293b", height=self._s(42))
        top.pack(fill="x")
        top.pack_propagate(False)
        tk.Label(top,
                 text=(f"CAM {cam_num}  ·  {cam.get('ip', '')}  ·  "
                       f"{cam.get('manufacturer', '')}"),
                 bg="#1e293b", fg="white",
                 font=("Segoe UI", self._f(10), "bold")).pack(
                 side="left", padx=self._s(12), pady=self._s(10))

        stop_btn = tk.Button(top, text="■ Stop", font=("Segoe UI", 9),
                             bg="#374151", fg="white", relief="flat", bd=0,
                             padx=self._s(8), pady=self._s(2), cursor="hand2",
                             command=lambda n=cam_num: self._stop_camera(n))
        stop_btn.pack(side="right", padx=(0, self._s(10)), pady=self._s(8))

        play_btn = tk.Button(top, text="▶ Play", font=("Segoe UI", 9, "bold"),
                             bg=COL_ACCENT, fg="white", relief="flat", bd=0,
                             padx=self._s(8), pady=self._s(2), cursor="hand2",
                             command=lambda n=cam_num: self._play_camera(n))
        play_btn.pack(side="right", pady=self._s(8))

        # ── video canvas ─────────────────────────────────────────────
        canvas = tk.Canvas(parent, bg="black", highlightthickness=0)
        canvas.pack(fill="both", expand=True)

        # Overlay shown when stream is idle
        overlay = tk.Label(canvas,
                            text="Press  ▶ Play  or  ▶ Play All  to start stream",
                            bg="black", fg="#4b5563", font=("Segoe UI", self._f(11)))
        overlay.place(relx=0.5, rely=0.5, anchor="center")

        # ── RTSP URL footer bar ───────────────────────────────────────
        url_bar = tk.Frame(parent, bg=COL_DARK, height=self._s(26))
        url_bar.pack(fill="x")
        url_bar.pack_propagate(False)
        masked = re.sub(r'(rtsp://[^:]+:)[^@]+(@)', r'\1****\2', rtsp_url)
        tk.Label(url_bar, text=masked[:110], bg=COL_DARK, fg="#475569",
                 font=("Segoe UI", 8)).pack(side="left", padx=self._s(8), pady=self._s(4))

        # Register viewer state
        self._stream_viewers[cam_num] = {
            "canvas":   canvas,
            "overlay":  overlay,
            "rtsp_url": rtsp_url,
            "running":  False,
            "thread":   None,
            "fq":       None,    # frame queue, created on play
            "photo":    None,    # PhotoImage reference (prevents GC)
            "error":    None,
        }

    # ------------------------------------------------------------------
    def _build_rtsp_no_preview_panel(self, parent, cam):
        """Shown when RTSP is working but opencv-python / Pillow is not installed."""
        wrap  = tk.Frame(parent, bg=COL_CARD)
        wrap.pack(fill="both", expand=True)
        inner = tk.Frame(wrap, bg=COL_CARD)
        inner.place(relx=0.5, rely=0.45, anchor="center")

        tk.Label(inner, text="🟢", bg=COL_CARD, font=("Segoe UI", self._f(36))).pack()
        tk.Label(inner, text="RTSP Stream Working", bg=COL_CARD, fg=COL_GREEN,
                 font=("Segoe UI", self._f(13), "bold")).pack(pady=(self._s(10), self._s(4)))
        tk.Label(inner,
                 text="Live preview requires opencv-python and Pillow.\n"
                      "Install them — no external software needed:",
                 bg=COL_CARD, fg=COL_MUTED,
                 font=("Segoe UI", self._f(10)), justify="center").pack()

        cmd = tk.Frame(inner, bg="#f1f5f9",
                       highlightbackground=COL_BORDER, highlightthickness=1)
        cmd.pack(pady=self._s(10), fill="x")
        tk.Label(cmd, text="pip install opencv-python Pillow",
                 bg="#f1f5f9", fg=COL_DARK,
                 font=("Courier New", self._f(10)),
                 padx=self._s(14), pady=self._s(8)).pack()

        tk.Frame(inner, bg=COL_BORDER, height=1).pack(fill="x", pady=self._s(12))
        masked = re.sub(r'(rtsp://[^:]+:)[^@]+(@)', r'\1****\2',
                        cam.get("rtsp_url", ""))
        tk.Label(inner, text=f"RTSP URL: {masked[:80]}",
                 bg=COL_CARD, fg=COL_TEXT, font=("Segoe UI", 9)).pack()

    # ------------------------------------------------------------------
    def _build_rtsp_error_panel(self, parent, cam):
        """Shown when RTSP is not working — displays the failure reason."""
        rtsp_status = cam.get("rtsp", "UNKNOWN")
        icon, title, detail = RTSP_INFO.get(
            rtsp_status, ("⚪", f"Status: {rtsp_status}", ""))

        if rtsp_status == "SKIPPED" and cam.get("ping_reason"):
            detail = f"Camera is offline.\n{cam['ping_reason']}"
        elif rtsp_status == "INVALID_URL":
            detail = f"The RTSP URL is malformed:\n{cam.get('rtsp_url', 'N/A')}"

        wrap  = tk.Frame(parent, bg=COL_CARD)
        wrap.pack(fill="both", expand=True)
        inner = tk.Frame(wrap, bg=COL_CARD)
        inner.place(relx=0.5, rely=0.42, anchor="center")

        tk.Label(inner, text=icon, bg=COL_CARD,
                 font=("Segoe UI", self._f(38))).pack()
        tk.Label(inner, text=title, bg=COL_CARD, fg=COL_TEXT,
                 font=("Segoe UI", self._f(13), "bold")).pack(pady=(self._s(10), self._s(4)))
        if detail:
            tk.Label(inner, text=detail, bg=COL_CARD, fg=COL_MUTED,
                     font=("Segoe UI", self._f(10)), justify="center",
                     wraplength=self._s(420)).pack()

        tk.Frame(inner, bg=COL_BORDER, height=1).pack(fill="x", pady=self._s(14))

        for label, value in [
            ("Camera",       cam.get("camera_number", "—")),
            ("IP Address",   cam.get("ip", "—")),
            ("Manufacturer", cam.get("manufacturer", "—")),
            ("Ping",         cam.get("ping", "—")),
            ("RTSP URL",     (cam.get("rtsp_url") or "—")[:72]),
        ]:
            row = tk.Frame(inner, bg=COL_CARD)
            row.pack(anchor="w", pady=1)
            tk.Label(row, text=f"{label}:", bg=COL_CARD, fg=COL_MUTED,
                     font=("Segoe UI", 9, "bold"),
                     width=14, anchor="e").pack(side="left")
            tk.Label(row, text=value, bg=COL_CARD, fg=COL_TEXT,
                     font=("Segoe UI", 9)).pack(side="left", padx=self._s(6))

    # ------------------------------------------------------------------
    # STREAM ENGINE  (cv2 reader thread + canvas painter)
    # ------------------------------------------------------------------
    def _start_stream_reader(self, cam_num):
        """Launch background reader thread for one camera."""
        viewer = self._stream_viewers.get(cam_num)
        if not viewer or viewer["running"]:
            return

        viewer["running"] = True
        viewer["error"]   = None
        viewer["fq"]      = queue.Queue(maxsize=3)

        rtsp_url = viewer["rtsp_url"]

        def _reader():
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"
            cap  = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
            if not cap.isOpened():
                viewer["error"]   = "Could not open RTSP stream"
                viewer["running"] = False
                cap.release()
                return
            fails = 0
            while viewer["running"]:
                ok, frame = cap.read()
                if not ok:
                    fails += 1
                    if fails > 15:
                        viewer["error"]   = "Stream lost — camera disconnected"
                        viewer["running"] = False
                        break
                    time.sleep(0.05)
                    continue
                fails = 0
                # Keep only the latest frame
                try:
                    viewer["fq"].get_nowait()
                except queue.Empty:
                    pass
                try:
                    viewer["fq"].put_nowait(frame)
                except queue.Full:
                    pass
            cap.release()

        viewer["thread"] = threading.Thread(target=_reader, daemon=True)
        viewer["thread"].start()

        # Hide idle overlay
        try:
            viewer["overlay"].place_forget()
        except Exception:
            pass

        # Start canvas update loop
        self._update_stream_canvas(cam_num)

    def _update_stream_canvas(self, cam_num):
        """Pull the latest frame from the queue and paint it on the Canvas (~30 fps)."""
        viewer = self._stream_viewers.get(cam_num)
        if not viewer:
            return

        canvas = viewer["canvas"]

        # Guard: canvas may have been destroyed when tabs were rebuilt
        try:
            if not canvas.winfo_exists():
                return
        except Exception:
            return

        # Show error state
        if viewer.get("error"):
            try:
                canvas.delete("all")
                w, h = canvas.winfo_width(), canvas.winfo_height()
                canvas.create_text(
                    max(w // 2, 10), max(h // 2, 10),
                    text=f"⚠  {viewer['error']}",
                    fill=COL_RED, font=("Segoe UI", 11), anchor="center")
            except Exception:
                pass
            viewer["running"] = False
            return

        if viewer["fq"] is not None:
            try:
                frame = viewer["fq"].get_nowait()
                w = canvas.winfo_width()
                h = canvas.winfo_height()
                if w > 10 and h > 10 and Image is not None:
                    rgb   = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    img   = Image.fromarray(rgb).resize((w, h))
                    photo = ImageTk.PhotoImage(image=img)
                    viewer["photo"] = photo          # hold reference → prevents GC
                    canvas.delete("all")
                    canvas.create_image(0, 0, anchor="nw", image=photo)
            except queue.Empty:
                pass
            except Exception:
                pass

        if viewer["running"]:
            canvas.after(33, lambda: self._update_stream_canvas(cam_num))  # ~30 fps

    # ------------------------------------------------------------------
    def _play_camera(self, cam_num):
        self._start_stream_reader(cam_num)

    def _stop_camera(self, cam_num):
        viewer = self._stream_viewers.get(cam_num)
        if not viewer:
            return
        viewer["running"] = False
        try:
            canvas  = viewer["canvas"]
            overlay = viewer["overlay"]
            if canvas.winfo_exists():
                canvas.delete("all")
                overlay.place(relx=0.5, rely=0.5, anchor="center")
        except Exception:
            pass

    def _play_all_rtsp(self):
        for cam_num in list(self._stream_viewers):
            self._play_camera(cam_num)

    def _stop_all_rtsp(self):
        for cam_num in list(self._stream_viewers):
            self._stop_camera(cam_num)

    def _cleanup_rtsp(self):
        """Stop all streams. Called on window close."""
        for viewer in self._stream_viewers.values():
            viewer["running"] = False
        self._stream_viewers.clear()

    def _on_close(self):
        self._cleanup_rtsp()
        self.root.destroy()

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # TAB 2: Other Camera IPs — local network camera discovery
    # ------------------------------------------------------------------
    def _build_other_cameras_tab(self, parent):
        # ── control bar ──────────────────────────────────────────────────
        ctrl_card = tk.Frame(parent, bg=COL_CARD,
                             highlightbackground=COL_BORDER, highlightthickness=1)
        ctrl_card.pack(fill="x", padx=self._s(14), pady=(self._s(14), self._s(6)))
        ctrl = tk.Frame(ctrl_card, bg=COL_CARD)
        ctrl.pack(fill="x", padx=self._s(14), pady=self._s(10))

        tk.Label(ctrl, text="Local Network Camera Discovery",
                 bg=COL_CARD, fg=COL_TEXT,
                 font=("Segoe UI", self._f(11), "bold")).pack(side="left")

        self._netscan_status = tk.Label(ctrl,
                                        text="Scans the local /24 subnet for camera devices",
                                        bg=COL_CARD, fg=COL_MUTED,
                                        font=("Segoe UI", 9))
        self._netscan_status.pack(side="left", padx=self._s(14))

        self._netscan_btn = tk.Button(
            ctrl, text="▶  Scan Network",
            font=("Segoe UI", self._f(10), "bold"),
            bg=COL_ACCENT, fg="white", activebackground="#1d4ed8",
            activeforeground="white", relief="flat", bd=0,
            padx=self._s(14), pady=self._s(5), cursor="hand2",
            command=self._run_netscan)
        self._netscan_btn.pack(side="right")

        self._netscan_pb = ttk.Progressbar(ctrl, mode="determinate",
                                            length=self._s(180),
                                            style="Accent.Horizontal.TProgressbar")

        # ── result table ─────────────────────────────────────────────────
        cols     = ("status", "ip", "ports", "manufacturer", "mac")
        headings = {
            "status": "Status", "ip": "IP Address", "ports": "Open Ports",
            "manufacturer": "Manufacturer", "mac": "MAC Address",
        }
        anchors = {"status": "w", "ip": "w", "ports": "w", "manufacturer": "w", "mac": "w"}

        wrap = tk.Frame(parent, bg=COL_CARD)
        wrap.pack(fill="both", expand=True, padx=self._s(14), pady=(0, self._s(14)))
        self.netscan_tree = ttk.Treeview(wrap, columns=cols,
                                          show="headings", selectmode="browse")
        for c in cols:
            self.netscan_tree.heading(c, text=headings[c], anchor=anchors[c])
            self.netscan_tree.column(c, width=self._s(120),
                                     anchor=anchors[c], stretch=True, minwidth=40)
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.netscan_tree.yview)
        self.netscan_tree.configure(yscrollcommand=vsb.set)
        self.netscan_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.netscan_tree.tag_configure("camera",  background="#f0fdf4", foreground="#15803d")
        self.netscan_tree.tag_configure("device",  background="#ffffff", foreground=COL_MUTED)
        self.netscan_tree.insert("", "end",
                                  values=("", "", "Click 'Scan Network' to discover cameras",
                                          "", ""))
        self._bind_col_resize(self.netscan_tree, {
            "status": 0.12, "ip": 0.17, "ports": 0.17,
            "manufacturer": 0.31, "mac": 0.23,
        })

    # ------------------------------------------------------------------
    def _run_netscan(self):
        self._netscan_btn.config(state="disabled")
        self._netscan_pb["value"] = 0
        self._netscan_pb.pack(side="right", padx=(0, self._s(8)))
        self._netscan_status.config(text="Scanning…  (takes ~10 s)", fg=COL_AMBER)
        self.netscan_tree.delete(*self.netscan_tree.get_children())
        self.netscan_tree.insert("", "end",
                                  values=("", "", "Scanning local network…", "", ""))

        def _progress(done, total):
            pct = int(done * 100 / total)
            self.root.after(0, lambda p=pct:
                            self._netscan_pb.config(value=p))
            self.root.after(0, lambda d=done, t=total:
                            self._netscan_status.config(
                                text=f"Scanning {d}/{t}…", fg=COL_AMBER))

        def _worker():
            results, error = scan_local_cameras(_progress)
            self.root.after(0, lambda: self._netscan_done(results, error))

        threading.Thread(target=_worker, daemon=True).start()

    def _netscan_done(self, results, error):
        self._netscan_btn.config(state="normal")
        self._netscan_pb.pack_forget()

        if error:
            self._netscan_status.config(text=f"Error: {error}", fg=COL_RED)
            return

        # Cross-reference with manufacturer names already fetched from dashboard
        if self.current_results:
            known = {c["ip"]: c.get("manufacturer", "")
                     for c in self.current_results.get("cameras", [])
                     if c.get("ip") and c.get("manufacturer")}
            for r in results:
                if r["ip"] in known:
                    r["manufacturer"] = known[r["ip"]]
                    r["is_camera"] = True

        self.netscan_tree.delete(*self.netscan_tree.get_children())

        cameras = [r for r in results if r.get("is_camera")]
        devices = [r for r in results if not r.get("is_camera")]

        for r in cameras:
            self.netscan_tree.insert("", "end", tags=("camera",), values=(
                "● Camera",
                r["ip"], r["ports_str"], r["manufacturer"], r["mac"],
            ))
        for r in devices:
            self.netscan_tree.insert("", "end", tags=("device",), values=(
                "○ Device",
                r["ip"], r["ports_str"], "—", r["mac"],
            ))

        n = len(cameras)
        self._netscan_status.config(
            text=(f"Found {n} camera{'s' if n != 1 else ''} "
                  f"({len(results)} devices total on this subnet)"),
            fg=COL_GREEN if n else COL_MUTED)
        self.notebook.tab(2, text=f"Other Camera IPs ({n})")

    # ------------------------------------------------------------------
    # TABS 3–5 (Wi-Fi, Sleep/Power, Antivirus)
    # ------------------------------------------------------------------
    def _build_wifi_table(self, parent):
        cols     = ("time", "event", "ssid")
        headings = {"time": "Time", "event": "Event", "ssid": "Wi-Fi Name (SSID)"}
        widths   = {"time": self._s(200), "event": self._s(140), "ssid": self._s(320)}
        anchors  = {"time": "w", "event": "w", "ssid": "w"}
        wrap = tk.Frame(parent, bg=COL_CARD)
        wrap.pack(fill="both", expand=True, padx=self._s(14), pady=self._s(14))
        self.wifi_tree = ttk.Treeview(wrap, columns=cols, show="headings", selectmode="browse")
        for c in cols:
            self.wifi_tree.heading(c, text=headings[c], anchor=anchors[c])
            self.wifi_tree.column(c, width=widths[c], anchor=anchors[c], stretch=True, minwidth=40)
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.wifi_tree.yview)
        self.wifi_tree.configure(yscrollcommand=vsb.set)
        self.wifi_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.wifi_tree.tag_configure("ok",   background="#ffffff")
        self.wifi_tree.tag_configure("warn", background="#fffbeb")
        self.wifi_tree.insert("", "end", values=("", "", "Run monitoring to load Wi-Fi history"))
        self._bind_col_resize(self.wifi_tree, {"time": 0.28, "event": 0.17, "ssid": 0.55})

    def _build_sleep_table(self, parent):
        cols     = ("time", "event", "detail")
        headings = {"time": "Time", "event": "Event", "detail": "Details"}
        widths   = {"time": self._s(200), "event": self._s(140), "detail": self._s(380)}
        anchors  = {"time": "w", "event": "w", "detail": "w"}
        wrap = tk.Frame(parent, bg=COL_CARD)
        wrap.pack(fill="both", expand=True, padx=self._s(14), pady=self._s(14))
        self.sleep_tree = ttk.Treeview(wrap, columns=cols, show="headings", selectmode="browse")
        for c in cols:
            self.sleep_tree.heading(c, text=headings[c], anchor=anchors[c])
            self.sleep_tree.column(c, width=widths[c], anchor=anchors[c], stretch=True, minwidth=40)
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.sleep_tree.yview)
        self.sleep_tree.configure(yscrollcommand=vsb.set)
        self.sleep_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.sleep_tree.tag_configure("ok",   background="#ffffff")
        self.sleep_tree.tag_configure("warn", background="#fffbeb")
        self.sleep_tree.tag_configure("bad",  background="#fef2f2")
        self.sleep_tree.insert("", "end",
                               values=("", "", "Run monitoring to load sleep / power history"))
        self._bind_col_resize(self.sleep_tree, {"time": 0.25, "event": 0.17, "detail": 0.58})

    def _build_av_table(self, parent):
        cols     = ("product", "status", "defs", "updated")
        headings = {"product": "Antivirus Product", "status": "Real-time Protection",
                    "defs": "Definitions", "updated": "Last Updated"}
        widths   = {"product": self._s(240), "status": self._s(170),
                    "defs": self._s(150), "updated": self._s(220)}
        anchors  = {"product": "w", "status": "w", "defs": "w", "updated": "w"}
        wrap = tk.Frame(parent, bg=COL_CARD)
        wrap.pack(fill="both", expand=True, padx=self._s(14), pady=self._s(14))
        self.av_tree = ttk.Treeview(wrap, columns=cols, show="headings", selectmode="browse")
        for c in cols:
            self.av_tree.heading(c, text=headings[c], anchor=anchors[c])
            self.av_tree.column(c, width=widths[c], anchor=anchors[c], stretch=True, minwidth=40)
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.av_tree.yview)
        self.av_tree.configure(yscrollcommand=vsb.set)
        self.av_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.av_tree.tag_configure("ok",   background="#ffffff")
        self.av_tree.tag_configure("warn", background="#fffbeb")
        self.av_tree.tag_configure("bad",  background="#fef2f2")
        self.av_tree.insert("", "end",
                            values=("", "Run monitoring to load antivirus status", "", ""))
        self._bind_col_resize(self.av_tree, {
            "product": 0.30, "status": 0.22, "defs": 0.20, "updated": 0.28,
        })

    # ------------------------------------------------------------------
    def _build_appstatus_tab(self, parent):
        STREAM_STATUS_TEXT = {"active":"● Active","stale":"◑ Stale","empty":"○ Empty","missing":"✕ Missing"}
        STREAM_STATUS_TAG  = {"active":"ok","stale":"warn","empty":"warn","missing":"bad"}
        self._appstatus_stream_text = STREAM_STATUS_TEXT
        self._appstatus_stream_tag  = STREAM_STATUS_TAG

        # process strip
        proc_frame = tk.Frame(parent, bg="#f1f5f9", pady=self._s(8))
        proc_frame.pack(fill="x", padx=self._s(10), pady=(self._s(8), 0))
        self.app_proc_labels = {}
        for i, (key, caption) in enumerate([("app","APPLICATION"),("proc_status","PROCESS"),
                                             ("pid","PID"),("uptime","UPTIME"),("crashes","CRASHES (2d)")]):
            col = tk.Frame(proc_frame, bg="#f1f5f9")
            col.grid(row=0, column=i, padx=self._s(14), sticky="w")
            tk.Label(col, text=caption, bg="#f1f5f9", fg=COL_MUTED,
                     font=("Segoe UI", self._f(7), "bold")).pack(anchor="w")
            v = tk.Label(col, text="—", bg="#f1f5f9", fg=COL_TEXT,
                         font=("Segoe UI", self._f(11), "bold"))
            v.pack(anchor="w")
            self.app_proc_labels[key] = v

        # crash events
        tk.Label(parent, text="Crash Events  (last 2 days)", bg=COL_CARD,
                 fg=COL_TEXT, font=("Segoe UI", self._f(9), "bold")).pack(
                 anchor="w", padx=self._s(10), pady=(self._s(8), 0))
        crash_frame = tk.Frame(parent, bg=COL_CARD, height=self._s(120))
        crash_frame.pack(fill="x", padx=self._s(10)); crash_frame.pack_propagate(False)
        crash_cols = ("time","evid","level","msg")
        wrap1 = tk.Frame(crash_frame, bg=COL_CARD); wrap1.pack(fill="both", expand=True)
        sb1   = ttk.Scrollbar(wrap1, orient="vertical")
        sb1.pack(side="right", fill="y")
        self.crash_tree = ttk.Treeview(wrap1, columns=crash_cols, show="headings",
                                        yscrollcommand=sb1.set)
        sb1.config(command=self.crash_tree.yview)
        self.crash_tree.pack(fill="both", expand=True)
        for c, h, w, a in zip(crash_cols, ("Time","Event ID","Level","Message"),
                               (self._s(155),self._s(80),self._s(80),self._s(500)),
                               ("w","center","center","w")):
            self.crash_tree.heading(c, text=h, anchor=a)
            self.crash_tree.column(c, width=w, anchor=a, stretch=True, minwidth=40)
        self.crash_tree.tag_configure("ok",  background="#f0fdf4", foreground="#15803d")
        self.crash_tree.tag_configure("warn",background="#fffbeb", foreground="#92400e")
        self.crash_tree.tag_configure("bad", background="#fef2f2", foreground="#b91c1c")
        self.crash_tree.insert("","end",values=("","","","Run monitoring to load crash events"))
        self._bind_col_resize(self.crash_tree,{"time":0.18,"evid":0.09,"level":0.09,"msg":0.64})

        # stream folders
        tk.Label(parent, text="Stream Folder Status  (images checked last 1h)",
                 bg=COL_CARD, fg=COL_TEXT,
                 font=("Segoe UI", self._f(9), "bold")).pack(
                 anchor="w", padx=self._s(10), pady=(self._s(6), 0))
        sf_frame = tk.Frame(parent, bg=COL_CARD)
        sf_frame.pack(fill="both", expand=True, padx=self._s(10), pady=(0, self._s(8)))
        sf_cols = ("stream_id","camera","ip","status","last_modified","recent","total")
        wrap2 = tk.Frame(sf_frame, bg=COL_CARD); wrap2.pack(fill="both", expand=True)
        sb2   = ttk.Scrollbar(wrap2, orient="vertical")
        sb2.pack(side="right", fill="y")
        self.sf_tree = ttk.Treeview(wrap2, columns=sf_cols, show="headings",
                                     yscrollcommand=sb2.set)
        sb2.config(command=self.sf_tree.yview)
        self.sf_tree.pack(fill="both", expand=True)
        for c, h, w, a in zip(sf_cols,
                               ("Stream ID","Camera","IP","Status","Last Modified","1h Images","Total"),
                               (self._s(140),self._s(80),self._s(120),self._s(100),
                                self._s(160),self._s(80),self._s(70)),
                               ("w","w","w","w","w","center","center")):
            self.sf_tree.heading(c, text=h, anchor=a)
            self.sf_tree.column(c, width=w, anchor=a, stretch=True, minwidth=40)
        self.sf_tree.tag_configure("ok",  background="#f0fdf4", foreground="#15803d")
        self.sf_tree.tag_configure("warn",background="#fffbeb", foreground="#92400e")
        self.sf_tree.tag_configure("bad", background="#fef2f2", foreground="#b91c1c")
        self.sf_tree.insert("","end",values=("","","","","Run monitoring to load stream status","",""))
        self._bind_col_resize(self.sf_tree,
                              {"stream_id":0.16,"camera":0.09,"ip":0.13,"status":0.11,
                               "last_modified":0.20,"recent":0.10,"total":0.08})

    # ------------------------------------------------------------------
    def _build_syslogs_tab(self, parent):
        nb = ttk.Notebook(parent)
        nb.pack(fill="both", expand=True, padx=self._s(6), pady=self._s(6))
        self._syslogs_nb = nb

        def _make_tree(frame, cols, headings, widths, anchors):
            wrap = tk.Frame(frame, bg=COL_CARD)
            wrap.pack(fill="both", expand=True, padx=self._s(8), pady=self._s(8))
            sb = ttk.Scrollbar(wrap, orient="vertical")
            sb.pack(side="right", fill="y")
            tv = ttk.Treeview(wrap, columns=cols, show="headings", yscrollcommand=sb.set)
            sb.config(command=tv.yview)
            tv.pack(fill="both", expand=True)
            for c in cols:
                tv.heading(c, text=headings[c], anchor=anchors[c])
                tv.column(c, width=widths[c], anchor=anchors[c], stretch=True, minwidth=40)
            tv.tag_configure("ok",   background="#f0fdf4", foreground="#15803d")
            tv.tag_configure("warn", background="#fffbeb", foreground="#92400e")
            tv.tag_configure("bad",  background="#fef2f2", foreground="#b91c1c")
            return tv

        # --- Internet connectivity ---
        inet_frame = tk.Frame(nb, bg=COL_CARD)
        nb.add(inet_frame, text="Internet")
        self.syslog_inet_tree = _make_tree(
            inet_frame,
            cols=("time", "status"),
            headings={"time": "Timestamp", "status": "Status"},
            widths={"time": self._s(200), "status": self._s(120)},
            anchors={"time": "w", "status": "w"},
        )
        self.syslog_inet_tree.insert("", "end", values=("", "Run monitoring to load internet logs"))

        # --- Camera status ---
        cam_frame = tk.Frame(nb, bg=COL_CARD)
        nb.add(cam_frame, text="Camera Events")
        self.syslog_cam_tree = _make_tree(
            cam_frame,
            cols=("time", "ip", "status"),
            headings={"time": "Timestamp", "ip": "Camera IP", "status": "Status"},
            widths={"time": self._s(200), "ip": self._s(160), "status": self._s(100)},
            anchors={"time": "w", "ip": "w", "status": "w"},
        )
        self.syslog_cam_tree.insert("", "end", values=("", "", "Run monitoring to load camera logs"))

        # --- Speed test ---
        speed_frame = tk.Frame(nb, bg=COL_CARD)
        nb.add(speed_frame, text="Speed Tests")
        self.syslog_speed_tree = _make_tree(
            speed_frame,
            cols=("time", "upload_mbps"),
            headings={"time": "Timestamp", "upload_mbps": "Upload (Mbps)"},
            widths={"time": self._s(200), "upload_mbps": self._s(140)},
            anchors={"time": "w", "upload_mbps": "center"},
        )
        self.syslog_speed_tree.insert("", "end", values=("", "Run monitoring to load speed logs"))

        # --- Power events ---
        power_frame = tk.Frame(nb, bg=COL_CARD)
        nb.add(power_frame, text="Power Events")
        self.syslog_power_tree = _make_tree(
            power_frame,
            cols=("time", "event", "detail"),
            headings={"time": "Timestamp", "event": "Event", "detail": "Detail"},
            widths={"time": self._s(200), "event": self._s(100), "detail": self._s(500)},
            anchors={"time": "w", "event": "w", "detail": "w"},
        )
        self.syslog_power_tree.insert("", "end", values=("", "", "Run monitoring to load power events"))

    # ------------------------------------------------------------------
    def _build_footer(self, parent):
        bar = tk.Frame(parent, bg=COL_BG)
        bar.pack(fill="x", pady=(self._s(12), 0))
        tk.Label(bar, text="Note:", bg=COL_BG, fg=COL_MUTED,
                 font=("Segoe UI", self._f(10), "bold")).pack(side="left")
        self.note_entry = tk.Entry(bar, font=("Segoe UI", self._f(10)), bg=COL_CARD,
                                   fg=COL_TEXT, relief="solid", bd=1, highlightthickness=1,
                                   highlightbackground=COL_BORDER, insertbackground=COL_TEXT)
        self.note_entry.pack(side="left", fill="x", expand=True,
                             padx=self._s(10), ipady=self._s(3))
        tk.Button(bar, text="Save Note", font=("Segoe UI", self._f(10)), bg="#e5e7eb",
                  fg=COL_TEXT, relief="flat", bd=0, padx=self._s(14), pady=self._s(5),
                  cursor="hand2", command=self.save_comment).pack(side="left")
        self.status_label = tk.Label(bar, text="● Ready", bg=COL_BG, fg=COL_GREEN,
                                     font=("Segoe UI", self._f(10), "bold"))
        self.status_label.pack(side="right")

    # ------------------------------------------------------------------
    def _tick(self):
        self.header_clock.config(
            text=datetime.datetime.now().strftime("%a %d %b %Y · %H:%M:%S"))
        self.root.after(1000, self._tick)

    def _set_busy(self, busy, message=""):
        if busy:
            self.run_button.config(state="disabled")
            self.progress.pack(side="left", padx=(12, 0))
            self.progress.start(12)
            self.controls_status.config(text=message)
            self.status_label.config(text="● Working…", fg=COL_AMBER)
        else:
            self.progress.stop()
            self.progress.pack_forget()
            self.run_button.config(state="normal")
            self.controls_status.config(text=message)

    def _set_tile(self, key, value, state, sub=""):
        t = self.tiles[key]
        t["val"].config(text=value, fg=STATE_COLORS.get(state, COL_TEXT))
        t["sub"].config(text=sub)

    # ------------------------------------------------------------------
    def run_monitoring(self):
        store_id = self.store_entry.get().strip()
        if not store_id:
            messagebox.showwarning("Store ID required",
                                   "Please enter a Store ID (e.g. 193-147).")
            return
        self.current_store_id = store_id
        self._set_busy(True, f"Fetching cameras for {store_id} …")
        threading.Thread(target=self._worker, args=(store_id,), daemon=True).start()

    def _worker(self, store_id):
        cameras, error = fetch_store_cameras(store_id)
        if error:
            self.root.after(0, self._show_error, error)
            return
        self.root.after(0, lambda: self.controls_status.config(
            text=f"{len(cameras)} cameras · running checks …"))
        try:
            results = run_complete_monitoring(store_id, cameras)
        except Exception as e:
            self.root.after(0, self._show_error, f"Monitoring failed: {e}")
            return
        self.root.after(0, self._display_results, results)

    # ------------------------------------------------------------------
    def _display_results(self, results):
        self._set_busy(False, "Completed")
        self.current_results = results

        cams = results.get("cameras", [])
        sysd = results.get("system", {})
        inet = results.get("internet", {})
        net  = results.get("network", {})
        summ = results.get("summary", {})

        # Info strip
        client = next((c.get("client_id") for c in cams if c.get("client_id")), None)
        ts     = results.get("timestamp", "")
        try:
            ts_disp = datetime.datetime.fromisoformat(ts).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            ts_disp = ts
        self.info["store"].config(text=results.get("store_id", "—"))
        self.info["brand"].config(text=f"Client {client}" if client else "—")
        self.info["cameras"].config(text=str(len(cams)))
        self.info["host"].config(text=net.get("hostname", "—"))
        self.info["network"].config(text=net.get("network_name", "—"))
        self.info["checked"].config(text=ts_disp or "—")

        # Tiles
        if inet.get("connected"):
            self._set_tile("internet", "Online", "ok",
                           f"{inet.get('avg_latency_ms', '?')} ms · "
                           f"{inet.get('packet_loss', 0)}% loss")
        else:
            self._set_tile("internet", "Down", "bad",
                           f"{inet.get('packet_loss', 0)}% packet loss")

        if "error" in sysd:
            for k in ("cpu", "ram", "disk"):
                self._set_tile(k, "N/A", "muted", sysd["error"])
        else:
            cpu  = sysd.get("cpu_percent", 0)
            ram  = sysd.get("ram_percent", 0)
            disk = sysd.get("disk_percent", 0)
            self._set_tile("cpu",  f"{cpu}%",
                           "bad" if cpu  > 80 else ("warn" if cpu  > 60 else "ok"))
            self._set_tile("ram",  f"{ram}%",
                           "bad" if ram  > 85 else ("warn" if ram  > 70 else "ok"))
            self._set_tile("disk", f"{disk}%",
                           "bad" if disk > 90 else ("warn" if disk > 75 else "ok"),
                           f"uptime {sysd.get('uptime_hours', 0)} h")

        total   = summ.get("total_cameras", len(cams)) or 0
        ping_ok = summ.get("cameras_passing", 0)
        rtsp_ok = summ.get("rtsp_working", 0)
        self._set_tile("cameras_ok", f"{ping_ok}/{total}",
                       "ok" if total and ping_ok == total else ("warn" if ping_ok else "bad"))
        self._set_tile("rtsp_ok", f"{rtsp_ok}/{total}",
                       "ok" if total and rtsp_ok == total else ("warn" if rtsp_ok else "bad"))

        # Tab 0: Camera Status (ping + reason)
        self.tree.delete(*self.tree.get_children())
        status_text = {"ok": "● Online", "warn": "◑ Partial", "bad": "○ Offline"}
        for cam in cams:
            state, _ = _camera_health(cam)
            lat    = f"{cam['ping_latency_ms']} ms" if cam.get("ping_latency_ms") else "—"
            reason = cam.get("ping_reason", "") if cam.get("ping") != "OK" else ""
            self.tree.insert("", "end", tags=(state,), values=(
                status_text.get(state, "○ Unknown"),
                cam.get("camera_number", "—"),
                cam.get("ip", "—"),
                cam.get("manufacturer", "—"),
                cam.get("ping", "—"),
                lat,
                reason,
            ))
        self.notebook.tab(0, text=f"Camera Status ({len(cams)})")

        # Tab 1: RTSP Preview
        self._rebuild_rtsp_tabs(cams)

        # Tab 2: Wi-Fi History
        wifi = results.get("wifi_changes", [])
        self.wifi_tree.delete(*self.wifi_tree.get_children())
        if wifi:
            for w in wifi:
                t   = (w.get("time", "") or "")[:19].replace("T", " ")
                tag = "ok" if w.get("event") == "Connected" else "warn"
                self.wifi_tree.insert("", "end", tags=(tag,),
                                      values=(t, w.get("event", ""), w.get("ssid", "")))
        else:
            self.wifi_tree.insert("", "end", values=(
                "", "", "No Wi-Fi changes in the last 24h (or no Wi-Fi adapter on this PC)"))
        self.notebook.tab(3, text=f"Wi-Fi History 24h ({len(wifi)})")

        # Tab 3: Sleep / Power
        sleep_logs = results.get("sleep_logs", [])
        self.sleep_tree.delete(*self.sleep_tree.get_children())
        if sleep_logs:
            for s in sleep_logs:
                t   = (s.get("time", "") or "")[:19].replace("T", " ")
                ev  = s.get("event", "")
                tag = ("bad"  if ev == "Power loss"
                       else "warn" if ev in ("Sleep", "Shutdown")
                       else "ok")
                self.sleep_tree.insert("", "end", tags=(tag,),
                                       values=(t, ev, s.get("detail", "")))
        else:
            self.sleep_tree.insert("", "end",
                                   values=("", "", "No sleep / power events in the last 24h"))
        self.notebook.tab(4, text=f"Sleep / Power Logs ({len(sleep_logs)})")

        # Tab 4: Antivirus
        av_list = results.get("antivirus_list", [])
        self.av_tree.delete(*self.av_tree.get_children())
        if av_list:
            for a in av_list:
                enabled = a.get("enabled")
                up      = a.get("up_to_date")
                if enabled is None:
                    status, defs, tag = "Unknown", "Unknown", "warn"
                else:
                    status = "🟢 Enabled" if enabled else "🔴 Disabled"
                    defs   = "Up to date" if up else "Out of date"
                    tag    = "ok" if (enabled and up) else ("warn" if enabled else "bad")
                updated = (a.get("updated", "") or "")[:25]
                self.av_tree.insert("", "end", tags=(tag,),
                                    values=(a.get("name", "—"), status, defs, updated or "—"))
        else:
            fallback = results.get("antivirus", {}).get("antivirus_name", "")
            self.av_tree.insert("", "end",
                                values=(fallback or "No antivirus product detected", "", "", ""))
        self.notebook.tab(5, text=f"Antivirus ({len(av_list)})")

        # Tab 6: App Status
        app_st   = results.get("app_status", {})
        process  = app_st.get("process", {})
        crashes  = app_st.get("crashes", [])
        streams  = app_st.get("stream_folders", [])
        app_summ = app_st.get("summary", {})

        proc_run  = process.get("running", False)
        crash_cnt = app_summ.get("crash_count_2d", 0)
        streams_t = app_summ.get("streams_total", 0)
        streams_a = app_summ.get("streams_active", 0)

        self.app_proc_labels["app"].config(text=app_st.get("app_exe", "TangoEyeStreamer.exe"))
        self.app_proc_labels["proc_status"].config(
            text="● Running" if proc_run else "○ Not Running",
            fg=COL_GREEN if proc_run else COL_RED)
        self.app_proc_labels["pid"].config(text=str(process.get("pid") or "—"))
        uptime = process.get("uptime_hours")
        self.app_proc_labels["uptime"].config(text=f"{uptime} h" if uptime is not None else "—")
        self.app_proc_labels["crashes"].config(
            text=str(crash_cnt), fg=COL_RED if crash_cnt > 0 else COL_GREEN)

        self.crash_tree.delete(*self.crash_tree.get_children())
        if crashes:
            for c in crashes:
                t   = (c.get("time","") or "")[:19].replace("T"," ")
                lvl = c.get("level","")
                tag = "bad" if "error" in lvl.lower() else "warn"
                self.crash_tree.insert("","end",tags=(tag,),
                                       values=(t, c.get("id",""), lvl, c.get("msg","")[:120]))
        else:
            self.crash_tree.insert("","end",values=("","","","No crash events in the last 2 days"))

        self.sf_tree.delete(*self.sf_tree.get_children())
        if streams:
            for s in streams:
                lm  = (s.get("last_modified","") or "")[:19].replace("T"," ")
                tag = self._appstatus_stream_tag.get(s.get("status",""), "warn")
                st  = self._appstatus_stream_text.get(s.get("status",""), s.get("status",""))
                self.sf_tree.insert("","end",tags=(tag,), values=(
                    s.get("stream_id","—"), s.get("camera_number","—"), s.get("ip","—"),
                    st, lm or "—", s.get("recent_images_1h",0), s.get("total_images",0)))
        else:
            self.sf_tree.insert("","end",values=("","","","No stream folders found","","",""))
        self.notebook.tab(6, text=f"App Status ({streams_t} streams)")

        # Tab 7: System Logs
        sl = results.get("system_logs", {})
        sl_inet   = sl.get("internet",  [])
        sl_cams   = sl.get("cameras",   [])
        sl_speed  = sl.get("speedtest", [])
        sl_power  = sl.get("power",     [])

        self.syslog_inet_tree.delete(*self.syslog_inet_tree.get_children())
        if sl_inet:
            for e in sl_inet:
                t   = (e.get("time","") or "")[:19].replace("T"," ")
                st  = e.get("status","")
                tag = "ok" if st == "ONLINE" else "bad"
                self.syslog_inet_tree.insert("","end", tags=(tag,), values=(t, "● Online" if st=="ONLINE" else "○ Offline"))
        else:
            self.syslog_inet_tree.insert("","end", values=("","No internet log files found for the last 2 days"))

        self.syslog_cam_tree.delete(*self.syslog_cam_tree.get_children())
        if sl_cams:
            for e in sl_cams:
                t   = (e.get("time","") or "")[:19].replace("T"," ")
                st  = e.get("status","")
                tag = "ok" if st == "UP" else "bad"
                self.syslog_cam_tree.insert("","end", tags=(tag,), values=(t, e.get("ip","—"), "● UP" if st=="UP" else "○ DOWN"))
        else:
            self.syslog_cam_tree.insert("","end", values=("","","No camera log files found for the last 2 days"))

        self.syslog_speed_tree.delete(*self.syslog_speed_tree.get_children())
        if sl_speed:
            for e in sl_speed:
                t     = (e.get("time","") or "")[:19].replace("T"," ")
                mbps  = e.get("upload_mbps", 0)
                tag   = "ok" if mbps >= 1.0 else ("warn" if mbps >= 0.2 else "bad")
                self.syslog_speed_tree.insert("","end", tags=(tag,), values=(t, f"{mbps}"))
        else:
            self.syslog_speed_tree.insert("","end", values=("","No speed test log files found for the last 2 days"))

        self.syslog_power_tree.delete(*self.syslog_power_tree.get_children())
        if sl_power:
            for e in sl_power:
                t   = (e.get("time","") or "")[:19].replace("T"," ")
                ev  = e.get("event","")
                tag = "warn" if ev.lower() == "sleep" else "ok"
                self.syslog_power_tree.insert("","end", tags=(tag,), values=(t, ev, e.get("detail","")))
        else:
            self.syslog_power_tree.insert("","end", values=("","","No power event log files found for the last 2 days"))

        total_sl = len(sl_inet) + len(sl_cams) + len(sl_speed) + len(sl_power)
        self.notebook.tab(7, text=f"System Logs ({total_sl})")

        # Footer
        if total and inet.get("connected") and ping_ok == total and rtsp_ok == total and proc_run:
            self.status_label.config(text="● All systems healthy",   fg=COL_GREEN)
        elif not proc_run:
            self.status_label.config(text="● App process not running", fg=COL_RED)
        elif total and ping_ok == 0:
            self.status_label.config(text="● Cameras unreachable",   fg=COL_RED)
        else:
            self.status_label.config(text="● Issues detected",       fg=COL_AMBER)

    # ------------------------------------------------------------------
    def save_comment(self):
        comment = self.note_entry.get().strip()
        if not comment:
            messagebox.showwarning("Empty note", "Please type a note before saving.")
            return
        entry = {
            "timestamp": datetime.datetime.now().isoformat(),
            "store_id":  self.current_store_id or "Unknown",
            "comment":   comment,
        }
        try:
            with open("ops_infra_comments.json", "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
            self.note_entry.delete(0, tk.END)
            self.controls_status.config(text="Note saved ✓")
            self.root.after(3000, lambda: self.controls_status.config(text=""))
        except Exception as e:
            self.controls_status.config(text=f"Save failed: {e}")

    def _show_error(self, error):
        self._set_busy(False, "Error")
        self.status_label.config(text="● Error", fg=COL_RED)
        messagebox.showerror("Error", str(error))


if __name__ == "__main__":
    # Must be called BEFORE tk.Tk() so the OS reports true pixel counts
    if platform.system() == "Windows":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(2)   # per-monitor v2
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
    root = tk.Tk()
    app  = OPSInfraApp(root)
    root.mainloop()
