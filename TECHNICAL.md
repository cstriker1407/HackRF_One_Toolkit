# HackRF One Toolkit — 技术文档

写给要**改代码 / 加功能 / 排障**的人(含 AI)。读完应能完整理解架构、每个模块的职责、数据如何从一次点击流到屏幕、以及各种坑。配套 [README.md](README.md)(面向使用者)。

当前版本见 `src/hackrf_web/app.py::APP_VERSION`。

---

## 1. 架构与进程模型

两个独立进程,严格自上而下、无反向依赖:

```text
┌─ 浏览器 ───────────────────────────────────────────────┐
│  单页多标签(Bootstrap + htmx + 原生 JS)              │
└──────────────▲────────────────────────┬────────────────┘
   HTTP(命令) │  SSE(实时事件,同源)  │
┌──────────────┴────────────────────────▼────────────────┐
│  hackrf_web   Flask + waitress  :30000                  │
│    · 渲染页面 + 频段/采样率预设                         │
│    · /api/* 透明代理到 RF_Bridge(含 SSE / 大文件流)   │
│    · 不含任何硬件逻辑                                    │
└──────────────▲────────────────────────┬────────────────┘
   HTTP + SSE   │                        │
┌──────────────┴────────────────────────▼────────────────┐
│  RF_Bridge    Flask + waitress  :30001  (默认仅 127.0.0.1) │
│    · JobManager:单任务(半双工 → 一次一个任务)       │
│    · 把 hackrf_* / rtl_433 / gps-sdr-sim 当子进程跑     │
│    · 解析 stdout/stderr → 事件 → 每任务 SSE 流          │
└──────────────┬──────────────────────────────────────────┘
               │ subprocess(argv 数组,从不经 shell)
   hackrf_info · hackrf_sweep · hackrf_transfer · rtl_433 · gps-sdr-sim
```

**为什么两进程**:① 硬件独占,单独进程好实现"一次一个任务";② 后端可用特殊权限,前端不需要;③ 前端可与硬件**异机**(硬件在 Kali、开发/浏览在 Windows)。前端 `--allow-lan` 绑 0.0.0.0,后端默认绑 127.0.0.1 只允许本机前端访问。

**端口**:web=30000(浏览器开这个)、bridge=30001。选 3000x 是为避开同机 CAN 工具占用的 20000/20010。

---

## 2. 端到端数据流:一次"频谱扫描"

理解了这条链,整个项目就通了:

1. **浏览器**:用户点「连续扫描」。`spectrum.html::startScan()` 先 `HRF.guardStart("sweep")` 查 `/api/jobs/current`——确认没有别的任务占着射频(半双工),再 `POST /api/sweep {f_low_mhz, f_high_mhz, bin_width_hz, lna, vga, amp, one_shot:false, serial}`。
2. **hackrf_web**:`api_sweep()` 用 `_proxy()` 原样转发到 `RF_Bridge` 的 `POST /jobs/sweep`。
3. **RF_Bridge**:`sweep()` 用 `hackrf.sweep_argv(...)` 拼出 `["hackrf_sweep","-f","2400:2483","-w","100000","-l","40","-g","40","-d","<serial>"]`,交给 `mgr.start("sweep", argv, stdout_parser=parse)`。JobManager 锁内确认空闲 → 建 `Job` → 起线程 `_run`,返回 `{job_id}`。
4. **子进程**:`_run` 用 `Popen` 起 hackrf_sweep,一个 reader 线程逐行读它的 stdout(CSV),`parse()` 调 `hackrf.parse_sweep_row()` → `job.emit("sweep", hz_low, hz_high, bin_width, n, bins[])`。`emit` 把事件塞进 `job.events`(bounded deque)并 `seq += 1`、`cond.notify_all()`。
5. **回传**:浏览器早已 `EventSource("/api/jobs/<id>/stream")`。web 端 `api_job_stream` 把 bridge 的 `GET /jobs/<id>/stream` SSE 流式转发过来(保持同源)。bridge 的 SSE 生成器按 **seq**(不是 deque 下标!见 §3.3)取新事件,`data: {json}\n\n` 逐条吐出。
6. **上屏**:`HRF.streamJob` 的回调收到 `sweep` 事件 → `ingest()` 把每个频点写进 `spec[]`(打印机式,青线标当前扫描位置);`requestAnimationFrame` 画实时频谱 + 峰值保持;一个 400ms 定时器 `updatePeaks()` 找峰值刷新右侧列表。
7. **停止**:点「停止扫描」→ `POST /api/jobs/<id>/stop` → `mgr.stop()` `terminate()` 子进程(≤3s 未退则 `kill()`),`_run` 收尾 emit `done` → SSE 补发 `end` → 前端 `streamJob` 关闭 EventSource、复位按钮。

capture / replay / rtl433 / gps 都是这套的变体:换 argv、换解析器 emit 的事件类型、换前端消费逻辑。

---

## 3. RF_Bridge 单任务模型(`src/rf_bridge/jobs.py`)

半双工 + 单设备 ⇒ 同一时刻**最多一个任务**。忙时第二个提交抛 `RuntimeError("busy: …")`,路由转 HTTP 409。

### 3.1 `Job`

- 标识/状态:`id`、`kind`(`sweep|rtl433|capture|replay|gps`)、`argv`、`state`(`starting|running|done|error|stopped`)、`rc`、`error`、`started_at`/`ended_at`、`meta`。
- 子进程:`proc`(`start()` 类任务的子进程);`child`(`start_func()` 自定义任务里**当前**的子进程,如 GPS 循环每轮的 gps-sdr-sim / hackrf_transfer)。
- `stop_event: threading.Event`:自定义任务协作式停止的旗标,函数需**主动轮询**。
- 事件:`events: deque(maxlen=8192)` + `seq`(单调计数)+ `cond: Condition`。
- `active` = state ∈ {starting, running};`snapshot()` 供 `/jobs/current`、`/jobs/<id>`。

### 3.2 `JobManager`

- `start(kind, argv, stdout_parser?, stderr_parser?, on_exit?)`:锁内查 `current.active` → 建 Job/设 current → 起线程 `_run`(Popen → reader 线程喂 parser → `wait()` → 收尾 → `on_exit` → emit `done` → 锁内清 current)。
- `start_func(kind, fn)`:同样占用单任务位,但起 `_run_func`,由 `fn(job)` 自己管理子进程(GPS 用)。
- `stop(job_id?)`:取 job(无 id 取 current)→ `state="stopped"` + `stop_event.set()` → 对 `proc` 和 `child` 都 `terminate()`,3s 未退 `kill()`。**stop 阻塞到子进程真死(≤3s)**,所以"先 stop 再 start"不会出现两个进程抢设备。

### 3.3 关键陷阱:SSE 用 `seq`,不用 deque 下标(曾经的致命 bug)

`events` 是 `maxlen=8192` 环形队列。历史上 SSE 消费端用**绝对下标**读它;扫频每秒上千事件,几秒后累计超 8192,队列开始丢旧、长度卡死在 8192,而下标继续涨 → `events[下标:]` 恒空 → **前端几秒后收不到数据,但任务还在后台跑**(表现:频谱冻结 + "3 秒无数据"告警)。修复:每事件 `seq += 1`,SSE 端记 `last_seq`,每轮取 `events[-(seq-last_seq):]`。**改这块务必保持 seq 语义。**

### 3.4 停止的另一半:子进程必须"可杀"

`stop()` 只能杀 `job.proc` / `job.child`。自定义任务(GPS)里**每个**子进程都必须:① 用 `Popen` 起并**登记为 `job.child`**;② 循环里**轮询 `job.stop_event`** 并在置位时 `terminate`。曾经 GPS 生成阶段用阻塞的 `subprocess.run`(没登记 child),stop 杀不掉 → 孤儿 gps-sdr-sim 进程卡在 sigsuspend/0%CPU。教训见 `gps.py::_generate_killable`。

---

## 4. hackrf.py —— 命令构建 / 输出解析 / 白名单(`src/rf_bridge/hackrf.py`)

纯函数模块,不依赖 Flask/jobs。三类职责:

- **argv 构建**:`sweep_argv / capture_argv / replay_argv`,UI 参数名 1:1 映射 CLI 标志(`-f/-s/-b/-l/-g/-x/-a/-p/-n/-R/-d`)。`_dev()` 统一追加 `-d <serial>`。
- **输出解析**:
  - `parse_transfer_power(line)`:从 hackrf_transfer 的 stderr 周期行抽 `average power … dBfs`。
  - `parse_sweep_row(line)`:hackrf_sweep 的 CSV 行 → `{hz_low, hz_high, bin_width, n, bins[]}`。
  - `device_info()`:跑 `hackrf_info`,**多板解析**(按 `Index:` 切块),识别克隆板。
  - `tool_versions()`:hackrf(读 libhackrf 版本)/ rtl_433(`-V`)版本。
- **专家白名单**:`validate_transfer_argv(argv)` 要求 `argv[0]=="hackrf_transfer"`、只允许已知标志、必须含 `-r`/`-t`;`transfer_argv_role_path` 取角色+路径;`parse_transfer_params` 从 argv 反解参数写元数据。

> ⚠️ `device_info()` / `tool_versions()` 会打开 USB 设备;**任务运行时设备被占用**,这两个会失败/超时 → 硬件页/工具页在有任务时可能短暂显示异常(仅这两页加载时调用,不轮询,属"已知小问题")。

---

## 5. gps.py —— GPS 流水线(`src/rf_bridge/gps.py`)

两步流水线,对每个坐标:① `gps-sdr-sim -e 星历 -l 纬,经,高 -d 停留 -s 2600000 -b 8 -o wpN.bin` 生成 8-bit IQ;② `hackrf_transfer -t wpN.bin -f 1575420000 -s 2600000 -x 增益` 发射。多坐标按序循环 → 接收机位置随时间跳变。生成远快于实时(5s 信号约 0.4s)。

- 路径:`GPS_BIN = tools/gps-sdr-sim/gps-sdr-sim`;`BUNDLED_EPH` = 仓库自带样本(2022 年)。
- `active_ephemeris`:优先 `gps/ephemeris.nav`(用户上传/自动获取),否则样本。
- `fetch_ephemeris`:从 IGS BKG 镜像下载当天 **RINEX3 混合星历**(`BRDC00WRD_R_{YYYY}{DDD}0000_01D_MN.rnx.gz`),试今天+昨天+CDDIS 兜底,gunzip 存为 `ephemeris.nav`(gps-sdr-sim 能直接读 RINEX3 MN)。**能否成功取决于当天文件是否已发布 + 网络**。
- `_generate_killable`:生成阶段的 Popen(登记 `job.child`、轮询 `stop_event`、stdout/stderr→DEVNULL 防管道堵)。
- `_run_child`:发射阶段的 hackrf_transfer(登记 child、pump stderr 出 `power`、轮询 stop)。
- `run_sequence`:生成所有 bin(可中止)→ 无限循环逐点发射,emit `gps_wp`(地图/表格高亮)与 `info`(日志)。
- `generate_bin`:**死代码**,已被 `_generate_killable` 取代,可删。

安全:`extra_gen`(专家额外参数)在 app.py 校验禁 shell 特殊字符;lat/lon/alt 强转 float。

---

## 6. HTTP API

### 6.1 RF_Bridge(:30001)

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | `{ok, version, busy, tools{}}`(只查 which,不开设备) |
| GET | `/device` | 多板 hackrf_info 解析 `{present, count, devices[]}` |
| GET | `/tools` | 工具链版本(含 gps-sdr-sim) |
| GET | `/captures` | 抓包库(读 `.raw` + `.meta.json`) |
| DELETE | `/captures/<name>` | 删除(basename 限定 CAP) |
| GET | `/captures/<name>/download` | 下载 `.raw`(附件) |
| POST | `/jobs/sweep` | 扫频 `{f_low_mhz,f_high_mhz,bin_width_hz,lna,vga,amp,one_shot,serial}` |
| POST | `/jobs/rtl433` | 解码;结构化 `{freq_hz,samp_rate?,extra?,serial}` 或专家 `{argv:[…]}` |
| POST | `/jobs/capture` | 录制 `{freq_hz,samp_rate,bandwidth?,lna,vga,amp,bias_tee,n_samples?,name?,serial}` |
| POST | `/jobs/replay` | 发射 `{path,freq_hz,samp_rate,…,txvga,repeat,serial}`(path 限定 CAP) |
| POST | `/jobs/transfer_raw` | 专家 `{argv:[…]}`,白名单 + 路径限定 CAP |
| POST | `/gps/simulate` | GPS `{waypoints[{lat,lon,alt,dwell}],txgain,amp,…,extra_gen?,serial}` |
| GET | `/gps/status` | gps-sdr-sim 可用性 + 星历状态 |
| POST | `/gps/ephemeris` | multipart 上传星历 → `gps/ephemeris.nav` |
| POST | `/gps/ephemeris/fetch` | 自动获取当天星历(可能失败) |
| POST | `/jobs/<id>/stop` · `/jobs/stop` | 停指定 / 停当前(全局停) |
| GET | `/jobs/current` | 当前任务快照或 `{current:null}`(前端重连/全局锁用) |
| GET | `/jobs/<id>` | 任务快照 |
| GET | `/jobs/<id>/stream` | **SSE** 事件流(§7) |

### 6.2 hackrf_web(:30000)

页面:`/`(跳 `/device`)、`/device`、`/spectrum`、`/capture`、`/gps`、`/help`、`/about`。

`/api/*` 除下列外都由 `_proxy(method, bridge_path)` 透明转发(保留状态码/内容类型):
- `/api/jobs/<id>/stream`、`/api/captures/<name>/download`:**流式**转发(SSE / 大文件);
- `/api/gps/ephemeris`:multipart 转发;`/api/gps/ephemeris/fetch`:单独 90s 超时(下载慢)。

---

## 7. SSE 事件类型

`GET /jobs/<id>/stream` → `text/event-stream`,每行 `data: <json>\n\n`,15s 一个 `: keepalive`。

| kind | payload | 谁 emit |
| --- | --- | --- |
| `info` | `{msg}` | 各任务(进度/日志) |
| `sweep` | `{hz_low,hz_high,bin_width,n,bins[]}` | sweep |
| `power` | `{dbfs}` | capture / replay / GPS 发射 |
| `decode` | `{model,dev_id,channel,data{完整JSON}}` | rtl433 |
| `gps_wp` | `{index,lat,lon,loop,dwell}` | GPS 切坐标 |
| `done` | `{state,rc?,error?}` | 任务收尾 |
| `end` | `{state}` | SSE 生成器在任务终止后补发;前端据此关 EventSource |

---

## 8. 前端(`static/app.js` + `templates/`)

### 8.1 `HRF` 命名空间

- 格式化 `fmtHz`/`fmtBytes`;设备 `getDevice/setDevice/withDevice`(选中的序列号存 localStorage,随请求带上)。
- SSE `streamJob(jobId, onEvent, onEnd)`;`post`;`buildTransferCmd(opts)`(命令预览拼装)。
- 画图 `PowerChart`(dBFS↔时间;`setDuration()` 按真实 `-n/-s` 时长画 X 轴,未知时长则滚动窗口)、`Waterfall`(旧,现未用)、`dbColor`。
- 任务协调 `currentJob() / kindLabel / guardStart(myKind) / pollBridge(状态灯,忙时可点停) / pollBanner(全局锁)`。
- 内联帮助 `HELP{key:{t,h}}` + `openHelp` + 委托点击 `[data-help]` → 右侧 offcanvas 抽屉。

> ⚠️ **HELP 里的 `h` 是双引号 JS 字符串**,内部中文强调必须用 `『』` 全角引号,**不能用 ASCII `"`**(会截断字符串)。`node --check` + `scratchpad/jscheck.py` 能抓到。

### 8.2 任务归属 / 互斥 / 全局锁 / 跨页重连(半双工的 UI 落地)

- 每页声明 `window.HRF_PAGE_KIND`(单)或 `HRF_PAGE_KINDS`(多):device 无、spectrum=`["sweep","rtl433"]`、capture=`["capture","replay"]`、gps=`"gps"`。
- **启动守卫** `guardStart(myKind)`:查当前任务——无→可启;同类→允许(可先停自己);**他类→拒绝**(绝不跨页误杀)。
- **全局锁** `pollBanner`(每 2s):当前任务不属本页 → 顶部黄条 + `#lockOverlay` 整页遮罩(z-index 1020,导航栏 1030 仍可点),本页所有操作被挡。多窗口下,一个窗口开任务,其他窗口 ~2s 内自动锁。
- **同页互斥**(spectrum 内 sweep vs rtl433):`refreshRadioLock()` 令一个运行时另一个按钮变灰 + 提示(`#scanXlock`/`#decXlock`)。
- **跨页重连**:任务运行时离开页面**不会停**;返回时各页 IIFE 查 `currentJob()`,若是本页类型则重连 SSE 恢复 UI。GPS 坐标序列另存 `localStorage("hrf_gps_wps")`,返回后恢复。
- **总规则**:一次一个任务;停止只能在**任务所属页面**或**右上角状态灯(全局停)**。

### 8.3 各页要点

- **device**:多设备列表 + "设为当前";工具链版本表。
- **spectrum**:两标签。① 频谱扫描:连续/单次 → `sweep` 事件 → 打印机式实时频谱(青线=扫描位置)+ 峰值保持 + 最多 8 峰 + 峰值列表(点"抓包"跳 `/capture?freq=`);纵轴自动/手动;高级(LNA/VGA/Amp)+ 命令预览。② 设备解码:`rtl_433 -F json` → `decode` 事件 → 设备表;高级(采样率)+ 专家(额外参数/可改命令)。
- **capture**:录制(标准/高级/专家 + 命令预览)→ `power` → PowerChart(真实时长)+ 倒计时;开始键兼作停止。抓包库(可滚动):重放/下载/删除。重放弹窗:参数回填、采样率锁定;**发射时弹窗不可关**(`hide.bs.modal` 拦截),命令可专家改,有倒计时。
- **gps**:Leaflet 地图(高德/OSM,高德做 GCJ-02→WGS-84 纠偏)点坐标 → 坐标表 → 生成并循环发射;发射时锁地图/表、当前坐标高亮;星历自动获取/上传;运行日志。
- **help**:10 节 SDR/射频技术手册。**about**:`APP_VERSION` + `CHANGELOG` + 作者/背景/特性。

---

## 9. 数据与文件

- **抓包**:`captures/<name>.raw`(交错 8-bit I/Q,每样本 2 字节;大小 = 采样率 × 2 × 秒)+ `captures/<name>.raw.meta.json`:
  `{name, raw_path, params{freq_hz,samp_rate,bandwidth,lna,vga,amp,n_samples}, size_bytes, state, power_min_dbfs, power_max_dbfs, started_at, ended_at}`。
- **GPS**:`gps/ephemeris.nav`(用户星历)、`gps/wp*.bin`(每坐标生成的 IQ)。
- **自动命名**:留空时 `cap_<频率MHz>M_<YYYYMMDD_HHMMSS>`。

---

## 10. 安全模型

- **信任边界**:局域网可信,**无登录鉴权**。bridge 默认 127.0.0.1;web `--allow-lan` 才 0.0.0.0。跨信任边界需自加反代 + 认证。
- **命令注入防护**:子进程一律 argv 数组,**从不经 shell**;专家输入走白名单(transfer_raw 必须 hackrf_transfer + 已知标志;rtl433/gps 额外参数禁 `;|&\`$><\n`)。
- **路径限定**:capture/replay/transfer_raw 的文件名一律 `basename` 后重挂到 `captures/`,防目录穿越。
- **发射合规**:GPS 页多处屏蔽箱警告;重放弹窗强调仅授权设备。

---

## 11. 部署与开发

- **运行**:`start.sh`(pkill 旧进程 + 清残留 → 起 bridge:30001 + web:30000 --allow-lan → 打印访问地址);`stop.sh` 反之;`launch.sh`(桌面图标用:健康检查 → 没起就 `start.sh` → 打开浏览器)。日志在 `logs/`。
- **依赖**:项目 venv `~/HackRF_One_Toolkit/.venv`(flask/waitress/requests);系统 `hackrf`、`rtl-433`、`soapysdr-module-hackrf`;可选 `tools/gps-sdr-sim/`。
- **开发工作流**:Windows 编辑 `d:\HackRF_One_Toolkit` → 同步到 Kali `~/HackRF_One_Toolkit` 跑真机(硬件在 Kali)。改完先 `node --check app.js` + 检查模板内联 JS + `python -m py_compile`,再同步 + `start.sh`。
  - ⚠️ **Git Bash 会改写 `/tmp`、`/home/...` 这类 POSIX 路径**(MSYS path mangling)。驱动 Kali 的命令用 PowerShell 调 python 脚本,别用 Bash 传含绝对路径的参数。

---

## 12. 已知问题 / 限制(审计结论)

1. **无登录鉴权**(LAN-trust)。计划:反代 + basic auth 或 Flask-Login。
2. `hackrf_info` 在任务运行时无法开设备 → 硬件/工具页有任务时可能短暂报错(cosmetic)。
3. GPS 星历上传**无大小限制 / 无 RINEX 校验**;自动获取依赖当天文件已发布。
4. `_write_capture_meta` 无锁遍历 `job.events` deque(因收尾时序实际安全,理论极端并发可 RuntimeError;低风险)。
5. `/jobs/current` 无锁读 `mgr.current`(良性)。
6. `gps.py::generate_bin` 为死代码,可删。
7. 多窗口各自 SSE 占 waitress 线程(单用户量级无碍)。
8. bridge `/health` 的 `VERSION` 与 web `APP_VERSION` 是两个常量,未同步(仅显示,无害)。
9. rtl_433 未显式给 `-s` 时由其自选采样率;hackrf 最低 1 Msps。

**尚无 git 仓、无登录**(用户手动提 GitHub)。

---

## 13. 扩展点

- **加任务类型**:bridge 加 `/jobs/<kind>`(`mgr.start` 或 `start_func`)+ 解析器 emit 事件;web 加 `/api/<kind>` 代理;前端页面加控件 + `guardStart(kind)` + `HRF_PAGE_KINDS` 收录 + SSE 消费。
- **加频段预设**:`hackrf_web/app.py::BANDS`(频谱/抓包/GPS 共用)、`SAMP_PRESETS`。
- **加内联帮助**:`app.js::HELP` 加 `{key:{t,h}}`(注意 `『』`),控件加 `data-help="key"`。
- **加前端库**:放 `static/vendor/`(全内置无 CDN:Leaflet / Bootstrap / htmx)。

---

## 14. 维护者速记(踩过的坑)

- 半双工 = 一次一个任务;停止只能原页面或右上角全局停;跨页有遮罩锁 + 重连。
- SSE 用 `seq` 别用 deque 下标(否则几秒后断流)。
- 生成/发射子进程必须登记 `job.child` 并轮询 `stop_event`,否则 stop 杀不掉(GPS 卡死教训)。
- HELP 字符串内部用 `『』`,不用 ASCII 双引号。
- 驱动 Kali 用 PowerShell + python 脚本,别让 Git Bash 改写路径。
- 子进程一律 argv 数组;专家输入走白名单;文件名 basename 限定 `captures/`。
- 高德底图是 GCJ-02,点坐标要 `gcj2wgs` 转 WGS-84 再发射。
