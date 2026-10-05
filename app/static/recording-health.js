(() => {
  "use strict";
  const banner = document.querySelector("#recording-warning");
  if (!banner) return;
  let timer,
    pending = false;
  async function check() {
    clearTimeout(timer);
    if (document.hidden || pending) return;
    pending = true;
    try {
      const response = await fetch("/api/recording-health", {
        credentials: "same-origin",
        signal: AbortSignal.timeout(10000),
      });
      if (response.status === 401) return;
      if (!response.ok) throw new Error("Monitoring unavailable");
      const data = await response.json();
      if (!data.configured) {
        banner.hidden = true;
        return;
      }
      if (!data.available) throw new Error("Monitoring unavailable");
      const affected = data.cameras.filter(
        (camera) => camera.status !== "healthy",
      );
      banner.hidden = affected.length === 0;
      if (affected.length) {
        const storage = affected.some(
          (camera) => camera.status === "storage-low",
        );
        banner.textContent = storage
          ? "Recording warning: server storage is running low. Recording may be interrupted."
          : `Recording warning: ${affected.map((camera) => camera.name).join(", ")} ${affected.every((camera) => ["starting", "recovering"].includes(camera.status)) ? "— automatic recovery in progress. Recording is not yet confirmed." : "— recording is interrupted or could not be verified. Automatic monitoring is active."}`;
      }
    } catch {
      banner.hidden = false;
      banner.textContent =
        "Recording warning: automatic recording monitoring is unavailable.";
    } finally {
      pending = false;
      timer = setTimeout(check, 30000);
    }
  }
  document.addEventListener("visibilitychange", check);
  addEventListener("pageshow", check);
  check();
})();
