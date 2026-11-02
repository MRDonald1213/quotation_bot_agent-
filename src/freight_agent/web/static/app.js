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
  supplierPreview: null,
};

const PAGES = {
  "": {
    title: "Overview",
    guide:
      "Operational status, quotations, supplier records, inbox activity and decisions waiting for approval.",
  },
  inbox: {
    title: "Operations Inbox",
    guide:
      "Unified email, document and supplier-workflow intake with explicit human review actions.",
  },
  documents: {
    title: "Documents",
    guide:
      "Drop quotation files in, run the reading step, then open a document to check it.",
  },
  shipments: {
    title: "Review & Quotations",
    guide:
      "Documents awaiting review and quotation lanes are shown on the same screen.",
  },
  drafts: {
    title: "Drafts",
    guide:
      "Questions for the carrier, written by the agent. Nothing leaves this computer until you approve the exact text and send it yourself.",
  },
  settings: {
    title: "System",
    guide:
      "What runs on this computer, where your data lives, and what to check if something fails.",
  },
  supplier: {
    title: "Supplier Workspace",
    guide: "Manage supplier master data, received quotations, approvals and supplier conversations.",
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

const SUPPLIER_DRAFT_STATUS = {
  draft: "Awaiting your approval",
  approved: "Approved — ready to send",
  sent: "Sent",
  rejected: "Rejected",
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
  const raw = (location.hash || "#/").replace(/^#\/?/, "").split("?")[0];
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
  const oauthMarker = (location.hash || "").includes("gmail_error=") || (location.hash || "").includes("gmail_connected=");
  if (oauthMarker) {
    const queryText = (location.hash.split("?")[1] || "");
    const params = new URLSearchParams(queryText);
    if (params.get("gmail_connected") === "1") {
      toast("Gmail account connected successfully.");
    } else if (params.get("gmail_error")) {
      const error = params.get("gmail_error");
      toast(error === "access_denied" ? "Gmail authorization was not granted. Add this account as a Test user, then try again." : "Gmail authorization failed. Please start the connection again.", true);
    }
    history.replaceState(null, "", location.pathname + location.search + "#/inbox");
  }
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
  if (page === "gmail") return id ? pageGmail(id) : pageInbox();
  if (page === "queue" || page === "comparison") return pageShipments();
  if (page === "shipments") return id ? pageShipment(id) : pageShipments();
  if (page === "drafts") return pageDrafts();
  if (page === "settings") return pageSettings();
  if (page === "inbox") return pageInbox();
  if (page === "supplier") return pageSupplier(id || "databases");
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
      "#/shipments",
    ]);
  if (c.awaiting_review)
    attention.push([
      plural(c.awaiting_review, "extracted quote"),
      "awaiting your review",
      "#/shipments",
    ]);
  if (c.ready_to_import)
    attention.push([
      plural(c.ready_to_import, "approved quote"),
      "ready to import into the database",
      "#/shipments",
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
      "waiting for you to approve — sending is a separate action you trigger",
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
      <p>Upload or open a quotation. The agent reads it on this computer, matches it to a supplier database,
         extracts fields against that database schema, and waits for an explicit human decision before
         creating or updating a supplier record.</p>
      <div class="steps">
        <div class="step rise-2"><i>1</i><b>Upload</b><span>Documents and email messages enter the inbox.</span></div>
        <div class="step rise-2"><i>2</i><b>Process</b><span>The agent matches a database and extracts the schema fields.</span></div>
        <div class="step rise-3"><i>3</i><b>Review</b><span>Review extracted data and approve or correct the record.</span></div>
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
          <td>
            ${d.processing === "processed" || d.classification === "irrelevant"
              ? `<a href="${esc(d.target)}">${esc(d.next_action)}</a>`
              : `<button class="btn small" data-act="process" data-id="${esc(d.id)}" data-force="${d.processing === "processed" ? "1" : "0"}">Process quotation</button>`}
          </td>
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
      <a class="btn neutral big" href="#/shipments">Review &amp; quotations</a>
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
            >${r.processing === "processed" ? "Process quotation again" : "Process quotation"}</button>
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
                 <button class="btn small" data-act="process" data-id="${esc(entry.intake_id)}" data-force="0">Process quotation</button>`
                : ""
            }
            ${
              entry.status === "duplicate" && entry.intake_id
                ? `<button class="btn small neutral" data-act="skip" data-id="${esc(entry.intake_id)}">Keep the existing copy</button>
                 <button class="btn small subtle" data-act="process" data-id="${esc(entry.intake_id)}" data-force="1">Process again anyway</button>`
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

// ---------------------------------------------------------- Gmail message workspace
async function pageGmail(messageId) {
  skeleton("loading Gmail message…");
  let message;
  try {
    message = await api.get("/api/gmail/messages/" + encodeURIComponent(messageId));
  } catch (exc) {
    return fail(exc);
  }

  const attachments = Array.isArray(message.attachments) ? message.attachments : [];
  view.innerHTML = `
    <div class="row spread">
      <div class="row tight"><a class="btn neutral small" href="#/inbox">Back to Operations Inbox</a></div>
      <div class="row tight" id="job-slot"></div>
    </div>
    <div class="split" style="margin-top:14px">
      <section class="panel">
        <span class="supplier-kicker">Gmail source</span>
        <h3>${esc(message.subject || "No subject")}</h3>
        <p class="meta">From ${esc(message.sender || "Unknown sender")}${message.sent_at ? " · " + esc(when(message.sent_at)) : ""}</p>
        <div class="row tight" style="margin:12px 0">
          ${message.intake_id ? `<a class="btn neutral" href="#/documents/${esc(message.intake_id)}">Open document workspace</a>` : ""}
          <button class="btn" id="gmail-process-quotation">Process quotation</button>
        </div>
        <p class="meta">The email text and supported attachments are passed together to the quotation analysis workflow.</p>
        <pre class="text" style="margin-top:8px">${esc(message.body_text || "No email body text")}</pre>
      </section>
      <section class="list">
        <div class="panel">
          <span class="supplier-kicker">Attachments</span>
          <h3>${attachments.length ? attachments.length + " attachment(s)" : "No attachments"}</h3>
          ${attachments.length ? `<ul>${attachments.map((attachment) => `<li>${esc(attachment.filename)} <span class="meta">${esc(fileSize(attachment.size_bytes))}</span></li>`).join("")}</ul>` : '<p class="meta">The email body is stored as an intake quotation.</p>'}
        </div>
        ${message.intake_id ? `<div class="panel"><span class="supplier-kicker">Quotation intake</span><h3>Quotation created</h3><p class="meta">Opening this message has created or found the linked intake document. “Process quotation” reads it as a freight quote; the supplier database match and the missing-field list are on the document workspace.</p></div>` : '<div class="empty">No quotation intake could be created from this message.</div>'}
      </section>
    </div>`;

  const processButton = document.getElementById("gmail-process-quotation");
  if (processButton) processButton.addEventListener("click", async () => {
    processButton.disabled = true;
    try {
      const ensured = await api.post("/api/gmail/messages/" + encodeURIComponent(messageId) + "/ensure-quotation");
      if (!ensured.intake_id) {
        toast("This Gmail message is not classified as a quotation", "error");
        processButton.disabled = false;
        return;
      }
      const response = await api.post("/api/documents/" + encodeURIComponent(ensured.intake_id) + "/process?force=true");
      trackJob(response.job);
    } catch (exc) {
      processButton.disabled = false;
      toast(errorText(exc), "error");
    }
  });
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
  let supplierAction = null;
  let supplierDatabases = [];
  try {
    const supplierPayload = await api.get("/api/supplier-quotations");
    supplierAction = (supplierPayload.actions || []).find((action) => action.intake_id === intakeId) ||
      (supplierPayload.received || []).find((item) => item.intake_id === intakeId) || null;
    supplierDatabases = await api.get("/api/supplier-databases");
  } catch (error) { supplierAction = null; }

  view.innerHTML = `
    <div class="row spread">
      <div class="row tight">
        <a class="btn neutral small" href="#/documents">Back to documents</a>
        <a class="btn neutral small" href="#/shipments">Review &amp; quotations</a>
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
            >${hasQuote ? "Process quotation again" : "Process quotation"}</button>`          }
          <a class="btn neutral" href="/api/documents/${esc(doc.id)}/source" download="${esc(doc.file)}">Open the original</a>
        </div>
        <p class="meta">The text below is the document itself. It is data only: nothing written inside it can
          instruct the agent, and macros, scripts and links in the file are never run.</p>
        <pre class="text" style="margin-top:8px">${esc(doc.text)}${doc.text_truncated ? "\n… truncated for display; the full text stays in the local workspace." : ""}</pre>
      </section>

      <section class="list">
        ${supplierIntelligenceBlock(supplierAction, supplierDatabases, intakeId)}
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

  const processButton = view.querySelector("[data-process-supplier-review]");
  if (processButton) processButton.addEventListener("click", () => {
    const selector = document.getElementById("supplier-review-database");
    const databaseId = selector ? selector.value : "";
    if (!databaseId) {
      toast("Select a supplier database before analyzing the quotation", "error");
      if (selector) selector.focus();
      return;
    }
    analyzeQuotation(intakeId, databaseId, processButton);
  });
  const approveButton = view.querySelector("[data-approve-supplier-review]");
  if (approveButton) approveButton.addEventListener("click", async () => {
    const confirmed = await confirmDialog({ title: "Add supplier record to CSV?", body: "<p>The approved row will be appended once. A timestamped backup will be created first.</p>", confirmLabel: "Approve & Create Record" });
    if (!confirmed) return;
    approveButton.disabled = true;
    try { await api.post(`/api/supplier-actions/${encodeURIComponent(approveButton.dataset.approveSupplierReview)}/approve`); toast("Supplier record created", "ok"); await route(); }
    catch (error) { approveButton.disabled = false; toast(error.message, "bad"); }
  });
  const rejectButton = view.querySelector("[data-reject-supplier-review]");
  if (rejectButton) rejectButton.addEventListener("click", async () => {
    await api.post(`/api/supplier-actions/${encodeURIComponent(rejectButton.dataset.rejectSupplierReview)}/status`, { status: "rejected", notes: "Rejected from quotation review workspace" });
    toast("Quotation rejected", "ok"); await route();
  });
  const draftButton = view.querySelector("[data-draft-supplier-review]");
  if (draftButton) draftButton.addEventListener("click", async () => {
    draftButton.disabled = true;
    draftButton.textContent = "Creating email draft…";
    try {
      const result = await api.post(`/api/supplier-actions/${encodeURIComponent(draftButton.dataset.draftSupplierReview)}/create-draft`, {});
      const draft = result.draft || {};
      toast(draft.recipient
        ? `Draft ready for ${draft.recipient}${draft.wording_note ? " — " + draft.wording_note : ""}`
        : "Draft created. No sender address is known, so open it to set one.", "ok");
      location.hash = "#/drafts";
    } catch (error) {
      draftButton.disabled = false;
      draftButton.textContent = "Review & Send Draft Email";
      toast(error.message, "bad");
    }
  });

  watchExistingJob(intakeId);
}

function supplierIntelligenceBlock(action, databases, intakeId) {
  if (!action) {
    return `<section class="panel supplier-intelligence">
      <span class="supplier-kicker">Agent intelligence</span>
      <h3>Supplier database not analyzed</h3>
      <p class="meta">Inspecting this source against connected database descriptions is the next step.</p>
      <label class="supplier-review-label">Target database
        <select class="supplier-select" id="supplier-review-database">
          <option value="">Choose a database</option>
          ${databases.map((database) => `<option value="${esc(database.id)}">${esc(database.name)}</option>`).join("")}
        </select>
      </label>
      <div class="supplier-card-actions"><button class="supplier-btn supplier-btn-primary" data-process-supplier-review="${esc(intakeId)}">Select database &amp; analyze</button></div>
    </section>`;
  }

  const statusLabels = {
    received: "Received",
    needs_human_review: "Needs human review",
    needs_clarification: "Missing information",
    ready_for_approval: "Ready to create record",
    record_created: "Record created",
    rejected: "Rejected",
  };
  const routing = Array.isArray(action.routing) ? action.routing : [];
  const proposed = action.proposed_row && typeof action.proposed_row === "object" ? action.proposed_row : {};
  const missing = Array.isArray(action.missing_fields) ? action.missing_fields : [];
  const evidence = action.evidence && typeof action.evidence === "object" ? action.evidence : {};
  const fieldRows = Object.keys(proposed).map((key) => {
    const evidenceText = evidence[key];
    return `<tr class="supplier-field-row"><td><strong>${esc(field(key))}</strong><span>${esc(key)}</span></td><td>${esc(proposed[key])}${evidenceText ? `<span class="supplier-evidence">Evidence: ${esc(evidenceText)}</span>` : ""}</td><td><span class="supplier-status ok">Extracted</span></td></tr>`;
  }).join("");
  const missingRows = missing.map((item) => `<tr class="supplier-field-missing"><td><strong>${esc(item.label || field(item.field))}</strong><span>${esc(item.field || "")}</span></td><td>${esc(item.reason || "Missing from source")}</td><td><span class="supplier-status ${item.required === false ? "plain" : "warn"}">${item.required === false ? "Ask the supplier" : "Required"}</span></td></tr>`).join("");
  const unmatchedRouting = routing.slice(0, 3).map((candidate) => `<li>${esc(candidate.name)}${candidate.reason ? ` — ${esc(candidate.reason)}` : ""}</li>`).join("");

  return `<section class="panel supplier-intelligence">
    <div class="supplier-action-head">
      <div><span class="supplier-kicker">Agent intelligence</span><h3>${esc(action.database_name || "Database not confirmed")}</h3></div>
      <span class="supplier-status ${action.status === "ready_for_approval" ? "ok" : action.status === "needs_clarification" || action.status === "needs_human_review" ? "warn" : "plain"}">${esc(statusLabels[action.status] || action.status)}</span>
    </div>
    ${action.database_name ? `<p class="supplier-meta-text">Matched by database name, description, category, aliases and target schema metadata.</p>` : `<p class="supplier-meta-text">No unique database match was confirmed. Select a database and re-analyze.</p>`}
    ${routing.length ? `<details class="supplier-routing"><summary>Routing candidates</summary><ol>${unmatchedRouting || "<li>No candidate scored the quotation.</li>"}</ol></details>` : ""}
    <div class="supplier-field-table-wrap">
      <table class="supplier-field-table">
        <thead><tr><th>Target field</th><th>Extracted value</th><th>State</th></tr></thead>
        <tbody>${fieldRows}${missingRows}</tbody>
      </table>
    </div>
    ${missing.length ? `<div class="supplier-missing"><strong>Missing fields</strong><ul>${missing.map((item) => `<li>${esc(item.label || item.field)} — ${esc(item.reason || "Missing from source")}</li>`).join("")}</ul></div>` : ""}
    <label class="supplier-review-label">Manual database override
      <select class="supplier-select" id="supplier-review-database">
        <option value="">Choose a database</option>
        ${databases.map((database) => `<option value="${esc(database.id)}" ${database.name === action.database_name ? "selected" : ""}>${esc(database.name)}</option>`).join("")}
      </select>
    </label>
    <div class="supplier-card-actions supplier-review-actions">
      <button class="supplier-btn supplier-btn-neutral" data-process-supplier-review="${esc(intakeId)}">${action.database_id ? "Re-analyze selected database" : "Select database &amp; analyze"}</button>
      ${action.status === "ready_for_approval" ? `<button class="supplier-btn supplier-btn-primary" data-approve-supplier-review="${esc(action.id)}">Approve &amp; Create Record</button>` : ""}
      ${action.status !== "record_created" && action.status !== "rejected" ? `<button class="supplier-btn supplier-btn-neutral" data-draft-supplier-review="${esc(action.id)}">Review &amp; Send Draft Email</button>` : ""}
      ${action.status !== "record_created" && action.status !== "rejected" ? `<button class="supplier-btn supplier-btn-neutral" data-reject-supplier-review="${esc(action.id)}">Reject</button>` : ""}
    </div>
  </section>`;
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
  if (result.supplier_action) {
    const action = result.supplier_action;
    if (!action.database_id) return "No unique supplier database matched — select a database and re-analyze";
    if (action.status === "ready_for_approval") return "Database matched — target-schema extraction is ready for human approval";
    if (action.status === "needs_clarification") return "Database matched — required fields are missing and need review";
    return "Database analysis completed — human review is required";
  }
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

function processingToastMessage(result) {
  const action = result && result.supplier_action;
  if (action) {
    if (!action.database_id) {
      return "No unique supplier database matched — select a database in the review workspace and analyze again.";
    }
    if (result.draft) {
      const missing = (action.missing_fields || []).length;
      return `Matched ${action.database_name}: ${missing} required field(s) still missing, and a reply draft is waiting for you in Draft review.`;
    }
    const statusLabels = {
      needs_human_review: "the reading needs a person before anything is recorded",
      needs_clarification: "required database fields are missing",
      ready_for_approval: "the extracted row is ready for your approval",
      record_created: "the supplier record is already created",
      rejected: "this quotation is marked rejected",
    };
    return `Matched ${action.database_name}: ${statusLabels[action.status] || action.status}.`;
  }
  const base = readingResultMessage(result);
  const notes = result && Array.isArray(result.notes) ? result.notes.filter(Boolean) : [];
  return notes.length ? `${base} ${notes[0]}` : base;
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
      toast(processingToastMessage(finalJob.result), "ok");
      state.watched = null;
      return route();
    })
    .catch((exc) => {
      state.watched = null;
      slot.innerHTML = jobLine({ status: "failed", error: errorText(exc) });
      toast(errorText(exc), "error");
    });
}

async function analyzeQuotation(intakeId, databaseId, button) {
  const label = button.textContent;
  const slot = document.getElementById("job-slot");
  button.disabled = true;
  button.textContent = "Analyzing quotation…";
  try {
    const query = databaseId ? `?database_id=${encodeURIComponent(databaseId)}` : "";
    const response = await api.post(`/api/supplier-quotations/${encodeURIComponent(intakeId)}/analyze${query}`);
    if (slot) {
      trackJob(response.job);
      button.textContent = label;
      return;
    }
    button.textContent = "Reading and matching…";
    const finalJob = await api.pollJob(response.job.id);
    toast(processingToastMessage(finalJob.result), "ok");
    await route();
  } catch (error) {
    button.disabled = false;
    button.textContent = label;
    toast(errorText(error), "bad");
  }
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
  const pendingDocuments = review.documents.filter((doc) =>
    doc.processing !== "processed" || doc.classification === "pending" || doc.classification === "needs_review" || doc.processing === "needs_review" || doc.processing === "failed"
  );
  view.innerHTML = `<div class="row"><button class="btn neutral" data-act="reload">Refresh</button></div>
    <h2>Review items</h2>
    <div class="list" style="margin-top:8px">
      ${pendingDocuments.length ? pendingDocuments.map((doc) => `
        <div class="panel">
          <div class="row spread"><strong>${esc(fileName(doc.file))}</strong><span class="row tight">${classificationChip(doc)} ${processingChip(doc)}</span></div>
          <div class="meta">${esc(clip(doc.reason || "This document is waiting to be processed.", 180))}</div>
          <div class="row tight" style="margin-top:8px">
            <a class="btn small neutral" href="#/documents/${esc(doc.id)}">Open document</a>
            <button class="btn small" data-act="process" data-id="${esc(doc.id)}" data-force="${doc.processing === "processed" ? "1" : "0"}">Process quotation</button>
          </div>
        </div>`).join("") : empty("No documents are waiting for review.")}
    </div>
    <h2 style="margin-top:18px">Quotation lanes</h2>
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
    <div class="row tight"><a class="btn neutral small" href="#/shipments">Back to review &amp; quotations</a></div>
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

// --------------------------------------------------------------------- drafts
async function pageDrafts() {
  view.className = "supplier-work";
  skeleton();
  let rows = [];
  let supplierDrafts = [];
  try { rows = await api.get("/api/drafts"); } catch (exc) { return fail(exc); }
  try { supplierDrafts = await api.get("/api/supplier-email-drafts"); } catch (exc) { supplierDrafts = []; }
  view.innerHTML = `
    <div class="supplier-header">
      <div><span class="supplier-kicker">Human review required</span><h2>Supplier draft review</h2></div>
      <button class="supplier-btn supplier-btn-neutral" type="button" data-supplier-drafts-refresh>Refresh</button>
    </div>
    <div class="supplier-action-group">
      ${supplierDrafts.length ? supplierDrafts.map(supplierDraftCard).join("") : `<div class="supplier-empty"><span class="supplier-kicker">No Gmail drafts</span><h2>Nothing is waiting for email review</h2><p>Clarification drafts will appear here after supplier quotation analysis or mailbox processing.</p></div>`}
    </div>
    <div class="supplier-header" style="margin-top:28px">
      <div><span class="supplier-kicker">Existing local drafts</span><h2>Document clarification drafts</h2></div>
    </div>
    <div class="note">These drafts stay on this computer. Nothing is emailed until you approve the exact text and choose Send yourself, and a supplier reply can only go to the person who sent the quotation.</div>
    <div class="list" style="margin-top:12px">
      ${rows.length ? rows.map(draftCard).join("") : empty("No open local clarification drafts.")}
    </div>`;

  document.querySelectorAll("[data-supplier-drafts-refresh]").forEach((button) => button.addEventListener("click", pageDrafts));
  document.querySelectorAll("[data-supplier-draft-save]").forEach((button) => button.addEventListener("click", async (event) => {
    const draftId = event.currentTarget.dataset.supplierDraftSave;
    const form = document.querySelector(`[data-supplier-draft-form="${CSS.escape(draftId)}"]`);
    if (!form) return;
    const payload = { recipient: form.elements.recipient.value, cc: form.elements.cc.value, subject: form.elements.subject.value, body: form.elements.body.value };
    try {
      const response = await fetch(`/api/supplier-email-drafts/${encodeURIComponent(draftId)}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      const result = await response.json(); if (!response.ok) throw new Error(result.error || "Could not save draft"); toast("Draft saved."); await pageDrafts();
    } catch (error) { toast(error.message, true); }
  }));
  document.querySelectorAll("[data-supplier-draft-approve]").forEach((button) => button.addEventListener("click", async (event) => {
    try { await api.post(`/api/supplier-email-drafts/${encodeURIComponent(event.currentTarget.dataset.supplierDraftApprove)}/approve`, {}); toast("Exact draft content locked for sending."); await pageDrafts(); } catch (error) { toast(error.message, true); }
  }));
  document.querySelectorAll("[data-supplier-draft-send]").forEach((button) => button.addEventListener("click", async (event) => {
    const draftId = event.currentTarget.dataset.supplierDraftSend;
    let draft = (await api.get("/api/supplier-email-drafts").catch(() => [])).find((row) => row.id === draftId);
    if (!draft) { toast("This draft is no longer listed; refresh the page.", "error"); return; }
    // window.prompt() is not available in this view, so the confirmation is a modal like every other question.
    const boundTo = draft.recipient_locked || draft.bound_recipient
      ? ", the sender of the quotation" : "";
    const confirmation = await askReason({
      title: "Send this email?",
      body: `It goes to <strong>${esc(draft.recipient)}</strong>${boundTo}, `
        + `with the approved text for “${esc(draft.subject)}”. Nothing can be edited after sending.`,
      label: "Type SEND to confirm",
      placeholder: "SEND",
      confirmLabel: "Send now",
      danger: true,
    });
    if (!confirmation) return;
    if (confirmation.trim().toUpperCase() !== "SEND") {
      toast("Nothing was sent — the confirmation must be SEND.", "error");
      return;
    }
    button.disabled = true;
    button.textContent = "Sending…";
    try {
      const idempotencyKey = crypto.randomUUID ? crypto.randomUUID() : String(Date.now());
      const result = await api.post(`/api/supplier-email-drafts/${encodeURIComponent(draftId)}/send`,
        { confirmation: "SEND", idempotency_key: idempotencyKey });
      toast(result.status === "sent"
        ? `Sent to ${result.recipient || draft.recipient}${result.idempotent ? " (already sent before)" : ""}.`
        : "Draft send resolved.", "ok");
      await pageDrafts();
    } catch (error) {
      button.disabled = false;
      button.textContent = "Send email";
      toast(errorText(error), "bad");
    }
  }));
}

function supplierDraftCard(draft) {
  const locked = draft.status === "approved" || draft.status === "sent";
  const recipientFixed = locked || draft.recipient_locked;
  const statusLabel = SUPPLIER_DRAFT_STATUS[draft.status] || draft.status;
  return `<article class="supplier-action-card supplier-action-card-draft">
    <div class="supplier-action-head"><div><span class="supplier-kicker">Gmail draft</span><h3>${esc(draft.subject || "Untitled")}</h3></div><span class="supplier-status">${esc(statusLabel)}</span></div>
    <form class="supplier-form" data-supplier-draft-form="${esc(draft.id)}">
      <label>Recipient<input name="recipient" value="${esc(draft.recipient)}" ${recipientFixed ? "readonly" : ""}></label>
      ${draft.recipient_locked ? `<p class="supplier-meta-text">Fixed to the sender of the received quotation email.</p>` : ""}
      <label>CC<input name="cc" value="${esc(draft.cc)}" ${locked ? "disabled" : ""}></label>
      <label>Subject<input name="subject" value="${esc(draft.subject)}" ${locked ? "disabled" : ""}></label>
      <label>Body<textarea name="body" rows="10" ${locked ? "disabled" : ""}>${esc(draft.body)}</textarea></label>
      <div class="supplier-card-actions">
        ${draft.status === "draft" ? `<button type="button" class="supplier-btn supplier-btn-neutral" data-supplier-draft-save="${esc(draft.id)}">Save changes</button><button type="button" class="supplier-btn supplier-btn-primary" data-supplier-draft-approve="${esc(draft.id)}">Approve exact content</button>` : ""}
        ${draft.status === "approved" ? `<button type="button" class="supplier-btn supplier-btn-primary" data-supplier-draft-send="${esc(draft.id)}">Send email</button>` : ""}
        ${draft.status === "sent" ? `<span class="supplier-meta-text">Sent: ${esc(draft.sent_message_id || "confirmed")}</span>` : ""}
      </div>
    </form>
  </article>`;
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

// ------------------------------------------------ unified supplier workspace
async function pageInbox() {
  view.className = "supplier-work";
  skeleton("loading Operations Inbox…");

  const [status, messages, documents, supplierQuotations] = await Promise.all([
    api.get("/api/gmail/status").catch(() => ({ configured: false, accounts: [], automatic_status: "stopped" })),
    api.get("/api/gmail/inbox").catch(() => []),
    api.get("/api/documents").catch(() => []),
    api.get("/api/supplier-quotations").catch(() => ({ received: [], actions: [] })),
  ]);

  const actions = supplierQuotations.actions || [];
  const actionByIntake = new Map(actions.map((action) => [action.intake_id, action]));
  const connected = status.accounts && status.accounts.some((account) => account.status === "connected");

  function operationsStatus(documentRow, action, message) {
    if (documentRow && documentRow.classification === "irrelevant") return ["Rejected", "bad"];
    if (action) {
      const labels = {
        received: ["Ready to Process", "info"], needs_human_review: ["Missing Info — Needs Review", "warn"],
        needs_clarification: ["Missing Info — Needs Review", "warn"], ready_for_approval: ["Ready to Create Record", "ok"],
        record_created: ["Completed", "ok"], rejected: ["Rejected", "bad"],
      };
      return labels[action.status] || [String(action.status).replaceAll("_", " "), "plain"];
    }
    if (message) {
      const labels = {
        supplier_reply_pending: ["Reply Awaiting Review", "warn"],
        awaiting_supplier_review: ["Ready to Process", "info"],
        draft_ready: ["Draft waiting for you", "warn"],
        needs_clarification: ["Missing Info — Needs Review", "warn"],
        needs_database: ["Pick a supplier database", "warn"],
        needs_human_review: ["Missing Info — Needs Review", "warn"],
        ready_for_approval: ["Ready to Create Record", "ok"],
        extraction_failed: ["Extraction stopped — needs review", "bad"],
        processed: ["Completed", "ok"],
      };
      return labels[message.processing_status] || ["Ready to Process", "info"];
    }
    if (documentRow) {
      if (documentRow.processing === "processing") return ["Processing", "info"];
      if (documentRow.processing === "failed" || documentRow.classification === "failed") return ["Missing Info — Needs Review", "warn"];
      if (documentRow.classification === "needs_review") return ["Missing Info — Needs Review", "warn"];
      if (documentRow.processing === "pending") return ["Ready to Process", "info"];
      return ["Ready to Process", "info"];
    }
    return ["Ready to Process", "info"];
  }

  const documentItems = documents.map((row) => {
    const action = actionByIntake.get(row.id);
    const [statusLabel, statusKind] = operationsStatus(row, action, null);
    return {
      key: "document-" + row.id,
      channel: "Document",
      title: row.file,
      meta: `${fileSize(row.size_bytes)} · ${when(row.created_at)}`,
      statusLabel, statusKind,
      databaseName: action && action.database_name,
      detailHref: `#/documents/${encodeURIComponent(row.id)}`,
      actionLabel: action ? (action.status === "ready_for_approval" ? "Review record" : "Open workspace") : "Open workspace",
      processable: false,
      timestamp: row.created_at || "",
      intakeId: row.id,
      action,
    };
  });

  const emailItems = messages.map((message) => {
    const [statusLabel, statusKind] = operationsStatus(null, null, message);
    const attachmentNames = message.attachment_names || [];
    const linkedAction = actions.find((action) => action.file && attachmentNames.includes(action.file));
    const attachment = attachmentNames.length ? attachmentNames.join(", ") : "Email body only";
    return {
      key: "email-" + message.id,
      channel: "Email",
      title: message.subject || "No subject",
      meta: `${message.sender || "Unknown sender"} · ${when(message.created_at)} · ${attachment}`,
      statusLabel, statusKind,
      databaseName: linkedAction && linkedAction.database_name,
      detailHref: `#/gmail/${encodeURIComponent(message.id)}`,
      actionLabel: linkedAction ? "Open message & workspace" : (message.processing_status === "supplier_reply_pending" ? "Review reply" : "Open message"),
      processable: false,
      timestamp: message.created_at || "",
      action: linkedAction || null,
    };
  });

  const items = [...documentItems, ...emailItems].sort((a, b) => String(b.meta).localeCompare(String(a.meta)));
  const waitingCount = items.filter((item) => item.statusKind === "warn" || item.statusKind === "info").length;
  const readyCount = items.filter((item) => item.statusLabel === "Ready to Create Record").length;

  view.innerHTML = `
    <div class="supplier-header operations-header">
      <div><span class="supplier-kicker">Unified intake</span><h2>Operations Inbox</h2><p class="supplier-meta-text">Email and document quotations in one human-review workflow.</p></div>
      <div class="supplier-card-actions">
        <button class="supplier-btn supplier-btn-neutral" id="gmail-sync" ${connected ? "" : "disabled"}>Sync Gmail</button>
        <button class="supplier-btn supplier-btn-primary" id="operations-import">Import quotation</button>
      </div>
    </div>

    <div class="operations-import" id="operations-import-panel" hidden>
      <div class="operations-import-head"><strong>Import quotation documents</strong><span>Stored and parsed locally</span></div>
      <div class="drop" id="operations-drop" tabindex="0">
        <div class="title">Drop quotation files here</div>
        <div class="meta">PDF, DOCX, XLSX, text, email, supported images and CSV</div>
        <div class="row" style="justify-content:center;margin-top:12px">
          <input type="file" id="operations-picker" multiple aria-label="Choose quotation files">
          <button class="supplier-btn supplier-btn-primary" id="operations-upload" disabled>Upload</button>
        </div>
      </div>
      <div id="operations-upload-result"></div>
    </div>

    <div class="operations-summary">
      <div><span>${items.length}</span><small>All items</small></div>
      <div><span>${waitingCount}</span><small>Needs attention</small></div>
      <div><span>${readyCount}</span><small>Ready to create</small></div>
      <div><span>${connected ? "Connected" : "Disconnected"}</span><small>Gmail</small></div>
    </div>

    <div class="supplier-action-group">
      <div class="supplier-group-head">
        <h3>Incoming quotations</h3>
        <span>${items.length}</span>
      </div>
      <div class="operations-filters">
        <input type="search" id="operations-search" placeholder="Search source, sender or status" aria-label="Search Operations Inbox">
        <select id="operations-filter" aria-label="Filter Operations Inbox">
          <option value="">All channels</option>
          <option value="Document">Documents</option>
          <option value="Email">Email</option>
        </select>
      </div>
      <div id="operations-feed">${items.length ? items.map((item) => `
        <article class="operations-feed-item" data-channel="${esc(item.channel)}" data-search="${esc(item.title + " " + item.meta + " " + item.statusLabel)}">
          <div class="operations-feed-main">
            <span class="supplier-kicker">${esc(item.channel)}</span>
            <h3>${esc(fileName(item.title))}</h3>
            <p>${esc(item.meta)}</p>
            ${item.databaseName ? `<span class="operations-match">Matched database: ${esc(item.databaseName)}</span>` : ""}
          </div>
          <div class="operations-feed-actions">
            <span class="supplier-status ${esc(item.statusKind)}">${esc(item.statusLabel)}</span>
            ${item.detailHref !== "#/inbox" ? `<a class="supplier-btn supplier-btn-neutral" href="${item.detailHref}">${esc(item.actionLabel)}</a>` : `<button class="supplier-btn supplier-btn-neutral" disabled>${esc(item.actionLabel)}</button>`}
          </div>
        </article>`).join("") : `<div class="supplier-coming"><h2>No quotations yet</h2><p>Import a document or sync Gmail to add the first item to Operations Inbox.</p></div>`}
      </div>
    </div>`;

  document.getElementById("gmail-sync").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    button.textContent = "Reading mailbox…";
    try {
      const response = await api.post("/api/gmail/sync", { limit: 25 });
      const job = await api.pollJob(response.job.id);
      const result = job.result || {};
      toast(result.message || "Gmail synchronized.", "ok");
      await pageInbox();
    } catch (error) {
      button.disabled = false;
      button.textContent = "Sync Gmail";
      toast(errorText(error), "bad");
    }
  });

  document.getElementById("operations-import").addEventListener("click", () => {
    const panel = document.getElementById("operations-import-panel");
    panel.hidden = !panel.hidden;
    if (!panel.hidden) document.getElementById("operations-picker").focus();
  });

  const picker = document.getElementById("operations-picker");
  const uploadButton = document.getElementById("operations-upload");
  const drop = document.getElementById("operations-drop");
  let staged = [];
  const stageFiles = (files) => {
    staged = staged.concat(Array.from(files));
    uploadButton.disabled = staged.length === 0;
    uploadButton.textContent = staged.length ? `Upload ${staged.length} file(s)` : "Upload";
  };
  picker.addEventListener("change", () => stageFiles(picker.files));
  ["dragenter", "dragover"].forEach((eventName) => drop.addEventListener(eventName, (event) => { event.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "dragend"].forEach((eventName) => drop.addEventListener(eventName, () => drop.classList.remove("over")));
  drop.addEventListener("drop", (event) => {
    event.preventDefault();
    drop.classList.remove("over");
    if (event.dataTransfer && event.dataTransfer.files.length) stageFiles(event.dataTransfer.files);
  });
  uploadButton.addEventListener("click", async () => {
    if (!staged.length) return;
    uploadButton.disabled = true;
    uploadButton.textContent = "Uploading…";
    try {
      const result = await api.upload(staged, "error");
      const rejected = result.results.filter((entry) => entry.status === "rejected");
      document.getElementById("operations-upload-result").innerHTML = result.results.map((entry) => `<p class="supplier-meta-text"><strong>${esc(fileName(entry.file))}</strong>: ${esc(entry.status)}${entry.error ? " — " + esc(entry.error) : ""}</p>`).join("");
      toast(rejected.length ? `${rejected.length} file(s) rejected.` : "Quotation files imported.", rejected.length ? "bad" : "ok");
      staged = [];
      picker.value = "";
      await pageInbox();
    } catch (error) { uploadButton.disabled = false; uploadButton.textContent = "Upload"; toast(error.message, "bad"); }
  });

  const applyFeedFilters = () => {
    const needle = document.getElementById("operations-search").value.trim().toLowerCase();
    const channel = document.getElementById("operations-filter").value;
    document.querySelectorAll(".operations-feed-item").forEach((card) => {
      const matchesChannel = !channel || card.dataset.channel === channel;
      const matchesSearch = !needle || card.dataset.search.toLowerCase().includes(needle);
      card.hidden = !(matchesChannel && matchesSearch);
    });
  };
  document.getElementById("operations-search").addEventListener("input", applyFeedFilters);
  document.getElementById("operations-filter").addEventListener("change", applyFeedFilters);
}

// ------------------------------------------------ supplier operations
async function pageSupplier(section) {
  view.className = "supplier-work";
  if (section === "quotations") return pageSupplierQuotations();
  if (section === "actions") return pageSupplierActions();
  if (section === "agent") return pageSupplierAgent();
  if (section !== "databases") {
    view.innerHTML = '<div class="supplier-coming"><h2>Unknown Supplier Operations screen</h2><a class="supplier-btn supplier-btn-primary" href="#/supplier/databases">Open Database Connections</a></div>';
    return;
  }

  skeleton();
  const databases = await api.get("/api/supplier-databases").catch(() => null);
  const gmailStatus = await api.get("/api/gmail/status").catch(() => ({ configured: false, accounts: [], automatic_status: "stopped" }));
  if (databases === null) {
    view.innerHTML = '<div class="supplier-note bad">The supplier database service is not available.</div>';
    return;
  }

  const cards = databases.length ? databases.map((database) => `
    <article class="supplier-db-card">
      <div class="supplier-db-head">
        <div>
          <span class="supplier-kicker">${esc(database.category || "Supplier database")}</span>
          <h2>${esc(database.name)}</h2>
        </div>
        <span class="supplier-status">${esc(database.status)}</span>
      </div>
      <p>${esc(database.description || "No database description yet.")}</p>
      <dl class="supplier-meta">
        <div><dt>Source connection</dt><dd>${esc(database.source_ref || database.csv_path || database.csv_filename)}</dd></div>
        <div><dt>Records</dt><dd>${esc(database.record_count)}</dd></div>
        <div><dt>Schema fields</dt><dd>${esc(database.schema.length)}</dd></div>
      </dl>
      <details class="supplier-schema">
        <summary>View detected schema</summary>
        <div class="supplier-schema-table">
          ${database.schema.map((field) => `
            <div><strong>${esc(field.label || field.name)}</strong><span>${esc(field.name)}</span><code>${esc(field.type)}</code></div>`).join("")}
        </div>
      </details>
      <p class="supplier-meta-text">Provider: ${esc(database.provider_type || "csv")} · Last analyzed: ${esc(database.last_analyzed_at ? when(database.last_analyzed_at) : "Not recorded")}</p>
      <div class="supplier-card-actions">
        <button class="supplier-btn supplier-btn-neutral" data-reanalyze="${esc(database.id)}">Re-analyze Database</button>
        <button class="supplier-btn supplier-btn-neutral" data-disconnect="${esc(database.id)}">Disconnect</button>
      </div>
    </article>`).join("") : `
    <div class="supplier-empty">
      <span class="supplier-kicker">No databases connected</span>
      <h2>Connect your first supplier database</h2>
      <p>Choose a CSV file, describe the supplier domain it represents, and review the detected schema.</p>
    </div>`;

  view.innerHTML = `
    <div class="supplier-header">
      <div>
        <span class="supplier-kicker">Generalized data sources</span>
        <h2>Data Sources &amp; Integrations</h2>
      </div>
      <button class="supplier-btn supplier-btn-primary" id="supplier-connect-open">Connect data source</button>
    </div>
    <section class="supplier-action-card" style="margin-bottom:14px">
      <div class="supplier-action-head"><div><span class="supplier-kicker">Communication channel</span><h3>Gmail</h3></div><span class="supplier-status ${gmailStatus.accounts && gmailStatus.accounts.some((account) => account.status === "connected") ? "ok" : "plain"}">${gmailStatus.accounts && gmailStatus.accounts.some((account) => account.status === "connected") ? "Connected" : "Disconnected"}</span></div>
      <p>Automatic synchronization: ${esc(gmailStatus.automatic_status || "stopped")}${gmailStatus.last_successful_sync_at ? ` · Last sync ${esc(when(gmailStatus.last_successful_sync_at))}` : ""}</p>
      <div class="supplier-card-actions"><a class="supplier-btn supplier-btn-neutral" href="#/inbox">Manage in Operations Inbox</a></div>
    </section>
    <div class="supplier-db-grid">${cards}</div>

    <section class="supplier-connect-panel" id="supplier-connect-panel" hidden>
      <div class="supplier-header">
        <div><span class="supplier-kicker">New connection</span><h2>Connect a data source</h2></div>
        <button class="supplier-icon-btn" id="supplier-connect-close" aria-label="Close connection form">×</button>
      </div>
      <form id="supplier-connect-form" class="supplier-form">
        <label>CSV file location<input type="text" id="supplier-file-path" required placeholder="data/supplier_databases/logistics_supplier_database.csv"></label>
        <div class="supplier-form-actions" style="justify-content:flex-start"><button type="button" class="supplier-btn supplier-btn-neutral" id="supplier-preview-path">Inspect CSV and preview schema</button></div>
        <div class="supplier-form-grid">
          <label>Database name<input type="text" name="name" required placeholder="Freight & Carrier Rate Quotes"></label>
          <label>Category<input type="text" name="category" placeholder="Freight quotations"></label>
        </div>
        <label>Description<textarea name="description" rows="3" placeholder="Describe the supplier domain this database represents and when quotations should use it."></textarea></label>
        <label>Routing aliases<input type="text" name="routing_aliases" placeholder="carrier rates, freight suppliers, fuel quotes"></label>
        <div id="supplier-preview"></div>
        <div class="supplier-form-actions">
          <button type="button" class="supplier-btn supplier-btn-neutral" id="supplier-cancel">Cancel</button>
          <button type="submit" class="supplier-btn supplier-btn-primary" id="supplier-submit" disabled>Connect database</button>
        </div>
      </form>
    </section>`;

  const panel = document.getElementById("supplier-connect-panel");
  const open = document.getElementById("supplier-connect-open");
  const close = () => { panel.hidden = true; state.supplierPreview = null; document.getElementById("supplier-submit").disabled = true; };
  open.addEventListener("click", () => { panel.hidden = false; panel.scrollIntoView({ behavior: "smooth", block: "start" }); });
  document.getElementById("supplier-connect-close").addEventListener("click", close);
  document.getElementById("supplier-cancel").addEventListener("click", close);

  document.getElementById("supplier-preview-path").addEventListener("click", async () => {
    const pathInput = document.getElementById("supplier-file-path");
    const suppliedPath = pathInput.value.trim();
    if (!suppliedPath) {
      document.getElementById("supplier-preview").innerHTML = '<p class="supplier-note bad">Enter the location of the CSV file.</p>';
      return;
    }
    document.getElementById("supplier-preview").innerHTML = '<p class="supplier-meta-text">Detecting CSV header and sample types…</p>';
    try {
      state.supplierPreview = await api.post("/api/supplier-databases/preview-path", { path: suppliedPath });
      document.getElementById("supplier-preview").innerHTML = `
        <div class="supplier-preview">
          <div class="supplier-preview-head"><strong>${esc(state.supplierPreview.csv_filename)}</strong><span>${esc(state.supplierPreview.record_count)} sample records</span></div>
          <div class="supplier-meta-text">The connected database will use this CSV path directly for reads and approved writes.</div>
          <div class="supplier-schema-table">${state.supplierPreview.schema.map((field) => `
            <div><strong>${esc(field.label)}</strong><span>${esc(field.name)}</span><code>${esc(field.type)}</code></div>`).join("")}</div>
        </div>`;
      document.getElementById("supplier-submit").disabled = false;
    } catch (error) {
      document.getElementById("supplier-preview").innerHTML = `<p class="supplier-note bad">${esc(error.message)}</p>`;
    }
  });

  document.getElementById("supplier-connect-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const suppliedPath = document.getElementById("supplier-file-path").value.trim();
    if (!suppliedPath || !state.supplierPreview) return;
    const submit = document.getElementById("supplier-submit");
    submit.disabled = true;
    submit.textContent = "Connecting…";
    try {
      await api.post("/api/supplier-databases/from-path", {
        path: suppliedPath,
        name: form.elements.name.value,
        description: form.elements.description.value,
        category: form.elements.category.value,
        routing_aliases: form.elements.routing_aliases.value,
        schema: state.supplierPreview.schema,
      });
      toast("Supplier database connected", "ok");
      await route();
    } catch (error) {
      submit.disabled = false;
      submit.textContent = "Connect database";
      toast(error.message, "bad");
    }
  });

  document.querySelectorAll("[data-reanalyze]").forEach((button) => {
    button.addEventListener("click", async () => {
      button.disabled = true;
      button.textContent = "Re-analyzing…";
      try {
        const result = await api.post(`/api/supplier-databases/${encodeURIComponent(button.dataset.reanalyze)}/reanalyze`);
        const removed = result.database.removed_fields || [];
        toast(removed.length ? `Database re-analyzed. Removed fields need review: ${removed.join(", ")}` : "Database schema and records re-analyzed.", removed.length ? "bad" : "ok");
        await route();
      } catch (error) {
        button.disabled = false;
        button.textContent = "Re-analyze Database";
        toast(error.message, "bad");
      }
    });
  });

  document.querySelectorAll("[data-disconnect]").forEach((button) => {
    button.addEventListener("click", async () => {
      const confirmed = await confirmDialog({
        title: "Disconnect this supplier database?",
        body: "<p>The CSV file will remain on disk, but it will stop being available to the app.</p>",
        confirmLabel: "Disconnect",
        danger: true,
      });
      if (!confirmed) return;
      const response = await fetch("/api/supplier-databases/" + encodeURIComponent(button.dataset.disconnect), {
        method: "DELETE",
      });
      const payload = await response.json();
      if (!response.ok) { toast(payload.error || "Could not disconnect database", "bad"); return; }
      toast("Supplier database disconnected", "ok");
      await route();
    });
  });
}

async function pageSupplierQuotations() {
  skeleton();
  const payload = await api.get("/api/supplier-quotations").catch(() => null);
  const databases = await api.get("/api/supplier-databases").catch(() => []);
  if (!payload) {
    view.innerHTML = '<div class="supplier-note bad">Could not load supplier quotations.</div>';
    return;
  }
  const options = databases.map((database) => `<option value="${esc(database.id)}">${esc(database.name)}</option>`).join("");
  const receivedCards = payload.received.map((item) => `
    <article class="supplier-action-card">
      <div class="supplier-action-head"><div><span class="supplier-kicker">Received quotation</span><h3>${esc(fileName(item.file))}</h3></div><span class="supplier-status">Received</span></div>
      <p class="supplier-meta-text">Waiting for database routing and schema analysis.</p>
      <div class="supplier-card-actions"><select class="supplier-select" data-database-for="${esc(item.intake_id)}"><option value="">Auto-route</option>${options}</select><button class="supplier-btn supplier-btn-primary" data-process-supplier="${esc(item.intake_id)}">Analyze quotation</button></div>
    </article>`).join("");
  const actionCards = payload.actions.map((action) => supplierActionCard(action, true)).join("");
  view.innerHTML = `
    <div class="supplier-header"><div><span class="supplier-kicker">Incoming</span><h2>Supplier Quotations</h2></div></div>
    <div class="supplier-db-grid">${receivedCards || ""}${actionCards || ""}</div>
    ${!receivedCards && !actionCards ? '<div class="supplier-empty"><h2>No quotations received</h2><p>Connect Gmail and the application will automatically detect supplier quotation emails.</p></div>' : ""}`;

  view.querySelectorAll("[data-process-supplier]").forEach((button) => {
    button.addEventListener("click", () => {
      const intakeId = button.dataset.processSupplier;
      const selector = view.querySelector(`[data-database-for="${CSS.escape(intakeId)}"]`);
      analyzeQuotation(intakeId, selector ? selector.value : "", button);
    });
  });
  bindSupplierActionButtons(view);
}

function supplierActionCard(action, showOpen) {
  const statusLabels = {
    received: "Received", needs_human_review: "Pending human review", needs_clarification: "Needs clarification",
    ready_for_approval: "Ready for approval", record_created: "Record created", rejected: "Rejected"
  };
  const missing = action.missing_fields.map((item) => `<li>${esc(item.label || item.field)} — ${esc(item.reason)}</li>`).join("");
  const rowEntries = Object.entries(action.proposed_row).map(([key, value]) => `<div><dt>${esc(field(key))}</dt><dd>${esc(value)}</dd></div>`).join("");
  return `<article class="supplier-action-card">
    <div class="supplier-action-head"><div><span class="supplier-kicker">${esc(action.database_name || "Database not confirmed")}</span><h3>${esc(fileName(action.file) || "Quotation")}</h3></div><span class="supplier-status">${esc(statusLabels[action.status] || action.status)}</span></div>
    <p>${esc(action.notes || "")}</p>
    ${missing ? `<div class="supplier-missing"><strong>Missing fields</strong><ul>${missing}</ul></div>` : ""}
    ${rowEntries ? `<dl class="supplier-meta supplier-row-preview">${rowEntries}</dl>` : ""}
    ${showOpen ? `<div class="supplier-card-actions">
      <a class="supplier-btn supplier-btn-neutral" href="#/documents/${esc(action.intake_id)}">Open document</a>
      ${action.status === "ready_for_approval" ? `<button class="supplier-btn supplier-btn-primary" data-approve-supplier="${esc(action.id)}">Approve and add record</button>` : ""}
      ${action.status === "needs_clarification" || action.status === "needs_human_review" ? `<button class="supplier-btn supplier-btn-neutral" data-review-supplier="${esc(action.id)}">Hold for review</button>` : ""}
      ${action.status !== "record_created" && action.status !== "rejected" ? `<button class="supplier-btn supplier-btn-neutral" data-reject-supplier="${esc(action.id)}">Reject</button>` : ""}
    </div>` : ""}
  </article>`;
}

function bindSupplierActionButtons(container) {
  container.querySelectorAll("[data-approve-supplier]").forEach((button) => {
    button.addEventListener("click", async () => {
      const approved = await confirmDialog({title: "Add supplier record to CSV?", body: "<p>The approved row will be appended once. A timestamped CSV backup will be created first.</p>", confirmLabel: "Approve and add record"});
      if (!approved) return;
      button.disabled = true;
      try { await api.post(`/api/supplier-actions/${encodeURIComponent(button.dataset.approveSupplier)}/approve`); toast("Supplier record created", "ok"); await route(); }
      catch (error) { button.disabled = false; toast(error.message, "bad"); }
    });
  });
  container.querySelectorAll("[data-review-supplier]").forEach((button) => {
    button.addEventListener("click", async () => {
      await api.post(`/api/supplier-actions/${encodeURIComponent(button.dataset.reviewSupplier)}/status`, {status: "needs_human_review", notes: "Held for explicit human review"});
      toast("Quotation held for human review", "ok"); await route();
    });
  });
  container.querySelectorAll("[data-reject-supplier]").forEach((button) => {
    button.addEventListener("click", async () => {
      await api.post(`/api/supplier-actions/${encodeURIComponent(button.dataset.rejectSupplier)}/status`, {status: "rejected", notes: "Rejected by operator"});
      toast("Supplier quotation rejected", "ok"); await route();
    });
  });
}

async function pageSupplierActions() {
  skeleton();
  const actions = await api.get("/api/supplier-actions").catch(() => []);
  const groups = {
    "Ready to add": actions.filter((action) => action.status === "ready_for_approval"),
    "Needs clarification": actions.filter((action) => action.status === "needs_clarification"),
    "Pending human review": actions.filter((action) => action.status === "needs_human_review"),
    "Record created": actions.filter((action) => action.status === "record_created"),
    "Rejected or set aside": actions.filter((action) => action.status === "rejected"),
  };
  view.innerHTML = `<div class="supplier-header"><div><span class="supplier-kicker">Workflow queue</span><h2>Action Center</h2></div></div>${Object.entries(groups).map(([name, items]) => `
    <section class="supplier-action-group"><div class="supplier-group-head"><h3>${esc(name)}</h3><span>${items.length}</span></div>
      <div class="supplier-db-grid">${items.length ? items.map((action) => supplierActionCard(action, true)).join("") : '<div class="supplier-empty"><p>No items.</p></div>'}</div>
    </section>`).join("")}`;
  bindSupplierActionButtons(view);
}

async function pageSupplierAgent() {
  const history = await api.get("/api/supplier-agent/history?limit=30").catch(() => []);
  const databases = await api.get("/api/supplier-databases").catch(() => []);
  view.innerHTML = `
    <div class="supplier-header"><div><span class="supplier-kicker">Read-only database assistant</span><h2>Supplier Agent</h2></div>
      <select class="supplier-select" id="supplier-chat-database"><option value="">All connected databases</option>${databases.map((database) => `<option value="${esc(database.id)}">${esc(database.name)}</option>`).join("")}</select>
    </div>
    <div class="supplier-chat">
      <div id="supplier-chat-messages" class="supplier-chat-messages">${history.map((item) => supplierChatBubble(item.question, item.answer, item.citations)).join("")}</div>
      <form id="supplier-chat-form" class="supplier-chat-form"><input id="supplier-chat-input" aria-label="Ask the supplier database agent" placeholder="Ask about suppliers, prices, dates, or units…" autocomplete="off"><button class="supplier-btn supplier-btn-primary" type="submit">Ask</button></form>
    </div>`;
  const messages = document.getElementById("supplier-chat-messages");
  document.getElementById("supplier-chat-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const input = document.getElementById("supplier-chat-input");
    const question = input.value.trim();
    if (!question) return;
    input.value = "";
    messages.insertAdjacentHTML("beforeend", `<div class="supplier-chat-bubble user">${esc(question)}</div><div class="supplier-chat-bubble agent">Thinking…</div>`);
    messages.scrollTop = messages.scrollHeight;
    try {
      const databaseId = document.getElementById("supplier-chat-database").value;
      const result = await api.post("/api/supplier-agent/chat", {question, database_id: databaseId || null});
      messages.lastElementChild.remove();
      messages.insertAdjacentHTML("beforeend", supplierChatBubble(result.question, result.answer, result.citations));
      messages.scrollTop = messages.scrollHeight;
    } catch (error) {
      messages.lastElementChild.textContent = error.message;
    }
  });
}

function supplierChatBubble(question, answer, citations) {
  const sources = (citations || []).slice(0, 5).map((citation) => `<li><strong>${esc(citation.database_name)}</strong> — CSV row ${esc(citation.csv_row)}</li>`).join("");
  return `<div class="supplier-chat-bubble user">${esc(question)}</div><div class="supplier-chat-bubble agent"><p>${esc(answer)}</p>${sources ? `<div class="supplier-citations"><span>Sources</span><ul>${sources}</ul></div>` : ""}</div>`;
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
      <p class="meta">Schema version ${esc(s.database.schema_version)}. Supplier database connections and their source CSV files are preserved.</p>
      <button class="btn destructive" data-act="clear-quotation-data">Clear all quotation documents and review data</button>
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

  "clear-quotation-data": async (button) => {
    const confirmed = await confirmDialog({
      title: "Clear all quotation data?",
      body: "<p>This removes uploaded and Gmail documents, extracted quotations, review records, drafts, and stored workspace copies. Supplier database connections and source CSV files are preserved.</p>",
      confirmLabel: "Clear quotation data",
      danger: true,
    });
    if (!confirmed) return;
    button.disabled = true;
    try {
      await api.remove("/api/workspace/quotation-data");
      toast("Quotation documents and review data cleared", "ok");
      location.hash = "#/inbox";
    } catch (error) {
      button.disabled = false;
      toast(errorText(error), "bad");
    }
  },

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
          "Processing started — opening the quotation workspace",
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
