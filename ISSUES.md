# Known Issues & Open Questions

Running list of bugs, limitations, and oddities found while building the async
GPU job pipeline and the upload-page rework (PR #8). Last updated 2026-08-23. Section 1 is fixed; sections 2-4 are open.

Severity is about user impact, not effort. Items are grouped by whether they
block the current PR, and each links to the code it lives in.

---

## 1. FIXED — 2026-08-23 (was: fix before merging PR #8)

All three are fixed on `upload-progress-and-preview`. Kept here rather than
deleted, because the reasoning is what stops them being reintroduced.

### 1.1 ~~Transient Modal errors are recorded as permanent failures~~ FIXED
**`backend/app.py`** (in `fetch_job_result`) — was high

The bare `except Exception` cannot distinguish "the GPU run failed" from "the
network hiccuped while asking Modal about it". `job_status` then writes that
verdict to SQLite as a *terminal* state, so a momentary blip permanently kills
a job whose image generated successfully. The user is told it failed and the
GPU time is wasted.

**Fixed by** giving `fetch_job_result` a fourth return state, `'transient'`,
distinct from `'error'`. Modal exceptions are now classified: `ConnectionError`,
`ServiceError`, `InternalError`, `InternalFailure`, `ResourceExhaustedError` and
`ClientClosed` are transient (`InternalFailure` is documented by Modal as
explicitly *retriable*); `OutputExpiredError`, `FunctionTimeoutError`,
`RemoteError` and anything the remote function itself raised are terminal. The
classes are resolved by name and cached, so a modal version that renames one
degrades to "not transient" rather than breaking the import.

`job_status` reports a transient failure as still `pending` and leaves the row
untouched so the next poll retries, giving up only past `JOB_MAX_AGE_SECONDS`
(660s — the worker's 600s timeout plus slack), at which point the job genuinely
cannot still be running. `/jobs/<id>/result` returns **503 + Retry-After**
rather than 500 for the same case.

### 1.2 ~~`uploadForm.reset()` silently downgrades the user's style choice~~ FIXED
**`frontend/js/upload.js`** (in `resetToIdle`) — was high

`form.reset()` restores HTML defaults, and the Coloring Page checkbox defaults
to unchecked. So after clicking **Download** or **Try Again**, the next upload
silently routes to the OpenCV sketch path instead of the GPU path. Different
algorithm, materially different output, no warning to the user.

Introduced by the "return to the upload page" behaviour, so it did not exist
before that change.

**Fixed by** replacing `uploadForm.reset()` with `fileInput.value = ""`, which
clears the chosen file without touching the style checkboxes. Regression-tested:
the suite now asserts the checkbox survives both Try Again and Download, and
that `form.reset()` is never called.

### 1.3 ~~`result["flux_sketch"]` is outside the `try`~~ FIXED
**`backend/app.py`** (in `fetch_job_result`) — was medium

A result-shape mismatch raises `KeyError` outside the guarded block. Three
consequences: it escapes as a 500, it leaks the open SQLite connection in
`job_status`, and it leaves the job row `pending` until the client's ~11 minute
ceiling gives up.

**Fixed by** wrapping the payload access in its own `except (KeyError,
TypeError)` guard that returns a terminal `'error'`, and by restructuring
`job_status` to hold its connection in `try/finally` so *no* unexpected raise
can leak it — not just this one.

---

## 2. Should fix soon (not strictly blocking)

### 2.1 `pollJob` has no retry
**`frontend/js/upload.js:169`**, and the single result fetch at **`:259`** — medium

One failed poll (502, dropped wifi) rejects the whole promise and discards the
job id, throwing away a result that has already been paid for in GPU time. The
job itself is still alive server-side and the row survives, so the client is
discarding recoverable work.

Fix direction: tolerate N consecutive poll failures before giving up; the job id
is the recovery handle.

### 2.2 The `/jobs/*` endpoints bypass the rate limiter
**`backend/app.py:1120`** (`job_status`) and **`:1169`** (`job_result`) — medium

Neither endpoint calls `rate_limit_retry_after`. `/jobs/<id>/result` is the
expensive one: each hit blocks one of only **4** uWSGI workers on a Modal RPC
plus a full image download. Ownership binding limits who can call it, but does
not limit how often.

This was a deliberate omission when the endpoints were written, on the
assumption that polls were cheap. That assumption holds for `/jobs/<id>` and
does not hold for `/result`.

### 2.3 Download teardown does not confirm the save started
**`frontend/js/upload.js:214`** — medium

The click handler tears down the preview, the link, and (after a grace period)
the blob URL, without any confirmation that the browser actually began the
download. If the download is blocked or cancelled, the image is simply gone and
the user has no way back to it.

Note the grace period already handles the *common* failure mode — revoking the
blob URL in the same tick can abort an in-flight save — so this is about the
blocked-download case specifically.

### 2.4 `predict_cold_start` counts a still-booting job as evidence of warmth
**`backend/app.py:893`** — low (cosmetic)

The query takes `MAX(COALESCE(finished_at, created_at))`, so a job created two
seconds ago that is *currently cold-booting* makes the next job look warm. Two
concurrent uploads: the second gets the 20s warm profile for what is really a
cold start, and its progress bar parks near 92% waiting.

Affects only bar pacing, never correctness of the image.

---

## 3. Known limitations (accepted, documented, not bugs)

### 3.1 Duration constants are guesses
**`backend/app.py`** — `WARM_ESTIMATE_SECONDS = 20`, `COLD_ESTIMATE_SECONDS = 80`

These drive the progress bar's pacing and were never calibrated. Tune them from
the `⏱️  upload-endpoint timing` and `⏱️  modal worker timing` lines in the
logs once there is real data.

### 3.2 The Modal spawn/poll path has never run against a live GPU
`modal` is not installed in `backend/.venv` (it is imported lazily and is
production-only), so `spawn()`, `FunctionCall.from_id().get(timeout=0)`, and the
`OutputExpiredError` handling are written to the documented API but have never
executed. **This is the first thing to exercise on a real deploy.**

Partially mitigated: the code review verified the exception behaviour against
modal 1.5.4 source — see §5.1.

### 3.3 A page reload mid-generation does not reconnect
The backend fully supports it (the job row survives in SQLite and is
owner-bound), but the frontend never stashes the job id, so a reload loses the
handle to a job that is still running and still costing GPU time.

Fix direction: `sessionStorage` the job id, check for it on load.

### 3.4 Anonymous jobs are IP-bound
**`backend/app.py:916`** (`job_owner`)

Unauthenticated uploads bind the job to `ip:<addr>`. A mobile user handing off
between LTE and wifi mid-generation changes address and gets a 404 on their own
job. Logged-in users are unaffected (bound to `user:<name>`).

### 3.5 Cold starts are predicted, not observed
`flux_1_kontext_modal.py` sets `scaledown_window=300`, so wall time is bimodal:
a warm run is inference only, a cold run adds a full FLUX bf16 load onto the
A100. Modal's API cannot report which you are getting — see §5.2 — so warmth is
inferred from our own dispatch history.

Observing it for real requires the worker to publish state into a `modal.Dict`,
which is the same plumbing needed for per-step progress (the deferred "option
D"). If cold-start honesty matters more than step granularity, that is the
argument for pulling it forward.

### 3.6 Multi-file upload is not supported
**`frontend/upload.html`** has no `multiple` attribute on the file input, and
**`frontend/js/upload.js`** reads only `fileInput.files[0]`. Requested during
planning; never built.

This is also the feature that would most justify a frontend library — a keyed
list of rows each with independent status, progress, result, and error is the
first thing here that is genuinely painful in imperative DOM code.

---

### 3.7 The local inference path is synchronous
`INFERENCE_BACKEND=local` posts to the resident worker and gets the PNG back on
the same request, so it returns the image directly like the OpenCV path and
**never creates a job row**. The polling machinery (`/jobs/<id>`, the phase
labels, cold-start prediction) is therefore only exercised with
`INFERENCE_BACKEND=modal`. Worth remembering when smoketesting: the local
backend cannot validate the async path.

Related: that call blocks its request for up to 180s if the worker hangs — the
same shape of problem the Modal path was refactored to avoid, but dev-only.

### 3.8 Local inference needs weights that are not on this machine
`flux_1_kontext.py` loads `~/models/flux1-kontext-dev-Q8_0.gguf`, which does not
exist here (`~/models` is absent). The GPU side is fine — the box has an **Intel
Arc Pro B70** and the venv has the matching `torch 2.12.0+xpu`, and the worker
targets `xpu`, not CUDA. Only the ~12GB quantised checkpoint is missing.

---

## 4. Pre-existing, unrelated to PR #8

### 4.1 `tour.js` fails the lint workflow
**`frontend/js/tour.js`** does not satisfy Prettier, so `npm run lint` — and
therefore CI — is likely red on `main` already, independent of any recent
change. One `npx prettier --write` fixes it, deliberately left out of PR #8 to
keep that diff clean.

### 4.2 The product tour is disabled and its positioning is fragile
**`frontend/js/upload.js:10-13`** has the tour trigger commented out
("still sandboxing the tooltip UI").

Likely why: `positionTooltip()` at **`frontend/js/tour.js:60-67`** hardcodes a
272px tooltip width and cannot flip or shift when it would overflow. This is
exactly the problem Floating UI (~6KB) exists to solve, and it would pay off
immediately without adopting a framework.

### 4.3 Watermarking is effectively off
**`backend/app.py`** — `WATERMARK_FREE_TIER = False`, and `MI_Watermark.png` is
not checked into the repo. Free-tier and anonymous output currently ships
**unwatermarked**. The code path is written and best-effort guarded; it just
never runs. Worth a deliberate decision rather than drifting.

### 4.4 Dead code left in place on purpose
`run_diffusion_workflow` / `run_remote_diffusion_workflow` in
**`backend/app.py:846+`** are no longer reachable from any route — the request
path now uses `spawn_remote_diffusion`. They are kept because the commented-out
local-inference restoration notes are written in terms of them. Delete both once
the local dev path is either restored or abandoned.

Related: `simplify_for_coloring` and `run_local_diffusion_workflow` are
commented out, and `rembg` is deliberately not imported because it is absent
from both requirements files.

### 4.5 `main.js` is dead code
`frontend/js/main.js` is referenced by **no** HTML page. It still carried a
hardcoded API URL, so it was updated alongside the others for consistency, but
it is not loaded anywhere and is a deletion candidate.

### 4.6 Untracked files in `backend/`
`flux_2_klein.py` and `test_client.py` are untracked and were deliberately left
out of the PR #8 commit. They show up as "2 uncommitted changes" warnings on
every `gh pr` command.

---

## 5. Verified correct — do not re-litigate

Recorded because each of these *looks* wrong on inspection and cost real time to
confirm.

### 5.1 The `except` ordering in `fetch_job_result` is safe
Checked against modal **1.5.4 source**: `poll_function` raises the **builtin**
`TimeoutError`, while `OutputExpiredError` derives from modal's *own*
`exception.TimeoutError`. The two clauses do not shadow each other. Also
confirmed: `poll_function` passes `clear_on_success=False`, so
`/jobs/<id>/result` re-fetching *after* `job_status` already consumed the result
works — which is what makes "Modal is the blob store" viable instead of stashing
bytes in SQLite. And `get(timeout=0)` performs exactly one non-blocking poll.

### 5.2 Modal genuinely cannot distinguish queued from running
`InputStatus` is only `PENDING / SUCCESS / FAILURE / INIT_FAILURE / TERMINATED /
TIMEOUT` — `PENDING` covers both "queued, no container yet" and "actively
denoising". `get_call_graph()` is documented as best-effort and *not* populated
in real time, so it is unfit for polling. This is why §3.5 exists.

### 5.3 Job state has to be in SQLite, not memory
`backend/uwsgi` sets `processes = 4`. The request that polls a job is usually
not the worker that created it, so in-process job state would fail
intermittently. Same reasoning the existing `confirmation_sends` and
`rate_limits` tables already follow.

### 5.4 Tailwind output is current and complete
Every new utility used in `upload.html` is present in the regenerated
`css/output.css`. Note the standing hazard: Tailwind's JIT only generates
classes it saw at build time, so **`npm run build` must be re-run after any
markup change** — this is why `tour.js` injects a plain stylesheet instead of
using Tailwind classes.

### 5.5 The JSON-vs-blob client branch works cross-origin
`Content-Type` is a CORS-safelisted response header, so the client can read it
to tell the 202-with-job-id response from the direct `image/png` response
without extra CORS config. `CORS(app, supports_credentials=True, ...)` at
**`backend/app.py:48`** is app-wide, so the new `/jobs/*` routes were already
covered.

### 5.6 The display-utility collision was real and is worked around
Toggling `hidden` on an element that also carries `flex` is a genuine conflict —
both are display utilities, and which wins depends on Tailwind's internal rule
order rather than class-attribute order. The upload form is wrapped in a plain
`#form-view` block specifically so the view switch never relies on that
ordering. Keep that wrapper.

---

## 6. Design constraints worth remembering

### 6.1 A web page cannot point at the browser's download folder
Explored and rejected during planning. Three hard limits:
- JavaScript **cannot read its own page's request headers**; `User-Agent` is not
  exposed to the document. `navigator.userAgentData` is the actual source, and
  echoing the header back from Flask is a longer path to the same string.
- The OS download **path is unknowable** to a page, and a page cannot render
  anything outside its own viewport, so it can never point at native browser
  chrome.
- Chrome, Edge, Safari, and Firefox all put that control in the **top-right** on
  desktop anyway, so per-browser sniffing buys almost nothing — and the metaphor
  collapses entirely on mobile.

If revisited: a top-right anchored toast with mobile-specific copy gets most of
the value without the fragility, and UA sniffing continues to decay as Chrome's
UA reduction proceeds.

### 6.2 `fetch` cannot report upload progress
This is why the upload leg uses `XMLHttpRequest` and `xhr.upload.onprogress`.
Not a preference — `fetch` has no equivalent with usable browser support.

---

## 7. Local tooling notes

### 7.1 `gh` uses SSH for git operations
`gh auth status` reports "Git operations protocol: ssh", so only the **API** goes
through the token. `git fetch` / `git push` from a non-interactive shell still
need `SSH_AUTH_SOCK`, and no agent is exported there — the sockets live under
`~/.ssh/agent/` with randomized names. `gh auth setup-git` would route git over
HTTPS and make both use one mechanism.

### 7.2 Two GitHub identities exist on this machine
The SSH key is `memoryillumination@gmail.com` (repo owner, push access). `gh`
was initially authenticated as `Akcl7777`, which has **pull-only** access and
therefore could not open a PR. Since resolved by re-authenticating as
`memoryillumination`, but the mismatch is easy to reintroduce.
