/* vedun: small progressive-enhancement layer on top of server-rendered HTML + HTMX.
   Everything here is optional sugar (theme, language switch, clipboard, lightbox, drag and drop, labels). */
(function () {
  "use strict";

  /* ------------------------------------------------------------------ helpers */
  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var $$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };

  /* ------------------------------------------------------------------ i18n
     The page embeds {English source: translation} for the strings below (empty for English);
     T() falls back to the English source text, so a missing entry never breaks the UI. */
  var strings = {};
  try { strings = JSON.parse(($("#i18n-strings") || {}).textContent || "{}"); } catch (err) { /* English */ }
  function T(text, vars) {
    var out = Object.prototype.hasOwnProperty.call(strings, text) ? strings[text] : text;
    return vars ? out.replace(/\{(\w+)\}/g, function (m, k) { return Object.prototype.hasOwnProperty.call(vars, k) ? vars[k] : m; }) : out;
  }
  var lang = document.documentElement.getAttribute("lang") || "en";
  function decimal(text) { return lang === "pl" ? text.replace(".", ",") : text; }
  function capitalize(text) { return text.charAt(0).toUpperCase() + text.slice(1); }

  function toast(message, isError) {
    var host = $("#toast");
    if (!host) return;
    var el = document.createElement("div");
    el.className = "toast-item" + (isError ? " is-error" : "");
    el.textContent = message;
    host.appendChild(el);
    setTimeout(function () { el.remove(); }, isError ? 6000 : 2200);
  }

  function svgIcon(name) {
    var ns = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(ns, "svg");
    svg.setAttribute("class", "icon");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "2");
    svg.setAttribute("stroke-linecap", "round");
    svg.setAttribute("stroke-linejoin", "round");
    var use = document.createElementNS(ns, "use");
    use.setAttribute("href", "#i-" + name);
    svg.appendChild(use);
    return svg;
  }

  function formatBytes(n) {
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return decimal((n / 1024).toFixed(1)) + " kB";
    return decimal((n / 1024 / 1024).toFixed(1)) + " MB";
  }

  /* ------------------------------------------------------------------ theme */
  var root = document.documentElement;
  function currentTheme() {
    var t = root.getAttribute("data-theme");
    if (t === "dark" || t === "light") return t;
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  function syncThemeLabel() {
    var label = $("[data-theme-label]");
    if (label) label.textContent = currentTheme() === "dark" ? T("Dark theme") : T("Light theme");
  }
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-theme-toggle]");
    if (!btn) return;
    var next = currentTheme() === "dark" ? "light" : "dark";
    root.setAttribute("data-theme", next);
    try { localStorage.setItem("vedun-theme", next); } catch (err) { /* storage blocked: keep for this page only */ }
    syncThemeLabel();
  });
  syncThemeLabel();

  /* ------------------------------------------------------------------ language switch
     The link already carries ?lang= for the current URL; recompute it from the live address, because HTMX
     history pushes change the URL without re-rendering the header. The server remembers the choice in a cookie. */
  document.addEventListener("click", function (e) {
    var a = e.target.closest("[data-lang]");
    if (!a || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var url = new URL(window.location.href);
    url.searchParams.set("lang", a.getAttribute("data-lang"));
    e.preventDefault();
    window.location.assign(url.toString());
  });

  /* ------------------------------------------------------------------ clipboard */
  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
    return new Promise(function (resolve, reject) {
      var ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy") ? resolve() : reject(new Error("copy failed")); }
      catch (err) { reject(err); }
      finally { ta.remove(); }
    });
  }
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-copy]");
    if (!btn) return;
    var text = btn.getAttribute("data-copy");
    copyText(text).then(function () {
      toast(T("Copied to clipboard"));
      var label = btn.querySelector("span");
      var old = label ? label.textContent : null;
      btn.classList.add("is-done");
      if (label) label.textContent = T("Copied");
      setTimeout(function () { btn.classList.remove("is-done"); if (label && old !== null) label.textContent = old; }, 1500);
    }, function () { toast(T("Could not copy. Select the text manually."), true); });
  });

  /* ------------------------------------------------------------------ weight slider labels */
  function fmt2(v) { return decimal(Number(v).toFixed(2)); }
  document.addEventListener("input", function (e) {
    var s = e.target.closest("[data-weight-slider]");
    if (!s) return;
    var w = parseFloat(s.value);
    var a = $("[data-w-fts]"), b = $("[data-w-vec]");
    if (a) a.textContent = fmt2(w);
    if (b) b.textContent = fmt2(1 - w);
  });

  /* ------------------------------------------------------------------ long evidence text: clamp + expand */
  function setupClamp(scope) {
    $$("[data-clamp]", scope).forEach(function (box) {
      if (box.dataset.clampReady) return;
      box.dataset.clampReady = "1";
      var btn = box.parentElement.querySelector(".js-expand");
      if (!btn) return;
      var limit = parseFloat(getComputedStyle(box).fontSize) * 15;
      if (box.scrollHeight > limit * 1.15) {
        box.classList.add("is-clamped");
        btn.hidden = false;
      }
    });
  }
  document.addEventListener("click", function (e) {
    var btn = e.target.closest(".js-expand");
    if (!btn) return;
    var box = btn.parentElement.querySelector("[data-clamp]");
    var open = box.classList.toggle("is-clamped") === false;
    btn.textContent = open ? T("Collapse") : T("Show all");
    btn.setAttribute("aria-expanded", String(open));
  });

  /* ------------------------------------------------------------------ TOC toggle + active link */
  document.addEventListener("click", function (e) {
    var t = e.target.closest(".toc-toggle");
    if (!t) return;
    var list = t.closest(".toc-node").querySelector(":scope > .toc-children");
    if (!list) return;
    var willOpen = list.hidden;
    list.hidden = !willOpen;
    t.setAttribute("aria-expanded", String(willOpen));
  });
  function markActiveToc(uid) {
    $$(".toc-row.is-active").forEach(function (r) { r.classList.remove("is-active"); });
    $$(".toc-link[aria-current]").forEach(function (a) { a.removeAttribute("aria-current"); });
    if (!uid) return;
    $$(".toc-link").forEach(function (a) {
      if (a.getAttribute("data-uid") === uid) {
        a.setAttribute("aria-current", "true");
        a.closest(".toc-row").classList.add("is-active");
      }
    });
  }

  /* ------------------------------------------------------------------ lightbox */
  var dlg = $("#lightbox");
  var lbImg = $("#lightbox-img");
  var lbTitle = $("#lightbox-title");
  var lbSpin = $("#lightbox-spinner");
  var lbPage = $("#lightbox-page");
  var lbFig = $("#lightbox-figure");
  var lbUnit = $("#lightbox-unit");
  var lbState = {};

  function lbShow(src, title) {
    if (!dlg) return;
    lbSpin.hidden = false;
    lbImg.removeAttribute("src");
    lbImg.alt = title || "";
    lbImg.onload = function () { lbSpin.hidden = true; };
    lbImg.onerror = function () { lbSpin.hidden = true; toast(T("Could not load the image."), true); };
    lbImg.src = src;
    lbTitle.textContent = title || "";
    if (!dlg.open) dlg.showModal();
  }
  function lbOpen(opts) {
    lbState = opts;
    lbPage.hidden = !opts.pageSrc;
    lbFig.hidden = true;
    lbUnit.hidden = !opts.unitHref;
    if (opts.unitHref) lbUnit.setAttribute("href", opts.unitHref);
    lbShow(opts.src, opts.caption);
  }
  document.addEventListener("click", function (e) {
    var a = e.target.closest("[data-lightbox]");
    if (a) {
      e.preventDefault();
      lbOpen({
        src: a.getAttribute("href"), caption: a.dataset.caption || "",
        pageSrc: a.dataset.pageSrc || "", figSrc: a.dataset.figSrc || a.getAttribute("href"), unitHref: a.dataset.unitHref || ""
      });
      return;
    }
    var p = e.target.closest("[data-lightbox-page]");
    if (p) {
      e.preventDefault();
      lbOpen({ src: p.getAttribute("data-lightbox-page"), caption: p.dataset.caption || capitalize(T("PDF page")), pageSrc: "", figSrc: "", unitHref: "" });
    }
  });
  if (dlg) {
    $("#lightbox-close").addEventListener("click", function () { dlg.close(); });
    dlg.addEventListener("click", function (e) { if (e.target === dlg) dlg.close(); });
    lbPage.addEventListener("click", function () {
      if (!lbState.pageSrc) return;
      lbFig.hidden = !lbState.figSrc; lbPage.hidden = true;
      lbShow(lbState.pageSrc, (lbState.caption || "") + " · " + T("PDF page"));
    });
    lbFig.addEventListener("click", function () {
      lbFig.hidden = true; lbPage.hidden = !lbState.pageSrc;
      lbShow(lbState.figSrc, lbState.caption);
    });
  }

  /* ------------------------------------------------------------------ sync: drag and drop upload */
  var form = $("#sync-form");
  if (form) {
    var input = $("#sync-files", form);
    var zone = $("#dropzone", form);
    var list = $("#file-list", form);
    var summary = $("#file-summary", form);
    var msg = $("#sync-msg", form);
    var start = $("#sync-start", form);
    var files = [];

    var isPdf = function (f) { return /\.pdf$/i.test(f.name); };
    var render = function () {
      list.textContent = "";
      files.forEach(function (f, i) {
        var li = document.createElement("li");
        var name = document.createElement("span"); name.className = "file-name"; name.textContent = f.name;
        var size = document.createElement("span"); size.className = "file-size"; size.textContent = formatBytes(f.size);
        var rm = document.createElement("button");
        rm.type = "button"; rm.className = "btn btn-icon btn-ghost btn-sm"; rm.setAttribute("aria-label", T("Remove {name}", { name: f.name }));
        rm.appendChild(svgIcon("x"));
        rm.addEventListener("click", function () { files.splice(i, 1); render(); });
        li.append(name, size, rm);
        list.appendChild(li);
      });
      var total = files.reduce(function (a, f) { return a + f.size; }, 0);
      summary.hidden = files.length === 0;
      summary.textContent = files.length ? T("Selected {n} file(s), {size} in total.", { n: files.length, size: formatBytes(total) }) : "";
    };
    var addFiles = function (incoming) {
      var rejected = [];
      Array.prototype.forEach.call(incoming, function (f) {
        if (!isPdf(f)) { rejected.push(f.name); return; }
        if (!files.some(function (x) { return x.name === f.name && x.size === f.size; })) files.push(f);
      });
      if (rejected.length) toast(T("Skipped files that are not PDF: {names}", { names: rejected.join(", ") }), true);
      render();
    };

    input.addEventListener("change", function () { addFiles(input.files); input.value = ""; });
    ["dragenter", "dragover"].forEach(function (ev) {
      zone.addEventListener(ev, function (e) { e.preventDefault(); zone.classList.add("is-over"); });
    });
    ["dragleave", "drop"].forEach(function (ev) {
      zone.addEventListener(ev, function (e) { e.preventDefault(); zone.classList.remove("is-over"); });
    });
    zone.addEventListener("drop", function (e) { if (e.dataTransfer) addFiles(e.dataTransfer.files); });
    window.addEventListener("dragover", function (e) { e.preventDefault(); });
    window.addEventListener("drop", function (e) { if (!zone.contains(e.target)) e.preventDefault(); });

    var showMsg = function (text, tone) {
      msg.textContent = "";
      if (!text) return;
      var div = document.createElement("div");
      div.className = "callout callout-" + (tone || "danger");
      div.setAttribute("role", "alert");
      div.textContent = text;
      msg.appendChild(div);
    };

    form.addEventListener("submit", function (e) {
      e.preventDefault();
      if (start.disabled) return;
      showMsg("");
      var fd = new FormData();
      files.forEach(function (f) { fd.append(form.dataset.fileField || "files", f, f.name); });
      var fetchBox = $("#fetch-from-source", form);  // absent when no source plugin is configured
      fd.append(form.dataset.fieldFetch || "fetch_from_source", fetchBox && fetchBox.checked ? "true" : "false");
      start.disabled = true;
      fetch(form.dataset.uploadUrl, { method: "POST", body: fd, headers: { "Accept": "application/json", "X-Requested-With": "vedun" } })
        .then(function (r) {
          return r.text().then(function (t) {
            var data = null;
            try { data = JSON.parse(t); } catch (err) { /* not JSON */ }
            if (!r.ok) {
              var detail = data && (data.detail || data.error || data.message);
              throw new Error(typeof detail === "string" ? detail : T("response code {status}", { status: r.status }));
            }
            return data || {};
          });
        })
        .then(function (data) {
          var id = data.id || data.job_id || (data.job && data.job.id);
          if (!id) throw new Error(T("the server returned no job identifier"));
          files = []; render();
          toast(T("Job started"));
          if (window.htmx) window.htmx.ajax("GET", form.dataset.jobUrl + encodeURIComponent(id) + "?poll=1&page=sync", { target: "#live-job", swap: "innerHTML" });
        })
        .catch(function (err) { showMsg(T("Could not start the sync: {error}", { error: err.message }), "danger"); })
        .then(function () { start.disabled = false; });
    });
  }

  /* ------------------------------------------------------------------ HTMX hooks */
  function scrollLogs(scope) {
    $$(".log", scope).forEach(function (el) { el.scrollTop = el.scrollHeight; });
  }
  document.addEventListener("htmx:load", function (e) {
    setupClamp(e.target);
    scrollLogs(e.target);
  });
  document.addEventListener("htmx:afterSwap", function (e) {
    var t = e.detail && e.detail.target;
    if (t && t.id === "unit-viewer") {
      var marker = $("[data-active-unit]", t);
      markActiveToc(marker ? marker.getAttribute("data-active-unit") : null);
      if (window.matchMedia("(max-width: 1180px)").matches) t.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  });
  document.addEventListener("htmx:responseError", function (e) {
    var code = e.detail && e.detail.xhr ? e.detail.xhr.status : "";
    toast(code ? T("Server error ({code}). Try again.", { code: code }) : T("Server error. Try again."), true);
  });
  document.addEventListener("htmx:sendError", function () {
    toast(T("No connection to the server."), true);
  });

  setupClamp(document);
  scrollLogs(document);
})();
