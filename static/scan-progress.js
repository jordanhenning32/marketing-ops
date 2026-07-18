// Live progress for the background listening scan.
// Polls /listening/scan/status and shows an animated bar + elapsed timer while a
// scan is running, then a result summary when it finishes. Activates on any page
// that includes <div id="scan-progress"> (add data-reload-on-done="1" to reload
// the page when the scan completes, e.g. to refresh the opportunities table).
(function () {
  const el = document.getElementById("scan-progress");
  if (!el) return;
  const reloadOnDone = el.dataset.reloadOnDone === "1";

  if (!document.getElementById("scan-progress-style")) {
    const s = document.createElement("style");
    s.id = "scan-progress-style";
    s.textContent = "@keyframes scanSlide{0%{left:-35%}100%{left:100%}}";
    document.head.appendChild(s);
  }

  let wasRunning = false;
  let pollId = null;
  let tickId = null;
  let baseElapsed = 0;
  let baseAt = 0;

  function fmt(sec) {
    sec = Math.max(0, Math.floor(sec));
    return Math.floor(sec / 60) + ":" + String(sec % 60).padStart(2, "0");
  }

  function tick() {
    const e = document.getElementById("scan-elapsed");
    if (!e) return;
    const secs = baseElapsed + (Date.now() / 1000 - baseAt);
    e.textContent = fmt(secs);
    const w = document.getElementById("scan-warn");
    if (w && secs > 240) w.style.display = "block";
  }

  function showRunning() {
    if (!document.getElementById("scan-bar")) {
      el.innerHTML =
        '<div class="se-card" style="border-left:3px solid #f59e0b;">' +
        '<div style="display:flex;justify-content:space-between;align-items:center;gap:1rem;">' +
        "<strong>🎧 Scanning trader posts…</strong>" +
        '<span class="mono" id="scan-elapsed">0:00</span></div>' +
        '<div style="position:relative;height:5px;background:#1d2333;border-radius:999px;overflow:hidden;margin-top:.6rem;">' +
        '<div id="scan-bar" style="position:absolute;top:0;left:-35%;width:35%;height:100%;' +
        "background:linear-gradient(90deg,rgba(34,211,238,0),#22d3ee,#a78bfa,rgba(167,139,250,0));" +
        'animation:scanSlide 1.4s ease-in-out infinite;"></div></div>' +
        '<div class="se-text-xs se-text-fg2" style="margin-top:.5rem;">Finding, scoring, and drafting replies via the LLM — can take a minute or two. Safe to leave this open.</div>' +
        '<div id="scan-warn" class="se-text-xs" style="color:#fbbf24;margin-top:.4rem;display:none;">⚠ Taking longer than usual — the scan may be stuck. If it doesn\'t finish in another minute, close and reopen the console, then try again.</div>' +
        "</div>";
    }
    if (!tickId) tickId = setInterval(tick, 500);
  }

  function showDone(st) {
    if (tickId) { clearInterval(tickId); tickId = null; }
    const s = st.summary || {};
    const found = s.found != null ? s.found : "?";
    const drafted = s.drafted || 0;
    const took = st.elapsed_seconds != null ? " in " + fmt(st.elapsed_seconds) : "";
    const review = drafted
      ? ' · <a href="/engagement">review ' + drafted + " draft" + (drafted === 1 ? "" : "s") + " →</a>"
      : "";
    const err = st.error
      ? '<div style="color:#f87171;margin-top:.3rem;">Scan error: ' + st.error + "</div>"
      : "";
    el.innerHTML =
      '<div class="se-card" style="border-left:3px solid #34d399;">' +
      "<strong>✓ Scan complete</strong>" + took + " — found " + found + " · drafted " + drafted + review + err +
      "</div>";
  }

  async function poll() {
    let st;
    try {
      st = await (await fetch("/listening/scan/status", { cache: "no-store" })).json();
    } catch (e) {
      return; // transient; try again next interval
    }
    if (st.running) {
      wasRunning = true;
      baseElapsed = st.elapsed_seconds || 0;
      baseAt = Date.now() / 1000;
      showRunning();
      tick();
    } else if (wasRunning) {
      wasRunning = false;
      if (pollId) { clearInterval(pollId); pollId = null; }
      showDone(st);
      if (reloadOnDone) setTimeout(function () { location.reload(); }, 2000);
    }
  }

  poll();
  pollId = setInterval(poll, 1500);
})();
