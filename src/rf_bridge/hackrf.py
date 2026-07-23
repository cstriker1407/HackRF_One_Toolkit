"""HackRF CLI adapter — build argv for hackrf_* tools + parse their output.

Knows nothing about Flask or jobs. It only:
  * builds command lines for hackrf_info / hackrf_sweep / hackrf_transfer
  * parses hackrf_info (multi-board), hackrf_sweep CSV rows, and the periodic
    "average power ... dBfs" line hackrf_transfer prints on stderr
  * validates a user-edited hackrf_transfer argv against an allowlist
"""
from __future__ import annotations
import re
import shutil
import subprocess
from typing import Optional, Dict, Any, List

TOOLS = ("hackrf_info", "hackrf_transfer", "hackrf_sweep", "rtl_433")


def have(tool: str) -> bool:
    return shutil.which(tool) is not None


def tool_versions() -> Dict[str, Dict[str, Any]]:
    """{tool: {present, version}} for the toolchain panel."""
    out: Dict[str, Dict[str, Any]] = {}
    # hackrf_* share libhackrf; read version from hackrf_info
    hv = None
    if have("hackrf_info"):
        try:
            r = subprocess.run(["hackrf_info"], capture_output=True, text=True, timeout=8)
            m = re.search(r"libhackrf version:\s*(.+)", r.stdout + r.stderr)
            hv = m.group(1).strip() if m else "已安装"
        except Exception:  # noqa: BLE001
            hv = "已安装"
    for t in ("hackrf_info", "hackrf_transfer", "hackrf_sweep"):
        out[t] = {"present": have(t), "version": hv if have(t) else None}
    # rtl_433
    rv = None
    if have("rtl_433"):
        try:
            r = subprocess.run(["rtl_433", "-V"], capture_output=True, text=True, timeout=8)
            m = re.search(r"rtl_433 version\s*(\S+)", r.stdout + r.stderr)
            rv = m.group(1) if m else "已安装"
        except Exception:  # noqa: BLE001
            rv = "已安装"
    out["rtl_433"] = {"present": have("rtl_433"), "version": rv}
    return out


# ---------------------------------------------------------------------------
# hackrf_info — may list MORE THAN ONE board
# ---------------------------------------------------------------------------

def device_info() -> Dict[str, Any]:
    """Parse hackrf_info into {present, count, devices:[...], raw}.
    Each device: {index, serial, board_id, firmware, part_id, hw_rev, is_clone}."""
    if not have("hackrf_info"):
        return {"present": False, "count": 0, "devices": [],
                "error": "hackrf_info not found in PATH"}
    try:
        r = subprocess.run(["hackrf_info"], capture_output=True, text=True, timeout=12)
        out = r.stdout + r.stderr
    except Exception as e:  # noqa: BLE001
        return {"present": False, "count": 0, "devices": [], "error": str(e)}

    if "Found HackRF" not in out and "Serial number:" not in out:
        return {"present": False, "count": 0, "devices": [], "raw": out.strip()}

    devices = []
    # Split into per-board chunks. hackrf_info prints "Index: N" before each board.
    chunks = re.split(r"(?=Index:\s*\d+)", out)
    for ch in chunks:
        if "Serial number:" not in ch:
            continue

        def grab(pat: str, c=ch) -> Optional[str]:
            m = re.search(pat, c)
            return m.group(1).strip() if m else None

        devices.append({
            "index": int(grab(r"Index:\s*(\d+)") or len(devices)),
            "serial": grab(r"Serial number:\s*(\S+)"),
            "board_id": grab(r"Board ID Number:\s*(.+)"),
            "firmware": grab(r"Firmware Version:\s*(.+)"),
            "part_id": grab(r"Part ID Number:\s*(.+)"),
            "hw_rev": grab(r"Hardware Revision:\s*(.+)"),
            "is_clone": "does not appear to have been manufactured" in ch,
        })

    return {"present": len(devices) > 0, "count": len(devices),
            "devices": devices, "raw": out.strip()}


# ---------------------------------------------------------------------------
# Output parsers
# ---------------------------------------------------------------------------

_POWER_RE = re.compile(r"average power\s*(-?\d+\.?\d*)\s*dBfs")


def parse_transfer_power(line: str) -> Optional[float]:
    m = _POWER_RE.search(line)
    return float(m.group(1)) if m else None


def parse_sweep_row(line: str) -> Optional[Dict[str, Any]]:
    """date, time, hz_low, hz_high, hz_bin_width, num_samples, dB, dB, ..."""
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 7:
        return None
    try:
        hz_low = int(parts[2]); hz_high = int(parts[3])
        bin_width = float(parts[4]); n = int(parts[5])
        bins = [float(x) for x in parts[6:] if x != ""]
    except ValueError:
        return None
    return {"hz_low": hz_low, "hz_high": hz_high, "bin_width": bin_width,
            "n": n, "bins": bins}


# ---------------------------------------------------------------------------
# argv builders  (UI param names map 1:1 to CLI flags)
# ---------------------------------------------------------------------------

def _dev(argv: List[str], serial: Optional[str]) -> List[str]:
    if serial:
        argv += ["-d", str(serial)]
    return argv


def sweep_argv(f_low_mhz: int, f_high_mhz: int, bin_width_hz: int = 100000,
               lna: int = 40, vga: int = 40, amp: int = 0,
               one_shot: bool = True, serial: Optional[str] = None) -> List[str]:
    argv = ["hackrf_sweep", "-f", f"{int(f_low_mhz)}:{int(f_high_mhz)}",
            "-w", str(int(bin_width_hz)), "-l", str(int(lna)), "-g", str(int(vga))]
    if amp:
        argv += ["-a", "1"]
    if one_shot:
        argv += ["-1"]
    return _dev(argv, serial)


def capture_argv(path: str, freq_hz: int, samp_rate: int,
                 bandwidth: Optional[int] = None, lna: int = 32, vga: int = 16,
                 amp: int = 0, n_samples: Optional[int] = None,
                 bias_tee: int = 0, serial: Optional[str] = None) -> List[str]:
    argv = ["hackrf_transfer", "-r", path, "-f", str(int(freq_hz)),
            "-s", str(int(samp_rate)), "-l", str(int(lna)), "-g", str(int(vga)),
            "-a", "1" if amp else "0"]
    if bandwidth:
        argv += ["-b", str(int(bandwidth))]
    if bias_tee:
        argv += ["-p", "1"]
    if n_samples:
        argv += ["-n", str(int(n_samples))]
    return _dev(argv, serial)


def replay_argv(path: str, freq_hz: int, samp_rate: int,
                bandwidth: Optional[int] = None, txvga: int = 20, amp: int = 0,
                n_samples: Optional[int] = None, repeat: bool = False,
                bias_tee: int = 0, serial: Optional[str] = None) -> List[str]:
    argv = ["hackrf_transfer", "-t", path, "-f", str(int(freq_hz)),
            "-s", str(int(samp_rate)), "-x", str(int(txvga)),
            "-a", "1" if amp else "0"]
    if bandwidth:
        argv += ["-b", str(int(bandwidth))]
    if bias_tee:
        argv += ["-p", "1"]
    if n_samples:
        argv += ["-n", str(int(n_samples))]
    if repeat:
        argv += ["-R"]
    return _dev(argv, serial)


# ---------------------------------------------------------------------------
# Validate a user-edited hackrf_transfer argv (expert mode)
# ---------------------------------------------------------------------------

_TRANSFER_VAL_FLAGS = {"-r", "-t", "-f", "-s", "-b", "-l", "-g", "-x",
                       "-a", "-n", "-d", "-p", "-c", "-H"}
_TRANSFER_BOOL_FLAGS = {"-R"}


def validate_transfer_argv(argv: List[str]) -> Optional[str]:
    """Return None if argv is a safe hackrf_transfer command, else an error str.
    Only hackrf_transfer with known flags is allowed — no shell, no other binary."""
    if not isinstance(argv, list) or not argv:
        return "空命令"
    if argv[0] != "hackrf_transfer":
        return "命令必须以 hackrf_transfer 开头"
    has_rt = False
    i = 1
    while i < len(argv):
        t = argv[i]
        if t in _TRANSFER_BOOL_FLAGS:
            i += 1
        elif t in _TRANSFER_VAL_FLAGS:
            if i + 1 >= len(argv):
                return f"参数 {t} 缺少取值"
            if t in ("-r", "-t"):
                has_rt = True
            i += 2
        else:
            return f"不允许或未知的参数: {t}"
    if not has_rt:
        return "必须包含 -r(录制)或 -t(发射)"
    return None


def transfer_argv_role_path(argv: List[str]):
    """Return ('capture'|'replay', path) for a validated transfer argv."""
    for i, t in enumerate(argv):
        if t == "-r":
            return "capture", argv[i + 1]
        if t == "-t":
            return "replay", argv[i + 1]
    return None, None


def parse_transfer_params(argv: List[str]) -> Dict[str, Any]:
    """Pull known params out of a transfer argv for metadata."""
    flagmap = {"-f": "freq_hz", "-s": "samp_rate", "-b": "bandwidth",
               "-l": "lna", "-g": "vga", "-x": "txvga", "-a": "amp",
               "-n": "n_samples", "-p": "bias_tee", "-d": "serial"}
    out: Dict[str, Any] = {}
    i = 1
    while i < len(argv):
        t = argv[i]
        if t in flagmap and i + 1 < len(argv):
            key = flagmap[t]
            val = argv[i + 1]
            out[key] = val if key == "serial" else _maybe_int(val)
            i += 2
        else:
            i += 1
    return out


def _maybe_int(s: str):
    try:
        return int(s)
    except (ValueError, TypeError):
        return s
