"use strict";
(() => {
  const $ = (s, root = document) => root.querySelector(s);
  const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const pad = (n) => String(n).padStart(2, "0");
  const stamp = (value) => {
    value = Math.max(0, Math.min(86399, Math.floor(value)));
    return [
      Math.floor(value / 3600),
      Math.floor((value % 3600) / 60),
      value % 60,
    ]
      .map(pad)
      .join(":");
  };
  const seconds = (text) =>
    /^([01]\d|2[0-3]):[0-5]\d:[0-5]\d$/.test(text)
      ? text.split(":").reduce((sum, v) => sum * 60 + Number(v), 0)
      : null;
  function localDate(date = new Date()) {
    const parts = new Intl.DateTimeFormat("en-CA", {
      timeZone: "Europe/Bucharest",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hourCycle: "h23",
    }).formatToParts(date);
    const part = (type) => parts.find((p) => p.type === type).value;
    return {
      date: `${part("year")}-${part("month")}-${part("day")}`,
      time: `${part("hour")}:${part("minute")}:${part("second")}`,
    };
  }
  async function api(path, method = "GET", signal) {
    const response = await fetch(path, {
      method,
      signal,
      credentials: "same-origin",
    });
    const data = await response.json();
    if (!response.ok)
      throw new Error(data.detail || "Connection lost. Please retry.");
    return data;
  }
  const icons = {
    play: '<path d="m8 4 12 8-12 8z"/>',
    pause: '<path d="M8 4v16M16 4v16"/>',
    sound:
      '<path d="M11 4 6 8H3v8h3l5 4zM15 8a6 6 0 0 1 0 8m3-11a10 10 0 0 1 0 14"/>',
    muted: '<path d="M11 4 6 8H3v8h3l5 4zM16 9l5 6m0-6-5 6"/>',
  };
  function control(button, icon, label) {
    if (icons[icon])
      button.innerHTML = `<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[icon]}</svg>`;
    button.setAttribute("aria-label", label);
    button.title = label;
  }
  async function fullscreen(stage, video) {
    try {
      if (!document.fullscreenElement) {
        if (stage.requestFullscreen) await stage.requestFullscreen();
        else if (video.webkitEnterFullscreen) video.webkitEnterFullscreen();
        try {
          await screen.orientation?.lock("landscape");
        } catch {}
      } else await document.exitFullscreen();
    } catch {}
  }
  const help = $("#shortcuts");
  $("#shortcut-help")?.addEventListener("click", () => help.showModal());
  document
    .querySelectorAll(".close-dialog")
    .forEach(
      (button) => (button.onclick = () => button.closest("dialog").close()),
    );
  for (const dialog of document.querySelectorAll("dialog"))
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog) {
        const r = dialog.getBoundingClientRect();
        if (
          event.clientX < r.left ||
          event.clientX > r.right ||
          event.clientY < r.top ||
          event.clientY > r.bottom
        )
          dialog.close();
      }
    });
  const connected = () => {
    const node = $("#connection-status");
    node.classList.toggle("offline", !navigator.onLine);
    node.replaceChildren();
    const dot = document.createElement("span");
    dot.className = "status-dot";
    node.append(
      dot,
      document.createTextNode(navigator.onLine ? " Connected" : " Offline"),
    );
  };
  addEventListener("online", connected);
  addEventListener("offline", connected);
  connected();
  let keyboard = () => {};
  document.addEventListener("keydown", (event) => {
    if (
      event.target.closest("input,select,textarea") ||
      event.ctrlKey ||
      event.metaKey ||
      event.altKey
    )
      return;
    if (document.querySelector("dialog[open]")) return;
    if (event.key === "?") {
      help.showModal();
      return;
    }
    if (event.key.toLowerCase() === "e") {
      if (!$("#events-view")) location.href = "/events";
      return;
    }
    if (event.key.toLowerCase() === "l") {
      if (!$("#live-view")) location.href = "/live";
      return;
    }
    if (event.key.toLowerCase() === "p") {
      if (!$("#nvr-playback")) $(".main-nav a[href^='/playback']")?.click();
      return;
    }
    keyboard(event);
  });
  const live = $("#live-view");
  if (live) {
    const grid = $("#live-grid"),
      selector = $("#live-camera"),
      tiles = [...grid.querySelectorAll(".live-tile")];
    let layout = 8,
      previousLayout = 8;
    try {
      layout = Number(localStorage.getItem("cctv.layout")) || 8;
      selector.value = localStorage.getItem("cctv.camera") || selector.value;
    } catch {}
    if (![1, 4, 8].includes(layout)) layout = 8;
    if (selector.selectedIndex < 0) selector.selectedIndex = 0;
    const requestedCamera = new URL(location.href).searchParams.get("cam");
    if (tiles.some((tile) => tile.dataset.camera === requestedCamera)) {
      previousLayout = layout;
      selector.value = requestedCamera;
      layout = 1;
    }
    const visible = (tile) =>
      !document.hidden && !tile.hidden && tile.intersecting;
    function state(tile, name, message = "") {
      const badge = $(".live-state", tile),
        overlay = $(".video-overlay", tile);
      badge.dataset.state = name;
      badge.textContent =
        name === "live"
          ? "Live"
          : name === "offline"
            ? "Offline"
            : name === "reconnecting"
              ? `Reconnecting (${tile.attempts})`
              : name === "idle"
                ? "Paused"
                : "Connecting…";
      $(".tile-message", tile).textContent = message;
      overlay.hidden = name === "live";
      overlay.classList.toggle("error", name === "offline");
      overlay.classList.toggle("idle", name === "idle");
      $(".retry-live", tile).hidden = name !== "offline";
    }
    function release(tile) {
      tile.serial = (tile.serial || 0) + 1;
      clearTimeout(tile.timer);
      tile.timer = null;
      tile.hls?.destroy();
      tile.hls = null;
      const lease = tile.lease;
      tile.lease = null;
      const video = $("video", tile);
      video.pause();
      video.removeAttribute("src");
      video.load();
      if (lease)
        fetch(`/api/live/${tile.dataset.camera}/sessions/${lease}`, {
          method: "DELETE",
          keepalive: true,
        }).catch(() => {});
    }
    function suspend(tile) {
      tile.want = false;
      release(tile);
      if (!tile.offline)
        state(tile, "idle", "Stream paused while out of view.");
    }
    function retry(tile, message) {
      if (!tile.want) return;
      release(tile);
      tile.attempts = (tile.attempts || 0) + 1;
      if (tile.attempts > 5) {
        tile.offline = true;
        state(
          tile,
          "offline",
          `${message} ${tile.lastSeen ? "Last seen " + localDate(new Date(tile.lastSeen)).time + "." : ""}`,
        );
        return;
      }
      state(tile, "reconnecting", message);
      tile.timer = setTimeout(
        () => start(tile),
        Math.min(30000, 1000 * 2 ** (tile.attempts - 1)),
      );
    }
    async function start(tile) {
      if (!tile.want || tile.pending || tile.lease || tile.offline) return;
      tile.pending = true;
      tile.timer = null;
      const serial = tile.serial || 0;
      state(
        tile,
        tile.attempts ? "reconnecting" : "connecting",
        tile.attempts ? "Reconnecting…" : "Connecting…",
      );
      try {
        const data = await api(
          `/api/live/${tile.dataset.camera}/sessions`,
          "POST",
        );
        if (!tile.want || serial !== (tile.serial || 0)) {
          fetch(`/api/live/${tile.dataset.camera}/sessions/${data.lease}`, {
            method: "DELETE",
          }).catch(() => {});
          return;
        }
        tile.lease = data.lease;
        await heartbeat(tile);
      } catch (error) {
        if (serial === (tile.serial || 0)) retry(tile, error.message);
      } finally {
        tile.pending = false;
        if (
          tile.want &&
          visible(tile) &&
          !tile.lease &&
          !tile.timer &&
          !tile.offline
        ) {
          tile.timer = setTimeout(() => start(tile), 100);
        }
      }
    }
    async function heartbeat(tile) {
      if (!tile.want || !tile.lease) return;
      const lease = tile.lease,
        serial = tile.serial || 0;
      try {
        const data = await api(
          `/api/live/${tile.dataset.camera}/sessions/${lease}`,
          "POST",
        );
        if (!tile.want || tile.lease !== lease || serial !== (tile.serial || 0))
          return;
        if (data.state === "error") {
          retry(tile, data.message || "Camera unavailable.");
          return;
        }
        const video = $("video", tile);
        if (data.state === "live" && !tile.hls && !video.getAttribute("src")) {
          if (video.canPlayType("application/vnd.apple.mpegurl")) {
            video.src = data.media_url;
            video
              .play()
              .catch(() => state(tile, "connecting", "Tap to start video."));
          } else if (window.Hls && Hls.isSupported()) {
            const hls = (tile.hls = new Hls({
              enableWorker: false,
              liveSyncDurationCount: 2,
              liveMaxLatencyDurationCount: 5,
              maxBufferLength: 6,
            }));
            hls.loadSource(data.media_url);
            hls.attachMedia(video);
            hls.on(Hls.Events.MANIFEST_PARSED, () => {
              if (tile.lease === lease) video.play().catch(() => {});
            });
            hls.on(Hls.Events.ERROR, (_, error) => {
              if (
                error.fatal &&
                tile.lease === lease &&
                serial === (tile.serial || 0)
              )
                retry(tile, "Connection lost.");
            });
          } else {
            tile.offline = true;
            release(tile);
            state(
              tile,
              "offline",
              "Live video is unavailable in this browser.",
            );
            return;
          }
        }
        tile.timer = setTimeout(
          () => heartbeat(tile),
          data.state === "starting" ? 500 : 8000,
        );
      } catch (error) {
        if (tile.lease === lease && serial === (tile.serial || 0))
          retry(tile, error.message);
      }
    }
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          const tile = entry.target;
          tile.intersecting = entry.isIntersecting;
          if (visible(tile) && !tile.want) {
            tile.want = true;
            tile.timer = setTimeout(
              () => start(tile),
              tiles.filter((item) => !item.hidden).indexOf(tile) * 350,
            );
          } else if (!visible(tile) && tile.want) suspend(tile);
        }
      },
      { threshold: 0.01 },
    );
    function arrange() {
      const focus = tiles.findIndex(
        (tile) => tile.dataset.camera === selector.value,
      );
      tiles.forEach((tile, i) => {
        tile.hidden = !(layout === 8 || (i - focus + 8) % 8 < layout);
        if (tile.hidden) suspend(tile);
        observer.unobserve(tile);
        if (!tile.hidden) observer.observe(tile);
      });
      grid.dataset.layout = String(layout);
      document
        .querySelectorAll("button[data-layout]")
        .forEach((button) =>
          button.setAttribute(
            "aria-pressed",
            String(Number(button.dataset.layout) === layout),
          ),
        );
      $("#live-camera-label").hidden = layout !== 1;
      $("#single-navigation").hidden = layout !== 1;
      $("#single-camera-name").textContent = selector.selectedOptions[0].text;
      try {
        localStorage.setItem("cctv.layout", String(layout));
        localStorage.setItem("cctv.camera", selector.value);
      } catch {}
    }
    function expand(tile) {
      if (layout !== 1) previousLayout = layout;
      selector.value = tile.dataset.camera;
      layout = 1;
      history.pushState({ single: true }, "");
      arrange();
    }
    function returnGrid() {
      layout = previousLayout === 1 ? 8 : previousLayout;
      arrange();
    }
    function move(amount) {
      selector.selectedIndex = (selector.selectedIndex + amount + 8) % 8;
      arrange();
    }
    $("#live-previous").onclick = () => move(-1);
    $("#live-next").onclick = () => move(1);
    $("#return-grid").onclick = returnGrid;
    addEventListener("popstate", returnGrid);
    for (const tile of tiles) {
      tile.want = false;
      tile.attempts = 0;
      const video = $("video", tile),
        stage = $(".video-stage", tile);
      observer.observe(tile);
      video.addEventListener("playing", () => {
        if (tile.want) {
          tile.attempts = 0;
          tile.lastSeen = Date.now();
          state(tile, "live");
        }
      });
      video.addEventListener("timeupdate", () => {
        tile.lastSeen = Date.now();
      });
      video.addEventListener("error", () => {
        if (tile.want && tile.lease && !tile.hls)
          retry(tile, "Video connection lost.");
      });
      $(".retry-live", tile).onclick = (event) => {
        event.stopPropagation();
        tile.offline = false;
        tile.attempts = 0;
        tile.want = visible(tile);
        release(tile);
        start(tile);
      };
      $(".sound", tile).onclick = () => {
        const enable = video.muted;
        for (const other of tiles) {
          $("video", other).muted = true;
          control($(".sound", other), "muted", "Enable sound");
        }
        video.muted = !enable;
        control(
          $(".sound", tile),
          video.muted ? "muted" : "sound",
          video.muted ? "Enable sound" : "Mute sound",
        );
      };
      function playbackLink() {
        const moment = localDate(new Date(Date.now() - 60000));
        $(".history", tile).href =
          `/playback?cam=${tile.dataset.camera}&date=${moment.date}&t=${moment.time}&fallback=latest`;
      }
      playbackLink();
      $(".history", tile).addEventListener("click", playbackLink);
      $(".expand", tile).onclick = () => expand(tile);
      $(".fullscreen", tile).onclick = () => fullscreen(stage, video);
      let tap = 0,
        startX = 0,
        startY = 0;
      tile.addEventListener("pointerdown", (e) => {
        startX = e.clientX;
        startY = e.clientY;
      });
      tile.addEventListener("pointerup", (e) => {
        if (e.target.closest("button,a")) return;
        const dx = e.clientX - startX,
          dy = e.clientY - startY;
        if (Math.abs(dx) > 60 && Math.abs(dy) < 50 && layout === 1) {
          move(dx > 0 ? -1 : 1);
          return;
        }
        if (Math.hypot(dx, dy) > 12) return;
        if (performance.now() - tap < 320) {
          fullscreen(stage, video);
          tap = 0;
        } else {
          tap = performance.now();
          if (layout !== 1) expand(tile);
          else if (video.paused && tile.lease) video.play().catch(() => {});
        }
      });
      stage.addEventListener("keydown", (e) => {
        if (e.target !== stage) return;
        if (["Enter", " "].includes(e.key)) {
          e.preventDefault();
          expand(tile);
        }
      });
    }
    document.querySelectorAll("button[data-layout]").forEach(
      (button) =>
        (button.onclick = () => {
          layout = Number(button.dataset.layout);
          arrange();
        }),
    );
    const playbackNavigation = $(".main-nav a[href^='/playback']");
    playbackNavigation.onclick = () => {
      const moment = localDate(new Date(Date.now() - 60000));
      playbackNavigation.href = `/playback?cam=${selector.value}&date=${moment.date}&t=${moment.time}&fallback=latest`;
    };
    selector.onchange = arrange;
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) tiles.forEach(suspend);
      else {
        for (const tile of tiles)
          if (visible(tile)) {
            tile.want = true;
            start(tile);
          }
      }
    });
    addEventListener("pagehide", () => tiles.forEach(suspend));
    keyboard = (event) => {
      if (/^[1-8]$/.test(event.key)) {
        selector.selectedIndex = Number(event.key) - 1;
        layout = 1;
        arrange();
      } else if (event.key === "Escape") returnGrid();
      else if (event.key === "ArrowLeft" && layout === 1) move(-1);
      else if (event.key === "ArrowRight" && layout === 1) move(1);
      else if (event.key.toLowerCase() === "f") {
        const tile = tiles.find((t) => !t.hidden);
        fullscreen($(".video-stage", tile), $("video", tile));
      } else if (event.key.toLowerCase() === "m")
        $(
          ".sound",
          tiles.find((t) => !t.hidden),
        ).click();
    };
    arrange();
  }
  const workspace = $("#nvr-playback");
  if (workspace) {
    let video = $("#nvr-video");
    const stage = $("#playback-stage"),
      canvas = $("#timeline"),
      date = $("#nvr-date"),
      time = $("#nvr-time"),
      camera = $("#playback-camera"),
      status = $("#nvr-status"),
      clock = $("#cctv-clock"),
      download = $("#original-download"),
      overlay = $("#playback-overlay");
    let intervals = [],
      personEvents = [],
      segments = [],
      selected = seconds(workspace.dataset.time) ?? 0,
      current = null,
      standby = null,
      intent = 0,
      controller = null,
      range = workspace.dataset.rangeStart
        ? {
            start: seconds(workspace.dataset.rangeStart),
            end: seconds(workspace.dataset.rangeEnd),
          }
        : null,
      rangePlaying = !!range,
      timeDirty = false,
      loaded = false,
      calendarDays = [],
      calendarMonth = date.value.slice(0, 7),
      prefetchTask = null,
      prefetchAfter = 0,
      controlTimer = null;
    const zoomSpans = [86400, 43200, 21600, 7200, 3600, 1800, 600, 300, 60];
    let viewStart = 0,
      viewSpan = 86400,
      drag = null,
      panMode = false;
    const zoomLabel = $("#timeline-scale"),
      windowLabel = $("#timeline-window"),
      hover = $("#timeline-hover");
    const clampView = (start) => Math.max(0, Math.min(86400 - viewSpan, start));
    const xTime = (x) =>
      viewStart +
      Math.max(
        0,
        Math.min(
          1,
          (x - canvas.getBoundingClientRect().left) / canvas.clientWidth,
        ),
      ) *
        viewSpan;
    function show(message = "", error = false) {
      status.textContent = message;
      overlay.hidden = !message;
      overlay.classList.toggle("error", error);
      overlay.classList.toggle("idle", error);
      $("#retry-playback-stream").hidden = !error;
    }
    function setView(span, anchor = selected, fraction = 0.5) {
      viewSpan = Math.max(60, Math.min(86400, span));
      viewStart = clampView(anchor - fraction * viewSpan);
      draw();
    }
    function zoom(direction, anchor = null, fraction = 0.5) {
      if (anchor === null)
        anchor =
          selected >= viewStart && selected <= viewStart + viewSpan
            ? selected
            : viewStart + viewSpan / 2;
      const next =
        direction > 0
          ? zoomSpans.find((span) => span < viewSpan - 0.1)
          : [...zoomSpans].reverse().find((span) => span > viewSpan + 0.1);
      if (next) setView(next, anchor, fraction);
    }
    function centerSelection() {
      viewStart = clampView(selected - viewSpan / 2);
      draw();
    }
    function ensureSelection() {
      if (selected < viewStart || selected > viewStart + viewSpan)
        centerSelection();
    }
    function draw() {
      controls();
      const width = canvas.clientWidth,
        height = canvas.clientHeight,
        ratio = devicePixelRatio || 1;
      if (!width) return;
      canvas.width = width * ratio;
      canvas.height = height * ratio;
      const theme = getComputedStyle(document.documentElement),
        colors = Object.fromEntries(
          [
            "bg",
            "border",
            "success",
            "warning",
            "danger",
            "text",
            "surface-2",
            "selection",
            "selection-tint",
          ].map((name) => [name, theme.getPropertyValue("--" + name).trim()]),
        );
      const ctx = canvas.getContext("2d");
      ctx.scale(ratio, ratio);
      ctx.fillStyle = colors["bg"];
      ctx.fillRect(0, 0, width, height);
      const position = (value) => ((value - viewStart) / viewSpan) * width;
      const step =
        [1, 5, 10, 15, 30, 60, 300, 900, 1800, 3600, 10800, 21600].find(
          (value) => value >= viewSpan / Math.max(2, width / 55),
        ) || 21600;
      ctx.strokeStyle = colors["border"];
      for (
        let t = Math.ceil(viewStart / step) * step;
        t <= viewStart + viewSpan;
        t += step
      ) {
        const x = position(t);
        ctx.beginPath();
        ctx.moveTo(x, 25);
        ctx.lineTo(x, height);
        ctx.stroke();
      }
      for (const item of intervals) {
        const left = Math.max(viewStart, item.start),
          right = Math.min(viewStart + viewSpan, item.end);
        if (right <= left) continue;
        ctx.fillStyle = colors["success"];
        ctx.fillRect(
          position(left),
          36,
          Math.max(1, position(right) - position(left)),
          32,
        );
      }
      for (const item of personEvents) {
        const left = Math.max(viewStart, item.start),
          right = Math.min(viewStart + viewSpan, item.end);
        if (right > left) {
          ctx.fillStyle = colors["warning"];
          ctx.fillRect(
            position(left),
            72,
            Math.max(3, position(right) - position(left)),
            12,
          );
        }
      }
      if (range) {
        const l = position(range.start),
          r = position(range.end);
        ctx.fillStyle = colors["selection-tint"];
        ctx.fillRect(l, 28, r - l, height - 28);
        ctx.fillStyle = colors["selection"];
        for (const x of [l, r])
          if (x >= 0 && x <= width) {
            ctx.fillRect(x - 3, 27, 6, height - 27);
            ctx.fillRect(x - 8, height - 22, 16, 20);
          }
      }
      const now = localDate();
      if (date.value === now.date) {
        const x = position(seconds(now.time));
        if (x >= 0 && x <= width) {
          ctx.strokeStyle = colors["danger"];
          ctx.setLineDash([3, 3]);
          ctx.beginPath();
          ctx.moveTo(x, 25);
          ctx.lineTo(x, height);
          ctx.stroke();
          ctx.setLineDash([]);
        }
      }
      if (selected >= viewStart && selected <= viewStart + viewSpan) {
        const x = position(selected);
        ctx.fillStyle = colors["text"];
        ctx.fillRect(x - 1, 26, 2, height - 26);
        ctx.font = "11px ui-monospace,monospace";
        const label = stamp(selected),
          w = ctx.measureText(label).width + 12,
          l = Math.max(0, Math.min(width - w, x - w / 2));
        ctx.fillStyle = colors["surface-2"];
        ctx.fillRect(l, 1, w, 22);
        ctx.fillStyle = colors["text"];
        ctx.fillText(label, l + 6, 16);
      }
      const labels = [...$(".timeline-labels", workspace).children];
      labels.forEach((label, i) => {
        const v = viewStart + (viewSpan * i) / (labels.length - 1);
        label.textContent =
          v >= 86400
            ? "24:00"
            : viewSpan <= 600
              ? stamp(v)
              : stamp(v).slice(0, 5);
      });
      zoomLabel.textContent =
        viewSpan % 3600 === 0
          ? `${viewSpan / 3600}h`
          : viewSpan % 60 === 0
            ? `${viewSpan / 60}m`
            : `${Math.round(viewSpan)}s`;
      windowLabel.textContent = `${stamp(viewStart)} – ${viewStart + viewSpan >= 86400 ? "24:00:00" : stamp(viewStart + viewSpan)}`;
      $("#timeline-zoom-in").disabled = viewSpan <= 60;
      $("#timeline-zoom-out").disabled = viewSpan >= 86400;
      $("#timeline-earlier").disabled = viewStart <= 0;
      $("#timeline-later").disabled = viewStart + viewSpan >= 86400;
      canvas.setAttribute("aria-valuenow", String(Math.floor(selected)));
      canvas.setAttribute("aria-valuetext", stamp(selected));
      clock.textContent = loaded ? stamp(selected) : "--:--:--";
      if (!timeDirty && document.activeElement !== time)
        time.value = loaded ? stamp(selected) : "";
    }
    function updateURL() {
      const url = new URL(location.href);
      url.searchParams.set("cam", camera.value);
      url.searchParams.set("date", date.value);
      if (loaded) url.searchParams.set("t", stamp(selected));
      url.searchParams.delete("time");
      if (loaded) url.searchParams.delete("fallback");
      if (rangePlaying && range) {
        url.searchParams.set("start", stamp(range.start));
        url.searchParams.set("end", stamp(range.end));
      } else {
        url.searchParams.delete("start");
        url.searchParams.delete("end");
      }
      history.replaceState(null, "", url);
      $("#legacy-archive").href = `/camera/${camera.value}?date=${date.value}`;
    }
    async function coverage() {
      const cam = camera.value,
        day = date.value;
      try {
        const data = await api(
          `/api/timeline/${cam}?date=${day}&focus=${stamp(viewSpan < 86400 ? viewStart + viewSpan / 2 : selected)}`,
        );
        if (cam !== camera.value || day !== date.value) return;
        intervals = [];
        for (const item of data.intervals) {
          const last = intervals.at(-1);
          if (last && item.start - last.end < 2) {
            last.end = Math.max(last.end, item.end);
          } else intervals.push({ ...item });
        }
        segments = data.segments || [];
        personEvents = data.person_events || [];
        $("#person-events-note").hidden = !personEvents.length;
        const analyzed = data.person_analysis_intervals || [],
          total = intervals.reduce(
            (sum, item) => sum + item.end - item.start,
            0,
          ),
          scanned = intervals.reduce(
            (sum, item) =>
              sum +
              analyzed.reduce(
                (n, done) =>
                  n +
                  Math.max(
                    0,
                    Math.min(item.end, done.end) -
                      Math.max(item.start, done.start),
                  ),
                0,
              ),
            0,
          );
        $("#coverage-note").textContent =
          data.person_events_source === "frigate"
            ? `Frigate person detection${data.frigate_connected ? "" : " · offline"}`
            : data.person_events_source === "server" && total
              ? `Person analysis: ${scanned > 0 && scanned / total < 0.01 ? "<1" : Math.round((scanned / total) * 100)}%`
              : !intervals.length
                ? "No recordings on this date."
                : "";
        draw();
        return data;
      } catch (error) {
        show(error.message, true);
      }
    }
    async function release(session) {
      if (!session) return;
      clearTimeout(session.timer);
      session.hls?.destroy();
      session.video.pause();
      session.video.removeAttribute("src");
      session.video.load();
      if (session !== current) session.video.remove();
      await fetch(`/api/instant/${session.token}`, {
        method: "DELETE",
        keepalive: true,
      }).catch(() => {});
    }
    async function heartbeat(session) {
      if (session.released) return;
      try {
        const data = await api(`/api/instant/${session.token}`, "POST");
        if (data.state === "failed") throw new Error(data.message);
      } catch (error) {
        if (session === current) show(error.message, true);
        return;
      }
      if (!session.released)
        session.timer = setTimeout(() => heartbeat(session), 15000);
    }
    function drop(session) {
      if (session) session.released = true;
      return release(session);
    }
    async function prepare(
      data,
      element,
      myIntent,
      signal,
      active,
      autoplay = true,
    ) {
      const session = {
        ...data,
        token: data.stream_id,
        autoplay,
        video: element,
        hls: null,
        timer: null,
        cam: camera.value,
        date: data.date || date.value,
      };
      if (active) current = session;
      else standby = session;
      try {
        while (data.state === "starting") {
          await delay(250);
          if (myIntent !== intent)
            throw new DOMException("Obsolete selection", "AbortError");
          data = {
            ...data,
            ...(await api(`/api/instant/${session.token}`, "POST", signal)),
          };
        }
        if (data.state !== "ready")
          throw new Error(data.message || "Playback unavailable");
        if (myIntent !== intent)
          throw new DOMException("Obsolete selection", "AbortError");
        Object.assign(session, data);
        element.muted = video.muted;
        element.volume = video.volume;
        element.playbackRate = Number($("#playback-speed").value);
        if (active) prefetch();
        // EVENT playlists initially contain only a few seconds. Do not let a player
        // clamp a requested start position to zero before that position is encoded.
        const deadline = performance.now() + 60000;
        while ((session.offset || 0) > 0) {
          if (myIntent !== intent)
            throw new DOMException("Obsolete selection", "AbortError");
          const response = await fetch(session.media_url, {
            signal,
            credentials: "same-origin",
          });
          if (!response.ok)
            throw new Error("Playback session expired. Please retry.");
          const playlist = await response.text(),
            available = [...playlist.matchAll(/#EXTINF:([0-9.]+)/g)].reduce(
              (sum, item) => sum + Number(item[1]),
              0,
            );
          if (
            available > (session.offset || 0) + 0.05 ||
            playlist.includes("#EXT-X-ENDLIST")
          )
            break;
          if (performance.now() > deadline)
            throw new Error(
              "Playback is taking longer than expected. Please retry.",
            );
          await delay(200);
        }
        if (window.Hls && Hls.isSupported()) {
          const hls = (session.hls = new Hls({
            enableWorker: false,
            liveSyncDuration: 2400,
            liveMaxLatencyDuration: 4800,
            startPosition: 0,
            maxBufferLength: 60,
            backBufferLength: 120,
            maxMaxBufferLength: 2400,
          }));
          hls.loadSource(session.media_url);
          hls.attachMedia(element);
          hls.on(Hls.Events.ERROR, (_, error) => {
            if (error.fatal && session === current)
              show("Playback connection lost. Retry to reconnect.", true);
          });
        } else if (element.canPlayType("application/vnd.apple.mpegurl")) {
          element.src = session.media_url;
          element.load();
        } else throw new Error("Playback is unavailable in this browser.");
        heartbeat(session);
        const mediaDeadline = performance.now() + 45000;
        const offset = session.offset || 0;
        while (true) {
          if (myIntent !== intent || session.released)
            throw new DOMException("Obsolete selection", "AbortError");
          const available = session.hls ? element.buffered : element.seekable;
          let ready = false;
          for (let i = 0; i < available.length; i++) {
            if (
              offset >= available.start(i) &&
              offset < available.end(i) - 0.02
            )
              ready = true;
          }
          if (element.readyState >= 1 && ready) break;
          if (performance.now() > mediaDeadline)
            throw new Error("Unable to buffer playback. Please retry.");
          await delay(50);
        }
        element.currentTime = offset;
        session.ready = true;
        if (active && session.autoplay && session === current)
          element.play().catch(() => show("Press Play to watch.", true));
        return session;
      } catch (error) {
        if (active && current === session) current = null;
        if (!active && standby === session) standby = null;
        await drop(session);
        throw error;
      }
    }
    function buffered(target, session = current) {
      if (
        !session ||
        session.cam !== camera.value ||
        session.date !== date.value
      )
        return false;
      const offset = target - session.segment_start;
      for (let i = 0; i < video.buffered.length; i++)
        if (
          offset >= video.buffered.start(i) &&
          offset < video.buffered.end(i) - 0.05
        )
          return true;
      return false;
    }
    async function seek(value, { resume = true } = {}) {
      if (current) $("#playback-notice").hidden = true;
      if (value === null || !Number.isFinite(value)) {
        show("Use a 24-hour time: HH:MM:SS.", true);
        return;
      }
      timeDirty = false;
      if (rangePlaying && range)
        value = Math.max(range.start, Math.min(range.end, value));
      const target = Math.max(
        0,
        Math.min(86399, resume ? Math.round(value) : value),
      );
      if (buffered(target)) {
        video.currentTime = target - current.segment_start;
        selected = target;
        loaded = true;
        ensureSelection();
        draw();
        updateURL();
        show();
        if (resume) video.play().catch(() => {});
        return;
      }
      const sound = { muted: video.muted, volume: video.volume },
        cam = camera.value,
        day = date.value,
        myIntent = ++intent;
      controller?.abort();
      controller = new AbortController();
      const signal = controller.signal;
      const old = current,
        next = standby;
      current = null;
      standby = null;
      prefetchTask = null;
      video.pause();
      selected = target;
      loaded = false;
      ensureSelection();
      draw();
      show("Loading recording…");
      await Promise.all([drop(old), drop(next)]);
      if (myIntent !== intent) return;
      video = $("#nvr-video") || video;
      if (!video.isConnected) {
        video = document.createElement("video");
        video.id = "nvr-video";
        video.playsInline = true;
        video.muted = true;
        stage.prepend(video);
        bindVideo(video);
      }
      video.hidden = false;
      video.muted = sound.muted;
      video.volume = sound.volume;
      download.hidden = true;
      try {
        const data = await api(
          `/api/timeline/${cam}/seek?date=${day}&time=${stamp(target)}&stream=true&window=true&window_seconds=${matchMedia("(max-width:700px)").matches ? 600 : 1200}`,
          "POST",
        );
        if (myIntent !== intent) {
          fetch(`/api/instant/${data.stream_id}`, { method: "DELETE" }).catch(
            () => {},
          );
          return;
        }
        await prepare(data, video, myIntent, signal, true, resume);
        loaded = true;
        download.href = data.download_url;
        download.hidden = false;
        download.title = `Download ${data.recording}`;
        $("#recording-description").textContent =
          `${camera.selectedOptions[0].text} · ${day} · ${data.recording}. Continuous playback window: ${stamp(data.segment_start)}–${stamp(data.window_end)}.`;
        updateURL();
        draw();
        if (!resume) {
          video.pause();
          show();
          controls();
        }
        coverage();
      } catch (error) {
        if (error.name !== "AbortError" && myIntent === intent)
          show(error.message, true);
      }
    }
    function syncRange() {
      if (!range) return;
      $("#range-start").value = stamp(range.start);
      $("#range-end").value = stamp(range.end);
      $("#timeframe-note").textContent =
        `${stamp(range.start)}–${stamp(range.end)} · ${Math.round(range.end - range.start)} seconds`;
      $("#export-download").href =
        `/api/timeline/${camera.value}/export?date=${date.value}&start=${stamp(range.start)}&end=${stamp(range.end)}`;
    }
    function endRange() {
      if (rangePlaying && range && current && selected >= range.end - 0.05) {
        video.pause();
        video.currentTime = Math.max(0, range.end - current.segment_start);
        selected = range.end;
        draw();
        show("End of selected range.", true);
        return true;
      }
      return false;
    }
    function controls() {
      for (const id of ["play-pause", "skip-back", "skip-forward", "snapshot"])
        $("#" + id).disabled = !loaded || !current?.ready;
      control(
        $("#play-pause"),
        video.paused ? "play" : "pause",
        video.paused ? "Play" : "Pause",
      );
      control(
        $("#nvr-sound"),
        video.muted ? "muted" : "sound",
        video.muted ? "Enable sound" : "Mute sound",
      );
    }
    function nextPoint() {
      if (!current) return null;
      return segments.find(
        (item) =>
          item.start >= current.window_end - 2 &&
          item.recording !== current.recording,
      );
    }
    async function prefetch() {
      if (
        !current ||
        standby ||
        prefetchTask ||
        performance.now() < prefetchAfter ||
        current.window_end - selected > 25 ||
        (rangePlaying && range && range.end <= current.window_end + 0.05)
      )
        return;
      const myIntent = intent,
        cam = camera.value,
        active = current;
      prefetchTask = (async () => {
        let day = date.value,
          item = nextPoint(),
          point = item ? Math.max(item.start, current.window_end) : null;
        if (!item && current.window_end >= 86397 && day < "9999-12-31") {
          const nextDay = new Date(day + "T12:00:00Z");
          nextDay.setUTCDate(nextDay.getUTCDate() + 1);
          day = nextDay.toISOString().slice(0, 10);
          const data = await api(`/api/timeline/${cam}?date=${day}`);
          item = data.segments?.find((row) => row.start <= 2 && row.end > 0);
          point = item ? Math.max(0, item.start) : null;
        }
        if (
          !item ||
          (rangePlaying && range && point >= range.end) ||
          myIntent !== intent ||
          current !== active
        ) {
          prefetchAfter = performance.now() + 5000;
          return;
        }
        const data = await api(
          `/api/timeline/${cam}/seek?date=${day}&time=${stamp(point)}&stream=true&window=true&prefetch=true&window_seconds=${matchMedia("(max-width:700px)").matches ? 600 : 1200}`,
          "POST",
        );
        if (data.state === "deferred") {
          if (myIntent === intent) prefetchAfter = performance.now() + 5000;
          return;
        }
        if (myIntent !== intent) {
          fetch(`/api/instant/${data.stream_id}`, { method: "DELETE" }).catch(
            () => {},
          );
          return;
        }
        const element = document.createElement("video");
        element.hidden = true;
        element.playsInline = true;
        element.muted = video.muted;
        stage.prepend(element);
        bindVideo(element);
        await prepare(
          { ...data, date: day },
          element,
          myIntent,
          controller.signal,
          false,
        );
      })()
        .catch(() => {
          prefetchAfter = performance.now() + 5000;
        })
        .finally(() => {
          if (myIntent === intent) prefetchTask = null;
        });
    }
    async function advance() {
      if (!current || endRange()) return;
      const old = current,
        myIntent = intent;
      if (prefetchTask) await prefetchTask;
      if (myIntent !== intent) return;
      if (standby) {
        const ready = standby;
        standby = null;
        const wasMuted = video.muted,
          volume = video.volume;
        video.hidden = true;
        video.removeAttribute("id");
        ready.video.id = "nvr-video";
        ready.video.hidden = false;
        video = ready.video;
        current = ready;
        if (date.value !== ready.date) {
          date.value = ready.date;
          calendarMonth = date.value.slice(0, 7);
          coverage();
          refreshCalendar();
        }
        video.muted = wasMuted;
        video.volume = volume;
        selected = ready.segment_start + (ready.offset || 0);
        video.play().catch(() => {});
        drop(old);
        download.href = ready.download_url;
        download.title = `Download ${ready.recording}`;
        show();
        updateURL();
        draw();
        return;
      }
      const item = nextPoint();
      if (item) {
        seek(Math.max(item.start, current.window_end));
        return;
      }
      if (date.value < "9999-12-31") {
        const nextDate = new Date(date.value + "T12:00:00Z");
        nextDate.setUTCDate(nextDate.getUTCDate() + 1);
        const day = nextDate.toISOString().slice(0, 10);
        if (current.window_end >= 86397) {
          const data = await api(`/api/timeline/${camera.value}?date=${day}`);
          if (myIntent !== intent) return;
          if (data.intervals.length && data.intervals[0].start < 3) {
            date.value = day;
            await coverage();
            seek(data.intervals[0].start);
            return;
          }
        }
      }
      show(
        "End of available recordings. Jump to latest or choose another time.",
        true,
      );
    }
    function bindVideo(element) {
      element.addEventListener("playing", () => {
        if (element === video) {
          show();
          controls();
        }
      });
      element.addEventListener("pause", () => {
        if (element === video) controls();
      });
      element.addEventListener("timeupdate", () => {
        if (
          element !== video ||
          !current ||
          !current.ready ||
          drag?.type === "scrub"
        )
          return;
        selected = Math.min(86399, current.segment_start + element.currentTime);
        loaded = true;
        draw();
        endRange();
        prefetch();
        const row = current.segments?.find(
          (item, i) =>
            selected >= item.start &&
            selected < (current.segments[i + 1]?.start || current.window_end),
        );
        if (row) {
          download.href = `/download/${camera.value}/${row.recording}`;
          download.title = `Download ${row.recording}`;
        }
        updateURL();
      });
      element.addEventListener("ended", () => {
        if (element === video)
          advance().catch((error) => show(error.message, true));
      });
      element.addEventListener("error", () => {
        if (element === video && element.getAttribute("src"))
          show("Playback unavailable. Please retry.", true);
      });
    }
    bindVideo(video);
    $("#timeline-zoom-in").onclick = () => zoom(1);
    $("#timeline-zoom-out").onclick = () => zoom(-1);
    $("#timeline-full-day").onclick = () => setView(86400);
    $("#timeline-center").onclick = centerSelection;
    $("#timeline-earlier").onclick = () => {
      viewStart = clampView(viewStart - viewSpan / 2);
      draw();
    };
    $("#timeline-later").onclick = () => {
      viewStart = clampView(viewStart + viewSpan / 2);
      draw();
    };
    $("#timeline-mode").onclick = () => {
      panMode = !panMode;
      canvas.classList.toggle("pan-mode", panMode);
      $("#timeline-mode").setAttribute("aria-pressed", String(panMode));
      $("#timeline-mode").setAttribute(
        "aria-label",
        panMode ? "Drag to scrub" : "Drag to pan",
      );
    };
    canvas.addEventListener(
      "wheel",
      (event) => {
        event.preventDefault();
        const fraction =
          (event.clientX - canvas.getBoundingClientRect().left) /
          canvas.clientWidth;
        zoom(event.deltaY < 0 ? 1 : -1, xTime(event.clientX), fraction);
      },
      { passive: false },
    );
    const pointers = new Map();
    canvas.addEventListener("pointerdown", (event) => {
      if (event.button !== 0) return;
      canvas.setPointerCapture(event.pointerId);
      pointers.set(event.pointerId, event.clientX);
      if (pointers.size === 2) {
        const xs = [...pointers.values()];
        drag = {
          type: "pinch",
          distance: Math.max(1, Math.abs(xs[1] - xs[0])),
          span: viewSpan,
          anchor: xTime((xs[0] + xs[1]) / 2),
        };
        return;
      }
      const x = ((selected - viewStart) / viewSpan) * canvas.clientWidth,
        local = event.clientX - canvas.getBoundingClientRect().left;
      let handle = null;
      if (range) {
        const left =
            ((range.start - viewStart) / viewSpan) * canvas.clientWidth,
          right = ((range.end - viewStart) / viewSpan) * canvas.clientWidth;
        if (Math.abs(local - left) < 22) handle = "start";
        else if (Math.abs(local - right) < 22) handle = "end";
      }
      drag = {
        type: handle
          ? "handle"
          : panMode ||
              event.shiftKey ||
              (event.pointerType === "touch" && Math.abs(x - local) > 18)
            ? "pan"
            : "scrub",
        handle,
        x: event.clientX,
        start: viewStart,
        moved: false,
        target: xTime(event.clientX),
        wasPaused: video.paused,
      };
    });
    canvas.addEventListener("pointermove", (event) => {
      const point = xTime(event.clientX);
      hover.textContent =
        stamp(point) +
        (personEvents.some((item) => item.start <= point && point < item.end)
          ? " · Person"
          : "");
      if (!pointers.has(event.pointerId)) return;
      pointers.set(event.pointerId, event.clientX);
      if (drag?.type === "pinch" && pointers.size === 2) {
        const xs = [...pointers.values()],
          middle = (xs[0] + xs[1]) / 2;
        setView(
          (drag.span * drag.distance) / Math.max(1, Math.abs(xs[1] - xs[0])),
          drag.anchor,
          (middle - canvas.getBoundingClientRect().left) / canvas.clientWidth,
        );
        return;
      }
      if (!drag) return;
      const delta = event.clientX - drag.x;
      if (Math.abs(delta) > 5) drag.moved = true;
      if (drag.type === "pan" && drag.moved) {
        viewStart = clampView(
          drag.start - (delta / canvas.clientWidth) * viewSpan,
        );
        draw();
      } else if (drag.type === "handle") {
        if (drag.handle === "start")
          range.start = Math.max(0, Math.min(range.end - 1, Math.round(point)));
        else
          range.end = Math.min(
            86399,
            Math.max(range.start + 1, Math.round(point)),
          );
        if (range.end - range.start > 900) {
          if (drag.handle === "start") range.start = range.end - 900;
          else range.end = range.start + 900;
        }
        syncRange();
        draw();
      } else if (drag.type === "scrub" && drag.moved) {
        drag.target = point;
        selected = Math.round(point);
        if (buffered(selected))
          video.currentTime = selected - current.segment_start;
        draw();
      }
    });
    function pointerEnd(event) {
      pointers.delete(event.pointerId);
      if (pointers.size) return;
      const ended = drag;
      drag = null;
      if (event.type === "pointercancel" || !ended) return;
      if (ended.type === "scrub")
        seek(ended.moved ? ended.target : xTime(event.clientX), {
          resume: !ended.wasPaused,
        });
      else if (ended.type === "pan" && !ended.moved) seek(xTime(event.clientX));
    }
    canvas.addEventListener("pointerup", pointerEnd);
    canvas.addEventListener("pointercancel", pointerEnd);
    canvas.addEventListener("pointerleave", () => {
      if (!drag) hover.textContent = "";
    });
    new ResizeObserver(draw).observe(canvas);
    const togglePlay = () => {
      if (!loaded || (current && !current.ready)) return;
      if (!current) seek(selected);
      else if (rangePlaying && range && selected >= range.end - 0.1)
        seek(range.start);
      else if (video.paused) video.play().catch(() => {});
      else video.pause();
    };
    time.addEventListener("input", () => {
      timeDirty = true;
    });
    time.addEventListener("keydown", (event) => {
      if (event.key === "Enter") seek(seconds(time.value));
    });
    time.addEventListener("blur", () => {
      if (/^\d{6}$/.test(time.value))
        time.value = time.value.replace(/(\d{2})(\d{2})(\d{2})/, "$1:$2:$3");
    });
    $("#seek-time").onclick = () => seek(seconds(time.value));
    $("#skip-back").onclick = () => seek(selected - 10);
    $("#skip-forward").onclick = () => seek(selected + 10);
    $("#play-pause").onclick = togglePlay;
    $("#nvr-sound").onclick = () => {
      video.muted = !video.muted;
      controls();
    };
    $("#nvr-fullscreen").onclick = () => fullscreen(stage, video);
    $("#playback-speed").onchange = () => {
      video.playbackRate = Number($("#playback-speed").value);
    };
    $("#playback-volume").oninput = () => {
      video.volume = Number($("#playback-volume").value);
      video.muted = video.volume === 0;
      controls();
    };
    $("#retry-playback-stream").onclick = () => seek(selected);
    $("#recording-info").onclick = () => $("#recording-details").showModal();
    $("#snapshot").onclick = () => {
      if (!video.videoWidth || video.readyState < 2 || !current?.ready) return;
      const image = document.createElement("canvas");
      image.width = video.videoWidth;
      image.height = video.videoHeight;
      image.getContext("2d").drawImage(video, 0, 0);
      image.toBlob(
        (blob) => {
          if (!blob) return;
          const url = URL.createObjectURL(blob),
            link = document.createElement("a");
          link.href = url;
          link.download = `${camera.value}_${date.value}_${stamp(selected).replaceAll(":", "-")}.jpg`;
          link.click();
          setTimeout(() => URL.revokeObjectURL(url), 1000);
        },
        "image/jpeg",
        0.95,
      );
    };
    $("#open-export").onclick = () => {
      if (!range)
        range = {
          start: Math.max(0, selected - 30),
          end: Math.min(86399, selected + 30),
        };
      setView(600, selected);
      syncRange();
      $("#export-sheet").showModal();
    };
    $("#adjust-range").onclick = () => $("#export-sheet").close();
    const readRange = () => {
      const start = seconds($("#range-start").value),
        end = seconds($("#range-end").value);
      if (start === null || end === null || end <= start || end - start > 900) {
        $("#timeframe-note").textContent =
          "Use HH:MM:SS and select a continuous range of 1 second to 15 minutes.";
        $("#export-download").removeAttribute("href");
        return false;
      }
      range = { start, end };
      syncRange();
      draw();
      return true;
    };
    for (const input of [$("#range-start"), $("#range-end")]) {
      input.onchange = () => {
        if (/^\d{6}$/.test(input.value))
          input.value = input.value.replace(
            /(\d{2})(\d{2})(\d{2})/,
            "$1:$2:$3",
          );
        readRange();
      };
    }
    $("#timeframe-form").onsubmit = (event) => {
      event.preventDefault();
      if (!readRange()) return;
      rangePlaying = true;
      $("#export-sheet").close();
      seek(range.start);
    };
    $("#export-download").onclick = async (event) => {
      event.preventDefault();
      if (!readRange()) return;
      const href = $("#export-download").href;
      $("#timeframe-note").textContent = "Checking clip availability…";
      try {
        prefetchAfter = performance.now() + 60000;
        if (prefetchTask) await prefetchTask;
        const extra = standby;
        standby = null;
        await drop(extra);
        const target = new URL(href);
        const result = await api(
          target.pathname + "/validate" + target.search,
          "POST",
        );
        if (result.state !== "ready") throw new Error(result.message);
        const link = document.createElement("a");
        link.href = href;
        link.download = "";
        link.click();
        $("#timeframe-note").textContent = "Download started.";
      } catch (error) {
        $("#timeframe-note").textContent = error.message;
      }
    };
    $("#clear-timeframe").onclick = () => {
      range = null;
      rangePlaying = false;
      $("#export-sheet").close();
      draw();
      updateURL();
    };
    function calendar() {
      const [year, month] = calendarMonth.split("-").map(Number),
        first = new Date(Date.UTC(year, month - 1, 1));
      $("#calendar-month").textContent = first.toLocaleDateString("en-GB", {
        month: "long",
        year: "numeric",
        timeZone: "UTC",
      });
      const grid = $("#recording-calendar");
      grid.replaceChildren();
      for (let i = 0; i < (first.getUTCDay() + 6) % 7; i++)
        grid.append(document.createElement("span"));
      const count = new Date(Date.UTC(year, month, 0)).getUTCDate();
      for (let day = 1; day <= count; day++) {
        const value = `${calendarMonth}-${pad(day)}`,
          button = document.createElement("button");
        button.textContent = day;
        button.classList.toggle("has-recordings", calendarDays.includes(value));
        button.setAttribute(
          "aria-label",
          `${value}${calendarDays.includes(value) ? ", recordings available" : ", no recordings"}`,
        );
        button.setAttribute("aria-pressed", String(date.value === value));
        button.onclick = () => {
          date.value = value;
          change();
        };
        grid.append(button);
      }
    }
    async function refreshCalendar() {
      const cam = camera.value;
      try {
        const data = await api(`/api/camera/${cam}/calendar`);
        if (cam !== camera.value) return;
        calendarDays = data.days;
        calendar();
        return data;
      } catch {
        calendarDays = [];
        calendar();
      }
    }
    for (const [id, amount] of [
      ["calendar-previous", -1],
      ["calendar-next", 1],
    ])
      $("#" + id).onclick = () => {
        const d = new Date(calendarMonth + "-15T12:00:00Z");
        d.setUTCMonth(d.getUTCMonth() + amount);
        calendarMonth = d.toISOString().slice(0, 7);
        calendar();
      };
    document.querySelectorAll(".sidebar-camera").forEach(
      (button) =>
        (button.onclick = () => {
          camera.value = button.dataset.cam;
          change();
        }),
    );
    async function latest() {
      range = null;
      rangePlaying = false;
      const myIntent = ++intent;
      show("Finding latest recording…");
      const data = await refreshCalendar();
      if (myIntent !== intent) return;
      if (!data?.latest) {
        loaded = false;
        draw();
        show("No recordings for this camera. Choose another camera.", true);
        return;
      }
      date.value = data.latest.date;
      calendarMonth = date.value.slice(0, 7);
      selected = seconds(data.latest.time) + 1;
      await coverage();
      calendar();
      seek(selected);
    }
    async function change() {
      $("#playback-notice").hidden = true;
      const myIntent = ++intent;
      controller?.abort();
      const old = current,
        next = standby;
      current = null;
      standby = null;
      prefetchTask = null;
      await Promise.all([drop(old), drop(next)]);
      if (myIntent !== intent) return;
      range = null;
      rangePlaying = false;
      loaded = false;
      intervals = [];
      personEvents = [];
      segments = [];
      calendarMonth = date.value.slice(0, 7);
      document
        .querySelectorAll(".sidebar-camera")
        .forEach((button) =>
          button.setAttribute(
            "aria-pressed",
            String(button.dataset.cam === camera.value),
          ),
        );
      draw();
      updateURL();
      refreshCalendar();
      const data = await coverage();
      if (myIntent !== intent) return;
      if (!data?.intervals.length) {
        show(
          "No recordings on this date. Try another day or Jump to latest.",
          true,
        );
        return;
      }
      const contains = data.intervals.some(
        (item) => selected >= item.start && selected < item.end,
      );
      if (!contains)
        selected = Math.min(
          86399,
          (data.segments?.at(-1)?.start ?? data.intervals.at(-1).start) + 1,
        );
      seek(selected);
    }
    date.onchange = change;
    camera.onchange = change;
    function moveDay(amount) {
      const d = new Date(date.value + "T12:00:00Z");
      d.setUTCDate(d.getUTCDate() + amount);
      date.value = d.toISOString().slice(0, 10);
      change();
    }
    $("#previous-day").onclick = () => moveDay(-1);
    $("#next-day").onclick = () => moveDay(1);
    $("#today").onclick = () => {
      date.value = localDate().date;
      change();
    };
    $("#jump-latest").onclick = latest;
    function revealControls() {
      stage.classList.remove("controls-hidden");
      clearTimeout(controlTimer);
      if (matchMedia("(hover:hover)").matches && !video.paused)
        controlTimer = setTimeout(() => {
          if (!stage.querySelector(":focus-visible"))
            stage.classList.add("controls-hidden");
        }, 3000);
    }
    stage.addEventListener("pointermove", revealControls);
    stage.addEventListener("pointerdown", revealControls);
    stage.addEventListener("focusin", revealControls);
    stage.addEventListener("keydown", revealControls);
    video.addEventListener("playing", revealControls);
    keyboard = (event) => {
      const key = event.key;
      if (
        [" ", "ArrowLeft", "ArrowRight", "+", "=", "-", ",", "."].includes(key)
      )
        event.preventDefault();
      if (key === " ") togglePlay();
      else if (key === "ArrowLeft" || key === "ArrowRight")
        seek(
          selected +
            (key === "ArrowRight" ? 1 : -1) * (event.shiftKey ? 60 : 10),
        );
      else if (["+", "=", "-"].includes(key)) zoom(key === "-" ? -1 : 1);
      else if ((key === "," || key === ".") && video.paused)
        seek(
          (current ? current.segment_start + video.currentTime : selected) +
            (key === "." ? 1 : -1) / 20,
          { resume: false },
        );
      else if (key.toLowerCase() === "f") fullscreen(stage, video);
      else if (key.toLowerCase() === "m") $("#nvr-sound").click();
      else if (/^[1-8]$/.test(key)) {
        camera.selectedIndex = Number(key) - 1;
        change();
      }
    };
    const coverageTimer = setInterval(() => {
      if (!document.hidden) coverage();
    }, 20000);
    addEventListener("pagehide", () => {
      intent++;
      controller?.abort();
      clearInterval(coverageTimer);
      clearTimeout(controlTimer);
      drop(current);
      drop(standby);
    });
    addEventListener("popstate", () => {
      const url = new URL(location.href);
      camera.value = url.searchParams.get("cam") || camera.value;
      date.value = url.searchParams.get("date") || date.value;
      selected =
        seconds(
          url.searchParams.get("t") || url.searchParams.get("time") || "",
        ) ?? selected;
      change();
    });
    const mobile = matchMedia("(max-width:700px)"),
      dateTools = $(".date-tools", workspace),
      archiveToolbar = $(".archive-toolbar", workspace);
    function placeDateTools() {
      if (mobile.matches) $(".timeline-bottom", workspace).prepend(dateTools);
      else
        archiveToolbar.insertBefore(dateTools, $(".timezone", archiveToolbar));
    }
    mobile.addEventListener("change", placeDateTools);
    placeDateTools();
    draw();
    if (new URL(location.href).searchParams.get("fallback") === "latest") {
      (async () => {
        await refreshCalendar();
        const data = await coverage();
        const available = data?.segments?.some(
          (item) =>
            !item.estimated && selected >= item.start && selected < item.end,
        );
        if (available) seek(selected);
        else {
          $("#playback-notice").hidden = false;
          $("#playback-notice").textContent = "Latest available footage";
          latest();
        }
      })();
    } else if (workspace.dataset.time) {
      refreshCalendar();
      coverage().then(() => seek(selected));
    } else if (new URL(location.href).searchParams.has("date")) change();
    else latest();
  }
})();
