/* Add daisyUI component classes to the existing semantic UI markup. */
(() => {
  "use strict";

  const add = (node, ...classes) => node.classList.add(...classes);
  const syncChoice = (element) => {
    const desired = element.matches('nav a')
      ? (element.classList.contains('active') ? 'btn-primary' : 'btn-ghost')
      : (element.getAttribute('aria-selected') === 'true' ? 'btn-primary' : 'btn-ghost');
    if (element.classList.contains(desired) && !element.classList.contains(desired === 'btn-primary' ? 'btn-ghost' : 'btn-primary')) return;
    element.classList.remove('btn-primary', 'btn-ghost');
    add(element, desired);
  };
  const decorate = (root) => {
    const elements = root instanceof Element ? [root, ...root.querySelectorAll("*")] : [...root.querySelectorAll("*")];
    for (const element of elements) {
      if (element.matches("button, a.btn, a.supplier-btn, nav a")) {
        const classes = element.classList;
        const wasButton = classes.contains("btn");
        add(element, "btn", "btn-sm");
        if (element.matches("nav a")) {
          syncChoice(element);
        } else if (element.closest(".workspace-tabs")) {
          syncChoice(element);
        } else if (classes.contains("destructive")) add(element, "btn-outline", "btn-error");
        else if (classes.contains("subtle")) add(element, "btn-outline", "btn-primary");
        else if (classes.contains("neutral") || classes.contains("supplier-btn-neutral")) add(element, "btn-outline");
        else if (classes.contains("supplier-btn-primary") || wasButton) add(element, "btn-primary");
        else add(element, "btn-outline");
      }

      if (element.matches("input")) {
        const type = (element.getAttribute("type") || "text").toLowerCase();
        if (["button", "submit", "reset"].includes(type)) {
          add(element, "btn", "btn-sm", type === "submit" ? "btn-primary" : "btn-outline");
        } else if (type === "file") add(element, "file-input", "file-input-sm");
        else if (type === "checkbox") add(element, "checkbox", "checkbox-primary");
        else if (type === "radio") add(element, "radio", "radio-primary");
        else if (type !== "hidden") add(element, "input", "input-sm");
      }
      if (element.matches("select")) add(element, "select", "select-sm");
      if (element.matches("textarea")) add(element, "textarea");
      if (element.matches("table")) add(element, "table", "table-sm");

      if (element.matches(".side")) add(element, "navbar", "bg-base-100");
      if (element.matches(".workspace-tabs")) add(element, "tabs", "tabs-box");
      if (element.matches(".workspace-tabs [role=tab]")) add(element, "tab");
      if (element.matches(".quote-stats")) add(element, "stats", "stats-horizontal", "bg-base-100", "border", "border-base-300");
      if (element.matches(".quote-stat")) add(element, "stat");
      if (element.matches(".quote-stat .stat-title")) add(element, "stat-title");
      if (element.matches(".quote-stat .stat-value")) add(element, "stat-value", "text-primary");
      if (element.matches(".quote-stat .stat-desc")) add(element, "stat-desc");
      if (element.matches(".proposal-item")) add(element, "collapse", "collapse-arrow", "bg-base-100", "border", "border-base-300");
      if (element.matches(".proposal-item > summary")) add(element, "collapse-title");
      if (element.matches(".proposal-content")) add(element, "collapse-content");
      if (element.matches(".supplier-db-card details, .panel details, .quote-detail-group")) add(element, "collapse", "collapse-arrow", "bg-base-100");
      if (element.matches(".supplier-db-card details > summary, .panel details > summary, .quote-detail-group > summary")) add(element, "collapse-title");
      if (element.matches(".supplier-db-card details > :not(summary), .panel details > :not(summary), .quote-detail-group > :not(summary)")) add(element, "collapse-content");
      if (element.matches(".operations-summary")) add(element, "stats", "stats-horizontal", "bg-base-100", "border", "border-base-300");
      if (element.matches(".operations-summary > div")) add(element, "stat");
      if (element.matches(".cards > a.card")) add(element, "stats", "stats-vertical", "bg-base-100", "border", "border-base-300");
      if (element.matches(".cards > a.card .label")) add(element, "stat-title");
      if (element.matches(".cards > a.card .value")) add(element, "stat-value", "text-primary");
      if (element.matches(".workspace-table")) add(element, "table-zebra");
      if (element.matches(".jobline .dot, .system .dot")) add(element, "status");

      const classes = element.classList;
      if (classes.contains("panel") || classes.contains("card")) {
        add(element, "card", "bg-base-100", "border", "border-base-300", "shadow-sm");
        const hasDirectBody = [...element.children].some((child) => child.classList.contains("card-body"));
        if (hasDirectBody) add(element, "card-has-body");
        else add(element, "card-body");
      } else if (["supplier-db-card", "supplier-action-card", "supplier-connect-panel", "supplier-preview", "operations-import", "operations-feed-item", "operations-summary", "supplier-chat", "workspace-footer", "assistant-drawer", "hero"].some((name) => classes.contains(name))) {
        add(element, "card", "bg-base-100", "border", "border-base-300", "shadow-sm");
        if (!["operations-summary", "operations-feed-item", "workspace-footer"].some((name) => classes.contains(name))) add(element, "card-body");
      }
      if (classes.contains("supplier-empty") || classes.contains("supplier-coming")) add(element, "card", "bg-base-100", "border", "border-base-300", "shadow-sm", "card-body");
      if (element.matches(".hero .step")) add(element, "card", "bg-base-100", "border", "border-base-300");
      if (element.matches(".supplier-schema-table code, .supplier-group-head span")) add(element, "badge", "badge-sm", "badge-outline");

      if (classes.contains("chip") || classes.contains("supplier-status")) {
        add(element, "badge", "badge-sm");
        if (["pending", "warn"].some((name) => classes.contains(name))) add(element, "badge-warning");
        else if (["failed", "bad", "critical"].some((name) => classes.contains(name))) add(element, "badge-error");
        else if (classes.contains("info")) add(element, "badge-info");
        else if (["ok", "imported"].some((name) => classes.contains(name))) add(element, "badge-success");
        else if (classes.contains("plain")) add(element, "badge-ghost");
        else add(element, "badge-primary", "badge-outline");
      }

      if (classes.contains("note") || classes.contains("supplier-note") || classes.contains("empty")) {
        add(element, "alert");
        if (["bad", "err"].some((name) => classes.contains(name))) add(element, "alert-error");
        else if (classes.contains("warn")) add(element, "alert-warning");
        else add(element, classes.contains("empty") ? "alert-info" : "alert-success");
      }
      if (classes.contains("toast-item")) add(element, "alert", ["err", "bad"].some((name) => classes.contains(name)) ? "alert-error" : "alert-success");
      if (classes.contains("modal-card")) add(element, "modal-box", "bg-base-100");
      if (classes.contains("archive-dialog")) add(element, "modal-box", "bg-base-100");
      if (classes.contains("supplier-missing")) add(element, "alert", "alert-warning");
      if (classes.contains("workspace-needs")) add(element, "alert", "alert-info");
      if (classes.contains("supplier-intelligence")) add(element, "card", "bg-base-100", "border", "border-base-300", "shadow-sm");
      if (classes.contains("supplier-chat-bubble")) {
        add(element, "chat-bubble");
        if (classes.contains("user")) add(element, "chat-bubble-primary");
      }
      if (classes.contains("workflow-step-list")) add(element, "steps", "steps-vertical");
      if (classes.contains("workflow-step")) add(element, "step");
      if (classes.contains("agent-message")) add(element, "chat", "chat-start", "agent-message-enter");
      if (classes.contains("user-message")) add(element, "chat", "chat-end", "agent-message-enter");
      if (classes.contains("agent-bubble")) add(element, "chat-bubble", "chat-bubble-primary");
      if (classes.contains("user-bubble")) add(element, "chat-bubble", "chat-bubble-neutral");
      if (classes.contains("agent-avatar")) add(element, "chat-image", "avatar", "placeholder");
      if (element.matches(".agent-avatar > div")) add(element, "agent-avatar-circle");
      if (classes.contains("agent-processing")) add(element, "loading", "loading-dots", "loading-sm");
      if (classes.contains("source-text")) add(element, "bg-base-200", "border", "border-base-300", "rounded-box");
      if (element.matches("pre.text")) add(element, "bg-base-200", "border", "border-base-300", "rounded-box");
    }
  };

  decorate(document.body);
  new MutationObserver((mutations) => {
    for (const mutation of mutations) {
      if (mutation.type === "childList") mutation.addedNodes.forEach((node) => node.nodeType === Node.ELEMENT_NODE && decorate(node));
      else if (mutation.target instanceof Element && mutation.target.matches('nav a, .workspace-tabs [role="tab"]')) syncChoice(mutation.target);
    }
  }).observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ["class", "aria-selected"] });
})();
