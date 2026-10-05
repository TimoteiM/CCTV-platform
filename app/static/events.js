"use strict";
(() => {
  const root = document.querySelector("#events-view");
  if (!root) return;
  const $ = (selector) => document.querySelector(selector);
  const camera = $("#events-camera"),
    date = $("#events-date"),
    grid = $("#events-grid"),
    dialog = $("#event-details");
  const pageSize = 24;
  let offset = Math.max(
      0,
      Number(new URL(location.href).searchParams.get("offset")) || 0,
    ),
    next = null,
    generation = 0,
    controller,
    current = [],
    signature = "",
    opener;
  const clock = (value) =>
    new Intl.DateTimeFormat("en-GB", {
      timeZone: "Europe/Bucharest",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hourCycle: "h23",
    }).format(new Date(value * 1000));
  const today = () =>
    new Intl.DateTimeFormat("en-CA", {
      timeZone: "Europe/Bucharest",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).format(new Date());
  const duration = (value) =>
    value < 60
      ? `${Math.round(value)}s`
      : `${Math.floor(value / 60)}m ${Math.round(value % 60)}s`;
  const confidence = (event) =>
    event.confidence === null
      ? "Not recorded"
      : `${Math.round(event.confidence * 100)}%`;
  const node = (tag, className, text) => {
    const result = document.createElement(tag);
    if (className) result.className = className;
    if (text !== undefined) result.textContent = text;
    return result;
  };
  function image(event, detailed = false) {
    const frame = node("div", "event-photo"),
      placeholder = node(
        "div",
        "event-photo-placeholder",
        event.thumbnail_state === "expired"
          ? "Thumbnail expired"
          : "Thumbnail unavailable",
      );
    frame.append(placeholder);
    if (
      event.thumbnail_url &&
      /^\/event-media\/[a-f0-9]{64}\.jpg$/.test(event.thumbnail_url)
    ) {
      const img = node("img");
      img.alt = `Person detected on ${event.camera_name} at ${event.time}`;
      img.loading = detailed ? "eager" : "lazy";
      img.decoding = "async";
      img.onload = () => {
        placeholder.hidden = true;
      };
      img.onerror = () => {
        img.remove();
        placeholder.hidden = false;
        placeholder.textContent = "Thumbnail unavailable";
      };
      img.src = event.thumbnail_url;
      frame.append(img);
    }
    return frame;
  }
  function recordingNote(event) {
    return event.recording_state === "pending"
      ? "Recording in progress. Playback will be available once this segment is finalized."
      : event.recording_state === "unavailable"
        ? "The matching recording is unavailable or has expired."
        : "Playback includes up to 3 seconds before the detection.";
  }
  function playbackLink(link, event) {
    if (
      event.playback_url &&
      /^\/playback\?cam=cam0[1-8]&date=\d{4}-\d{2}-\d{2}&t=\d{2}:\d{2}:\d{2}$/.test(
        event.playback_url,
      )
    ) {
      link.href = event.playback_url;
      link.removeAttribute("aria-disabled");
      link.removeAttribute("tabindex");
    } else {
      link.removeAttribute("href");
      link.setAttribute("aria-disabled", "true");
      link.setAttribute("tabindex", "-1");
    }
  }
  function details(event, button) {
    opener = button;
    $("#event-detail-title").textContent = `Person · ${event.camera_name}`;
    $("#event-detail-image").replaceChildren(image(event, true));
    const facts = $("#event-detail-facts");
    facts.replaceChildren();
    for (const [label, value] of [
      ["Camera", event.camera_name],
      ["Started", `${event.start_local.slice(0, 10)} · ${clock(event.start)}`],
      [
        "Ended",
        event.active
          ? "Ongoing"
          : `${event.end_local.slice(0, 10)} · ${clock(event.end)}`,
      ],
      ["Duration", duration(event.duration)],
      ["Confidence", confidence(event)],
      [
        "Zones",
        event.zones.length ? event.zones.join(", ") : "No zones configured",
      ],
      ["Detection source", "Frigate"],
      ["Time zone", "Europe/Bucharest"],
    ])
      facts.append(node("dt", null, label), node("dd", null, value));
    playbackLink($("#event-playback"), event);
    $("#event-live").href = `/live?cam=${event.camera}`;
    $("#event-recording-note").textContent = recordingNote(event);
    dialog.showModal();
  }
  dialog.addEventListener("close", () => {
    if (opener?.isConnected) opener.focus();
  });
  function render(events) {
    grid.replaceChildren();
    for (const event of events) {
      const card = node("article", "event-card"),
        photo = node("button", "event-photo-button");
      photo.type = "button";
      photo.setAttribute(
        "aria-label",
        `View person event on ${event.camera_name} at ${event.time}`,
      );
      photo.append(image(event));
      const badge = node("span", "event-person-badge", "Person");
      photo.append(badge);
      if (event.active) photo.append(node("span", "event-active", "Ongoing"));
      photo.onclick = () => details(event, photo);
      const body = node("div", "event-card-body"),
        heading = node("div", "event-card-heading");
      heading.append(
        node("h2", null, event.camera_name),
        node("time", "event-time", event.time),
      );
      heading.lastChild.dateTime = event.start_local;
      const summary = node(
        "p",
        "small",
        `${duration(event.duration)} · ${event.confidence === null ? "Confidence not recorded" : confidence(event) + " confidence"}`,
      );
      const actions = node("div", "event-card-actions"),
        detail = node("button", "secondary", "Details"),
        play = node("a", "button", "Play recording");
      detail.type = "button";
      detail.onclick = () => details(event, detail);
      detail.setAttribute(
        "aria-label",
        `Details for ${event.camera_name} at ${event.time}`,
      );
      playbackLink(play, event);
      if (!event.recording_available) {
        play.textContent =
          event.recording_state === "pending"
            ? "Recording in progress"
            : "Recording unavailable";
        play.title = recordingNote(event);
      }
      actions.append(detail, play);
      body.append(heading, summary, actions);
      card.append(photo, body);
      grid.append(card);
    }
  }
  function bookmark() {
    const query = new URLSearchParams({ cam: camera.value, date: date.value });
    if (offset) query.set("offset", offset);
    history.replaceState(null, "", "/events?" + query);
  }
  function status(info) {
    const element = $("#events-status");
    element.replaceChildren(node("span", "status-dot"));
    element.dataset.state =
      info.connected && info.cameras.length === 8 ? "connected" : "warning";
    element.append(
      document.createTextNode(
        info.connected
          ? ` Frigate connected · ${info.cameras.length}/8 cameras`
          : " Frigate offline · saved events",
      ),
    );
    $("#events-updated").textContent = info.updated
      ? `Last sync ${clock(info.updated)}`
      : "";
  }
  async function load({ silent = false } = {}) {
    if (!date.checkValidity() || !/^\d{4}-\d{2}-\d{2}$/.test(date.value))
      return;
    const mine = ++generation;
    controller?.abort();
    controller = new AbortController();
    $("#events-refresh").disabled = true;
    grid.setAttribute("aria-busy", "true");
    if (!silent) $("#events-count").textContent = "Loading events…";
    try {
      const response = await fetch(
        `/api/events?${new URLSearchParams({ cam: camera.value, date: date.value, offset, limit: pageSize })}`,
        { signal: controller.signal, credentials: "same-origin" },
      );
      if (response.status === 401) {
        location.assign(
          `/login?next=${encodeURIComponent(location.pathname + location.search)}`,
        );
        throw new Error("Sign in required.");
      }
      const data = await response.json();
      if (!response.ok)
        throw new Error(data.detail || "Events could not be loaded.");
      if (mine !== generation) return;
      if (offset && offset >= data.total) {
        offset = Math.max(
          0,
          Math.floor((data.total - 1) / pageSize) * pageSize,
        );
        return load();
      }
      if (data.catalog_state === "unavailable")
        throw new Error("Some event data is unavailable. Please retry.");
      current = data.events;
      next = data.next_offset;
      const updatedSignature = JSON.stringify(current);
      if (signature !== updatedSignature) {
        render(current);
        signature = updatedSignature;
      }
      $("#events-error").hidden = true;
      $("#events-empty").hidden = current.length > 0;
      const selectedDate = new Intl.DateTimeFormat("en-GB", {
        timeZone: "UTC",
        day: "numeric",
        month: "short",
        year: "numeric",
      }).format(new Date(date.value + "T12:00:00Z"));
      $("#events-count").textContent =
        `${data.total} person ${data.total === 1 ? "event" : "events"} · ${camera.options[camera.selectedIndex].text} · ${selectedDate}`;
      $("#events-page").textContent = data.total
        ? `${offset + 1}–${Math.min(offset + pageSize, data.total)} of ${data.total}`
        : "";
      $("#events-previous").disabled = offset === 0;
      $("#events-next").disabled = next === null;
      $(".events-pagination").hidden = data.total <= pageSize;
      status(data.status);
      bookmark();
    } catch (error) {
      if (error.name !== "AbortError" && mine === generation) {
        $("#events-error p").textContent = error.message;
        $("#events-error").hidden = false;
        $("#events-empty").hidden = true;
        $("#events-count").textContent = "Unable to update events.";
      }
    } finally {
      if (mine === generation) {
        grid.setAttribute("aria-busy", "false");
        $("#events-refresh").disabled = false;
      }
    }
  }
  function change() {
    offset = 0;
    signature = "";
    grid.replaceChildren();
    load();
  }
  $("#events-filters").onsubmit = (event) => {
    event.preventDefault();
    change();
  };
  camera.onchange = change;
  date.onchange = change;
  $("#events-today").onclick = () => {
    date.value = today();
    change();
  };
  $("#events-refresh").onclick = () => load();
  $("#events-retry").onclick = () => load();
  $("#events-previous").onclick = () => {
    offset = Math.max(0, offset - pageSize);
    load();
  };
  $("#events-next").onclick = () => {
    if (next !== null) {
      offset = next;
      load();
    }
  };
  setInterval(() => {
    if (!document.hidden && !dialog.open && offset === 0)
      load({ silent: true });
  }, 15000);
  addEventListener("pagehide", () => controller?.abort());
  load();
})();
