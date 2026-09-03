// Shared save chrome: post(), the #status save-toast, and the fcAutosave
// debounce/generation/dwell machinery. Pages provide the payload and the
// #status / #error elements; .save-toast CSS lives in base.html.
async function post(url, body, init) {
  const res = await fetch(url, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body),
    ...(init || {}),
  });
  return res.json();
}

let fcToastHideTimer = 0;
function showToast(text, kind) {
  clearTimeout(fcToastHideTimer);
  const el = document.getElementById("status");
  el.hidden = false;
  el.textContent = text;
  el.classList.toggle("saving", kind === "saving");
  el.classList.toggle("fail", kind === "fail");
  if (!el.classList.contains("show")) {
    void el.offsetWidth; // register the hidden state so the entrance transition runs
    el.classList.add("show");
  }
  if (kind === "saved") {
    fcToastHideTimer = setTimeout(() => { el.classList.remove("show"); }, 2000);
  }
}
function hideToast() {
  clearTimeout(fcToastHideTimer);
  document.getElementById("status").classList.remove("show");
}

const SAVE_PENDING_MS = 1000;
function fcAutosave(opts) {
  const debounceMs = opts.debounceMs || 350;
  let saveTimer = 0;
  let saveGen = 0;
  let savingSince = 0;
  async function run(init) {
    if (opts.gate && !opts.gate()) {
      savingSince = 0;
      hideToast();
      return;
    }
    const gen = ++saveGen;
    try {
      const data = await opts.save(init);
      if (gen !== saveGen) return;
      if (!data.ok) {
        savingSince = 0;
        opts.onFail(data.error || "");
        return;
      }
      const wait = SAVE_PENDING_MS - (Date.now() - savingSince);
      if (wait > 0) await new Promise((resolve) => setTimeout(resolve, wait));
      if (gen !== saveGen) return;
      savingSince = 0;
      opts.onSaved(data);
    } catch (err) {
      if (gen !== saveGen) return;
      savingSince = 0;
      opts.onFail(err && err.message ? err.message : "");
    }
  }
  function begin() {
    clearTimeout(saveTimer);
    if (opts.gate && !opts.gate()) {
      savingSince = 0;
      hideToast();
      return false;
    }
    if (!savingSince) savingSince = Date.now();
    showToast("Saving…", "saving");
    return true;
  }
  return {
    schedule() {
      if (opts.suppressed && opts.suppressed()) return;
      if (begin()) saveTimer = setTimeout(run, debounceMs);
    },
    // Called when the page is about to go away. `keepalive` is the only
    // thing that makes the POST survive the navigation that follows: a plain
    // fetch is aborted with the document, which is the save we are trying not
    // to lose.
    flush() {
      if (begin()) run({keepalive: true});
    },
  };
}
