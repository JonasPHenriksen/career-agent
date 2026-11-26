// Career Agent: small progressive enhancements. Everything works without JS except live progress.
(function () {
  "use strict";

  // Poll a URL and swap the element; reload the page when the server says the work is done.
  function poll(el) {
    const url = el.dataset.poll;
    const every = Number(el.dataset.interval || 3000);
    const reloadOnDone = el.dataset.reload !== "false";
    const tick = async () => {
      if (!document.body.contains(el)) return;
      try {
        const r = await fetch(url, { headers: { "X-Poll": "1" }, cache: "no-store" });
        if (r.headers.get("X-Done") === "1") {
          if (reloadOnDone) { location.reload(); } else { el.innerHTML = ""; }
          return;
        }
        const html = (await r.text()).trim();
        const box = document.createElement("div");
        box.innerHTML = html;
        const fresh = box.firstElementChild;
        if (fresh && el.parentNode) {
          el.replaceWith(fresh);
          if (fresh.dataset.poll) poll(fresh);
          return;
        }
      } catch (e) { /* offline or restarting: try again */ }
      setTimeout(tick, every);
    };
    setTimeout(tick, el.dataset.eager ? 50 : every);
  }

  function init(root) {
    root.querySelectorAll("[data-poll]").forEach(poll);

    // Copy buttons: <button data-copy="#selector">
    root.querySelectorAll("[data-copy]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const src = document.querySelector(btn.dataset.copy);
        if (!src) return;
        const text = "value" in src && src.value !== undefined ? src.value : src.innerText;
        let ok = false;
        try {
          await navigator.clipboard.writeText(text);
          ok = true;
        } catch (e) {  // some browsers refuse the clipboard API: the older way still works there
          const t = document.createElement("textarea");
          t.value = text;
          t.setAttribute("readonly", "");
          t.style.cssText = "position:fixed;top:0;left:0;opacity:0";
          document.body.appendChild(t);
          t.select();
          try { ok = document.execCommand("copy"); } catch (_) { ok = false; }
          t.remove();
        }
        if (!ok) { alert("Couldn't copy. Select the text and press ⌘C."); return; }
        const old = btn.textContent;
        btn.textContent = "Copied ✓";
        setTimeout(() => (btn.textContent = old), 1600);
      });
    });

    // Instruction chips: <button data-fill="#textarea" data-text="…">
    root.querySelectorAll("[data-fill]").forEach((chip) => {
      chip.addEventListener("click", () => {
        const t = document.querySelector(chip.dataset.fill);
        if (!t) return;
        t.value = t.value ? t.value.replace(/\s*$/, "") + "\n" + chip.dataset.text : chip.dataset.text;
        t.focus();
      });
    });

    // Confirm before destructive submits: <form data-confirm="…">
    root.querySelectorAll("form[data-confirm]").forEach((f) => {
      f.addEventListener("submit", (e) => { if (!confirm(f.dataset.confirm)) e.preventDefault(); });
    });

    // Submit selects on change: <select data-autosubmit>
    root.querySelectorAll("select[data-autosubmit], input[data-autosubmit]").forEach((s) => {
      s.addEventListener("change", () => s.form && s.form.requestSubmit());
    });

    // Prevent double submits and show that something is happening.
    root.querySelectorAll("form").forEach((f) => {
      f.addEventListener("submit", (e) => {
        if (e.defaultPrevented) return;
        const b = e.submitter || f.querySelector("button[type=submit], button:not([type])");
        if (b && !b.dataset.noBusy) {
          setTimeout(() => { b.classList.add("busy"); b.disabled = true; if (b.dataset.busy) b.textContent = b.dataset.busy; }, 0);
        }
      });
    });

    // Warn about unsaved edits in editors.
    root.querySelectorAll("textarea.editor").forEach((t) => {
      const initial = t.value;
      const form = t.form;
      let submitting = false;
      if (form) form.addEventListener("submit", () => { submitting = true; });
      window.addEventListener("beforeunload", (e) => {
        if (!submitting && t.value !== initial) { e.preventDefault(); e.returnValue = ""; }
      });
    });

    // Drag & drop upload zone.
    root.querySelectorAll(".dropzone").forEach((zone) => {
      const input = zone.querySelector("input[type=file]");
      ["dragenter", "dragover"].forEach((ev) => zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.add("over"); }));
      ["dragleave", "drop"].forEach((ev) => zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.remove("over"); }));
      zone.addEventListener("drop", (e) => {
        if (!input || !e.dataTransfer.files.length) return;
        input.files = e.dataTransfer.files;
        input.form.requestSubmit();
      });
      if (input) input.addEventListener("change", () => input.files.length && input.form.requestSubmit());
    });

    // Show/hide inline edit forms: <button data-toggle="#id">
    root.querySelectorAll("[data-toggle]").forEach((b) => {
      b.addEventListener("click", () => {
        const t = document.querySelector(b.dataset.toggle);
        if (t) t.hidden = !t.hidden;
      });
    });

    // Effort fits the model (as the effort_options macro renders it): five levels for Claude, thinking on/off for a
    // local model. The usual model ("") is the tier's, in data-usual-provider. A tier's effort shows once it has a model.
    root.querySelectorAll("select[data-effort-select]").forEach((s) => {
      const target = s.dataset.effortSelect ? document.querySelector(s.dataset.effortSelect)
        : s.form && s.form.querySelector('select[name="effort"]');
      if (!target) return;
      const wrap = s.dataset.effort && document.querySelector(s.dataset.effort);
      const sync = () => {
        const opt = s.selectedOptions[0];
        // An option that follows a picker at the top (Settings → steps) says which provider that is.
        const provider = (opt && opt.dataset.provider) || (s.value ? s.value.split("|")[0] : (s.dataset.usualProvider || "claude_code"));
        if (s.dataset.home) {
          // A step in Settings: its effort is the picker its model follows ("") or the other picker's ("tier|…").
          const follows = (opt && opt.dataset.follows) || s.dataset.home;
          const other = follows === "writing" ? "analysis" : "writing";
          const want = {usualClaude: target.dataset[follows + "Claude"], usualLocal: target.dataset[follows + "Local"],
                        otherValue: "tier|" + other, otherClaude: target.dataset[other + "Claude"],
                        otherLocal: target.dataset[other + "Local"]};
          if (Object.keys(want).some((k) => target.dataset[k] !== want[k])) {
            Object.assign(target.dataset, want);
            delete target.dataset.kind;  // rebuild the options with the new labels
          }
        }
        fillEffort(target, provider === "ollama" ? "local" : "claude");
        if (wrap) wrap.hidden = !s.value;
      };
      s.addEventListener("change", sync);
      sync();
    });
  }

  function fillEffort(sel, kind) {
    if (sel.dataset.kind === kind) return;
    const cur = sel.value;
    const thinking = ["high", "xhigh", "max"].includes(cur);
    const add = (value, label, selected) => sel.add(new Option(label, value, selected, selected));
    sel.innerHTML = "";
    const usual = kind === "local" ? sel.dataset.usualLocal : sel.dataset.usualClaude;
    if (usual) add("", usual, !cur);
    const other = kind === "local" ? sel.dataset.otherLocal : sel.dataset.otherClaude;
    if (sel.dataset.otherValue && other) add(sel.dataset.otherValue, other, cur === sel.dataset.otherValue);
    if (kind === "local") {
      add("low", "thinking off", ["low", "medium"].includes(cur));
      add("high", "thinking on", thinking);
    } else {
      ["low", "medium", "high", "xhigh", "max"].forEach((e) => add(e, e + (sel.dataset.suffix || ""), cur === e));
    }
    sel.dataset.kind = kind;
  }

  document.addEventListener("DOMContentLoaded", () => init(document));
})();
