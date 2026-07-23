"""GPS simulation via gps-sdr-sim + hackrf_transfer.

Pipeline: for each waypoint, gps-sdr-sim generates a short 8-bit I/Q file for
that lat/lon/alt lasting `dwell` seconds; then a loop transmits the waypoints in
order, each for its dwell, cycling forever — so a receiver in a shielded box sees
the fake position "jump" between the points on a timed loop.

Generation is far faster than real time (~0.4s for 5s of signal), so we generate
all bins up front, then stream them.

SAFETY: transmitting GPS must happen inside an RF shielded enclosure. This module
only builds/runs commands; the operator is responsible for containment.
"""
from __future__ import annotations
import os
import subprocess
import time
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

from . import hackrf

GPS_L1_HZ = 1575420000
GPS_SAMP = 2600000  # gps-sdr-sim default sample rate

_TOOLS = Path(os.path.expanduser("~/HackRF_One_Toolkit/tools/gps-sdr-sim"))
GPS_BIN = _TOOLS / "gps-sdr-sim"
BUNDLED_EPH = _TOOLS / "brdc0010.22n"   # sample ephemeris shipped with the repo


def have_bin() -> bool:
    return GPS_BIN.exists() and os.access(GPS_BIN, os.X_OK)


def active_ephemeris(gps_dir: Path) -> Optional[Path]:
    """Prefer a user-uploaded ephemeris, else the bundled sample."""
    up = gps_dir / "ephemeris.nav"
    if up.exists():
        return up
    if BUNDLED_EPH.exists():
        return BUNDLED_EPH
    return None


def fetch_ephemeris(gps_dir: Path) -> Tuple[bool, str]:
    """Best-effort download of today's GPS broadcast ephemeris from public
    mirrors. Fragile (server naming / auth / date vary) — on failure the UI
    falls back to manual upload with the links documented in Help."""
    import urllib.request
    import gzip
    import time as _t
    gps_dir.mkdir(parents=True, exist_ok=True)
    t = _t.gmtime()
    yy, doy, yyyy = t.tm_year % 100, t.tm_yday, t.tm_year
    # gps-sdr-sim reads RINEX 3 mixed-nav (MN); IGS mirrors publish it under the
    # long RINEX3 name. Try today then yesterday (today's may lag a few hours),
    # plus legacy RINEX2 GPS-only names as fallback.
    def bkg(y, d):
        base = f"https://igs.bkg.bund.de/root_ftp/IGS/BRDC/{y}/{d:03d}/"
        return [base + f"BRDC00WRD_R_{y}{d:03d}0000_01D_MN.rnx.gz",
                base + f"BRDC00WRD_S_{y}{d:03d}0000_01D_MN.rnx.gz"]
    pdoy, pyy, pyyyy = (doy - 1, yy, yyyy) if doy > 1 else (365, (yy - 1) % 100, yyyy - 1)
    cands = bkg(yyyy, doy) + bkg(pyyyy, pdoy) + [
        f"https://cddis.nasa.gov/archive/gnss/data/daily/{yyyy}/{doy:03d}/{yy:02d}n/brdc{doy:03d}0.{yy:02d}n.gz",
    ]
    errs = []
    for url in cands:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "hackrf-toolkit"})
            data = urllib.request.urlopen(req, timeout=15).read()
            if url.endswith(".gz"):
                data = gzip.decompress(data)
            if b"RINEX" not in data[:120] and b"NAVIGATION" not in data[:120].upper():
                raise ValueError("下载内容不是 RINEX 导航文件")
            (gps_dir / "ephemeris.nav").write_bytes(data)
            fname = url.rsplit("/", 1)[-1]
            return True, f"已获取星历 {fname}({len(data) // 1024} KB)"
        except Exception as e:  # noqa: BLE001
            errs.append(f"{url.split('/')[2]}: {type(e).__name__}")
    return False, "自动获取失败(" + "; ".join(errs) + ")。请在帮助页按链接手动下载后上传。"


def status(gps_dir: Path) -> Dict[str, Any]:
    eph = active_ephemeris(gps_dir)
    eph_info = None
    if eph:
        uploaded = eph.name == "ephemeris.nav"
        eph_info = {
            "name": eph.name,
            "source": "user" if uploaded else "bundled-sample",
            "bundled_date": None if uploaded else "2022-001 (样本, 建议自动获取或上传当天星历)",
            "mtime": int(eph.stat().st_mtime),
        }
    return {
        "available": have_bin(),
        "bin": str(GPS_BIN),
        "ephemeris": eph_info,
        "l1_hz": GPS_L1_HZ,
        "samp_rate": GPS_SAMP,
    }


def generate_bin(eph: Path, lat: float, lon: float, alt: float, dwell: int,
                 samp: int, out_path: Path) -> Tuple[bool, str]:
    """Blocking: generate one 8-bit I/Q file for a static location."""
    argv = [str(GPS_BIN), "-e", str(eph),
            "-l", f"{lat},{lon},{alt}", "-d", str(int(dwell)),
            "-s", str(int(samp)), "-b", "8", "-o", str(out_path)]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    except Exception as e:  # noqa: BLE001
        return False, str(e)
    if r.returncode != 0 or not out_path.exists():
        return False, (r.stderr or r.stdout or "gps-sdr-sim failed").strip()[-300:]
    return True, ""


def _generate_killable(job, eph: Path, lat: float, lon: float, alt: float,
                       dwell: int, samp: int, out_path: Path,
                       extra: Optional[List[str]] = None) -> Tuple[bool, str]:
    """Generate one waypoint bin as a KILLABLE child (registered as job.child so
    stop() can terminate it mid-generation). stderr/stdout → DEVNULL to avoid any
    pipe-fill stall on long dwells."""
    argv = [str(GPS_BIN), "-e", str(eph), "-l", f"{lat},{lon},{alt}",
            "-d", str(int(dwell)), "-s", str(int(samp)), "-b", "8"]
    if extra:
        argv += list(extra)
    argv += ["-o", str(out_path)]
    proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    job.child = proc
    try:
        while proc.poll() is None:
            if job.stop_event.is_set():
                try:
                    proc.terminate()
                    proc.wait(timeout=3)
                except Exception:  # noqa: BLE001
                    try:
                        proc.kill()
                    except Exception:  # noqa: BLE001
                        pass
                break
            time.sleep(0.1)
    finally:
        job.child = None
    if job.stop_event.is_set():
        return False, "已停止"
    if proc.returncode != 0 or not out_path.exists() or out_path.stat().st_size == 0:
        return False, f"gps-sdr-sim 返回码 {proc.returncode}"
    return True, ""


def _run_child(job, argv: List[str]) -> None:
    """Spawn one hackrf_transfer, stream its power line, wait; abort on stop."""
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            bufsize=1, text=True)
    job.child = proc

    def pump():
        try:
            for line in iter(proc.stderr.readline, ""):
                if not line:
                    break
                pw = hackrf.parse_transfer_power(line)
                if pw is not None:
                    job.emit("power", dbfs=pw)
        except Exception:  # noqa: BLE001
            pass
    import threading
    t = threading.Thread(target=pump, daemon=True)
    t.start()
    while proc.poll() is None:
        if job.stop_event.is_set():
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
            break
        time.sleep(0.1)
    try:
        proc.wait(timeout=3)
    except Exception:  # noqa: BLE001
        pass
    t.join(timeout=1)
    job.child = None


def run_sequence(job, waypoints: List[Dict[str, Any]], eph: Path, gps_dir: Path,
                 samp: int, freq: int, txgain: int, amp: int,
                 serial: Optional[str] = None, bias_tee: int = 0,
                 extra_gen: Optional[List[str]] = None) -> None:
    """Job function: generate per-waypoint bins (killable), then loop-transmit."""
    gps_dir.mkdir(parents=True, exist_ok=True)
    bins: List[Tuple[Path, Dict[str, Any]]] = []
    for i, wp in enumerate(waypoints):
        if job.stop_event.is_set():
            return
        job.emit("info", msg=f"[生成 {i+1}/{len(waypoints)}] {wp['lat']:.5f}, {wp['lon']:.5f} "
                             f"海拔 {wp.get('alt', 0)}m · {wp['dwell']}s")
        out = gps_dir / f"wp{i}.bin"
        ok, err = _generate_killable(job, eph, float(wp["lat"]), float(wp["lon"]),
                                     float(wp.get("alt", 0)), int(wp["dwell"]), samp,
                                     out, extra_gen)
        if job.stop_event.is_set():
            job.emit("info", msg="已停止(生成阶段)")
            return
        if not ok:
            raise RuntimeError(f"生成第 {i+1} 个坐标失败: {err}")
        bins.append((out, wp))

    job.emit("info", msg=f"生成完成,开始循环发射(L1 {freq/1e6:.3f} MHz, TX 增益 {txgain})…")
    loop = 0
    while not job.stop_event.is_set():
        loop += 1
        for i, (b, wp) in enumerate(bins):
            if job.stop_event.is_set():
                break
            job.emit("gps_wp", index=i, lat=wp["lat"], lon=wp["lon"],
                     loop=loop, dwell=wp["dwell"])
            job.emit("info", msg=f"[第 {loop} 轮] 发射坐标 {i+1}: "
                                 f"{wp['lat']:.5f}, {wp['lon']:.5f} · {wp['dwell']}s")
            argv = ["hackrf_transfer", "-t", str(b), "-f", str(int(freq)),
                    "-s", str(int(samp)), "-x", str(int(txgain)),
                    "-a", "1" if amp else "0"]
            if bias_tee:
                argv += ["-p", "1"]
            if serial:
                argv += ["-d", str(serial)]
            _run_child(job, argv)
    job.emit("info", msg="GPS 发射已停止")
