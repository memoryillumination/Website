window.addEventListener("DOMContentLoaded", () => {
  const loginForm = document.querySelector("#login-form");
  const statusMessage = document.querySelector("#status-message");
  const usernameInput = document.querySelector("#login-username-input");
  const resendBlock = document.querySelector("#resend-block");
  const resendBtn = document.querySelector("#resend-btn");

  const API_BASE_URL = "https://api.memoryillumination.com";

  // Must match the normalisation the backend applies, so the address we send
  // to /login and /resend-confirmation is the one stored at registration.
  const normalize = (value) => value.trim().toLowerCase();

  const setStatus = (text, color = "") => {
    statusMessage.textContent = text;
    statusMessage.style.color = color;
  };

  // Set inline rather than with disabled: utilities — the committed
  // css/output.css has no disabled: variants compiled, so those classes
  // would be inert without a Tailwind rebuild.
  const setResendDisabled = (disabled) => {
    resendBtn.disabled = disabled;
    resendBtn.style.opacity = disabled ? "0.5" : "";
    resendBtn.style.cursor = disabled ? "default" : "";
  };

  // Show success message if arriving after registration
  if (new URLSearchParams(window.location.search).get("registered") === "1") {
    setStatus("Registration successful! Please check your email to activate your account.", "#28a745");
    resendBlock.classList.remove("hidden");
  }

  loginForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const username = normalize(usernameInput.value);
    const password = document.querySelector("#login-password-input").value;
    setStatus("Logging in...");

    fetch(`${API_BASE_URL}/login`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    })
      .then((res) => (res.ok ? res.json() : Promise.reject()))
      .then((data) => {
        sessionStorage.setItem("username", username);
        sessionStorage.setItem("newUser", data.newUser ? "1" : "0");
        window.location.href = "upload.html";
      })
      .catch(() => {
        // The backend deliberately can't say whether this was a bad password
        // or an unconfirmed account, so offer the resend path either way.
        setStatus("Login failed. Check your credentials, or confirm your email first.", "#c0392b");
        resendBlock.classList.remove("hidden");
      });
  });

  resendBtn.addEventListener("click", () => {
    const username = normalize(usernameInput.value);
    if (!username) {
      setStatus("Enter your email address above first.", "#c0392b");
      usernameInput.focus();
      return;
    }

    setResendDisabled(true);
    setStatus("Sending...");

    fetch(`${API_BASE_URL}/resend-confirmation`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username }),
    })
      .then((res) => res.json().then((body) => ({ ok: res.ok, body })))
      .then(({ ok, body }) => {
        if (!ok) {
          setStatus(body.error || "Could not send. Try again.", "#c0392b");
          setResendDisabled(false);
          return;
        }
        // Always the same message: the endpoint won't reveal whether the
        // address is registered, and the button stays disabled so repeated
        // clicks don't burn through the server-side hourly limit.
        setStatus("If that account needs confirming, a new link is on its way.", "#28a745");
      })
      .catch(() => {
        setStatus("Could not send. Try again.", "#c0392b");
        setResendDisabled(false);
      });
  });
});
