// Policy Modal
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

// Register Form
window.addEventListener("DOMContentLoaded", () => {
  const registerForm = document.querySelector("#register-form");
  const statusMessage = document.querySelector("#status-message");

  const API_BASE_URL = "https://api.memoryillumination.com";

  registerForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const username = document.querySelector("#reg-username-input").value;
    const password = document.querySelector("#reg-password-input").value;
    statusMessage.textContent = "Processing registration...";

    fetch(`${API_BASE_URL}/register`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    })
      .then((res) => (res.ok ? res.json() : Promise.reject()))
      .then(() => {
        window.location.href = "login.html?registered=1";
      })
      .catch(() => {
        statusMessage.textContent = "Registration failed. Try again.";
      });
  });
});
