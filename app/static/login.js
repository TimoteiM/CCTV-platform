"use strict";
const password = document.querySelector("#password");
const toggle = document.querySelector("#toggle-password");
toggle.addEventListener("click", () => {
  const show = password.type === "password";
  password.type = show ? "text" : "password";
  toggle.setAttribute("aria-label", show ? "Hide password" : "Show password");
  toggle.setAttribute("aria-pressed", String(show));
});
document.querySelector("form").addEventListener("submit", () => {
  const button = document.querySelector(".submit");
  button.disabled = true;
  button.textContent = "Signing in…";
});
window.addEventListener("pageshow", () => {
  const button = document.querySelector(".submit");
  button.disabled = false;
  button.innerHTML = 'Sign in <span aria-hidden="true">→</span>';
});
