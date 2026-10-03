"use strict";
// Compatibility for older archive links; all playback uses the NVR player.
document
  .querySelectorAll(".play[data-camera][data-recording]")
  .forEach((button) => {
    button.addEventListener("click", () => {
      const date = button.dataset.recording.match(/\d{4}-\d{2}-\d{2}/)?.[0];
      const query = new URLSearchParams({
        cam: button.dataset.camera,
        t: button.dataset.time || "00:00:00",
      });
      if (date) query.set("date", date);
      location.assign("/playback?" + query);
    });
  });
