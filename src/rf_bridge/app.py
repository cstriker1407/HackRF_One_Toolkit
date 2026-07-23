"""RF_Bridge — Flask service that owns the HackRF hardware.

The Web frontend (separate process) never touches hackrf_* directly; it calls
this service over HTTP and listens to per-job SSE streams for live power /
spectrum data. Kept intentionally thin: build argv (hackrf.py) → run as a
single job (jobs.py) → stream parsed events.
"""
from __future__ import annotations
import json
import os
import time
from pathlib import Path

from flask import (Flask, jsonify, request, Response, stream_with_context,
                   send_from_directory)

from . import hackrf
from . import gps as gpsmod
from .jobs import JobManager

VERSION = "0.1.0"


def create_app(captures_dir: str) -> Flask:
    app = Flask("rf_bridge")
    mgr = JobManager()
    CAP = Path(captures_dir)
    CAP.mkdir(parents=True, exist_ok=True)
    GPS_DIR = CAP.parent / "gps"

    # ---- helpers ----------------------------------------------------------
    def _power_parser(job, line):
        pw = hackrf.parse_transfer_power(line)
        if pw is not None:
            job.emit("power", dbfs=pw)

    def _write_capture_meta(raw_path: str, eff: dict, job):
        """eff = the EFFECTIVE params actually used (defaults already applied),
        so the metadata reflects what ran, not just what the request carried."""
        try:
            size = os.path.getsize(raw_path) if os.path.exists(raw_path) else 0
        except OSError:
            size = 0
        powers = [e["dbfs"] for e in job.events if e.get("kind") == "power"]
        meta = {
            "name": Path(raw_path).stem,
            "raw_path": raw_path,
            "params": {k: eff.get(k) for k in
                       ("freq_hz", "samp_rate", "bandwidth", "lna", "vga",
                        "amp", "n_samples")},
            "size_bytes": size,
            "state": job.state,
            "power_min_dbfs": min(powers) if powers else None,
            "power_max_dbfs": max(powers) if powers else None,
            "started_at": job.started_at,
            "ended_at": job.ended_at,
        }
        try:
            with open(raw_path + ".meta.json", "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def _busy_409(e):
        return jsonify(ok=False, error=str(e)), 409

    # ---- system -----------------------------------------------------------
    @app.get("/health")
    def health():
        return jsonify(ok=True, service="rf_bridge", version=VERSION,
                       busy=bool(mgr.current and mgr.current.active),
                       tools={t: hackrf.have(t) for t in hackrf.TOOLS})

    @app.get("/device")
    def device():
        return jsonify(hackrf.device_info())

    @app.get("/tools")
    def tools():
        v = hackrf.tool_versions()
        gs = gpsmod.have_bin()
        v["gps-sdr-sim"] = {"present": gs, "version": "git build" if gs else None}
        return jsonify(tools=v)

    # ---- captures library -------------------------------------------------
    @app.get("/captures")
    def captures():
        rows = []
        for raw in sorted(CAP.glob("*.raw"), reverse=True):
            meta_p = Path(str(raw) + ".meta.json")
            row = {"name": raw.stem, "raw_path": str(raw),
                   "size_bytes": raw.stat().st_size}
            if meta_p.exists():
                try:
                    row.update(json.loads(meta_p.read_text("utf-8")))
                except Exception:  # noqa: BLE001
                    pass
            rows.append(row)
        return jsonify(captures=rows)

    @app.delete("/captures/<name>")
    def capture_delete(name):
        raw = CAP / f"{Path(name).name}.raw"
        if not raw.exists():
            return jsonify(ok=False, error="not found"), 404
        raw.unlink()
        meta_p = Path(str(raw) + ".meta.json")
        if meta_p.exists():
            meta_p.unlink()
        return jsonify(ok=True)

    # ---- jobs -------------------------------------------------------------
    @app.post("/jobs/sweep")
    def sweep():
        p = request.get_json(force=True, silent=True) or {}
        argv = hackrf.sweep_argv(
            p.get("f_low_mhz", 433), p.get("f_high_mhz", 435),
            p.get("bin_width_hz", 100000), p.get("lna", 40),
            p.get("vga", 40), p.get("amp", 0), p.get("one_shot", True),
            serial=p.get("serial"))

        def parse(job, line):
            row = hackrf.parse_sweep_row(line)
            if row:
                job.emit("sweep", **row)

        try:
            job = mgr.start("sweep", argv, stdout_parser=parse)
        except RuntimeError as e:
            return _busy_409(e)
        job.meta = {"params": p}
        return jsonify(ok=True, job_id=job.id, argv=argv)

    @app.post("/jobs/rtl433")
    def rtl433():
        if not hackrf.have("rtl_433"):
            return jsonify(ok=False, error="rtl_433 未安装"), 501
        p = request.get_json(force=True, silent=True) or {}
        override = p.get("argv")
        if override:  # expert mode: user-edited command
            if not isinstance(override, list) or not override or override[0] != "rtl_433":
                return jsonify(ok=False, error="命令必须以 rtl_433 开头"), 400
            if any(any(c in str(t) for c in ";|&`$><\n") for t in override):
                return jsonify(ok=False, error="命令含非法字符"), 400
            argv = [str(t) for t in override]
            if "-F" not in argv:   # ensure JSON output so we can parse decodes
                argv += ["-F", "json"]
        else:
            freq = int(p.get("freq_hz", 433920000))
            serial = p.get("serial")
            dev = "driver=hackrf" + (f",serial={serial}" if serial else "")
            argv = ["rtl_433", "-d", dev, "-f", str(freq), "-F", "json"]
            if p.get("samp_rate"):
                argv += ["-s", str(int(p["samp_rate"]))]
            raw_extra = (p.get("extra") or "").strip()
            if raw_extra:
                toks = raw_extra.split()
                if any(any(c in t for c in ";|&`$><\n") for t in toks):
                    return jsonify(ok=False, error="额外参数含非法字符"), 400
                argv += toks

        def parse(job, line):
            line = line.strip()
            if not line.startswith("{"):
                return
            try:
                obj = json.loads(line)
            except Exception:  # noqa: BLE001
                return
            job.emit("decode", model=obj.get("model"), dev_id=obj.get("id"),
                     channel=obj.get("channel"), data=obj)

        try:
            job = mgr.start("rtl433", argv, stdout_parser=parse)
        except RuntimeError as e:
            return _busy_409(e)
        job.meta = {"argv": argv}
        return jsonify(ok=True, job_id=job.id, argv=argv)

    @app.post("/jobs/capture")
    def capture():
        p = request.get_json(force=True, silent=True) or {}
        for req in ("freq_hz", "samp_rate"):
            if req not in p:
                return jsonify(ok=False, error=f"missing {req}"), 400
        name = Path(str(p.get("name") or time.strftime("cap_%Y%m%d_%H%M%S"))).name
        path = str(CAP / f"{name}.raw")
        eff = {
            "freq_hz": int(p["freq_hz"]), "samp_rate": int(p["samp_rate"]),
            "bandwidth": p.get("bandwidth"), "lna": p.get("lna", 32),
            "vga": p.get("vga", 16), "amp": p.get("amp", 0),
            "n_samples": p.get("n_samples"), "bias_tee": p.get("bias_tee", 0),
            "serial": p.get("serial"),
        }
        argv = hackrf.capture_argv(
            path, eff["freq_hz"], eff["samp_rate"], eff["bandwidth"],
            eff["lna"], eff["vga"], eff["amp"], eff["n_samples"],
            bias_tee=eff["bias_tee"], serial=eff["serial"])

        try:
            job = mgr.start("capture", argv, stderr_parser=_power_parser,
                            on_exit=lambda j: _write_capture_meta(path, eff, j))
        except RuntimeError as e:
            return _busy_409(e)
        job.meta = {"path": path, "params": p}
        return jsonify(ok=True, job_id=job.id, path=path, argv=argv)

    @app.post("/jobs/replay")
    def replay():
        p = request.get_json(force=True, silent=True) or {}
        req_path = p.get("path")
        if not req_path:
            return jsonify(ok=False, error="missing path"), 400
        # confine to the captures dir (defense in depth — only transmit our own
        # recordings, never an arbitrary file path from the request)
        path = str(CAP / Path(req_path).name)
        if not os.path.isfile(path):
            return jsonify(ok=False, error="file not found"), 404
        for req in ("freq_hz", "samp_rate"):
            if req not in p:
                return jsonify(ok=False, error=f"missing {req}"), 400
        argv = hackrf.replay_argv(
            path, p["freq_hz"], p["samp_rate"], p.get("bandwidth"),
            p.get("txvga", 20), p.get("amp", 0), p.get("n_samples"),
            p.get("repeat", False), bias_tee=p.get("bias_tee", 0),
            serial=p.get("serial"))

        try:
            job = mgr.start("replay", argv, stderr_parser=_power_parser)
        except RuntimeError as e:
            return _busy_409(e)
        job.meta = {"path": path, "params": p}
        return jsonify(ok=True, job_id=job.id, argv=argv)

    @app.post("/jobs/transfer_raw")
    def transfer_raw():
        """Run a user-edited hackrf_transfer command (expert mode).
        argv is allowlist-validated; the -r/-t path is forced inside CAP."""
        p = request.get_json(force=True, silent=True) or {}
        argv = p.get("argv")
        err = hackrf.validate_transfer_argv(argv)
        if err:
            return jsonify(ok=False, error=err), 400
        role, path = hackrf.transfer_argv_role_path(argv)
        # Confine the file to the captures dir: take basename, re-root under CAP.
        safe = CAP / Path(path).name
        argv = [str(safe) if a == path else a for a in argv]
        if role == "replay" and not safe.exists():
            return jsonify(ok=False, error=f"文件不存在: {safe.name}"), 404
        eff = hackrf.parse_transfer_params(argv)

        if role == "capture":
            try:
                job = mgr.start("capture", argv, stderr_parser=_power_parser,
                                on_exit=lambda j: _write_capture_meta(str(safe), eff, j))
            except RuntimeError as e:
                return _busy_409(e)
            job.meta = {"path": str(safe), "argv": argv, "raw": True}
            return jsonify(ok=True, job_id=job.id, path=str(safe), argv=argv)
        else:
            try:
                job = mgr.start("replay", argv, stderr_parser=_power_parser)
            except RuntimeError as e:
                return _busy_409(e)
            job.meta = {"path": str(safe), "argv": argv, "raw": True}
            return jsonify(ok=True, job_id=job.id, argv=argv)

    @app.get("/captures/<name>/download")
    def capture_download(name):
        raw = CAP / f"{Path(name).name}.raw"
        if not raw.exists():
            return jsonify(ok=False, error="not found"), 404
        return send_from_directory(str(CAP), raw.name, as_attachment=True,
                                   mimetype="application/octet-stream")

    # ---- GPS simulation ---------------------------------------------------
    @app.get("/gps/status")
    def gps_status():
        return jsonify(gpsmod.status(GPS_DIR))

    @app.post("/gps/ephemeris")
    def gps_ephemeris():
        f = request.files.get("file")
        if not f:
            return jsonify(ok=False, error="no file"), 400
        GPS_DIR.mkdir(parents=True, exist_ok=True)
        f.save(str(GPS_DIR / "ephemeris.nav"))
        return jsonify(ok=True, ephemeris=gpsmod.status(GPS_DIR)["ephemeris"])

    @app.post("/gps/ephemeris/fetch")
    def gps_ephemeris_fetch():
        ok, msg = gpsmod.fetch_ephemeris(GPS_DIR)
        return (jsonify(ok=ok, message=msg,
                        ephemeris=gpsmod.status(GPS_DIR)["ephemeris"] if ok else None),
                200 if ok else 502)

    @app.post("/gps/simulate")
    def gps_simulate():
        if not gpsmod.have_bin():
            return jsonify(ok=False, error="gps-sdr-sim 未安装"), 501
        p = request.get_json(force=True, silent=True) or {}
        wps = p.get("waypoints") or []
        if not wps:
            return jsonify(ok=False, error="至少需要一个坐标"), 400
        eph = gpsmod.active_ephemeris(GPS_DIR)
        if not eph:
            return jsonify(ok=False, error="缺少星历文件(.nav),请上传"), 400
        samp = int(p.get("samp_rate", gpsmod.GPS_SAMP))
        freq = int(p.get("freq_hz", gpsmod.GPS_L1_HZ))
        txgain = int(p.get("txgain", 30))
        amp = int(p.get("amp", 0))
        bias_tee = int(p.get("bias_tee", 0))
        serial = p.get("serial")
        # optional extra gps-sdr-sim flags (expert mode); split + basic sanity
        extra_gen = None
        raw_extra = (p.get("extra_gen") or "").strip()
        if raw_extra:
            toks = raw_extra.split()
            if any(any(c in t for c in ";|&`$><\n") for t in toks):
                return jsonify(ok=False, error="额外参数含非法字符"), 400
            extra_gen = toks

        def _fn(job):
            gpsmod.run_sequence(job, wps, eph, GPS_DIR, samp, freq, txgain, amp,
                                serial, bias_tee, extra_gen)

        try:
            job = mgr.start_func("gps", _fn)
        except RuntimeError as e:
            return _busy_409(e)
        job.meta = {"waypoints": len(wps), "freq_hz": freq}
        return jsonify(ok=True, job_id=job.id)

    @app.post("/jobs/<job_id>/stop")
    def job_stop(job_id):
        return jsonify(ok=mgr.stop(job_id))

    @app.post("/jobs/stop")
    def job_stop_current():
        """Stop whatever job is currently running (if any). Used to clear a
        stale continuous sweep before starting a new one."""
        return jsonify(ok=mgr.stop())

    @app.get("/jobs/current")
    def job_current():
        """The currently-running job (id/kind/meta) or null — lets a page that
        was navigated away from and back reconnect to its running job."""
        j = mgr.current
        if j and j.active:
            return jsonify(current=j.snapshot())
        return jsonify(current=None)

    @app.get("/jobs/<job_id>")
    def job_status(job_id):
        job = mgr.jobs.get(job_id)
        if not job:
            return jsonify(error="no such job"), 404
        return jsonify(job.snapshot())

    @app.get("/jobs/<job_id>/stream")
    def job_stream(job_id):
        job = mgr.jobs.get(job_id)
        if not job:
            return jsonify(error="no such job"), 404

        def gen():
            # Track a monotonic sequence number, NOT a deque index — the deque
            # is bounded (maxlen) and drops old events, so an absolute index
            # would overrun it and stall the stream after ~8192 events.
            last_seq = 0
            last_beat = time.monotonic()
            while True:
                with job.cond:
                    while job.seq <= last_seq and job.active:
                        if not job.cond.wait(0.5):
                            break
                    new = job.seq - last_seq
                    if new > 0:
                        buf = list(job.events)
                        evs = buf[-new:] if new <= len(buf) else buf
                        last_seq = job.seq
                    else:
                        evs = []
                    terminal = not job.active
                for e in evs:
                    yield f"data: {json.dumps(e, ensure_ascii=False)}\n\n"
                if terminal and job.seq <= last_seq:
                    yield f"data: {json.dumps({'kind': 'end', 'state': job.state})}\n\n"
                    break
                if time.monotonic() - last_beat > 15:
                    yield ": keepalive\n\n"
                    last_beat = time.monotonic()

        return Response(stream_with_context(gen()),
                        headers={"Content-Type": "text/event-stream",
                                 "Cache-Control": "no-cache",
                                 "X-Accel-Buffering": "no"})

    return app
