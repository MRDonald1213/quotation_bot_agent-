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
  quotations: {title:"Quotations", guide:"Review quotations, missing information and next actions."},
  archived: {title:"Archived quotations", guide:"Preserved sources, correspondence and decisions."},
  comparison: {title:"Comparison", guide:"Compare compatible quotations for the selected lane."},
  draft: {title:"Clarification draft", guide:"Review the recipient and exact content before sending."},
  "email-draft": {title:"Email review", guide:"Review and explicitly approve the exact email content."},
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
    title: "Quotation workspace",
    guide:
      "What the agent read from this quotation, the evidence, what is still missing, and your review and import actions.",
  },
  drafts: {
    title: "Drafts",
    guide:
      "One card per quotation: who the question goes to, the email it answers, what is still missing, and the text itself. Nothing leaves this computer until you approve the exact wording and press Send.",
  },
  gmail: {
    title: "Email workspace",
    guide:
      "The email, what the agent read from it, the supplier it matched, what is still open, and the question it drafted — all in one place, processed in the background.",
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

// Quotations are titled "Supplier — route"; the filename is only the fallback for unread rows.
function quoteLabel(row) {
  const label = row && (row.name || row.quotation_name);
  return clip(label || fileName(row && row.file) || "Quotation", 64);
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
  important: ["Affects the offer", "warn"],
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

function utc(value) {
  const text = String(value);
  return new Date(/Z|[+-]\d{2}:?\d{2}$/.test(text) ? text : text + "Z");
}

function when(value) {
  if (!value) return "";
  const date = utc(value);
  if (Number.isNaN(date.getTime())) return String(value).replace("T", " ").slice(0, 16);
  return date.toLocaleString(undefined, {
    day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

function ago(value) {
  if (!value) return "";
  const seconds = (Date.now() - utc(value).getTime()) / 1000;
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
  if (exc?.name === "AbortError") return;
  view.innerHTML = `<div class="note bad">${esc(errorText(exc))}</div>
    <p><button class="btn neutral" data-act="reload">Try again</button></p>`;
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
  const routeEpoch = (window.freightRouteEpoch || 0) + 1;
  window.freightRouteEpoch = routeEpoch;
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
  skeleton("Loading workspace…");
  await refreshStatus();
  if (window.freightRouteEpoch !== routeEpoch) return;
  try {
    if (page === "") return await WorkspacePages.dashboard();
    if (page === "quotations") return id ? await WorkspacePages.detail(id) : await WorkspacePages.list();
    if (page === "archived") return await WorkspacePages.list(true);
    if (page === "comparison") return await ComparisonPage();
    if (page === "draft") return await QuotationDraftPage(id);
    if (page === "drafts") return await DraftListPage();
    if (page === "email-draft") return await EmailDraftPage(id);
  } catch (error) { return fail(error); }
  if (page === "documents") return id ? IntakePages.document(id).catch(fail) : IntakePages.list().catch(fail);
  if (page === "gmail") return id ? IntakePages.mail(id).catch(fail) : IntakePages.list().catch(fail);
  if (page === "shipments" || page === "queue") { location.hash = "#/inbox"; return; }
  if (page === "notifications") return NotificationsPage().catch(fail);
  if (page === "settings") return SettingsPage().catch(fail);
  if (page === "inbox") return IntakePages.list().catch(fail);
  if (page === "supplier" && ["actions", "quotations"].includes(id)) { location.hash = "#/quotations"; return; }
  if (page === "supplier" && id === "agent") { document.getElementById("open-assistant").click(); location.hash="#/comparison"; return; }
  if (page === "supplier") return ConnectionsPage().catch(fail);
  view.innerHTML = empty("This page does not exist.");
}

const ACTIONS = {
  reload: async () => route(),

  "clear-quotation-data": async (button) => {
    const confirmed = await confirmDialog({
      title: "Clear all quotation data?",
      body: "<p>This removes uploaded and Gmail documents, extracted quotations, review records, drafts, and stored workspace copies. Your Gmail account stays connected and keeps receiving new mail — only what has already been read is cleared. Supplier database connections and source CSV files are preserved.</p>",
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

document.getElementById("logout").onclick = async () => { await fetch("/api/logout", {method:"POST"}); location.href="/login"; };
