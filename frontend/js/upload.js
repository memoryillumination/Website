window.addEventListener("DOMContentLoaded", () => {
  const username = sessionStorage.getItem("username");

  const navUsername = document.querySelector("#nav-username");
  if (navUsername && username) navUsername.textContent = username;

  const API_BASE_URL = window.MI_CONFIG?.apiBaseUrl || "https://api.memoryillumination.com";

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
  const submitButton = document.querySelector("#upload-form button[type='submit']");
  const statusMessage = document.querySelector("#status-message");
  const fileInput = document.querySelector("#file-input");
  const check1 = document.querySelector("#check-option-1");
  const check2 = document.querySelector("#check-option-2");
  const resultSection = document.querySelector("#result");
  const resultPreview = document.querySelector("#result-preview");
  const downloadLink = document.querySelector("#download-link");

  // Progress indicator elements
  const progressContainer = document.querySelector("#progress-container");
  const progressBar = document.querySelector("#progress-bar");
  const progressStatus = document.querySelector("#progress-status");

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

  function showProgress() {
    progressContainer.classList.remove("hidden");
    progressBar.style.width = "0%";
  }

  function setProgress(percent, message) {
    progressBar.style.width = `${percent}%`;
    progressStatus.textContent = message;
    // Update ARIA attributes for screen readers.
    progressContainer.setAttribute("aria-valuenow", String(percent));
  }

  function hideProgress() {
    progressContainer.classList.add("hidden");
    progressBar.style.width = "0%";
  }

  function setUploadState(state) {
    // state: "idle" | "processing" | "done"
    submitButton.disabled = state === "processing";
    if (state === "processing") {
      submitButton.textContent = "Processing…";
    } else if (state === "idle") {
      submitButton.textContent = "Upload";
    }
  }

  check1.addEventListener("change", () => {
    if (check1.checked) check2.checked = false;
  });
  check2.addEventListener("change", () => {
    if (check2.checked) check1.checked = false;
  });

  // Wire up Try Again button
  document.querySelector("#try-again-btn")?.addEventListener("click", () => {
    clearResult();
    fileInput.value = "";
    statusMessage.textContent = "";
    window.scrollTo({ top: 0, behavior: "smooth" });
  });

  // Show/hide the Try Again button based on whether we have an error to recover from.
  function updateTryAgainVisibility(show) {
    const tryAgainBtn = document.querySelector("#try-again-btn");
    if (tryAgainBtn) {
      tryAgainBtn.classList.toggle("hidden", !show);
    }
  }

  uploadForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const file = fileInput.files[0];
    if (!file) {
      statusMessage.textContent = "Select a file.";
      updateTryAgainVisibility(true);
      return;
    }

    // Validate file size before upload (catch large files early)
    const maxSize = 50 * 1024 * 1024; // 50 MB
    if (file.size > maxSize) {
      statusMessage.textContent = "File too large. Maximum size is 50 MB.";
      updateTryAgainVisibility(true);
      return;
    }

    clearResult();
    showProgress();
    setProgress(10, "Uploading…");
    setUploadState("processing");
    updateTryAgainVisibility(false);

    const formData = new FormData();
    formData.append("myFile", file);
    formData.append("settings", JSON.stringify({ featureA: check1.checked, featureB: check2.checked }));

    // Track the processing interval so it can be cleared in the finally path.
    let processingIntervalId = null;

    try {
      const response = await fetch(`${API_BASE_URL}/upload-endpoint`, {
        method: "POST",
        credentials: "include",
        body: formData,
        headers: {
          "Content-Length": file.size,
        },
      });

      if (!response.ok) {
        const contentType = response.headers.get("content-type") || "";
        const body = contentType.includes("json") ? await response.json().catch(() => ({})) : {};

        if (response.status === 429) {
          throw new Error("Too many requests. Please wait a moment and try again.");
        } else if (response.status === 413) {
          throw new Error(body.error || "Image too large. Please upload a smaller photo.");
        } else if (response.status === 400) {
          throw new Error(body.error || "Invalid image file. Please upload a JPEG, PNG, HEIC, AVIF, or WEBP photo.");
        } else {
          throw new Error(body.error || "Server error. Please try again in a moment.");
        }
      }

      setProgress(90, "Processing image…");

      // Simulate processing progress (backend is working on GPU).
      processingIntervalId = setInterval(() => {
        const currentWidth = parseFloat(progressBar.style.width) || 90;
        if (currentWidth < 98) {
          setProgress(Math.min(currentWidth + 2, 98), "Generating your image…");
        }
      }, 3000);

      const blob = await response.blob();
      clearInterval(processingIntervalId);
      processingIntervalId = null;
      setProgress(100, "Complete!");

      resultObjectUrl = URL.createObjectURL(blob);
      resultPreview.src = resultObjectUrl;
      downloadLink.href = resultObjectUrl;
      resultSection.classList.remove("hidden");
      statusMessage.textContent = "Ready — preview below, then download.";
      updateTryAgainVisibility(false);

      // Scroll to result and set focus on a semantic element for screen readers.
      resultSection.scrollIntoView({ behavior: "smooth", block: "nearest" });
      // Focus the result heading instead of the img element (which is not a
      // standard focus target for screen readers).
      const resultHeading = resultSection.querySelector("h3");
      if (resultHeading) {
        resultHeading.setAttribute("tabindex", "-1");
        resultHeading.focus();
      }

      // Hide progress after a brief delay.
      setTimeout(() => hideProgress(), 1500);
      setUploadState("idle");
    } catch (err) {
      // Always clear the interval in the error path.
      if (processingIntervalId !== null) {
        clearInterval(processingIntervalId);
        processingIntervalId = null;
      }

      statusMessage.textContent = err.message || "Error processing image. Please try again.";
      hideProgress();
      setUploadState("idle");
      updateTryAgainVisibility(true);
    } finally {
      // Safety net: if the interval somehow survived to here, clear it.
      if (processingIntervalId !== null) {
        clearInterval(processingIntervalId);
        processingIntervalId = null;
      }
    }
  });

  // Clean up blob URLs when the user navigates away from the page
  window.addEventListener("beforeunload", () => {
    if (resultObjectUrl) {
      URL.revokeObjectURL(resultObjectUrl);
      resultObjectUrl = null;
    }
  });
});
