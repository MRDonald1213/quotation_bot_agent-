/* Freight AI Agent — local review app.
 * Renders real backend data only; every verb is one request onto an existing
 * service. Document text is untrusted input, so all interpolation goes via esc().
 * People read labels and sentences here — never uuids, enum values or stack traces.
 */
"use strict";

const view = document.getElementById("view");
const nav = document.getElementById("nav");
const pageTitle = document.getElementById("page-title");
const pageGuide = document.getElementById("page-guide");
const modal = document.getElementById("modal");
const modalTitle = document.getElementById("modal-title");
const modalBody = document.getElementById("modal-body");
const modalCancel = document.getElementById("modal-cancel");
const modalConfirm = document.getElementById("modal-confirm");
const toasts = document.getElementById("toast");

const state = {
  review: null,
  status: null,
  dirty: false,
  busy: new Set(),
  watched: null,
};

const PAGES = {
  "": {
    title: "Dashboard",
    guide:
      "What the agent read, what still waits for you, and what is already in the database.",
  },
  documents: {
    title: "Documents",
    guide:
      "Drop quotation files in, run the reading step, then open a document to check it.",
  },
  queue: {
    title: "Review queue",
    guide:
      "Every decision still waiting for a person. Nothing is discarded silently.",
  },
  shipments: {
    title: "Quotations",
    guide:
      "One lane, the carriers that quoted it, and where each quote stands.",
  },
  comparison: {
    title: "Comparison",
    guide:
      "Prices compared by the Python service — the browser never does arithmetic.",
  },
  drafts: {
    title: "Drafts",
    guide:
      "Questions for the carrier, written for you to approve and send yourself.",
  },
  settings: {
    title: "System",
    guide:
      "What runs on this computer, where your data lives, and what to check if something fails.",
  },
};

// ------------------------------------------------------------------ helpers
function esc(value) {
  if (value === null || value === undefined) return "";
  return String(value).replace(
    /[&<>"']/g,
    (ch) =>
      ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      })[ch],
  );
}

function show(value, fallback) {
  if (value === null || value === undefined || value === "") {
    return `<span class="v soft">${esc(fallback || "not extracted")}</span>`;
  }
  return esc(value);
}

function chip(label, kind) {
  return `<span class="chip ${kind || ""}">${esc(label)}</span>`;
}

const FIELD_LABELS = {
  carrier_name: "Carrier",
  quote_reference: "Quote reference",
  shipment_reference: "Shipment reference",
  currency: "Currency",
  base_price: "Base price",
  charges: "Extra charges",
  known_additional_charges: "Extra charges",
  total_known_cost: "Known total",
  delivery_scope: "Delivery scope",
  origin: "Origin",
  destination: "Destination",
  transit_days: "Transit time",
  transit_days_min: "Transit time",
  transit_days_max: "Transit time",
  valid_until: "Valid until",
  insurance_included: "Insurance included",
  customs_included: "Customs included",
  included_services: "Services included",
  excluded_services: "Services excluded",
  weight_kg: "Weight",
  volume_m3: "Volume",
  cargo_description: "Cargo",
  route_category: "Route type",
  name: "Name",
  target_currency: "Target currency",
};

function field(name) {
  if (!name) return "Field";
  if (FIELD_LABELS[name]) return FIELD_LABELS[name];
  return String(name)
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

// A route endpoint on a document is a full delivery address; people only need the city.
const ADDRESS_NOISE =
  /(warehouse|terminal|depot|zone|street|str\.?|avenue|ave\.?|road|rd\.?|building|bldg\.?|block|floor|industrial|logistics|office|no\.?)/i;

function place(value) {
  if (!value) return "unknown";
  const parts = String(value)
    .split(/[,;]|\s{2,}/)
    .map((s) => s.trim())
    .filter(Boolean);
  for (const part of parts) {
    if (part.length <= 30 && !ADDRESS_NOISE.test(part)) return part;
  }
  return (parts[0] || String(value)).slice(0, 30);
}

function clip(value, max) {
  const text = String(value || "")
    .replace(/\s+/g, " ")
    .trim();
  return text.length > max ? text.slice(0, max - 1).trimEnd() + "…" : text;
}

function fileName(value) {
  const name = String(value || "document");
  return name.length > 46 ? name.slice(0, 24) + "…" + name.slice(-16) : name;
}

const REVIEW_CHIPS = {
  extracted: ["Awaiting your review", "pending"],
  approved: ["Approved — ready to import", "info"],
  imported: ["In the database", "ok"],
  rejected: ["Rejected", "bad"],
};

function reviewChip(status) {
  const [label, kind] = REVIEW_CHIPS[status] || [status || "unknown", ""];
  return chip(label, kind);
}

function reviewWord(status) {
  return (
    {
      extracted: "awaiting your review",
      approved: "approved and waiting",
      imported: "already imported",
      rejected: "rejected",
    }[status] || String(status || "unknown")
  );
}

function classificationChip(row) {
  const map = {
    pending: ["Not read yet", "pending"],
    relevant: ["Freight quote", "ok"],
    needs_review: ["Not sure — check", "warn"],
    irrelevant: ["Not a quote", "plain"],
    failed: ["Could not classify", "bad"],
  };
  const [label, kind] = map[row.classification] || [row.classification, ""];
  return chip(label, kind);
}

function processingChip(row) {
  const map = {
    pending: ["Not processed", "pending"],
    processed: ["Read and extracted", "ok"],
    needs_review: ["Needs review", "warn"],
    failed: ["Failed", "bad"],
    skipped: ["Skipped", "plain"],
  };
  const [label, kind] = map[row.processing] || [row.processing, ""];
  return chip(label, kind);
}

const SEVERITY = {
  critical: ["Blocks comparison", "critical"],
  important: ["Should confirm", "warn"],
  informational: ["Good to know", "plain"],
};

function severityChip(value) {
  const [label, kind] = SEVERITY[value] || [value || "open", "plain"];
  return chip(label, kind);
}

const SCOPE = {
  port_to_port: "port to port",
  door_to_door: "door to door",
  door_to_port: "door to port",
  port_to_door: "port to door",
  unknown: "not stated",
};

function scope(value) {
  return SCOPE[value] || value || "not stated";
}

function fileSize(bytes) {
  if (bytes === null || bytes === undefined) return "";
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1048576) return (bytes / 1024).toFixed(1) + " KB";
  return (bytes / 1048576).toFixed(1) + " MB";
}

function when(value) {
  return value ? String(value).replace("T", " ").slice(0, 16) : "";
}

function ago(value) {
  if (!value) return "";
  const seconds = (Date.now() - new Date(value).getTime()) / 1000;
  if (!(seconds >= 0)) return when(value);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return Math.floor(seconds / 60) + " min ago";
  if (seconds < 86400) return Math.floor(seconds / 3600) + " h ago";
  return Math.floor(seconds / 86400) + " d ago";
}

function plural(count, singular, suffix) {
  return `${count} ${singular}${count === 1 ? "" : suffix || "s"}`;
}

function toast(message, kind) {
  const node = document.createElement("div");
  node.className = "toast-item" + (kind === "error" ? " err" : "");
  node.textContent = message;
  toasts.appendChild(node);
  setTimeout(() => node.remove(), kind === "error" ? 9000 : 4500);
}

function errorText(exc) {
  return exc && exc.message ? exc.message : String(exc);
}

// One modal for every question the app asks the human.
function openModal(options) {
  return new Promise((resolve) => {
    modalTitle.textContent = options.title || "Confirm";
    modalBody.innerHTML = options.body || "";
    modalConfirm.textContent = options.confirmLabel || "Confirm";
    modalConfirm.className = "btn" + (options.danger ? " destructive" : "");
    modal.hidden = false;
    const input = options.input ? modalBody.querySelector("textarea") : null;

    function finish(value) {
      modal.hidden = true;
      modalCancel.removeEventListener("click", onCancel);
      modalConfirm.removeEventListener("click", onConfirm);
      document.removeEventListener("keydown", onKey);
      resolve(value);
    }
    function onCancel() {
      finish(null);
    }
    function onConfirm() {
      const value = input ? input.value.trim() : true;
      if (input && !value) {
        toast(
          "A short reason is needed — it is kept in the audit trail.",
          "error",
        );
        input.focus();
        return;
      }
      finish(value);
    }
    function onKey(event) {
      if (event.key === "Escape") finish(null);
    }
    modalCancel.addEventListener("click", onCancel);
    modalConfirm.addEventListener("click", onConfirm);
    document.addEventListener("keydown", onKey);
    (input || modalConfirm).focus();
  });
}

async function confirmDialog(options) {
  return (await openModal(options)) === true;
}

// Replaces window.prompt(), which the app view does not support.
async function askReason(options) {
  const body = `<p>${options.body || ""}</p>
    <label class="label" for="reason-input">${esc(options.label || "Reason")}</label>
    <textarea id="reason-input" placeholder="${esc(options.placeholder || "")}">${esc(options.value || "")}</textarea>`;
  const value = await openModal({
    title: options.title,
    body,
    confirmLabel: options.confirmLabel,
    danger: options.danger,
    input: true,
  });
  return value;
}

function setDirty(flag) {
  state.dirty = flag;
}

// Guards against double submissions on every async verb.
async function busy(key, work) {
  if (state.busy.has(key)) return null;
  state.busy.add(key);
  try {
    return await work();
  } catch (exc) {
    toast(errorText(exc), "error");
    return null;
  } finally {
    state.busy.delete(key);
  }
}

function skeleton(message) {
  view.innerHTML = `<p class="skeleton">${esc(message || "loading…")}</p>`;
}

function empty(message) {
  return `<div class="empty">${esc(message)}</div>`;
}

function fail(exc) {
  view.innerHTML = `<div class="note bad">${esc(errorText(exc))}</div>
    <p><button class="btn neutral" data-act="reload">Try again</button></p>`;
}

function limits() {
  return (
    (state.status && state.status.limits) || {
      max_file_size_mb: 25,
      supported_extensions: [],
      confidence_threshold: 0.8,
    }
  );
}

function jobLine(job) {
  if (!job) return "";
  if (job.status === "succeeded")
    return `<span class="jobline"><span class="dot ok"></span> Processing result ready — check the details below</span>`;
  if (job.status === "failed") {
    return `<span class="jobline"><span class="dot bad"></span> Stopped: ${esc(job.error || "the run did not finish")}</span>`;
  }
  return `<span class="jobline"><span class="dot warn"></span> Reading this document — ${esc(clip(job.progress, 60))}</span>`;
}

// ---------------------------------------------------------------- system bar
async function refreshStatus() {
  try {
    state.status = await api.get("/api/status");
  } catch (exc) {
    state.status = null;
  }
  const host = document.getElementById("system");
  if (!host) return;
  if (!state.status) {
    host.innerHTML = `<div class="line bad"><span class="dot bad"></span> Backend is not answering</div>
      <div class="line">Start it with <code class="path">freight review</code></div>`;
    return;
  }
  const s = state.status;
  const modelLine = !s.ollama.reachable
    ? `<div class="line bad"><span class="dot bad"></span> Local model is offline</div>`
    : s.ollama.model_installed === false
      ? `<div class="line warn"><span class="dot warn"></span> ${esc(s.ollama.model)} is not installed</div>`
      : `<div class="line"><span class="dot ok"></span> ${esc(s.ollama.model || "local model")}</div>`;
  host.innerHTML =
    modelLine +
    `<div class="line"><span class="dot ${s.database.initialized ? "ok" : "warn"}"></span> Database ${s.database.initialized ? "ready" : "needs init"}</div>` +
    `<div class="line"><a href="#/settings">System details</a></div>`;
}

// -------------------------------------------------------------------- routes
function parseHash() {
  const raw = (location.hash || "#/").replace(/^#\/?/, "");
  const parts = raw.split("/").filter(Boolean);
  return { page: parts[0] || "", id: parts[1] || null };
}

function markActive(page) {
  for (const link of nav.querySelectorAll("a")) {
    const linkPage = (link.getAttribute("href") || "#/")
      .replace(/^#\/?/, "")
      .split("/")[0];
    link.classList.toggle("active", linkPage === (page || ""));
  }
}

async function route() {
  if (state.dirty) {
    const keep = await confirmDialog({
      title: "Discard unsaved edits?",
      body: "<p>You changed fields or notes that are not saved yet. Leaving this page discards them.</p>",
      confirmLabel: "Discard and leave",
      danger: true,
    });
    if (!keep) {
      setDirty(true);
      return;
    }
    setDirty(false);
  }
  const { page, id } = parseHash();
  markActive(page);
  const meta = PAGES[page] || PAGES[""];
  pageTitle.textContent = meta.title;
  pageGuide.textContent = meta.guide;
  await refreshStatus();
  if (page === "") return pageDashboard();
  if (page === "documents") return id ? pageReview(id) : pageDocuments();
  if (page === "queue") return pageQueue();
  if (page === "shipments") return id ? pageShipment(id) : pageShipments();
  if (page === "comparison")
    return id ? pageComparison(id) : pageComparisonIndex();
  if (page === "drafts") return pageDrafts();
  if (page === "settings") return pageSettings();
  view.innerHTML = empty("This page does not exist.");
}

async function loadReview() {
  state.review = await api.get("/api/review");
  return state.review;
}

function findShipment(id) {
  const review = state.review || { shipments: [] };
  return (
    review.shipments.find((s) => s.id === id || s.reference === id) || null
  );
}

function labelOf(shipment) {
  return shipment.label || shipment.route || shipment.reference;
}

// ------------------------------------------------------------------ dashboard
async function pageDashboard() {
  skeleton();
  let data;
  try {
    data = await api.get("/api/dashboard");
  } catch (exc) {
    return fail(exc);
  }
  const c = data.counts;
  const live = data.jobs.filter(
    (j) => j.status === "queued" || j.status === "running",
  );
  const failed = data.recent.filter(
    (d) => d.processing === "failed" || d.classification === "failed",
  ).length;
  const attention = [];
  if (c.proposals)
    attention.push([
      plural(c.proposals, "new route", "s"),
      "awaiting your approval or correction",
      "#/queue",
    ]);
  if (c.awaiting_review)
    attention.push([
      plural(c.awaiting_review, "extracted quote"),
      "awaiting your review",
      "#/queue",
    ]);
  if (c.ready_to_import)
    attention.push([
      plural(c.ready_to_import, "approved quote"),
      "ready to import into the database",
      "#/queue",
    ]);
  if (c.gaps)
    attention.push([
      plural(c.gaps, "item", "s"),
      "still missing from the quotes the agent read",
      "#/shipments",
    ]);
  if (c.drafts)
    attention.push([
      plural(c.drafts, "question draft"),
      "ready for you to approve and send yourself",
      "#/drafts",
    ]);
  if (failed)
    attention.push([
      plural(failed, "document"),
      "could not be read — open it to see why",
      "#/documents",
    ]);
  if (live.length)
    attention.push([
      plural(live.length, "document"),
      "being read right now",
      "#/documents",
    ]);

  const ollamaNote = !state.status
    ? '<div class="note bad">The backend is not answering. Start it with <code>freight review</code> in a terminal.</div>'
    : state.status.ollama.reachable
      ? state.status.ollama.model_installed === false
        ? `<div class="note warn">Ollama is running but <strong>${esc(state.status.ollama.model)}</strong> is not installed. Run <code>ollama pull ${esc(state.status.ollama.model)}</code>.</div>`
        : ""
      : '<div class="note bad">Ollama is not running, so documents cannot be read. Start it with <code>ollama serve</code>. Nothing is ever sent to a cloud model.</div>';

  view.innerHTML = `
    <section class="hero rise">
      <h2>Carrier quotes, read and checked — so you compare prices, not PDFs.</h2>
      <p>Drop a quotation in. The agent reads it on this computer, pulls out the price, currency,
         transit time, delivery scope and every extra charge, shows the exact line in the document
         behind each figure, and asks before anything is written to the database. What a carrier
         did not state stays on screen as a question — it is never guessed and never treated as zero.</p>
      <div class="steps">
        <div class="step rise-2"><i>1</i><b>Upload</b><span>Quotes and carrier replies, parsed and hashed locally.</span></div>
        <div class="step rise-2"><i>2</i><b>Check</b><span>Each figure next to the sentence it came from.</span></div>
        <div class="step rise-3"><i>3</i><b>Compare</b><span>Totals computed by the Python service, then exported.</span></div>
      </div>
    </section>

    <h2>Needs your attention</h2>
    ${
      attention.length
        ? `<div class="list">${attention
            .map(
              ([count, rest, href], i) => `
      <a class="panel rise${i > 1 ? "-2" : ""}" href="${esc(href)}" style="text-decoration:none;color:inherit;display:flex;justify-content:space-between;align-items:center;gap:12px">
        <span><strong>${esc(count)}</strong> <span class="meta">${esc(rest)}</span></span>
        <span class="meta">open →</span>
      </a>`,
            )
            .join("")}</div>`
        : empty(
            "Nothing waits for a decision right now. New uploads appear here automatically.",
          )
    }

    ${ollamaNote ? `<div style="margin-top:14px">${ollamaNote}</div>` : ""}

    <p class="meta" style="margin-top:18px">${plural(c.all_documents, "document")} in the workspace ·
      ${plural(c.documents, "document")} awaiting a decision ·
      ${plural(c.imported, "quote")} in the database · ${plural(c.ready_to_import, "quote")} approved and waiting to import</p>

    <h2>Recent documents</h2>
    ${
      data.recent.length
        ? `<div class="panel"><table>
      <thead><tr><th>Document</th><th>What the agent decided</th><th>Next step</th></tr></thead>
      <tbody>${data.recent
        .map(
          (d) => `
        <tr>
          <td><a class="name" href="${esc(d.target)}">${esc(fileName(d.file))}</a>
            <div class="sub">${esc(ago(d.created_at))}</div></td>
          <td>${classificationChip(d)} ${processingChip(d)}
            ${d.reason ? `<div class="sub">${esc(clip(d.reason, 110))}</div>` : ""}</td>
          <td><a href="${esc(d.target)}">${esc(d.next_action)}</a></td>
        </tr>`,
        )
        .join("")}</tbody></table></div>`
        : empty(
            "No documents yet. Upload a quotation on the Documents page to start.",
          )
    }

    <h2>Recent changes</h2>
    ${
      data.activity.length
        ? `<div class="list">${data.activity
            .map(
              (a) => `
      <div class="panel" style="display:flex;justify-content:space-between;gap:12px">
        <span>${esc(a.text)}</span><span class="meta">${esc(ago(a.at))}</span>
      </div>`,
            )
            .join("")}</div>`
        : `<p class="meta">Nothing recorded yet.</p>`
    }

    <div class="row" style="margin-top:20px">
      <a class="btn big" href="#/documents">Upload documents</a>
      <a class="btn neutral big" href="#/queue">Review queue</a>
      <button class="btn neutral big" data-act="reload">Refresh</button>
    </div>`;
}

// ------------------------------------------------------------------ documents
async function pageDocuments() {
  skeleton();
  let rows;
  try {
    rows = await api.get("/api/documents");
  } catch (exc) {
    return fail(exc);
  }
  const allowed = limits().supported_extensions;
  view.innerHTML = `
    <div class="drop" id="drop" tabindex="0">
      <div class="title">Drop quotation files here</div>
      <div class="meta">or choose files below · parsed, hashed and stored on this machine only</div>
      <div class="row" style="justify-content:center;margin-top:12px">
        <input type="file" id="picker" multiple style="border:0;padding:0" aria-label="Choose files to upload">
        <button class="btn" id="upload-btn" disabled>Upload</button>
      </div>
      <div class="meta">${esc(allowed.join(" "))} · up to ${esc(limits().max_file_size_mb)} MB each</div>
    </div>
    <div id="upload-result" class="list" style="margin-top:12px"></div>
    <div class="row spread" style="margin-top:20px">
      <div class="row tight">
        <input type="search" id="q" placeholder="Search file name or reason" aria-label="Search documents">
        <select id="f-status" aria-label="Filter by status">
          <option value="">All statuses</option>
          <option value="needs_review">Needs review</option>
          <option value="pending">Not processed</option>
          <option value="processed">Read and extracted</option>
          <option value="failed">Failed</option>
          <option value="relevant">Freight quote</option>
          <option value="irrelevant">Not a quote</option>
        </select>
        <select id="f-kind" aria-label="Filter by file type">
          <option value="">All types</option>
          ${allowed.map((ext) => `<option value="${esc(ext)}">${esc(ext)}</option>`).join("")}
        </select>
      </div>
      <button class="btn neutral" data-act="reload">Refresh</button>
    </div>
    <div id="doc-table" style="margin-top:12px">${documentTable(rows)}</div>`;

  const drop = document.getElementById("drop");
  const picker = document.getElementById("picker");
  const uploadBtn = document.getElementById("upload-btn");
  let staged = [];
  const renderStaged = (clear) => {
    if (clear) staged = [];
    uploadBtn.disabled = staged.length === 0;
    uploadBtn.textContent = staged.length
      ? `Upload ${staged.length} file(s)`
      : "Upload";
  };
  const stage = (files) => {
    staged = staged.concat(Array.from(files));
    renderStaged();
  };
  picker.addEventListener("change", () => stage(picker.files));
  ["dragenter", "dragover"].forEach((evt) =>
    drop.addEventListener(evt, (event) => {
      event.preventDefault();
      drop.classList.add("over");
    }),
  );
  ["dragleave", "dragend"].forEach((evt) =>
    drop.addEventListener(evt, () => drop.classList.remove("over")),
  );
  drop.addEventListener("drop", (event) => {
    event.preventDefault();
    drop.classList.remove("over");
    if (event.dataTransfer && event.dataTransfer.files.length)
      stage(event.dataTransfer.files);
  });
  uploadBtn.addEventListener("click", () => runUpload(staged, renderStaged));

  const tableHost = document.getElementById("doc-table");
  const apply = () => {
    const needle = document.getElementById("q").value.trim().toLowerCase();
    const status = document.getElementById("f-status").value;
    const kind = document.getElementById("f-kind").value;
    tableHost.innerHTML = documentTable(
      rows.filter((r) => {
        if (status && r.processing !== status && r.classification !== status)
          return false;
        if (kind && !r.file.toLowerCase().endsWith(kind)) return false;
        if (
          needle &&
          !(
            r.file.toLowerCase().includes(needle) ||
            (r.reason || "").toLowerCase().includes(needle)
          )
        )
          return false;
        return true;
      }),
    );
  };
  ["q", "f-status", "f-kind"].forEach((id) =>
    document.getElementById(id).addEventListener("input", apply),
  );
}

function documentTable(rows) {
  if (!rows.length) {
    return empty(
      "No documents yet. Upload one above — it is parsed and hash-checked on this machine.",
    );
  }
  return `<div class="panel"><table>
    <thead><tr><th>Document</th><th class="num">Size</th><th>What the agent decided</th><th>Reading</th><th>Lane</th><th></th></tr></thead>
    <tbody>${rows
      .map(
        (r) => `
      <tr>
        <td><a class="name" href="#/documents/${esc(r.id)}">${esc(fileName(r.file))}</a>
          ${r.reason ? `<div class="sub">${esc(clip(r.reason, 110))}</div>` : ""}</td>
        <td class="num meta">${esc(fileSize(r.size_bytes))}<div class="sub">${esc(ago(r.created_at))}</div></td>
        <td>${classificationChip(r)}${
          r.confidence !== null && r.confidence !== undefined
            ? ` <span class="meta">${esc(Math.round(r.confidence * 100))}% sure</span>`
            : ""
        }</td>
        <td>${processingChip(r)}</td>
        <td class="meta">${r.shipment_label ? `<a href="#/shipments/${esc(r.shipment_id)}">${esc(r.shipment_label)}</a>` : "—"}</td>
        <td class="row tight">
          <a class="btn neutral small" href="#/documents/${esc(r.id)}">Open</a>
          <button class="btn small" data-act="process" data-id="${esc(r.id)}"
            data-force="${r.processing === "processed" ? "1" : "0"}"
            >${r.processing === "processed" ? "Read again" : "Read this document"}</button>
          ${r.classification !== "irrelevant" ? `<button class="btn small destructive" data-act="ignore" data-id="${esc(r.id)}" data-file="${esc(r.file)}">Not a quote</button>` : `<span class="chip plain">Retained as not a quote</span>`}
        </td>
      </tr>`,
      )
      .join("")}</tbody></table></div>`;
}

async function runUpload(files, renderStaged) {
  if (!files.length) return;
  const host = document.getElementById("upload-result");
  const button = document.getElementById("upload-btn");
  await busy("upload", async () => {
    button.disabled = true;
    button.textContent = "Uploading…";
    host.innerHTML = `<p class="skeleton">Uploading ${files.length} file(s) — parsing and hashing happen on this machine.</p>`;
    const result = await api.upload(files, "error");
    host.innerHTML = `<div class="panel"><table>
      <thead><tr><th>File</th><th>Result</th><th></th></tr></thead>
      <tbody>${result.results
        .map(
          (entry) => `
        <tr>
          <td class="name">${esc(fileName(entry.file))}</td>
          <td>${chip(
            entry.status === "registered"
              ? "Stored"
              : entry.status === "duplicate"
                ? "Already imported"
                : "Rejected",
            entry.status === "registered"
              ? "ok"
              : entry.status === "duplicate"
                ? "warn"
                : "bad",
          )}
            <div class="sub">${esc(clip(entry.error || entry.message || "", 140))}</div></td>
          <td class="row tight">
            ${
              entry.intake_id && entry.status !== "duplicate"
                ? `<a class="btn small neutral" href="#/documents/${esc(entry.intake_id)}">Open</a>
                 <button class="btn small" data-act="process" data-id="${esc(entry.intake_id)}" data-force="0">Read this document</button>`
                : ""
            }
            ${
              entry.status === "duplicate" && entry.intake_id
                ? `<button class="btn small neutral" data-act="skip" data-id="${esc(entry.intake_id)}">Keep the existing copy</button>
                 <button class="btn small subtle" data-act="process" data-id="${esc(entry.intake_id)}" data-force="1">Read it again anyway</button>`
                : ""
            }
          </td>
        </tr>`,
        )
        .join("")}</tbody></table></div>`;
    const ok = result.results.filter((r) => r.status === "registered").length;
    toast(
      ok
        ? `${ok} file(s) stored locally`
        : "nothing was stored — see the result above",
      ok ? "ok" : "error",
    );
    renderStaged(true);
  });
  renderStaged(true);
}

// ---------------------------------------------------------- review workspace
async function pageReview(intakeId) {
  skeleton("loading document…");
  let doc;
  try {
    doc = await api.get("/api/documents/" + encodeURIComponent(intakeId));
  } catch (exc) {
    return fail(exc);
  }
  try {
    await loadReview();
  } catch (exc) {
    toast(errorText(exc), "error");
  }
  const shipment = doc.shipment ? findShipment(doc.shipment.id) : null;
  const quotations = doc.quotations || [];
  const hasQuote = quotations.length > 0;

  view.innerHTML = `
    <div class="row spread">
      <div class="row tight">
        <a class="btn neutral small" href="#/documents">Back to documents</a>
        <a class="btn neutral small" href="#/queue">Review queue</a>
      </div>
      <div class="row tight" id="job-slot"></div>
    </div>
    <div class="split" style="margin-top:14px">
      <section class="panel">
        <h3>${esc(fileName(doc.file))}</h3>
        <div class="field-pair">
          <div><div class="k">Received</div><div class="v">${esc(when(doc.created_at))}</div></div>
          <div><div class="k">Size</div><div class="v">${esc(fileSize(doc.size_bytes))}</div></div>
          <div><div class="k">Pages of text</div><div class="v">${esc(doc.chars)} <span class="v soft">characters</span></div></div>
        </div>
        ${doc.sender ? `<p class="meta">From ${esc(doc.sender)}${doc.subject ? " · " + esc(clip(doc.subject, 80)) : ""}</p>` : ""}
        ${doc.reason ? `<div class="note warn" style="margin-top:8px">${esc(clip(doc.reason, 400))}</div>` : ""}
        <div class="row tight" style="margin:12px 0">
          ${
            doc.classification === "irrelevant"
              ? '<button class="btn neutral" disabled>Marked not a quote</button>'
              : `<button class="btn" data-act="process" data-id="${esc(doc.id)}" data-force="${hasQuote ? "1" : "0"}"
            >${hasQuote ? "Read this document again" : "Read this document"}</button>`
          }
          <a class="btn neutral" href="/api/documents/${esc(doc.id)}/source" download="${esc(doc.file)}">Open the original</a>
        </div>
        <p class="meta">The text below is the document itself. It is data only: nothing written inside it can
          instruct the agent, and macros, scripts and links in the file are never run.</p>
        <pre class="text" style="margin-top:8px">${esc(doc.text)}${doc.text_truncated ? "\n… truncated for display; the full text stays in the local workspace." : ""}</pre>
      </section>

      <section class="list">
        ${
          doc.shipment
            ? shipmentCard(doc.shipment, shipment)
            : '<div class="empty">No lane is linked to this document yet. Reading it resolves the route and proposes one for your approval.</div>'
        }
        ${
          hasQuote
            ? quotations.map((block) => quoteBlock(block, shipment)).join("")
            : '<div class="empty">Nothing has been extracted from this document yet.</div>'
        }
      </section>
    </div>`;

  watchExistingJob(intakeId);
}

function shipmentCard(docShipment, fullShipment) {
  const editable = !!fullShipment && fullShipment.status === "needs_review";
  const evidence = (fullShipment && fullShipment.route_evidence) || [];
  return `<section class="panel">
    <div class="row spread">
      <h3>${esc(labelOf(fullShipment || docShipment))}</h3>
      ${chip(editable ? "Proposed lane — needs your approval" : "Lane approved", editable ? "warn" : "ok")}
    </div>
    <div class="field-pair">
      <div><div class="k">Origin</div><div class="v">${show(place(docShipment.origin), "not stated")}</div></div>
      <div><div class="k">Destination</div><div class="v">${show(place(docShipment.destination), "not stated")}</div></div>
      <div><div class="k">Route type</div><div class="v">${show(docShipment.route_category, "not stated")}</div></div>
    </div>
    <p class="meta">As written in the document: ${esc(clip(docShipment.origin || "?", 90))} → ${esc(clip(docShipment.destination || "?", 90))}</p>
    ${
      evidence.length
        ? `<p class="evidence">Where the route came from:
      ${evidence
        .map((e) =>
          typeof e === "string"
            ? esc(e)
            : esc(
                [field(e.field_name), e.page_or_sheet]
                  .filter(Boolean)
                  .join(" · "),
              ) +
              (e.source_filename
                ? " in " + esc(fileName(e.source_filename))
                : "") +
              (e.snippet ? ` — <q>${esc(clip(e.snippet, 120))}</q>` : ""),
        )
        .join("; ")}</p>`
        : ""
    }
    ${
      editable
        ? `
      <div class="row tight" style="margin-top:10px">
        <input type="text" id="sh-origin" placeholder="Origin" value="${esc(docShipment.origin || "")}" aria-label="Origin">
        <input type="text" id="sh-destination" placeholder="Destination" value="${esc(docShipment.destination || "")}" aria-label="Destination">
      </div>
      <div class="row tight" style="margin-top:6px">
        <button class="btn small" data-act="shipment-save" data-id="${esc(docShipment.id)}">Save correction</button>
        <button class="btn small subtle" data-act="shipment-approve" data-id="${esc(docShipment.id)}"
          data-updated="${esc(fullShipment.updated_at || "")}">Approve this lane</button>
        <button class="btn small destructive" data-act="shipment-reject" data-id="${esc(docShipment.id)}">Reject</button>
      </div>
      <p class="meta">Approving the lane also prepares the questions for the carrier.</p>`
        : ""
    }
  </section>`;
}

function quoteBlock(block, shipment) {
  const q = block.quote;
  const gaps = block.gaps || [];
  const evidence = block.evidence || [];
  const blocking = gaps.filter((g) => g.severity === "critical").length;
  const charges = q.known_additional_charges || [];
  const yesNo = (value) =>
    value === null || value === undefined ? null : value ? "yes" : "no";
  const row = (key, value) => `<li>
      <div class="row spread"><strong>${esc(field(key))}</strong>${
        value === null || value === undefined || value === ""
          ? chip("Missing", "pending")
          : chip("Extracted", "ok")
      }</div>
      <div>${show(value)}</div>
    </li>`;
  const carrier = q.carrier || "carrier not named";
  return `<section class="panel">
    <div class="row spread">
      <h3>${esc(carrier)}${q.quote_reference ? " · " + esc(q.quote_reference) : ""} <span class="meta">version ${esc(q.version)}</span></h3>
      ${reviewChip(q.review_status)}
    </div>
    <div class="field-pair">
      <div><div class="k">Currency</div><div class="v">${show(q.currency, "not stated")}</div></div>
      <div><div class="k">Base price</div><div class="v">${show(q.base_price)}</div></div>
      <div><div class="k">Known total</div><div class="v">${show(q.total_known_cost, "cannot be totalled yet")}</div></div>
      <div><div class="k">Comparison</div><div class="v">${show(q.comparison_status, "not comparable yet")}</div></div>
    </div>
    ${
      charges.length
        ? `<p class="evidence">Extra charges: ${charges
            .map(
              (c) =>
                esc(c.label) +
                " " +
                esc(c.amount === null ? "price not stated" : c.amount) +
                esc(c.currency ? " " + c.currency : "") +
                (c.included_in_base_price === false
                  ? " (not in the base price)"
                  : ""),
            )
            .join(" · ")}</p>`
        : ""
    }
    ${q.included_services && q.included_services.length ? `<p class="evidence">Included: ${q.included_services.map(esc).join(", ")}</p>` : ""}
    ${q.excluded_services && q.excluded_services.length ? `<p class="evidence">Excluded: ${q.excluded_services.map(esc).join(", ")}</p>` : ""}
    ${q.assumptions && q.assumptions.length ? `<p class="evidence">Assumptions: ${q.assumptions.map(esc).join("; ")}</p>` : ""}

    <h4>What was extracted, and from which line</h4>
    <ul class="fields">
      ${row("quote_reference", q.quote_reference)}
      ${row("origin", q.origin)}
      ${row("destination", q.destination)}
      ${row(
        "transit_days",
        q.transit_days_min === null || q.transit_days_min === undefined
          ? null
          : q.transit_days_min + "–" + q.transit_days_max + " days",
      )}
      ${row("valid_until", q.valid_until)}
      ${row("delivery_scope", q.delivery_scope ? scope(q.delivery_scope) : null)}
      ${row("insurance_included", yesNo(q.insurance_included))}
      ${row("customs_included", yesNo(q.customs_included))}
      ${row("base_price", q.base_price === null ? null : q.base_price + " " + (q.currency || ""))}
    </ul>
    ${
      evidence.length
        ? `<h4>Evidence (${evidence.length})</h4><ul class="fields">
      ${evidence
        .map(
          (e) => `
        <li>
          <div class="row spread">
            <strong>${esc(field(e.field_name))}</strong>
            ${
              e.validation_status === "needs_review"
                ? chip("Not confirmed", "warn")
                : e.validation_status === "rejected"
                  ? chip("Sent back", "bad")
                  : e.validation_status === "accepted"
                    ? chip("Confirmed", "ok")
                    : chip("New", "plain")
            }
          </div>
          <div>Read as: <strong>${esc(e.value_as_extracted)}</strong></div>
          <div class="evidence">${esc(fileName(e.source_filename))}${e.page_or_sheet ? " · " + esc(e.page_or_sheet) : ""}
            ${e.confidence !== null && e.confidence !== undefined ? " · " + esc(Math.round(e.confidence * 100)) + "% sure" : ""}
            ${e.snippet ? " — <q>" + esc(e.snippet) + "</q>" : " — <em>no matching line found in the document</em>"}</div>
          <div class="row tight" style="margin-top:6px">
            <button class="btn small" data-act="evidence" data-id="${esc(e.id)}" data-accept="1"
              data-quoted="${esc(field(e.field_name))}">Confirm this value</button>
            <button class="btn small destructive" data-act="evidence" data-id="${esc(e.id)}" data-accept="0"
              data-quoted="${esc(field(e.field_name))}">Flag for review</button>
          </div>
        </li>`,
        )
        .join("")}</ul>`
        : ""
    }

    <h4>Still missing (${gaps.length})</h4>
    ${
      blocking
        ? `<div class="note bad">${plural(blocking, "item")} block${blocking === 1 ? "s" : ""} a fair price
      comparison — a charge with no stated price is never treated as zero.</div>`
        : ""
    }
    ${
      gaps.length
        ? `<ul class="fields">${gaps
            .map(
              (g) => `
      <li>
        <div class="row spread"><strong>${esc(field(g.field_name))}</strong>${severityChip(g.severity)}</div>
        <div class="meta">${esc(clip(g.description, 260))}</div>
        ${g.question_text ? `<div class="evidence">Ask the carrier: <q>${esc(g.question_text)}</q></div>` : ""}
        <div class="row tight" style="margin-top:6px">
          <button class="btn small neutral" data-act="waive" data-id="${esc(g.id)}" data-field="${esc(field(g.field_name))}">
            Waive — with a reason</button>
        </div>
      </li>`,
            )
            .join("")}</ul>`
        : `<p class="meta">Nothing is missing from this quote.</p>`
    }

    <h4>Your review notes</h4>
    <textarea id="notes-${esc(q.id)}" placeholder="Kept with the decision on this computer only."
      aria-label="Review notes">${esc(q.review_notes || "")}</textarea>
    <div class="row tight" style="margin-top:10px">
      <button class="btn" data-act="review-approve" data-id="${esc(q.id)}"
        ${q.review_status === "approved" || q.review_status === "imported" ? "disabled" : ""}>Approve review</button>
      <button class="btn subtle" data-act="review-import" data-id="${esc(q.id)}"
        ${q.review_status !== "approved" ? "disabled" : ""}>Import to database</button>
      <button class="btn destructive" data-act="review-reject" data-id="${esc(q.id)}"
        ${q.review_status === "imported" ? "disabled" : ""}>Reject</button>
    </div>
    ${
      q.review_status === "imported"
        ? `<p class="meta">In the database since ${esc(when(q.imported_at))} — reading the document again creates a new version to review.</p>`
        : q.review_status === "approved"
          ? `<p class="meta">Approved ${esc(when(q.reviewed_at))}. Importing is a separate, deliberate step.</p>`
          : ""
    }
    ${shipment ? `<p style="margin-top:10px"><a class="btn small neutral" href="#/shipments/${esc(shipment.id)}">Open this lane and its comparison</a></p>` : ""}
  </section>`;
}

function watchExistingJob(intakeId) {
  busy("jobs", async () => {
    const jobs = await api.get("/api/jobs");
    const live = jobs.find(
      (j) =>
        j.subject_id === intakeId &&
        (j.status === "queued" || j.status === "running"),
    );
    if (live) trackJob(live);
  });
}

function readingResultMessage(result) {
  if (!result) return "Processing completed — reloading this page";
  if (result.created_quote && result.shipment_status === "needs_review") {
    return "Quote extracted — lane approval is required before review can continue";
  }
  if (result.created_quote) return "Quote extracted — ready for review";
  const notes = (result.notes || []).join(" ").toLowerCase();
  if (
    notes.includes("not a quote") ||
    notes.includes("marked irrelevant") ||
    notes.includes("irrelevant/rejected")
  ) {
    return "No quote extracted — this document is marked not a quote";
  }
  if (
    notes.includes("needs_review") ||
    notes.includes("classification review") ||
    notes.includes("no quotation created")
  ) {
    return "Reading stopped with a review item — no quote was created";
  }
  return "Processing completed with no quote created — check the document details";
}

function trackJob(job) {
  const slot = document.getElementById("job-slot");
  if (!slot) return;
  state.watched = job.id;
  const paint = (update) => {
    slot.innerHTML = jobLine(update);
  };
  paint(job);
  api
    .pollJob(job.id, paint)
    .then((finalJob) => {
      toast(readingResultMessage(finalJob.result), "ok");
      state.watched = null;
      return route();
    })
    .catch((exc) => {
      state.watched = null;
      slot.innerHTML = jobLine({ status: "failed", error: errorText(exc) });
      toast(errorText(exc), "error");
    });
}

// -------------------------------------------------------------- review queue
async function pageQueue() {
  skeleton("loading queue…");
  let review;
  try {
    review = await loadReview();
  } catch (exc) {
    return fail(exc);
  }
  const threshold = limits().confidence_threshold;
  const rows = [];
  for (const doc of review.documents) {
    const uncertain =
      doc.classification === "needs_review" || doc.classification === "pending";
    const failed =
      doc.classification === "failed" || doc.processing === "failed";
    rows.push({
      kind: "document",
      id: doc.id,
      title: fileName(doc.file),
      reason: doc.reason
        ? clip(doc.reason, 180)
        : "This document has not been read yet.",
      blocking: failed,
      chips: [classificationChip(doc), processingChip(doc)],
      lowConfidence:
        doc.confidence !== null &&
        doc.confidence !== undefined &&
        doc.confidence < threshold,
      uncertain,
      target: "#/documents/" + doc.id,
      action: "Open the document",
    });
  }
  for (const shipment of review.shipments) {
    if (shipment.status === "needs_review") {
      rows.push({
        kind: "route",
        id: shipment.id,
        title: `New lane: ${shipment.route}`,
        reason:
          "The agent read this route from a document. Approving it locks the lane and prepares the carrier questions.",
        blocking: true,
        chips: [chip("Needs your approval", "warn")],
        lowConfidence: false,
        uncertain: true,
        target:
          "#/documents/" +
          (shipment.quotes[0]
            ? shipment.quotes[0].intake_item_id
            : shipment.id),
        action: "Check the route",
      });
    }
    for (const q of shipment.quotes) {
      const blocking = q.gaps.filter((g) => g.severity === "critical").length;
      const undecided = q.review_evidence.length;
      if (q.review_status !== "extracted" && blocking === 0 && undecided === 0)
        continue;
      rows.push({
        kind: "quote",
        id: q.id,
        title: `${shipment.route} · ${q.carrier}`,
        reason:
          `${plural(q.gaps.length, "missing item")}${blocking ? `, ${blocking} of them block a fair comparison` : ""}` +
          (undecided
            ? ` · ${plural(undecided, "extracted value")} not confirmed`
            : ""),
        blocking: blocking > 0,
        chips: [reviewChip(q.review_status)],
        lowConfidence: q.review_evidence.some((e) => e.status !== "accepted"),
        uncertain: false,
        target: "#/documents/" + q.intake_item_id,
        action: "Review this quote",
      });
    }
  }
  const filters = [
    ["", "Everything"],
    ["critical", "Blocking comparison"],
    ["low", "Agent unsure"],
    ["uncertain", "Needs a decision"],
    ["ready", "Quotes to review"],
  ];
  view.innerHTML = `
    <div class="row spread">
      <div class="row tight" id="queue-filters">
        ${filters
          .map(
            (
              [value, label],
              i,
            ) => `<button class="btn small ${i === 0 ? "" : "neutral"}"
          data-filter="${esc(value)}">${esc(label)}</button>`,
          )
          .join("")}
      </div>
      <button class="btn neutral" data-act="reload">Refresh</button>
    </div>
    <p class="meta" style="margin-top:8px">${plural(rows.length, "item")} awaiting a person. An ambiguous document
      is never thrown away — the decision and the reason stay on screen.</p>
    <div class="list" id="queue-list" style="margin-top:10px"></div>`;

  const list = document.getElementById("queue-list");
  const paint = (filter) => {
    const subset = rows.filter((r) => {
      if (filter === "critical") return r.blocking;
      if (filter === "low") return r.lowConfidence;
      if (filter === "uncertain") return r.uncertain;
      if (filter === "ready") return r.kind === "quote";
      return true;
    });
    list.innerHTML = subset.length
      ? subset
          .map(
            (r) => `
      <div class="panel">
        <div class="row spread"><strong>${esc(r.title)}</strong><span class="row tight">${r.chips.join(" ")}</span></div>
        <div class="meta">${esc(r.reason)}</div>
        <div class="row tight" style="margin-top:8px">
          <a class="btn small" href="${esc(r.target)}">${esc(r.action)}</a>
          ${
            r.kind === "route"
              ? `<button class="btn small subtle" data-act="shipment-approve" data-id="${esc(r.id)}"
            data-updated="">Approve lane</button>
            <button class="btn small destructive" data-act="shipment-reject" data-id="${esc(r.id)}">Reject</button>`
              : ""
          }
          ${
            r.kind === "document"
              ? `<button class="btn small neutral" data-act="process" data-id="${esc(r.id)}"
            data-force="1">Read this document</button>`
              : ""
          }
          ${r.kind === "quote" ? `<button class="btn small subtle" data-act="review-approve" data-id="${esc(r.id)}">Approve review</button>` : ""}
        </div>
      </div>`,
          )
          .join("")
      : empty("Nothing matches this filter.");
  };
  paint("");
  document
    .getElementById("queue-filters")
    .addEventListener("click", (event) => {
      const button = event.target.closest("[data-filter]");
      if (!button) return;
      for (const node of document.querySelectorAll("#queue-filters .btn"))
        node.className = "btn small neutral";
      button.className = "btn small";
      paint(button.dataset.filter);
    });
}

// ---------------------------------------------------------------- quotations
async function pageShipments() {
  skeleton();
  let review;
  try {
    review = await loadReview();
  } catch (exc) {
    return fail(exc);
  }
  view.innerHTML = `<div class="row"><button class="btn neutral" data-act="reload">Refresh</button></div>
    <div class="grid" style="margin-top:12px">
      ${
        review.shipments.length
          ? review.shipments
              .map(
                (s) => `
        <div class="panel">
          <div class="row spread"><h3>${esc(s.route)}</h3>
            ${s.status === "needs_review" ? chip("Needs approval", "warn") : chip("Lane approved", "ok")}</div>
          <div class="meta">${esc(labelOf(s))} · ${esc(scope(s.required_delivery_scope))} · target ${esc(s.target_currency)}</div>
          <div class="field-pair">
            <div><div class="k">Quotes</div><div class="v">${esc(s.quotes.length)}</div></div>
            <div><div class="k">Missing</div><div class="v">${esc(s.open_gaps)}</div></div>
            <div><div class="k">Drafts</div><div class="v">${esc(s.drafts.length)}</div></div>
          </div>
          <ul class="fields">
            ${s.quotes
              .map(
                (
                  q,
                ) => `<li><strong>${esc(q.carrier)}</strong> ${reviewChip(q.review_status)}
              <div class="evidence">${esc(q.base_price === null ? "no base price stated" : q.base_price + " " + (q.currency || ""))}
                · known total ${esc(q.total_known_cost === null ? "not available" : q.total_known_cost)}
                · ${plural(q.gaps.length, "missing item")}
                · <a href="#/documents/${esc(q.intake_item_id)}">source document</a></div></li>`,
              )
              .join("")}
          </ul>
          <div class="row tight">
            <a class="btn small" href="#/shipments/${esc(s.id)}">Open</a>
            <a class="btn small neutral" href="#/comparison/${esc(s.id)}">Compare</a>
          </div>
        </div>`,
              )
              .join("")
          : empty(
              "No lanes yet — reading a quotation creates or matches one automatically.",
            )
      }
    </div>`;
}

async function pageShipment(id) {
  skeleton();
  try {
    await loadReview();
  } catch (exc) {
    return fail(exc);
  }
  const shipment = findShipment(id);
  if (!shipment)
    return (view.innerHTML = empty("This lane is not in the database."));
  view.innerHTML = `
    <div class="row tight"><a class="btn neutral small" href="#/shipments">Back to quotations</a>
      <a class="btn neutral small" href="#/comparison/${esc(shipment.id)}">Compare</a></div>
    <div class="panel" style="margin-top:12px">
      <div class="row spread"><h3>${esc(shipment.route)}</h3>
        ${shipment.status === "needs_review" ? chip("Needs approval", "warn") : chip("Lane approved", "ok")}</div>
      <div class="field-pair">
        <div><div class="k">Reference</div><div class="v">${esc(labelOf(shipment))}</div></div>
        <div><div class="k">Cargo</div><div class="v">${show(shipment.cargo, "not stated")}</div></div>
        <div><div class="k">Weight</div><div class="v">${show(shipment.weight_kg, "not stated")} <span class="v soft">kg</span></div></div>
        <div><div class="k">Volume</div><div class="v">${show(shipment.volume_m3, "not stated")} <span class="v soft">m³</span></div></div>
        <div><div class="k">Needed scope</div><div class="v">${esc(scope(shipment.required_delivery_scope))}</div></div>
        <div><div class="k">Target currency</div><div class="v">${esc(shipment.target_currency)}</div></div>
        <div><div class="k">Approved</div><div class="v">${show(when(shipment.approved_at), "not yet")}</div></div>
      </div>
      <p class="meta">Full addresses as stated: ${esc(clip(shipment.origin || "?", 120))} → ${esc(clip(shipment.destination || "?", 120))}</p>
      <div class="row tight">
        <a class="btn small neutral" href="/api/shipments/${esc(shipment.id)}/report?format=md" download>Download report (md)</a>
        <a class="btn small neutral" href="/api/shipments/${esc(shipment.id)}/report?format=csv" download>CSV</a>
        <a class="btn small neutral" href="/api/shipments/${esc(shipment.id)}/report?format=json" download>JSON</a>
      </div>
    </div>
    <h2>Quotes (${shipment.quotes.length})</h2>
    <div class="list">
      ${shipment.quotes
        .map(
          (q) => `
        <div class="panel">
          <div class="row spread"><h3>${esc(q.carrier)}${q.quote_reference ? " · " + esc(q.quote_reference) : ""}
            <span class="meta">version ${esc(q.version)}</span></h3>${reviewChip(q.review_status)}</div>
          <div class="field-pair">
            <div><div class="k">Base</div><div class="v">${show(q.base_price)} ${esc(q.currency || "")}</div></div>
            <div><div class="k">Known total</div><div class="v">${show(q.total_known_cost, "not available")}</div></div>
            <div><div class="k">Valid until</div><div class="v">${show(q.valid_until, "not stated")}</div></div>
            <div><div class="k">Scope</div><div class="v">${q.delivery_scope ? esc(scope(q.delivery_scope)) : "not stated"}</div></div>
          </div>
          <p class="evidence">${plural(q.gaps.length, "missing item")} · ${plural(q.review_evidence.length, "value")} not confirmed ·
            ${q.imported_at ? "in the database since " + esc(when(q.imported_at)) : "not imported yet"}</p>
          ${q.review_notes ? `<div class="note">${esc(q.review_notes)}</div>` : ""}
          <div class="row tight" style="margin-top:8px">
            <a class="btn small" href="#/documents/${esc(q.intake_item_id)}">Open the document</a>
            <button class="btn small subtle" data-act="review-import" data-id="${esc(q.id)}"
              ${q.review_status !== "approved" ? "disabled" : ""}>Import to database</button>
          </div>
        </div>`,
        )
        .join("")}
    </div>
    ${shipment.drafts.length ? `<h2>Questions for the carrier</h2><div class="list">${shipment.drafts.map(draftCard).join("")}</div>` : ""}
    ${
      shipment.replies.length
        ? `<h2>Carrier replies</h2><ul class="fields">${shipment.replies
            .map(
              (r) => `
      <li>${esc(when(r.created_at))} <span class="meta">reply ${esc(r.association.replace("_", " "))} to this lane</span>
        <button class="btn small neutral" data-act="reply-process" data-id="${esc(r.id)}">Apply this reply again</button></li>`,
            )
            .join("")}</ul>`
        : ""
    }`;
}

// ---------------------------------------------------------------- comparison
async function pageComparisonIndex() {
  skeleton();
  let rows;
  try {
    rows = await api.get("/api/shipments");
  } catch (exc) {
    return fail(exc);
  }
  if (!rows.length)
    return (view.innerHTML = empty(
      "No lanes yet, so there is nothing to compare.",
    ));
  view.innerHTML = `
    <div class="row tight">
      <select id="cmp" aria-label="Lane to compare">
        ${rows.map((s) => `<option value="${esc(s.id)}">${esc(s.route)} (${esc(s.quotes)} quote(s))</option>`).join("")}
      </select>
      <button class="btn" id="cmp-go">Compare</button>
    </div>
    <p class="meta" style="margin-top:8px">Totals come from the deterministic Python comparison service. A quote with a
      missing figure stays marked incomplete; an unpriced charge is never counted as zero.</p>`;
  document.getElementById("cmp-go").addEventListener("click", () => {
    location.hash = "#/comparison/" + document.getElementById("cmp").value;
  });
}

async function pageComparison(id) {
  skeleton();
  try {
    await loadReview();
  } catch (exc) {
    return fail(exc);
  }
  const shipment = findShipment(id);
  if (!shipment)
    return (view.innerHTML = empty("This lane is not in the database."));
  const report = shipment.comparison;
  if (!report) {
    view.innerHTML = `<a class="btn neutral small" href="#/comparison">Choose another lane</a>
      <div class="note warn" style="margin-top:12px">${esc(
        shipment.comparison_error ||
          "Nothing to compare yet — at least one quote needs to be imported for this lane.",
      )}</div>`;
    return;
  }
  view.innerHTML = `
    <a class="btn neutral small" href="#/comparison">Choose another lane</a>
    <div class="panel" style="margin-top:12px">
      <div class="row spread"><h3>${esc(report.route || shipment.route)}</h3>
        ${chip("Computed by the Python service", "info")}</div>
      <div class="field-pair">
        <div><div class="k">Needed scope</div><div class="v">${esc(scope(report.required_scope))}</div></div>
        <div><div class="k">Target currency</div><div class="v">${esc(report.target_currency)}</div></div>
        <div><div class="k">Quotes</div><div class="v">${esc(report.rows.length)}</div></div>
        <div><div class="k">Calculated</div><div class="v">${esc(when(report.generated_at))}</div></div>
      </div>
      ${
        report.recommendation
          ? `<div class="note">Recommendation: ${esc(report.recommendation)}</div>
        <p class="evidence">Because: ${report.recommendation_basis.map(esc).join("; ")}</p>`
          : '<div class="note warn">No recommendation yet: these quotes are not comparable as they stand. Missing values are not guessed.</div>'
      }
    </div>
    <div class="panel" style="margin-top:12px;overflow:auto">
      <table>
        <caption>Currencies are only converted when an explicit rate and date are configured.</caption>
        <thead><tr>
          <th>Carrier</th><th class="num">Base</th><th class="num">Known extras</th>
          <th class="num">Known subtotal</th><th>Scope</th><th>Transit</th><th>Valid until</th><th>Status</th><th>Still missing</th>
        </tr></thead>
        <tbody>${report.rows
          .map(
            (r) => `
          <tr>
            <td><span class="name">${esc(r.carrier_name || "carrier not named")}</span>
              ${r.quote_reference ? `<div class="sub">${esc(r.quote_reference)}</div>` : ""}</td>
            <td class="num">${r.base_price === null ? '<span class="meta">not stated</span>' : esc(r.base_price)}</td>
            <td class="num">${r.known_additional_total === null ? '<span class="meta">not stated</span>' : esc(r.known_additional_total)}</td>
            <td class="num">${
              r.known_subtotal === null
                ? '<span class="meta">cannot total yet</span>'
                : esc(r.known_subtotal) + " " + esc(r.currency || "")
            }
              ${r.converted_subtotal !== null ? `<div class="sub">= ${esc(r.converted_subtotal)} ${esc(report.target_currency)}${r.fx_note ? " · " + esc(r.fx_note) : ""}</div>` : ""}</td>
            <td>${esc(scope(r.delivery_scope))}${r.scope_compatible ? "" : " " + chip("Wrong scope", "bad")}</td>
            <td>${esc(r.transit_time || "not stated")}</td>
            <td>${esc(r.valid_until || "not stated")}</td>
            <td>${r.comparison_status === "comparable" ? chip("Comparable", "ok") : chip("Incomplete", "warn")}</td>
            <td class="meta">${esc(r.unresolved_critical_gaps.length)} blocking · ${esc(r.unresolved_other_gaps.length)} other
              ${r.uncertain_charges.length ? `<div class="sub">uncertain: ${r.uncertain_charges.map(esc).join(", ")}</div>` : ""}</td>
          </tr>`,
          )
          .join("")}</tbody>
      </table>
    </div>
    ${report.caveats.length ? `<h2>Read before deciding</h2><ul class="fields">${report.caveats.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>` : ""}
    <div class="row tight" style="margin-top:14px">
      <a class="btn neutral" href="/api/shipments/${esc(report.shipment_id)}/report?format=md" download>Export report (md)</a>
      <a class="btn neutral" href="/api/shipments/${esc(report.shipment_id)}/report?format=csv" download>Export (csv)</a>
      ${shipment.quotes.map((q) => `<a class="btn small subtle" href="#/documents/${esc(q.intake_item_id)}">Check ${esc(q.carrier)}</a>`).join("")}
    </div>`;
}

// --------------------------------------------------------------------- drafts
async function pageDrafts() {
  skeleton();
  let rows;
  try {
    rows = await api.get("/api/drafts");
  } catch (exc) {
    return fail(exc);
  }
  view.innerHTML = `
    <div class="note">These are written for you to read and send yourself. The app never sends anything:
      approving records your decision, exporting saves a file on this computer.</div>
    <div class="row" style="margin-top:10px"><button class="btn neutral" data-act="reload">Refresh</button></div>
    <div class="list" style="margin-top:12px">
      ${
        rows.length
          ? rows.map(draftCard).join("")
          : empty(
              "No open drafts. They are prepared once a proposed lane is approved and figures are still missing.",
            )
      }
    </div>`;
}

function draftCard(d) {
  return `<div class="panel">
    <div class="row spread"><strong>${esc(d.subject)}</strong>
      ${
        d.status === "draft"
          ? chip("Awaiting your approval", "pending")
          : d.status === "approved"
            ? chip("Approved — not sent", "info")
            : d.status === "exported"
              ? chip("Exported as a file", "ok")
              : chip(String(d.status).replace("_", " "), "plain")
      }</div>
    <div class="meta">${esc(d.shipment_reference || "")}${d.recipient ? " · to " + esc(d.recipient) : ""}</div>
    <pre class="text" style="max-height:240px;margin-top:8px">${esc(d.body)}</pre>
    <div class="row tight" style="margin-top:10px">
      <input type="text" id="rec-${esc(d.id)}" placeholder="carrier@example.com (only needed for .eml)"
        value="${esc(d.recipient || "")}" aria-label="Recipient for export">
      <button class="btn small" data-act="draft-approve" data-id="${esc(d.id)}" ${d.status !== "draft" ? "disabled" : ""}>Approve draft</button>
      <button class="btn small neutral" data-act="draft-export" data-format="txt" data-id="${esc(d.id)}">Save as .txt</button>
      <button class="btn small neutral" data-act="draft-export" data-format="eml" data-id="${esc(d.id)}">Save as .eml</button>
    </div>
  </div>`;
}

// --------------------------------------------------------------------- system
async function pageSettings() {
  skeleton();
  await refreshStatus();
  const s = state.status;
  if (!s) {
    view.innerHTML =
      '<div class="note bad">The backend is not answering. Start it in a terminal with <code>freight review</code>.</div>';
    return;
  }
  const problems = [];
  if (!s.ollama.reachable) {
    problems.push(
      '<div class="note bad"><strong>The local model is offline.</strong> Run <code>ollama serve</code>. ' +
        "Until it answers, documents cannot be read. There is no cloud fallback by design.</div>",
    );
  } else if (s.ollama.model_installed === false) {
    problems.push(
      `<div class="note warn"><strong>${esc(s.ollama.model)} is not installed.</strong> ` +
        `Run <code>ollama pull ${esc(s.ollama.model)}</code>, or set <code>OLLAMA_MODEL</code> in <code>.env</code> ` +
        "to a model you already have.</div>",
    );
  }
  if (!s.database.initialized) {
    problems.push(
      '<div class="note warn"><strong>The database is not set up yet.</strong> Run <code>freight init</code>.</div>',
    );
  }
  const missingParsers = Object.entries(s.parsers)
    .filter(([, ok]) => !ok)
    .map(([name]) => name);
  if (missingParsers.length) {
    problems.push(
      `<div class="note warn"><strong>These file types cannot be read yet:</strong> ` +
        `${esc(missingParsers.join(", "))}. Everything else keeps working.</div>`,
    );
  }
  view.innerHTML = `
    ${
      problems.length
        ? `<div class="list">${problems.join("")}</div>`
        : '<div class="note"><strong>Everything is working.</strong> The model answers locally and the database is ready.</div>'
    }

    <h2>Where things run</h2>
    <div class="panel">
      <p>Every part of this app runs on this computer. Your documents go to the local model only — never to a
         cloud service, never to a vendor. Nothing is emailed or written to an ERP system by itself.</p>
      <div class="field-pair">
        <div><div class="k">Model</div><div class="v">${esc(s.ollama.model || "not configured")}</div></div>
        <div><div class="k">Model address</div><div class="v">${esc(s.ollama.base_url)}</div></div>
        <div><div class="k">App address</div><div class="v">127.0.0.1 <span class="v soft">this computer only</span></div></div>
      </div>
    </div>

    <h2>Your data</h2>
    <div class="panel">
      <div class="field-pair">
        <div><div class="k">Database</div><div class="v"><code class="path">${esc(s.database.path)}</code></div></div>
        <div><div class="k">Stored copies</div><div class="v"><code class="path">${esc(s.workspace)}</code></div></div>
      </div>
      <p class="meta">Schema version ${esc(s.database.schema_version)}. To take a safe copy while the app is running:
        <code class="path">freight db backup ~/freight-backup.db</code>. Deleting the workspace folder removes the
        imported files; the database keeps the extracted figures.</p>
    </div>

    <h2>Upload rules</h2>
    <div class="panel">
      <div class="field-pair">
        <div><div class="k">Largest file</div><div class="v">${esc(s.limits.max_file_size_mb)} MB</div></div>
        <div><div class="k">File types</div><div class="v">${esc(s.limits.supported_extensions.join("  "))}</div></div>
        <div><div class="k">Confidence floor</div><div class="v">${esc(s.limits.confidence_threshold)}</div></div>
      </div>
      <p class="meta">Change these in the <code class="path">.env</code> file next to the project
        (<code class="path">MAX_FILE_SIZE_MB</code>, <code class="path">OLLAMA_MODEL</code>) and restart
        <code class="path">freight review</code>. A document the agent is less sure about than the floor is sent to
        you for a decision instead of being used as if it were certain.</p>
    </div>`;
}

// ------------------------------------------------------------- action verbs
const ACTIONS = {
  reload: async () => route(),

  process: async (button) =>
    busy("process-" + button.dataset.id, async () => {
      button.disabled = true;
      const response = await api.post(
        `/api/documents/${encodeURIComponent(button.dataset.id)}/process?force=${button.dataset.force === "1" ? "true" : "false"}`,
      );
      if (document.getElementById("job-slot")) {
        trackJob(response.job);
      } else {
        toast(
          "Reading started — opening the document so you can watch it",
          "ok",
        );
        location.hash = "#/documents/" + button.dataset.id;
      }
    }),

  skip: async (button) => {
    button.textContent = "Keeping the existing copy";
    button.disabled = true;
  },

  ignore: async (button) =>
    busy("ignore-" + button.dataset.id, async () => {
      const reason = await askReason({
        title: "Mark this document as not a quote?",
        body: `<p><strong>${esc(fileName(button.dataset.file))}</strong> will stay in the database with your note,
             so the decision is traceable. Nothing is deleted.</p>`,
        label: "Why is this not a freight quote?",
        placeholder: "e.g. carrier newsletter, no prices offered",
        confirmLabel: "Mark as not a quote",
        danger: true,
      });
      if (!reason) return;
      const r = await api.action("intake.ignore", {
        id: button.dataset.id,
        reason,
      });
      toast(r.message, "ok");
      await route();
    }),

  "shipment-save": async (button) =>
    busy("sh-save", async () => {
      const origin = document.getElementById("sh-origin").value.trim();
      const destination = document
        .getElementById("sh-destination")
        .value.trim();
      if (!origin || !destination)
        throw new api.ApiError("Origin and destination cannot be empty.", 400);
      const r = await api.action("shipment.edit", {
        id: button.dataset.id,
        fields: { origin, destination },
      });
      setDirty(false);
      toast(r.message, "ok");
      await route();
    }),

  "shipment-approve": async (button) =>
    busy("sh-approve-" + button.dataset.id, async () => {
      const r = await api.action("shipment.approve", {
        id: button.dataset.id,
        expected_updated_at: button.dataset.updated || null,
      });
      setDirty(false);
      toast(
        r.message +
          (r.notes && r.notes.length ? " · " + r.notes.join(" ") : ""),
        "ok",
      );
      await route();
    }),

  "shipment-reject": async (button) =>
    busy("sh-reject-" + button.dataset.id, async () => {
      const reason = await askReason({
        title: "Reject this proposed lane?",
        body: "<p>The lane is closed and future documents will not be matched to it. Your reason is kept in the audit trail.</p>",
        label: "What is wrong with this route?",
        placeholder:
          "e.g. this carrier quoted from their own office, not our origin",
        confirmLabel: "Reject lane",
        danger: true,
      });
      if (!reason) return;
      const r = await api.action("shipment.reject", {
        id: button.dataset.id,
        reason,
      });
      toast(r.message, "ok");
      await route();
    }),

  waive: async (button) =>
    busy("waive-" + button.dataset.id, async () => {
      const reason = await askReason({
        title: `Waive the missing ${button.dataset.field}?`,
        body:
          "<p>Use this when the carrier will not answer and you accept the quote as it stands. " +
          "The comparison keeps showing that this figure was waived, not found.</p>",
        label: "Why can this be waived?",
        placeholder:
          "e.g. carrier confirmed by phone that terminal handling is included",
        confirmLabel: "Waive with this reason",
        danger: true,
      });
      if (!reason) return;
      const r = await api.action("gap.waive", {
        id: button.dataset.id,
        reason,
      });
      toast(r.message, "ok");
      await route();
    }),

  evidence: async (button) =>
    busy("evidence-" + button.dataset.id, async () => {
      const accept = button.dataset.accept === "1";
      const r = await api.post(
        `/api/evidence/${encodeURIComponent(button.dataset.id)}/decide`,
        { accept },
      );
      toast(
        `${button.dataset.quoted}: ${r.validation_status === "accepted" ? "confirmed" : "flagged for review"}`,
        "ok",
      );
      await route();
    }),

  "review-approve": async (button) =>
    busy("approve-" + button.dataset.id, async () => {
      const notes = document.getElementById("notes-" + button.dataset.id);
      const r = await api.post(
        `/api/quotes/${encodeURIComponent(button.dataset.id)}/review/approve`,
        { notes: notes ? notes.value : null },
      );
      setDirty(false);
      toast(r.message, "ok");
      await route();
    }),

  "review-reject": async (button) =>
    busy("reject-" + button.dataset.id, async () => {
      const notes = document.getElementById("notes-" + button.dataset.id);
      const go = await confirmDialog({
        title: "Reject this quote?",
        body: "<p>Nothing is written to the database. Your decision and note stay in the audit trail.</p>",
        confirmLabel: "Reject quote",
        danger: true,
      });
      if (!go) return;
      const r = await api.post(
        `/api/quotes/${encodeURIComponent(button.dataset.id)}/review/reject`,
        { reason: notes ? notes.value : null },
      );
      setDirty(false);
      toast(r.message, "ok");
      await route();
    }),

  "review-import": async (button) =>
    busy("preview-" + button.dataset.id, async () => {
      const preview = await api.get(
        `/api/quotes/${encodeURIComponent(button.dataset.id)}/import/preview`,
      );
      const body = `
      <p>${esc(preview.linked.shipment)} · quote version ${esc(preview.version)} · currently
         <strong>${esc(reviewWord(preview.review_status))}</strong></p>
      <table>
        <thead><tr><th>Record</th><th>Change</th></tr></thead>
        <tbody>
          ${preview.creates.map((c) => `<tr><td>${esc(field(c.record))}</td><td>will be added</td></tr>`).join("")}
          ${preview.updates.map((u) => `<tr><td>${esc(field(u.record))}</td><td>${esc(reviewWord(u.from))} → marked as in the database</td></tr>`).join("")}
        </tbody>
      </table>
      <div class="field-pair" style="margin-top:10px">
        <div><div class="k">Evidence lines</div><div class="v">${esc(preview.linked.carrier_evidence)}</div></div>
        <div><div class="k">Still missing</div><div class="v">${esc(preview.linked.open_gaps)}</div></div>
        <div><div class="k">Blocking</div><div class="v">${esc(preview.linked.critical_gaps)}</div></div>
        <div><div class="k">Drafts</div><div class="v">${esc(preview.linked.drafts)}</div></div>
      </div>
      ${preview.warnings.map((w) => `<div class="note warn" style="margin-top:8px">${esc(w)}</div>`).join("")}
      ${preview.already_imported ? '<div class="note" style="margin-top:8px">Already imported — importing again changes nothing.</div>' : ""}
      <p class="meta" style="margin-top:10px">This happens in one database transaction. Clicking twice never creates a duplicate.</p>`;
      const go = await confirmDialog({
        title: "Import this approved quote",
        body,
        confirmLabel: "Import now",
      });
      if (!go) return;
      const result = await api.post(
        `/api/quotes/${encodeURIComponent(button.dataset.id)}/import`,
      );
      toast(
        result.already_imported
          ? "Already imported — nothing changed"
          : "Imported into the database",
        "ok",
      );
      await route();
    }),

  "draft-approve": async (button) =>
    busy("draft-approve-" + button.dataset.id, async () => {
      const r = await api.post(
        `/api/drafts/${encodeURIComponent(button.dataset.id)}/approve`,
      );
      toast(r.message + " — still not sent", "ok");
      await route();
    }),

  "draft-export": async (button) =>
    busy("draft-export-" + button.dataset.id, async () => {
      const format = button.dataset.format;
      const recipientField = document.getElementById(
        "rec-" + button.dataset.id,
      );
      await api.downloadBlob(
        `/api/drafts/${encodeURIComponent(button.dataset.id)}/export`,
        {
          format,
          recipient: recipientField ? recipientField.value.trim() : null,
        },
        `clarification.${format}`,
      );
      toast("Saved on this computer — nothing was sent", "ok");
    }),

  "reply-process": async (button) =>
    busy("reply-" + button.dataset.id, async () => {
      const r = await api.action("reply.process", { id: button.dataset.id });
      toast(r.message, "ok");
      await route();
    }),
};

document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-act]");
  if (!button || button.disabled) return;
  const handler = ACTIONS[button.dataset.act];
  if (!handler) return;
  event.preventDefault();
  handler(button);
});

document.addEventListener("input", (event) => {
  const target = event.target;
  if (
    (target.id || "").startsWith("notes-") ||
    target.id === "sh-origin" ||
    target.id === "sh-destination"
  ) {
    setDirty(true);
  }
});

window.addEventListener("hashchange", route);
window.addEventListener("beforeunload", (event) => {
  if (state.dirty) {
    event.preventDefault();
    event.returnValue = "";
  }
});

route();
