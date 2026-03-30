window.addEventListener("DOMContentLoaded", () => {
  const registerForm = document.querySelector("#register-form");
  const loginForm = document.querySelector("#login-form");
  const uploadForm = document.querySelector("#upload-form");
  const statusMessage = document.querySelector("#status-message");
  const fileInput = document.querySelector("#file-input");
  const check1 = document.querySelector("#check-option-1");
  const check2 = document.querySelector("#check-option-2");
  const toRegisterBtn = document.querySelector("#to-register-btn");
  const toLoginBtn = document.querySelector("#to-login-btn");

  const API_BASE_URL = "https://api.memory-illumination.com";

  // Navigation Logic
  toRegisterBtn.addEventListener("click", () => {
    loginForm.style.display = "none";
    registerForm.style.display = "flex";
    statusMessage.textContent = ""; 
  });

  toLoginBtn.addEventListener("click", () => {
    registerForm.style.display = "none";
    loginForm.style.display = "flex";
    statusMessage.textContent = "";
  });

  check1.addEventListener('change', () => { if (check1.checked) check2.checked = false; });
  check2.addEventListener('change', () => { if (check2.checked) check1.checked = false; });

  // Register
  registerForm.addEventListener("submit", (e) => {
    e.preventDefault(); 
    const username = document.querySelector("#reg-username-input").value;
    const password = document.querySelector("#reg-password-input").value;
    statusMessage.textContent = "Processing registration...";

    fetch(`${API_BASE_URL}/register`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    })
    .then(res => res.ok ? res.json() : Promise.reject())
    .then(() => {
      registerForm.style.display = "none";
      loginForm.style.display = "flex";
      statusMessage.textContent = "Success! Please check your email to activate.";
    })
    .catch(() => { statusMessage.textContent = "Registration failed. Try again."; });
  });

  // Login
  loginForm.addEventListener("submit", (e) => {
    e.preventDefault(); 
    const username = document.querySelector("#login-username-input").value;
    const password = document.querySelector("#login-password-input").value;

    fetch(`${API_BASE_URL}/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    })
    .then(res => res.ok ? res.json() : Promise.reject())
    .then(() => {
      loginForm.style.display = "none";
      uploadForm.style.display = "flex";
      statusMessage.textContent = `Welcome, ${username}!`;
    })
    .catch(() => { statusMessage.textContent = "Login failed. Verify email or check credentials."; });
  });

  // Upload
  uploadForm.addEventListener("submit", (e) => {
    e.preventDefault(); 
    const file = fileInput.files[0];
    if (!file) return statusMessage.textContent = "Select a file.";

    statusMessage.textContent = "Processing image...";
    const formData = new FormData();
    formData.append("myFile", file);
    formData.append("settings", JSON.stringify({ featureA: check1.checked, featureB: check2.checked }));

    fetch(`${API_BASE_URL}/upload-endpoint`, { method: "POST", body: formData })
    .then(res => res.ok ? res.blob() : Promise.reject())
    .then(blob => {
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = "illuminated.png";
      a.click();
      statusMessage.textContent = "Download complete!";
    })
    .catch(() => { statusMessage.textContent = "Error processing image."; });
  });
});