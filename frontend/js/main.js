// --- Policy Modal ---
const policyModal = document.getElementById("policy-modal");
const modalTitle = document.getElementById("modal-title");
const modalBody = document.getElementById("modal-body");

function openModal(url, title) {
  modalTitle.textContent = title;
  modalBody.innerHTML = '<p class="text-center text-[#6c757d] py-8">Loading...</p>';
  policyModal.classList.remove("hidden");
  document.body.style.overflow = "hidden";

  fetch(url)
    .then((r) => r.text())
    .then((html) => {
      const doc = new DOMParser().parseFromString(html, "text/html");
      const main = doc.querySelector("main");
      // Drop the page-level h1 and subtitle — modal header already shows the title
      main.querySelector("h1")?.remove();
      main.querySelectorAll("p.text-center").forEach((el) => el.remove());
      modalBody.innerHTML = main.innerHTML;
    })
    .catch(() => {
      modalBody.innerHTML = '<p class="text-center text-red-500 py-8">Failed to load content.</p>';
    });
}

function closeModal() {
  policyModal.classList.add("hidden");
  document.body.style.overflow = "";
}

document.getElementById("modal-close").addEventListener("click", closeModal);
policyModal.addEventListener("click", (e) => {
  if (e.target === policyModal) closeModal();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeModal();
});

document.querySelectorAll("[data-modal]").forEach((link) => {
  link.addEventListener("click", (e) => {
    e.preventDefault();
    const type = link.dataset.modal;
    openModal(
      type === "privacy" ? "privacy.html" : "tos.html",
      type === "privacy" ? "Privacy Policy" : "Terms of Service",
    );
  });
});

// --- App ---
window.addEventListener("DOMContentLoaded", () => {
  const homePage = document.querySelector("#home-page");
  const appContainer = document.querySelector("#app-container");
  const registerForm = document.querySelector("#register-form");
  const loginForm = document.querySelector("#login-form");
  const uploadForm = document.querySelector("#upload-form");
  const statusMessage = document.querySelector("#status-message");
  const fileInput = document.querySelector("#file-input");
  const check1 = document.querySelector("#check-option-1");
  const check2 = document.querySelector("#check-option-2");
  const toRegisterBtn = document.querySelector("#to-register-btn");

  const API_BASE_URL = "https://api.memoryillumination.com";
  let sessionToken = "";

  function showHome() {
    homePage.style.display = "flex";
    appContainer.style.display = "none";
    statusMessage.textContent = "";
  }

  function showLogin() {
    homePage.style.display = "none";
    appContainer.style.display = "flex";
    loginForm.style.display = "flex";
    registerForm.style.display = "none";
    uploadForm.style.display = "none";
    statusMessage.textContent = "";
  }

  function showRegister() {
    homePage.style.display = "none";
    appContainer.style.display = "flex";
    registerForm.style.display = "flex";
    loginForm.style.display = "none";
    uploadForm.style.display = "none";
    statusMessage.textContent = "";
  }

  // Nav & landing
  document.querySelector("#home-link").addEventListener("click", showHome);
  document.querySelector("#nav-login-btn").addEventListener("click", showLogin);
  document.querySelector("#nav-register-btn").addEventListener("click", showRegister);
  document.querySelector("#get-started-btn").addEventListener("click", showLogin);

  // Open login/register directly when arriving via hash link from another page
  if (window.location.hash === "#login") showLogin();
  else if (window.location.hash === "#register") showRegister();

  // Within-form navigation
  toRegisterBtn.addEventListener("click", () => {
    loginForm.style.display = "none";
    registerForm.style.display = "flex";
    statusMessage.textContent = "";
  });

  check1.addEventListener("change", () => {
    if (check1.checked) check2.checked = false;
  });
  check2.addEventListener("change", () => {
    if (check2.checked) check1.checked = false;
  });

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
      .then((res) => (res.ok ? res.json() : Promise.reject()))
      .then(() => {
        registerForm.style.display = "none";
        loginForm.style.display = "flex";
        statusMessage.textContent = "Success! Please check your email to activate.";
      })
      .catch(() => {
        statusMessage.textContent = "Registration failed. Try again.";
      });
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
      .then((res) => (res.ok ? res.json() : Promise.reject()))
      .then((data) => {
        sessionToken = data.token;
        loginForm.style.display = "none";
        uploadForm.style.display = "flex";
        statusMessage.textContent = `Welcome, ${username}!`;
      })
      .catch(() => {
        statusMessage.textContent = "Login failed. Verify email or check credentials.";
      });
  });

  // Upload
  uploadForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const file = fileInput.files[0];
    if (!file) return (statusMessage.textContent = "Select a file.");

    statusMessage.textContent = "Processing image...";
    const formData = new FormData();
    formData.append("myFile", file);
    formData.append("settings", JSON.stringify({ featureA: check1.checked, featureB: check2.checked }));
    formData.append("token", sessionToken);

    fetch(`${API_BASE_URL}/upload-endpoint`, { method: "POST", body: formData })
      .then((res) => (res.ok ? res.blob() : Promise.reject()))
      .then((blob) => {
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = "illuminated.png";
        a.click();
        statusMessage.textContent = "Download complete!";
      })
      .catch(() => {
        statusMessage.textContent = "Error processing image.";
      });
  });
});
