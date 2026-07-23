/* HackRF Toolkit — shared frontend helpers */
window.HRF = (function () {
  // ---- formatting ---------------------------------------------------------
  function fmtHz(hz) {
    hz = Number(hz);
    if (hz >= 1e9) return (hz / 1e9).toFixed(3) + " GHz";
    if (hz >= 1e6) return (hz / 1e6).toFixed(3) + " MHz";
    if (hz >= 1e3) return (hz / 1e3).toFixed(1) + " kHz";
    return hz + " Hz";
  }
  function fmtBytes(b) {
    b = Number(b);
    if (b >= 1e9) return (b / 1e9).toFixed(2) + " GB";
    if (b >= 1e6) return (b / 1e6).toFixed(1) + " MB";
    if (b >= 1e3) return (b / 1e3).toFixed(1) + " KB";
    return b + " B";
  }

  // ---- active device (serial) persisted across pages ----------------------
  function getDevice() { return localStorage.getItem("hrf_device") || null; }
  function setDevice(serial) {
    if (serial) localStorage.setItem("hrf_device", serial);
    else localStorage.removeItem("hrf_device");
  }
  function withDevice(body) {
    const s = getDevice();
    if (s) body.serial = s;
    return body;
  }

  // ---- SSE helper ---------------------------------------------------------
  function streamJob(jobId, onEvent, onEnd) {
    const es = new EventSource("/api/jobs/" + jobId + "/stream");
    es.onmessage = function (m) {
      let ev; try { ev = JSON.parse(m.data); } catch (e) { return; }
      if (ev.kind === "end") { es.close(); if (onEnd) onEnd(ev); return; }
      if (onEvent) onEvent(ev);
    };
    es.onerror = function () { es.close(); if (onEnd) onEnd({ kind: "end", state: "closed" }); };
    return es;
  }

  async function post(path, body) {
    const r = await fetch(path, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    let j = {}; try { j = await r.json(); } catch (e) {}
    return { ok: r.ok, status: r.status, body: j };
  }

  // ---- build a hackrf_transfer command from structured opts ---------------
  // Used for the live command preview on capture/replay pages.
  function buildTransferCmd(o) {
    const a = ["hackrf_transfer"];
    a.push(o.mode === "tx" ? "-t" : "-r", o.file || "OUT.raw");
    a.push("-f", o.freq_hz);
    a.push("-s", o.samp_rate);
    if (o.bandwidth) a.push("-b", o.bandwidth);
    if (o.mode === "rx") { a.push("-l", o.lna); a.push("-g", o.vga); }
    else { a.push("-x", o.txvga); }
    a.push("-a", o.amp ? 1 : 0);
    if (o.bias_tee) a.push("-p", 1);
    if (o.n_samples) a.push("-n", o.n_samples);
    if (o.mode === "tx" && o.repeat) a.push("-R");
    if (o.serial) a.push("-d", o.serial);
    return a.join(" ");
  }

  // ---- dBfs → color -------------------------------------------------------
  function dbColor(db, minDb, maxDb) {
    let t = (db - minDb) / (maxDb - minDb); t = Math.max(0, Math.min(1, t));
    const stops = [[8,12,60],[0,160,200],[240,220,40],[220,40,30]];
    const seg = t * (stops.length - 1);
    const i = Math.min(stops.length - 2, Math.floor(seg)), f = seg - i;
    const a = stops[i], b = stops[i + 1];
    return [Math.round(a[0]+(b[0]-a[0])*f), Math.round(a[1]+(b[1]-a[1])*f), Math.round(a[2]+(b[2]-a[2])*f)];
  }

  // ---- Waterfall ----------------------------------------------------------
  class Waterfall {
    constructor(canvas, opts) {
      opts = opts || {};
      this.canvas = canvas; this.ctx = canvas.getContext("2d");
      this.minDb = opts.minDb != null ? opts.minDb : -90;
      this.maxDb = opts.maxDb != null ? opts.maxDb : -20;
      this.W = canvas.width; this.H = canvas.height;
    }
    setRange(mn, mx) { this.minDb = mn; this.maxDb = mx; }
    clear() { this.ctx.clearRect(0, 0, this.W, this.H); }
    addRow(values, peakIdx) {
      const ctx = this.ctx, W = this.W, H = this.H;
      const img = ctx.getImageData(0, 0, W, H - 1);
      ctx.putImageData(img, 0, 1);
      const rowImg = ctx.createImageData(W, 1), n = values.length;
      for (let x = 0; x < W; x++) {
        const c = dbColor(values[Math.floor((x / W) * n)], this.minDb, this.maxDb);
        const o = x * 4; rowImg.data[o]=c[0]; rowImg.data[o+1]=c[1]; rowImg.data[o+2]=c[2]; rowImg.data[o+3]=255;
      }
      ctx.putImageData(rowImg, 0, 0);
      if (peakIdx != null) { const x = Math.floor(peakIdx / n * W);
        ctx.fillStyle = "rgba(255,255,255,.9)"; ctx.fillRect(x, 0, 1, 1); }
    }
  }

  // ---- current running job (for cross-page reconnect) ---------------------
  async function currentJob() {
    try { const j = await (await fetch("/api/jobs/current")).json(); return j.current || null; }
    catch (e) { return null; }
  }

  // ---- bridge status pill (clickable to stop when busy) -------------------
  async function pollBridge() {
    const el = document.getElementById("bridgeStatus");
    if (!el) return;
    try {
      const j = await (await fetch("/api/health")).json();
      if (j.ok) {
        if (j.busy) {
          el.className = "badge rounded-pill bg-warning text-dark";
          el.style.cursor = "pointer"; el.title = "点击停止当前任务";
          el.innerHTML = '<i class="bi bi-activity"></i> 运行任务中 · 点击停止';
          el.onclick = async () => { if (confirm("停止当前正在运行的任务?")) { await fetch("/api/stop", {method:"POST"}); pollBridge(); } };
        } else {
          el.className = "badge rounded-pill bg-success";
          el.style.cursor = "default"; el.title = ""; el.onclick = null;
          el.innerHTML = '<i class="bi bi-check-circle"></i> RF_Bridge 就绪';
        }
      } else { el.className = "badge rounded-pill bg-danger"; el.onclick=null; el.innerHTML = '<i class="bi bi-x-circle"></i> Bridge 异常'; }
    } catch (e) { el.className = "badge rounded-pill bg-danger"; el.onclick=null; el.innerHTML = '<i class="bi bi-x-circle"></i> Bridge 不可达'; }
  }

  // ---- power strip-chart (dBFS over time) --------------------------------
  // Two modes: fixed (duration known → X axis = 0..duration, fills left→right)
  // or rolling (duration null → last `window` seconds scroll past).
  class PowerChart {
    constructor(canvas, opts) {
      opts = opts || {};
      this.c = canvas; this.ctx = canvas.getContext("2d");
      this.min = opts.min != null ? opts.min : -60;
      this.max = opts.max != null ? opts.max : 0;
      this.window = opts.seconds || 30;
      this.duration = opts.duration || null;   // seconds; null = rolling
      this.W = canvas.width; this.H = canvas.height;
      this.data = []; this.t0 = null;
      this.draw();
    }
    setDuration(sec) { this.duration = (sec && sec > 0) ? sec : null; this.reset(); }
    reset() { this.data = []; this.t0 = null; this.draw(); }
    push(db) {
      const now = Date.now() / 1000;
      if (this.t0 == null) this.t0 = now;
      this.data.push({ t: now, db });
      if (!this.duration) { const c = now - this.window; while (this.data.length && this.data[0].t < c) this.data.shift(); }
      this.draw();
    }
    draw() {
      const ctx = this.ctx, W = this.W, H = this.H, now = Date.now()/1000;
      ctx.fillStyle = "#0b0e14"; ctx.fillRect(0, 0, W, H);
      ctx.strokeStyle = "rgba(255,255,255,.08)"; ctx.fillStyle = "rgba(255,255,255,.45)"; ctx.font = "10px monospace";
      for (let k = 0; k <= 3; k++) { const y = H*k/3; ctx.beginPath(); ctx.moveTo(0,y); ctx.lineTo(W,y); ctx.stroke();
        ctx.fillText(Math.round(this.max-(this.max-this.min)*k/3)+"", 2, Math.min(H-2,y+10)); }
      const yOf = v => { let t=(v-this.min)/(this.max-this.min); t=Math.max(0,Math.min(1,t)); return H-t*H; };
      const t0 = this.t0 || now;
      const xOf = this.duration
        ? (ts) => Math.min(W, (ts - t0) / this.duration * W)
        : (ts) => W - (now - ts) / this.window * W;
      if (this.data.length) {
        ctx.beginPath(); ctx.strokeStyle="#39d353"; ctx.lineWidth=1.6;
        this.data.forEach((p,i)=>{ const x=xOf(p.t), y=yOf(p.db); i?ctx.lineTo(x,y):ctx.moveTo(x,y); });
        ctx.stroke();
        const last = this.data[this.data.length-1];
        ctx.fillStyle="#39d353"; ctx.beginPath(); ctx.arc(xOf(last.t), yOf(last.db), 2.5, 0, 7); ctx.fill();
      }
      ctx.fillStyle="rgba(255,255,255,.4)"; ctx.font="10px monospace";
      const leftLbl = this.duration ? "0s" : "-"+this.window+"s";
      const rightLbl = this.duration ? this.duration.toFixed(this.duration<10?1:0)+"s" : "现在";
      ctx.textAlign="left"; ctx.fillText(leftLbl, 2, H-2);
      ctx.textAlign="right"; ctx.fillText(rightLbl, W-2, H-2); ctx.textAlign="left";
    }
  }

  // ---- job-kind labels + start guard (same-page-only stop policy) ---------
  const KIND_LABEL = { sweep: "频谱扫描", capture: "录制", replay: "发射", gps: "GPS 发射" };
  function kindLabel(k) { return KIND_LABEL[k] || k; }

  // Returns {ok:true} to start, {ok:true,restart:true} if a same-kind job of
  // ours is running (caller may stop it first), or {ok:false,kind} if a
  // DIFFERENT task owns the radio (must be stopped from its own page or the
  // global top-right stop — not from here).
  async function guardStart(myKind) {
    const cur = await currentJob();
    if (!cur) return { ok: true };
    if (cur.kind === myKind) return { ok: true, restart: true };
    return { ok: false, kind: cur.kind };
  }

  // ---- inline help drawer -------------------------------------------------
  // Any element with data-help="key" opens the drawer with HELP[key].
  const HELP = {
    "device-select": { t: "设备选择 (-d serial)", h:
      "<p>当接了<b>多个 HackRF</b> 时,用它指定这次任务用哪一台(底层 <code>-d 序列号</code>)。只有一台时无需选择。</p>" +
      "<p>选择会记住并应用到扫描/抓包/重放。半双工 + 单设备,所以同一台同一时刻只能跑一个任务;不同的两台可以各跑各的(当前版本仍是一次一个任务)。</p>" },
    "freq": { t: "中心频率 (-f)", h:
      "<p>HackRF 一次以这个频点为中心接收/发射一段频谱。单位 Hz。</p><p>例:汽车遥控 <code>433920000</code>(433.92 MHz)。可从频谱扫描页的峰值读到准确频点。</p>" },
    "samp-rate": { t: "采样率 (-s)", h:
      "<p>每秒采样点数,决定一次能覆盖多宽的频谱(≈采样率大小的带宽)。单位 Hz。</p>" +
      "<ul><li><b>窄带 2 Msps</b>:钥匙/遥控等窄信号,文件最小(推荐)</li><li><b>标准 8 Msps</b>:抗频偏余量大</li><li><b>宽带 20 Msps</b>:宽信号,文件巨大</li></ul>" +
      "<p class='mb-0 text-danger'>⚠ 重放时采样率必须与录制时<b>完全一致</b>,否则信号时间轴被拉伸解不开。</p>" },
    "bandwidth": { t: "基带带宽 (-b)", h:
      "<p>硬件模拟低通滤波器的宽度,通常设为略小于采样率(如 2 Msps → 1.75 MHz)。作用是滤掉带外噪声。留空则用采样率自动匹配的值。</p>" },
    "lna": { t: "LNA 增益 (-l, 接收)", h:
      "<p>低噪声放大器,位于最靠近天线的第一级。范围 <b>0–40</b>,步进 8。信号弱就调大;太大可能过载失真。</p><p>典型:32。</p>" },
    "vga": { t: "VGA 增益 (-g, 接收)", h:
      "<p>可变增益放大器,接收链路的后级(基带)。范围 <b>0–62</b>,步进 2。和 LNA 配合把信号抬到合适电平。</p><p>典型:16–20。</p>" },
    "txvga": { t: "TX 增益 (-x, 发射)", h:
      "<p>发射增益,范围 <b>0–47</b>。越大发射越强、作用距离越远。重放时先从中等值(如 30)试,不行再加。</p><p class='mb-0 text-danger'>⚠ 仅对授权设备发射。</p>" },
    "amp": { t: "RF 放大器 Amp (-a)", h:
      "<p>HackRF 前端的一级额外<b>射频放大器(约 +11~14 dB)</b>,收发都可用。</p>" +
      "<ul><li><b>接收</b>:信号很弱时打开,能抬高微弱信号。</li><li><b>发射</b>:增大输出功率。</li></ul>" +
      "<p class='mb-0'>信号已经够强时<b>不要开</b>,否则容易过载/失真,反而变差。默认关。</p>" },
    "bias-tee": { t: "天线偏置电源 (-p)", h:
      "<p>通过天线接口向外供直流电(bias-tee),用来给<b>有源天线 / 低噪放</b>供电。普通无源天线<b>不要打开</b>。默认关。</p>" },
    "n-samples": { t: "采样点数 / 时长 (-n)", h:
      "<p>限制录制/发射的样本总数,达到即自动停止。本工具用<b>时长(秒)</b>换算:点数 = 采样率 × 秒数。</p><p>时长填 0 = 不限制,手动点『停止』。</p>" },
    "sweep-bin": { t: "频点分辨率", h:
      "<p>扫频时每个频点(bin)的宽度。越小越细(能分辨靠得很近的信号),但扫得越慢。粗看用 100 kHz,细看用 25 kHz。</p>" },
    "dbfs-range": { t: "配色强弱阈值 (dBfs)", h:
      "<p><b>dBfs</b> 是信号强度刻度:0 最强,越负越弱(如 −80 很弱,−10 很强)。这两个值决定<b>颜色怎么映射强弱</b>:</p>" +
      "<ul><li>低于<b>下限</b> → 冷色(蓝,当作背景噪声)</li><li>高于<b>上限</b> → 暖色(红,当作强信号)</li></ul>" +
      "<p>阈值设得合适,信号才醒目。勾选<b>自动</b>会按当前画面里的最弱~最强持续调整,一般保持自动即可;想手动突出某强度范围时再关掉自己填。</p>" },
    "spectrum-line": { t: "实时频谱(上图)", h:
      "<p>横轴=频率(左=起始,右=结束),纵轴=该频点当前的信号强度。就是『此刻各频点有多强』。</p>" +
      "<p>扫描是从左到右<b>逐段扫过去</b>的,那条竖线是<b>当前扫到的位置</b>(像打印机走纸头)。红色虚线标出的是<b>峰值频点</b>。</p>" },
    "waterfall": { t: "瀑布图(下图)有什么用", h:
      "<p>把每一遍扫描的频谱压成一条彩色横线,一行一行往下叠 —— 横轴还是频率,<b>纵轴是时间</b>(越往下越久以前)。颜色=强度。</p>" +
      "<p><b>作用</b>:一眼看出<b>什么时候、哪个频点</b>出现过信号。比如你按一下钥匙,瀑布图上会出现一条短暂的亮竖条,即使信号一闪而过也能被『记录』下来,比只看实时频谱更容易抓到间歇/突发信号。竖白线标的是每行的峰值频点。</p>" },
    "realtime-power": { t: "实时功率 (dBfs)", h:
      "<p>录制/发射时,HackRF 每秒报告一次这段信号的<b>平均功率</b>(dBfs,0 最强越负越弱)。</p>" +
      "<p><b>怎么用</b>:安静时是<b>底噪</b>(如 −28 dBfs)。当你按下钥匙、信号进来,功率会<b>明显跳升</b>(如跳到 −3)。看到这个跳升,就说明『录到有效信号了』,而不是录了一段空白。</p>" },
    "command-preview": { t: "hackrf_transfer 参数详解", h:
      "<p><code>hackrf_transfer</code> 是 HackRF 的收发工具:<code>-r</code> 把射频采样成 IQ 写入文件,<code>-t</code> 把 IQ 文件发射出去。常用参数:</p>" +
      "<table class='table table-sm mb-2'><tbody>" +
      "<tr><td class='mono'>-r &lt;file&gt;</td><td>接收:把 8-bit I/Q 采样写入文件(录制)</td></tr>" +
      "<tr><td class='mono'>-t &lt;file&gt;</td><td>发射:从文件读 I/Q 采样发射</td></tr>" +
      "<tr><td class='mono'>-f &lt;hz&gt;</td><td>中心频率,单位 Hz(1M–6G)</td></tr>" +
      "<tr><td class='mono'>-s &lt;hz&gt;</td><td>采样率,单位 Hz(2M–20M 常用)。收发必须一致</td></tr>" +
      "<tr><td class='mono'>-b &lt;hz&gt;</td><td>基带滤波带宽(1.75M–28M 的档位),通常≈采样率</td></tr>" +
      "<tr><td class='mono'>-l &lt;db&gt;</td><td>LNA(IF)增益,0–40,步进 8(仅接收)</td></tr>" +
      "<tr><td class='mono'>-g &lt;db&gt;</td><td>VGA(基带)增益,0–62,步进 2(仅接收)</td></tr>" +
      "<tr><td class='mono'>-x &lt;db&gt;</td><td>TX VGA 发射增益,0–47(仅发射)</td></tr>" +
      "<tr><td class='mono'>-a &lt;0|1&gt;</td><td>RF 前端放大器 (~+11dB),收发通用</td></tr>" +
      "<tr><td class='mono'>-p &lt;0|1&gt;</td><td>天线口偏置电源(bias-tee,给有源天线供电)</td></tr>" +
      "<tr><td class='mono'>-n &lt;n&gt;</td><td>传输 n 个采样后停止(n = 采样率 × 秒数)</td></tr>" +
      "<tr><td class='mono'>-R</td><td>循环发射,反复播放文件(仅发射)</td></tr>" +
      "<tr><td class='mono'>-d &lt;serial&gt;</td><td>指定用哪一台 HackRF(多设备时)</td></tr>" +
      "</tbody></table>" +
      "<p class='mb-1'><b>I/Q 文件格式</b>:交错的有符号 8-bit(I,Q,I,Q…),即每采样 2 字节。文件大小 = 采样率 × 2 × 秒数。</p>" +
      "<p class='mb-0'>下方文本框就是本工具据上面参数拼出的真实命令。打开<b>专家模式</b>可直接手改(仅允许 hackrf_transfer 及以上参数,文件被限制在抓包目录内),点开始即按你写的执行。</p>" },
    "sweep-cmd": { t: "hackrf_sweep 命令", h:
      "<p>频谱扫描底层调用 <code>hackrf_sweep</code>。参数:<code>-f low:high</code> 频率范围(MHz)、<code>-w</code> 每个频点带宽(Hz)、<code>-l/-g</code> LNA/VGA 增益、<code>-a 1</code> 开 RF 放大器、<code>-1</code> 只扫一遍(单次扫描)、<code>-d</code> 指定设备。</p>" +
      "<p class='mb-0'>它每行输出:时间、频段起止、bin 宽、每个频点的 dB 值 —— 本工具据此画出频谱与峰值。</p>" },
    "repeat": { t: "循环发射 (-R)", h:
      "<p>把文件<b>循环</b>反复发射,直到你手动停止。适合需要持续发射的场景。单次重放不用勾。</p>" },
    "yaxis-range": { t: "纵轴范围 (dBfs)", h:
      "<p><b>dBfs</b> 是信号强度刻度:0 最强,越负越弱。这两个值是频谱图<b>纵轴的上下边界</b>:上限对应图顶、下限对应图底。</p>" +
      "<p>范围收窄能把弱信号『放大』看清,放宽能容纳很强的信号不顶格。勾选<b>自动</b>会按当前画面最弱~最强持续调整,一般保持自动即可。</p>" },
    "rtl433-extra": { t: "rtl_433 额外参数(专家)", h:
      "<p>追加到解码命令的原始 rtl_433 参数,常用:</p>" +
      "<ul>" +
      "<li><code>-R &lt;n&gt;</code> — 只启用第 n 号协议解码器(默认全开;全开更慢)</li>" +
      "<li><code>-A</code> — 分析未知脉冲(逆向新设备时用)</li>" +
      "<li><code>-Y minmax</code> — 调整脉冲检测模式</li>" +
      "<li><code>-M level</code> — 在输出里附带信号电平/信噪比</li>" +
      "</ul>" +
      "<p class='mb-0'>多个参数空格分隔;禁止 shell 特殊字符。</p>" },
    "rtl433-cmd": { t: "rtl_433 将执行的命令", h:
      "<p>下方就是本工具将执行的真实 <code>rtl_433</code> 命令,参数由上面表单实时拼出。</p>" +
      "<p><code>-d driver=hackrf</code> 用 HackRF(经 SoapySDR)· <code>-f</code> 频率 · <code>-F json</code> 结构化输出(工具据此列表)· <code>-s</code> 采样率 · 其余为额外参数。</p>" +
      "<p class='mb-0'>开<b>专家模式</b>可直接手改这行(仅允许 rtl_433 开头 + 禁 shell 特殊字符),开始后按你写的执行。</p>" },
    "rtl433": { t: "rtl_433 怎么用(设备解码)", h:
      "<p><code>rtl_433</code> 能直接<b>解码</b> 400+ 种 433/315/868 MHz 设备(胎压 TPMS、气象站、门磁、温湿度计、遥控…),输出结构化数据 —— 比只看频谱更进一步。本机通过 SoapySDR 用 HackRF(无需 RTL-SDR 棒)。</p>" +
      "<p><b>基本用法</b>(在 Kali 终端):</p>" +
      "<p class='cmd-box'>rtl_433 -d driver=hackrf</p>" +
      "<p>默认监听 433.92 MHz、自动跑全部解码器,收到设备就打印。常用参数:</p>" +
      "<ul>" +
      "<li><code>-f 315M</code> — 改频率(如北美 315 MHz)</li>" +
      "<li><code>-F json</code> — 输出 JSON(便于程序处理)</li>" +
      "<li><code>-T 30</code> — 跑 30 秒后退出</li>" +
      "<li><code>-s 2000000</code> — 采样率;<code>-R &lt;n&gt;</code> 只启用某个解码器,<code>-A</code> 分析未知脉冲</li>" +
      "</ul>" +
      "<p class='mb-0 text-muted'>提示:rtl_433 与本工具都独占 HackRF(半双工),同一时刻只能跑一个。目前 rtl_433 走命令行,后续可考虑做成页面。</p>" },
    "gps-cmd": { t: "GPS 将执行的命令(两步)", h:
      "<p>GPS 模拟不是单条命令,而是两步流水线,对每个坐标:</p>" +
      "<p><b>① 生成</b>:<code>gps-sdr-sim</code> 按星历 + 坐标 + 停留秒数合成该点的 8-bit I/Q 文件。</p>" +
      "<p><b>② 发射</b>:<code>hackrf_transfer -t</code> 把文件发射出去;多个坐标按顺序循环播放,实现位置按时间跳变。</p>" +
      "<p class='mb-0'>预览里的 <code>&lt;纬度&gt;</code> 等占位符会被每个坐标的实际值替换。改高级参数(频率/采样率/增益/额外参数)这里会实时反映。</p>" },
    "gps-extra": { t: "gps-sdr-sim 额外参数(专家)", h:
      "<p>追加到生成命令的原始 gps-sdr-sim 参数,用于精调,常见:</p>" +
      "<ul>" +
      "<li><code>-t yyyy/mm/dd,hh:mm:ss</code> — 指定场景时间(默认用星历时间)</li>" +
      "<li><code>-T yyyy/mm/dd,hh:mm:ss</code> — 用当前时间并覆盖星历时间</li>" +
      "<li><code>-i</code> — 关闭电离层延迟模型</li>" +
      "<li><code>-v</code> — 详细输出</li>" +
      "</ul>" +
      "<p class='mb-0'>多个参数用空格分隔。只允许参数标志,禁止 shell 特殊字符。</p>" },
    "gps-eph": { t: "星历文件 (RINEX Navigation)", h:
      "<p>GPS 卫星的<b>轨道参数</b>文件(RINEX <code>.nav</code>/<code>.??n</code>),gps-sdr-sim 用它算出某时某地各卫星的信号。</p>" +
      "<p>本工具自带一份<b>样本星历</b>(2022 年),能生成信号做通路验证;但真实接收机锁定需要<b>与当前 GPS 时间匹配</b>的星历。要让被测设备真正锁到伪造位置,建议<b>上传当天的广播星历</b>(brdc 文件)。</p>" +
      "<p class='mb-0'>获取途径:各 GNSS 数据中心的 daily/brdc 目录(如 NASA CDDIS、ESA GSSC、IGS 镜像),文件名形如 <code>brdcDDD0.YYn</code>。</p>" },
    "one-shot": { t: "单次扫描 vs 连续扫描", h:
      "<p><b>不勾(默认)= 连续扫描</b>:像频谱仪一样不停地扫、实时刷新,直到你点『停止』。适合盯着看、配合峰值保持找信号。</p>" +
      "<p><b>勾选 = 单次扫描</b>:整个频段<b>扫一遍就自动停</b>,得到一张快照。适合『看一眼当前有什么』。</p>" +
      "<p>两种模式点停止都会立即结束后台的 hackrf_sweep 进程,不会残留。</p>" },
    "peak-hold": { t: "峰值保持 (Peak Hold)", h:
      "<p>打开后,图上会多出一条<b>暗黄色包络线</b>,记录每个频点<b>出现过的最强值</b>并一直保留。</p>" +
      "<p><b>作用</b>:即使某个信号只是一闪而过(比如按一下钥匙),它的峰也会被『定格』在包络线上不消失,方便你回头数清楚<b>到底有几个频点、分别在哪</b> —— 这就是找信号最实用的功能,替代瀑布图。点『清零』重新开始记录。</p>" },
    "sett-advanced": { t: "标准 / 高级设置", h:
      "<p><b>标准</b>只露最常用的几项(频率、采样率、时长)。<b>高级</b>展开全部参数(增益、带宽、放大器、偏置电源、设备等),每项旁边的 <i class='bi bi-question-circle'></i> 都有说明。</p>" },
  };

  function openHelp(key, titleOverride, bodyOverride) {
    const info = HELP[key] || {};
    const title = titleOverride || info.t || "帮助";
    const body = bodyOverride || info.h || "<p class='text-muted'>暂无说明。</p>";
    document.getElementById("helpDrawerTitle").innerHTML = '<i class="bi bi-question-circle text-primary"></i> ' + title;
    document.getElementById("helpDrawerBody").innerHTML = body;
    bootstrap.Offcanvas.getOrCreateInstance(document.getElementById("helpDrawer")).show();
  }

  // ---- global "another task is running" banner ---------------------------
  async function pollBanner() {
    const el = document.getElementById("runBanner");
    const overlay = document.getElementById("lockOverlay");
    if (!el) return;
    const cur = await currentJob();
    const owned = window.HRF_PAGE_KINDS || (window.HRF_PAGE_KIND ? [window.HRF_PAGE_KIND] : []);
    const foreign = cur && !owned.includes(cur.kind);
    if (foreign) {
      const txt = document.getElementById("runBannerText");
      if (txt) txt.innerHTML = `<b>${kindLabel(cur.kind)}</b> 任务正在运行 —— 本页已锁定,只能在其所属页面或右上角状态灯停止。`;
      el.classList.remove("d-none");
      if (overlay) {
        const lt = document.getElementById("lockText");
        if (lt) lt.textContent = `『${kindLabel(cur.kind)}』任务正在运行`;
        overlay.classList.remove("d-none");
      }
    } else {
      el.classList.add("d-none");
      if (overlay) overlay.classList.add("d-none");
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    pollBridge(); setInterval(pollBridge, 2000);
    pollBanner(); setInterval(pollBanner, 2000);
    // delegated help triggers
    document.addEventListener("click", function (e) {
      const t = e.target.closest("[data-help]");
      if (!t) return;
      e.preventDefault();
      openHelp(t.getAttribute("data-help"),
               t.getAttribute("data-help-title"), t.getAttribute("data-help-body"));
    });
  });

  return { fmtHz, fmtBytes, streamJob, post, buildTransferCmd, Waterfall,
           dbColor, getDevice, setDevice, withDevice, openHelp, HELP,
           currentJob, PowerChart, guardStart, kindLabel };
})();
