"""hackrf_web — Flask console for the HackRF One Toolkit.

Design language mirrors the OBD_Hacker console (Bootstrap 5 + bootstrap-icons +
htmx, vendored; no CDN). This process holds NO hardware logic — it renders pages
and transparently proxies /api/* to RF_Bridge, including the per-job SSE stream.
"""
from __future__ import annotations
import os
from pathlib import Path

import requests
from flask import (Flask, render_template, request, jsonify, redirect,
                   url_for, Response, stream_with_context)

from .bridge_client import BridgeClient

HERE = Path(__file__).resolve().parent
APP_VERSION = "0.4.5"

# --- Presets ---------------------------------------------------------------
# Band presets for the spectrum scanner (quick recon of common bands).
BANDS = [
    {"id": "car433", "name": "汽车遥控 433 MHz", "f_low_mhz": 433, "f_high_mhz": 435,
     "center_hz": 433920000, "note": "无钥匙进入 / 车库门 / 多数遥控"},
    {"id": "car315", "name": "汽车遥控 315 MHz", "f_low_mhz": 314, "f_high_mhz": 316,
     "center_hz": 315000000, "note": "北美常见遥控频段"},
    {"id": "ism868", "name": "ISM 868 MHz", "f_low_mhz": 867, "f_high_mhz": 869,
     "center_hz": 868300000, "note": "欧洲 ISM / 传感器 / LoRa"},
    {"id": "wifi24", "name": "WiFi·BLE 2.4 GHz", "f_low_mhz": 2400, "f_high_mhz": 2483,
     "center_hz": 2440000000, "note": "仅看频谱占用度(不解调)"},
    {"id": "gps_l1", "name": "GPS L1 1575 MHz", "f_low_mhz": 1574, "f_high_mhz": 1577,
     "center_hz": 1575420000, "note": "GPS 民用 L1 C/A"},
    {"id": "full", "name": "全范围 1M–6GHz", "f_low_mhz": 1, "f_high_mhz": 6000,
     "center_hz": 433920000, "note": "扫描 HackRF 整个频率范围(较慢),先粗看哪里有信号"},
]

# Sample-rate presets for capture/replay (narrow → small files, wide → margin).
SAMP_PRESETS = [
    {"id": "narrow", "name": "窄带 2 Msps", "samp_rate": 2000000, "bandwidth": 1750000,
     "note": "钥匙/遥控等窄带信号,文件最小(推荐)"},
    {"id": "standard", "name": "标准 8 Msps", "samp_rate": 8000000, "bandwidth": 5000000,
     "note": "抗频偏余量大,文件较大"},
    {"id": "wide", "name": "宽带 20 Msps", "samp_rate": 20000000, "bandwidth": 15000000,
     "note": "宽信号;文件很大,注意 USB 带宽"},
]

AUTHOR_INFO = {
    "role": "汽车网络安全工程师 · ISO/SAE 21434 从业者",
    "website": "cstriker1407.blog.csdn.net",
    "website_url": "https://cstriker1407.blog.csdn.net",
    "note": "如有问题反馈、功能建议或合作意向, 欢迎通过上述网站联系.",
}

FEATURES = [
    ("双进程架构", "RF_Bridge (独占硬件, 跑 hackrf_* 子进程) + hackrf_web (Flask/htmx/Bootstrap 控制台), HTTP + SSE 通信, 前端可与硬件不在同一台机器"),
    ("频谱扫描 / 瀑布图", "hackrf_sweep 驱动, 预设常见频段 (315/433/868MHz, 2.4G, GPS L1), 实时频谱线 + 瀑布图 + 峰值频点标记"),
    ("IQ 抓包 / 重放", "hackrf_transfer 录制/发射, 窄带/标准/宽带采样率预设, 实时功率表, 抓包库管理"),
    ("重放防呆", "重放参数从抓包元数据自动回填, 采样率锁定一致, 避免收发不匹配导致解不开"),
    ("GPS 模拟 (开发中)", "gps-sdr-sim 合成 → 统一 TX 引擎发射, 仅限射频屏蔽箱内的授权抗欺骗测试"),
    ("半双工单任务", "收发互斥 + 单设备 → 同时仅一个任务, 第二个任务返回 HTTP 409"),
    ("离线运行", "本地单用户, 资源全部内置无外链, 部署机可无外网"),
]

CHANGELOG = [
    {"version": "0.4.5", "date": "2026-07-23", "highlights": [
        "重放弹窗发射时不可关闭(X/背景/ESC 均拦截),须先停止;离开页面后返回可重连到进行中的发射",
        "设备解码新增高级设置(采样率)+ 专家模式(rtl_433 额外参数 / 可手改命令)+ 命令预览",
        "频谱扫描与设备解码在同页内互斥:一个运行时另一个按钮变灰并提示(半双工)",
        "多窗口通知提速到 2 秒:一个窗口开始任务,其他窗口更快弹出整页锁定",
    ]},
    {"version": "0.4.4", "date": "2026-07-22", "highlights": [
        "rtl_433 设备解码并入频谱页(新增『设备解码』标签)—— 选频率即可解码周边 ISM 设备并实时列出",
        "全局锁通用化 — 任意任务(扫描/GPS/解码/录制)运行时,其他页面整页锁定遮罩,所有操作禁用;扫描现在也跨页保持并可重连,只能在其所属页面或右上角停止",
        "GPS 停止更可靠 — 点一次即轮询直到后端空闲,运行日志会打印『正在停止…』『已停止』,不再需要点好几次",
    ]},
    {"version": "0.4.3", "date": "2026-07-22", "highlights": [
        "🐛 修复 GPS『卡死』 — 生成阶段的 gps-sdr-sim 子进程改为可随时中止(登记为可停止子进程),停止/重启不再残留孤儿进程;start.sh/stop.sh 会清理 RF 子进程",
        "GPS 高级设置 + 专家模式 — 可配中心频率/采样率/天线偏置 + gps-sdr-sim 额外参数(如 -t 场景时间 / -i 关电离层);新增两步命令预览",
        "硬件页工具链显示版本号,并补上 gps-sdr-sim;新增 rtl_433 使用说明(点问号)",
    ]},
    {"version": "0.4.2", "date": "2026-07-22", "highlights": [
        "GPS 星历『自动获取当天星历』现已真正可用 — 从 IGS BKG 镜像下载当天 RINEX3 混合星历(gps-sdr-sim 可直接读),约几秒完成;失败才回退手动上传",
    ]},
    {"version": "0.4.1", "date": "2026-07-22", "highlights": [
        "同页才能停 — 一个任务运行时,其他页面无法启动或停止它(会 409 且不误杀),只能回其页面或用右上角全局停止;其他页出现醒目运行横幅",
        "实时功率曲线按真实命令时长绘制 X 轴(解析 -n/-s),短录制不再一闪而过",
        "GPS — 发射时锁定地图与坐标表(不可增改);当前发射坐标在右侧表格同步高亮;坐标序列本地保存,切换页面/返回不再丢失",
        "命令预览框加高;专家模式下明确提示『按命令原样执行』",
    ]},
    {"version": "0.4.0", "date": "2026-07-22", "highlights": [
        "🐛 修复跨页『卡死』 — 任务(GPS/录制)运行时切走再切回会重连到进行中的任务而非丢失;顶栏状态灯运行时可点击一键停止(全局急停)",
        "抓包/重放 — 实时功率改为『dBFS↔时间』滚动曲线图;录制/发射带倒计时;开始按钮兼作停止(去掉单独停止键);录制卡片收窄、抓包库加宽并可滚动",
        "频谱 — 峰值字体加大;新增高级设置(LNA/VGA/Amp);显示将执行的 hackrf_sweep 命令",
        "GPS — 地图/坐标序列并排,发射设置/运行日志并排(日志可滚动);新增高德底图(国内快,坐标自动 GCJ-02→WGS-84 纠偏);星历支持『尝试自动获取』+ 帮助页给出手动下载链接",
    ]},
    {"version": "0.3.0", "date": "2026-07-22", "highlights": [
        "🛰️ GPS 模拟落地 — 地图上点多个坐标、各设停留时长,gps-sdr-sim 逐点合成信号并循环发射,接收机位置按时间跳变(仅限屏蔽箱内授权测试)",
        "🐛 修复致命 bug — SSE 事件流用绝对索引读环形缓冲,~8192 事件后卡死(扫频几秒即停),改用单调序号,已验证连续 12 秒不断流",
        "频谱页重构 — 移除瀑布图,改为大号实时频谱 + 峰值保持(Peak Hold)包络 + 峰值列表(可直接跳去抓包);单次/连续两个按钮",
        "内联帮助升级 — 命令预览改为 hackrf_transfer 参数详解;帮助中心重写为 SDR/射频技术手册(10 节,含调制/IQ/dBFS/增益/重放/GPS 原理)",
        "抓包/重放 — 高级设置移到命令预览前;自动命名改为 cap_频率_日期时间;抓包库支持 .raw 下载",
        "多设备 -d 选择、全范围预设、端口 30000/30001(承接 0.2.0)",
    ]},
    {"version": "0.2.0", "date": "2026-07-22", "highlights": [
        "多设备支持 — 硬件页可列出多台 HackRF 并选择当前使用的一台(底层 -d 序列号),扫描/抓包/重放自动带上",
        "内联帮助系统 — 各参数旁 ? 问号点开即弹出详细说明(dBfs/增益/Amp/偏置/采样率/实时功率/瀑布图…),不再只有简略文档",
        "频谱页重构 — 打印机式扫描(青色竖线=当前扫描位置)、最多标注 3 个峰值频点、瀑布与实时频谱解耦(瀑布滚动速度可调)、扫描时锁定参数、3 秒无数据告警",
        "抓包/重放 — 新增标准/高级设置(全部参数可配 + 每项说明)、实时命令预览(hackrf_transfer …)、专家模式可手改命令(allowlist 校验 + 文件限制在抓包目录)",
        "抓包库新增 .raw 文件下载导出",
        "新增『全范围 1M–6GHz』扫描预设",
        "端口调整为 web :30000 / RF_Bridge :30001",
    ]},
    {"version": "0.1.0", "date": "2026-07-22", "highlights": [
        "🎉 首个版本 — 双进程架构 (RF_Bridge :30001 + hackrf_web :30000) 落地, 真机验证通过",
        "RF_Bridge — /device 解析 hackrf_info, /jobs/sweep|capture|replay 单任务模型 (半双工→409 busy), 每任务 SSE 实时流",
        "频谱扫描页 — 5 频段预设 + 实时瀑布图 (sweep 分块拼装成整行) + 频谱线 + 峰值频点标记 + 颜色范围自动",
        "抓包/重放页 — 采样率预设 + 实时功率表 + 抓包库 (含 .meta.json 元数据) + 重放弹窗参数自动回填",
        "设计语言复用 OBD Hacker (Bootstrap5 + bootstrap-icons + htmx, 全部内置无 CDN)",
        "start.sh / stop.sh 一键起停; 前端可 LAN 开放, 从另一台机器 (如 Windows) 浏览",
    ]},
]

PROJECT_BACKGROUND = [
    "HackRF One 是一款 1 MHz–6 GHz 的开源软件无线电 (SDR), 覆盖车钥匙遥控 (315/433 MHz)、ISM 传感器、WiFi/蓝牙、GPS 等绝大多数民用频段, 是无线安全测试的通用工具。",
    "但它的原生工具全是命令行 (hackrf_transfer / hackrf_sweep), 参数繁杂、需要记忆, 每次抓包重放都要手敲一长串 flag, 且收发采样率不一致等细节极易出错。",
    "本工具把这些命令行能力封装成浏览器操作: 预设频段一键扫描、参数表单化抓包、抓包库管理、一键重放 (参数自动回填防呆), 让无线信号的侦察与授权重放测试变得可视、可复用。",
    "定位始终是 侦察 + 授权重放: 发射类功能仅限自有/授权设备; GPS 模拟等高危发射必须在射频屏蔽箱内进行, 不向开放空间辐射。",
]


def create_app(bridge_url: str) -> Flask:
    app = Flask("hackrf_web",
                template_folder=str(HERE / "templates"),
                static_folder=str(HERE / "static"))
    app.config["JSON_AS_ASCII"] = False
    app.config["BRIDGE_URL"] = bridge_url

    def client() -> BridgeClient:
        return BridgeClient(app.config["BRIDGE_URL"])

    @app.context_processor
    def inject_globals():
        return dict(app_version=APP_VERSION, bridge_url=app.config["BRIDGE_URL"],
                    bands=BANDS, samp_presets=SAMP_PRESETS)

    # ---- pages ------------------------------------------------------------
    @app.route("/")
    def index():
        return redirect(url_for("device"))

    @app.route("/device")
    def device():
        return render_template("device.html")

    @app.route("/spectrum")
    def spectrum():
        return render_template("spectrum.html")

    @app.route("/capture")
    def capture():
        return render_template("capture.html")

    @app.route("/gps")
    def gps():
        return render_template("gps.html")

    @app.route("/help")
    def help_page():
        return render_template("help.html")

    @app.route("/about")
    def about():
        return render_template("about.html", author=AUTHOR_INFO,
                               changelog=CHANGELOG, features=FEATURES,
                               background=PROJECT_BACKGROUND)

    # ---- transparent proxy to RF_Bridge -----------------------------------
    BASE = lambda: app.config["BRIDGE_URL"].rstrip("/")

    def _proxy(method: str, bridge_path: str):
        url = BASE() + bridge_path
        try:
            if method == "GET":
                r = requests.get(url, timeout=15)
            elif method == "DELETE":
                r = requests.delete(url, timeout=15)
            else:  # POST
                r = requests.post(url, json=request.get_json(silent=True) or {},
                                  timeout=20)
        except requests.RequestException as e:
            return jsonify(ok=False, error=f"RF_Bridge 不可达: {e}"), 502
        return Response(r.content, status=r.status_code,
                        content_type=r.headers.get("Content-Type", "application/json"))

    @app.get("/api/health")
    def api_health():
        return _proxy("GET", "/health")

    @app.get("/api/device")
    def api_device():
        return _proxy("GET", "/device")

    @app.get("/api/tools")
    def api_tools():
        return _proxy("GET", "/tools")

    @app.get("/api/captures")
    def api_captures():
        return _proxy("GET", "/captures")

    @app.delete("/api/captures/<name>")
    def api_capture_delete(name):
        return _proxy("DELETE", f"/captures/{name}")

    @app.post("/api/sweep")
    def api_sweep():
        return _proxy("POST", "/jobs/sweep")

    @app.post("/api/capture")
    def api_capture():
        return _proxy("POST", "/jobs/capture")

    @app.post("/api/rtl433")
    def api_rtl433():
        return _proxy("POST", "/jobs/rtl433")

    @app.post("/api/replay")
    def api_replay():
        return _proxy("POST", "/jobs/replay")

    @app.post("/api/transfer_raw")
    def api_transfer_raw():
        return _proxy("POST", "/jobs/transfer_raw")

    @app.get("/api/captures/<name>/download")
    def api_capture_download(name):
        try:
            r = requests.get(BASE() + f"/captures/{name}/download",
                             stream=True, timeout=(10, 600))
        except requests.RequestException as e:
            return jsonify(ok=False, error=f"RF_Bridge 不可达: {e}"), 502
        if r.status_code != 200:
            return Response(r.content, status=r.status_code,
                            content_type=r.headers.get("Content-Type", "application/json"))

        def gen():
            try:
                for chunk in r.iter_content(65536):
                    if chunk:
                        yield chunk
            finally:
                r.close()

        return Response(stream_with_context(gen()), headers={
            "Content-Type": "application/octet-stream",
            "Content-Disposition": r.headers.get(
                "Content-Disposition", f'attachment; filename="{name}.raw"'),
        })

    @app.get("/api/jobs/current")
    def api_job_current():
        return _proxy("GET", "/jobs/current")

    @app.get("/api/gps/status")
    def api_gps_status():
        return _proxy("GET", "/gps/status")

    @app.post("/api/gps/ephemeris/fetch")
    def api_gps_eph_fetch():
        # downloading a real ephemeris can take a while — allow more time than the
        # generic 20s proxy (bridge tries several mirrors, ~2MB each)
        try:
            r = requests.post(BASE() + "/gps/ephemeris/fetch", timeout=(10, 90))
        except requests.RequestException as e:
            return jsonify(ok=False, message=f"RF_Bridge 不可达: {e}"), 502
        return Response(r.content, status=r.status_code,
                        content_type=r.headers.get("Content-Type", "application/json"))

    @app.post("/api/gps/simulate")
    def api_gps_simulate():
        return _proxy("POST", "/gps/simulate")

    @app.post("/api/gps/ephemeris")
    def api_gps_ephemeris():
        f = request.files.get("file")
        if not f:
            return jsonify(ok=False, error="未选择文件"), 400
        try:
            r = requests.post(BASE() + "/gps/ephemeris",
                              files={"file": (f.filename, f.stream, f.mimetype)},
                              timeout=30)
        except requests.RequestException as e:
            return jsonify(ok=False, error=f"RF_Bridge 不可达: {e}"), 502
        return Response(r.content, status=r.status_code,
                        content_type=r.headers.get("Content-Type", "application/json"))

    @app.get("/api/jobs/<job_id>")
    def api_job(job_id):
        return _proxy("GET", f"/jobs/{job_id}")

    @app.post("/api/jobs/<job_id>/stop")
    def api_job_stop(job_id):
        return _proxy("POST", f"/jobs/{job_id}/stop")

    @app.post("/api/stop")
    def api_stop_current():
        return _proxy("POST", "/jobs/stop")

    @app.get("/api/jobs/<job_id>/stream")
    def api_job_stream(job_id):
        """Proxy the RF_Bridge SSE stream through so the browser stays same-origin."""
        try:
            r = client().stream(job_id)
        except requests.RequestException as e:
            return jsonify(ok=False, error=f"RF_Bridge 不可达: {e}"), 502

        def gen():
            try:
                for chunk in r.iter_content(chunk_size=None):
                    if chunk:
                        yield chunk
            except requests.RequestException:
                pass
            finally:
                r.close()

        return Response(stream_with_context(gen()),
                        headers={"Content-Type": "text/event-stream",
                                 "Cache-Control": "no-cache",
                                 "X-Accel-Buffering": "no"})

    return app
