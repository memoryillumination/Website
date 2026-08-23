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
  const formView = document.querySelector("#form-view");
  const submitButton = uploadForm.querySelector("button[type='submit']");
  const tryAgainButton = document.querySelector("#try-again");
  const statusMessage = document.querySelector("#status-message");
  const fileInput = document.querySelector("#file-input");
  const check1 = document.querySelector("#check-option-1");
  const check2 = document.querySelector("#check-option-2");
  const progress = document.querySelector("#progress");
  const progressBar = document.querySelector("#progress-bar");
  const resultSection = document.querySelector("#result");
  const resultPreview = document.querySelector("#result-preview");
  const downloadLink = document.querySelector("#download-link");

  // --- PROGRESS MODEL -------------------------------------------------------
  //
  // The bar has three real anchors — bytes uploaded, poll-confirmed elapsed
  // time, and actual completion. Only the stretch between them is estimated,
  // and the estimate comes from the server (which knows whether the GPU
  // container is likely cold). Bands are the share of the bar each phase owns.
  const UPLOAD_BAND = 15;
  const GENERATION_BAND = 77; // 15% -> 92%
  const FETCH_MARK = 96;

  const POLL_INTERVAL_MS = 1500;
  // flux_1_kontext_modal.py sets the Modal function timeout to 600s. Stop
  // polling a little past that rather than spinning forever on a lost job.
  const POLL_CEILING_MS = 660000;

  /**
   * Share of the generation band to show after `elapsed` seconds.
   *
   * Asymptotic on purpose: it approaches 1 without reaching it, so a job that
   * overruns its estimate keeps creeping instead of parking at a hard stop.
   * At elapsed === estimate this sits at ~80% of the band.
   */
  function generationFraction(elapsed, estimate) {
    if (!estimate || estimate <= 0) return 0;
    return 1 - Math.exp((-1.6 * elapsed) / estimate);
  }

  function setProgress(percent) {
    progressBar.style.width = `${Math.min(100, Math.max(0, percent))}%`;
  }

  // --- VIEWS ----------------------------------------------------------------
  //
  // The page is one of three mutually exclusive views. Each element is shown by
  // exactly one of them, so nothing can be left on screen by a missed cleanup.
  //
  //   idle    the upload form
  //   working the progress bar (replaces the form)
  //   done    the image with its Download / Try Again buttons
  function showView(view) {
    formView.classList.toggle("hidden", view !== "idle");
    progress.classList.toggle("hidden", view !== "working");
    resultSection.classList.toggle("hidden", view !== "done");
  }

  // The blob URL backing both the preview and the download link. Held so it can
  // be revoked when the result goes away — otherwise every generation leaks a
  // blob for the lifetime of the page.
  let resultObjectUrl = null;

  /**
   * Drop the current result.
   *
   * revokeDelayMs exists for the Download button: clicking it starts a save
   * from the blob URL, and revoking that URL in the same tick can abort the
   * save in some browsers. Callers that aren't racing a download pass 0.
   */
  function clearResult(revokeDelayMs = 0) {
    resultPreview.removeAttribute("src");
    downloadLink.removeAttribute("href");

    const url = resultObjectUrl;
    resultObjectUrl = null;
    if (!url) return;

    if (revokeDelayMs > 0) setTimeout(() => URL.revokeObjectURL(url), revokeDelayMs);
    else URL.revokeObjectURL(url);
  }

  function showResult(blob) {
    // No synthetic a.click() here: the download is driven by a real click on a
    // real link, which keeps it clear of browser blocking of programmatic
    // downloads that follow an await. Preview and link share one object URL,
    // so what's on screen is byte-for-byte what gets saved.
    resultObjectUrl = URL.createObjectURL(blob);
    resultPreview.src = resultObjectUrl;
    downloadLink.href = resultObjectUrl;
    statusMessage.textContent = "";
    showView("done");
    resultSection.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  /** Back to a clean upload form, after a download or a Try Again. */
  function resetToIdle(revokeDelayMs = 0) {
    clearResult(revokeDelayMs);
    setProgress(0);
    statusMessage.textContent = "";
    // Clear the chosen file, but deliberately NOT the whole form: reset() would
    // also clear the style checkboxes back to their unchecked defaults, which
    // silently sends the next upload down the OpenCV path instead of the one
    // the user picked.
    fileInput.value = "";
    showView("idle");
  }

  // --- TRANSPORT ------------------------------------------------------------

  /**
   * POST the form over XHR rather than fetch, because fetch cannot report
   * upload progress. Resolves { isJson, body } — the backend answers the fast
   * OpenCV path with an image/png blob and the GPU path with a 202 job id.
   */
  function postUpload(formData, onUploadProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", `${API_BASE_URL}/upload-endpoint`);
      xhr.withCredentials = true;
      xhr.responseType = "blob";

      xhr.upload.addEventListener("progress", (e) => {
        if (e.lengthComputable) onUploadProgress(e.loaded / e.total);
      });

      xhr.addEventListener("load", async () => {
        const isJson = (xhr.getResponseHeader("Content-Type") || "").includes("application/json");
        let body = xhr.response;
        if (isJson) {
          body = await xhr.response
            .text()
            .then(JSON.parse)
            .catch(() => ({}));
        }
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve({ isJson, body });
          return;
        }
        reject(new Error((isJson && body.error) || "Error processing image."));
      });

      xhr.addEventListener("error", () => reject(new Error("Network error. Please try again.")));
      xhr.addEventListener("abort", () => reject(new Error("Upload cancelled.")));

      xhr.send(formData);
    });
  }

  /** Poll /jobs/<id> until it leaves the pending state. */
  function pollJob(jobId, onTick) {
    const startedAt = Date.now();

    return new Promise((resolve, reject) => {
      const tick = () => {
        fetch(`${API_BASE_URL}/jobs/${jobId}`, { credentials: "include" })
          .then(async (res) => {
            const data = await res.json().catch(() => ({}));
            if (!res.ok) throw new Error(data.error || "Lost track of that image. Please try again.");
            return data;
          })
          .then((data) => {
            if (data.status === "error") throw new Error(data.error || "Error processing image.");
            if (data.status === "done") return resolve(data);

            onTick(data);
            if (Date.now() - startedAt > POLL_CEILING_MS) {
              throw new Error("That took longer than expected. Please try again.");
            }
            setTimeout(tick, POLL_INTERVAL_MS);
          })
          .catch(reject);
      };

      tick();
    });
  }

  const PHASE_TEXT = {
    warming: "Warming up the GPU — first run in a while, so this one takes about a minute…",
    generating: "Generating your image…",
  };

  // --- FORM -----------------------------------------------------------------

  check1.addEventListener("change", () => {
    if (check1.checked) check2.checked = false;
  });
  check2.addEventListener("change", () => {
    if (check2.checked) check1.checked = false;
  });

  // Both exits from the result view lead back to a fresh upload form. The
  // download link keeps its blob URL for a few seconds so the browser can
  // finish starting the save after the view has already moved on.
  downloadLink.addEventListener("click", () => {
    setTimeout(() => resetToIdle(5000), 150);
  });

  tryAgainButton.addEventListener("click", () => resetToIdle());

  uploadForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const file = fileInput.files[0];
    if (!file) return (statusMessage.textContent = "Select a file.");

    clearResult();
    submitButton.disabled = true;
    setProgress(0);
    showView("working");
    statusMessage.textContent = "Uploading your photo…";

    const formData = new FormData();
    formData.append("myFile", file);
    formData.append("settings", JSON.stringify({ featureA: check1.checked, featureB: check2.checked }));

    try {
      const { isJson, body } = await postUpload(formData, (fraction) => {
        setProgress(fraction * UPLOAD_BAND);
      });

      // Fast OpenCV path: the PNG comes straight back, no job to poll.
      if (!isJson) {
        setProgress(100);
        showResult(body);
        return;
      }

      setProgress(UPLOAD_BAND);
      statusMessage.textContent = PHASE_TEXT[body.cold_start ? "warming" : "generating"];

      await pollJob(body.job_id, (data) => {
        const fraction = generationFraction(data.elapsed, data.estimate_seconds);
        setProgress(UPLOAD_BAND + fraction * GENERATION_BAND);
        statusMessage.textContent = PHASE_TEXT[data.phase] || PHASE_TEXT.generating;
      });

      setProgress(FETCH_MARK);
      statusMessage.textContent = "Almost there — fetching your image…";

      const res = await fetch(`${API_BASE_URL}/jobs/${body.job_id}/result`, { credentials: "include" });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.error || "Error retrieving image.");
      }

      setProgress(100);
      showResult(await res.blob());
    } catch (err) {
      // Back to the form, but the file selection is left alone so the user can
      // just hit Upload again rather than re-picking the photo.
      setProgress(0);
      showView("idle");
      statusMessage.textContent = err.message || "Error processing image.";
    } finally {
      submitButton.disabled = false;
    }
  });
});
