window.addEventListener("DOMContentLoaded", () => {
  const username = sessionStorage.getItem("username");

  const navUsername = document.querySelector("#nav-username");
  if (navUsername && username) navUsername.textContent = username;

  const API_BASE_URL = "https://api.memoryillumination.com";

  document.querySelector("#logout-btn").addEventListener("click", () => {
    fetch(`${API_BASE_URL}/logout`, { method: "POST", credentials: "include" }).finally(() => {
      sessionStorage.removeItem("username");
      window.location.href = "index.html";
    });
  });

  const uploadForm = document.querySelector("#upload-form");
  const statusMessage = document.querySelector("#status-message");
  const fileInput = document.querySelector("#file-input");
  const check1 = document.querySelector("#check-option-1");
  const check2 = document.querySelector("#check-option-2");

  check1.addEventListener("change", () => { if (check1.checked) check2.checked = false; });
  check2.addEventListener("change", () => { if (check2.checked) check1.checked = false; });

  uploadForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const file = fileInput.files[0];
    if (!file) return (statusMessage.textContent = "Select a file.");

    statusMessage.textContent = "Processing image...";
    const formData = new FormData();
    formData.append("myFile", file);
    formData.append("settings", JSON.stringify({ featureA: check1.checked, featureB: check2.checked }));

    fetch(`${API_BASE_URL}/upload-endpoint`, { method: "POST", credentials: "include", body: formData })
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
