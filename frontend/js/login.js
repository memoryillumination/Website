window.addEventListener("DOMContentLoaded", () => {
  const loginForm = document.querySelector("#login-form");
  const statusMessage = document.querySelector("#status-message");

  const API_BASE_URL = "https://api.memoryillumination.com";

  // Show success message if arriving after registration
  if (new URLSearchParams(window.location.search).get("registered") === "1") {
    statusMessage.textContent = "Registration successful! Please check your email to activate your account.";
    statusMessage.style.color = "#28a745";
  }

  loginForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const username = document.querySelector("#login-username-input").value;
    const password = document.querySelector("#login-password-input").value;
    statusMessage.style.color = "";
    statusMessage.textContent = "Logging in...";

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
        statusMessage.textContent = "Login failed. Verify your email or check credentials.";
      });
  });
});
