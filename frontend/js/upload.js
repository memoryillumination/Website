window.addEventListener("DOMContentLoaded", () => {
  const username = sessionStorage.getItem("username");

  const navUsername = document.querySelector("#nav-username");
  if (navUsername && username) navUsername.textContent = username;

  const API_BASE_URL = "https://api.memoryillumination.com";

  // Tour trigger disabled for now — still sandboxing the tooltip UI.
  // if (sessionStorage.getItem("newUser") === "1") {
  //   sessionStorage.removeItem("newUser");
  //   startTour(UPLOAD_TOUR_STEPS, { apiBaseUrl: API_BASE_URL });
  // }

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
  const resultSection = document.querySelector("#result");
  const resultPreview = document.querySelector("#result-preview");
  const downloadLink = document.querySelector("#download-link");

  // The blob URL backing both the preview and the download link. Held so it can
  // be revoked before a new result replaces it — otherwise every generation
  // leaks a blob for the lifetime of the page.
  let resultObjectUrl = null;

  function clearResult() {
    resultSection.classList.add("hidden");
    resultPreview.removeAttribute("src");
    downloadLink.removeAttribute("href");
    if (resultObjectUrl) {
      URL.revokeObjectURL(resultObjectUrl);
      resultObjectUrl = null;
    }
  }

  check1.addEventListener("change", () => {
    if (check1.checked) check2.checked = false;
  });
  check2.addEventListener("change", () => {
    if (check2.checked) check1.checked = false;
  });

  uploadForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const file = fileInput.files[0];
    if (!file) return (statusMessage.textContent = "Select a file.");

    clearResult();
    statusMessage.textContent = "Processing image...";
    const formData = new FormData();
    formData.append("myFile", file);
    formData.append("settings", JSON.stringify({ featureA: check1.checked, featureB: check2.checked }));

    fetch(`${API_BASE_URL}/upload-endpoint`, { method: "POST", credentials: "include", body: formData })
      .then(async (res) => {
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          throw new Error(body.error || "Error processing image.");
        }
        return res.blob();
      })
      .then((blob) => {
        // No synthetic a.click() here: the download is now driven by a real
        // click on a real link, which keeps it out of the way of browser
        // blocking of programmatic downloads that follow an await.
        resultObjectUrl = URL.createObjectURL(blob);
        resultPreview.src = resultObjectUrl;
        downloadLink.href = resultObjectUrl;
        resultSection.classList.remove("hidden");
        statusMessage.textContent = "Ready — preview below, then download.";
        resultSection.scrollIntoView({ behavior: "smooth", block: "nearest" });
      })
      .catch((err) => {
        statusMessage.textContent = err.message || "Error processing image.";
      });
  });
});
