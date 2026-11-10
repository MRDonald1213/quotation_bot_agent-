window.ConnectionsPage = async function() {
  view.className = "supplier-work";
  skeleton();
  const [databases, gmailStatus] = await Promise.all([
    WorkspacePages.request("/api/supplier-databases"),
    WorkspacePages.request("/api/gmail/status"),
  ]);
  WorkspacePages.heading("Connections", "One supplier master stores supplier profiles and every quotation. Gmail receives offers and sends drafts only after your approval.");
  if (databases === null) {
    view.innerHTML = '<div class="alert alert-error">The supplier master service is not available.</div>';
    return;
  }
  const database = databases[0];
  const connected = gmailStatus.accounts?.filter(account => account.status === "connected") || [];
  const masterCard = database ? `
    <article class="card bg-base-100 border border-base-300 shadow-sm">
      <div class="card-body">
        <div class="flex flex-wrap items-start justify-between gap-3"><div><p class="text-xs uppercase tracking-wide opacity-60">Canonical database</p><h2 class="card-title">Supplier Master Database</h2></div><span class="badge badge-success">${esc(database.status)}</span></div>
        <p>Supplier attributes and quotation rows share this one CSV. New quotations append as separate rows keyed by Supplier_ID and Quotation_ID.</p>
        <dl class="grid grid-cols-1 gap-3 sm:grid-cols-3"><div><dt class="text-sm opacity-60">File</dt><dd><code>${esc(database.csv_filename)}</code></dd></div><div><dt class="text-sm opacity-60">Records</dt><dd>${esc(database.record_count)}</dd></div><div><dt class="text-sm opacity-60">Fields</dt><dd>${esc(database.schema.length)}</dd></div></dl>
        <details class="collapse collapse-arrow border border-base-300"><summary class="collapse-title">View master fields</summary><div class="collapse-content"><div class="grid grid-cols-1 gap-2 sm:grid-cols-2">${database.schema.map(field=>`<div class="flex justify-between gap-2 border-b border-base-200 py-1"><strong>${esc(field.label||field.name)}</strong><code>${esc(field.name)}</code></div>`).join("")}</div></div></details>
        <div class="card-actions justify-end"><button class="btn btn-outline" id="reanalyze-master">Re-analyze master</button></div>
      </div>
    </article>` : `
    <div class="alert alert-error"><span>Supplier_Master_Database.csv is missing. Restore the canonical master file before processing quotations.</span></div>`;
  view.innerHTML = `<p id="workspace-notice" role="status" aria-live="polite"></p>
    <div class="grid gap-4 lg:grid-cols-2">${masterCard}
      <section class="card bg-base-100 border border-base-300 shadow-sm"><div class="card-body">
        <div class="flex items-start justify-between gap-3"><div><p class="text-xs uppercase tracking-wide opacity-60">Communication channel</p><h2 class="card-title">Gmail</h2></div><span class="badge ${connected.length?'badge-success':'badge-ghost'}">${connected.length?'Connected':'Disconnected'}</span></div>
        <p>Incoming quotation messages are classified, parsed, and extracted automatically. Supplier messages remain local drafts until you explicitly approve and send them.</p>
        <p>Synchronization: ${esc(gmailStatus.automatic_status||"stopped")}${gmailStatus.last_successful_sync_at?` · Last sync ${esc(when(gmailStatus.last_successful_sync_at))}`:""}</p>
        <div class="card-actions justify-end">${connected.map(account=>`<button class="btn btn-outline" data-disconnect-gmail="${esc(account.id)}">Disconnect ${esc(account.email)}</button>`).join("")||`<button class="btn btn-primary" id="connect-gmail">${gmailStatus.stored_authorisation?'Reconnect Gmail':'Connect Gmail'}</button>`}<a class="btn btn-ghost" href="#/inbox">Open mailbox</a></div>
      </div></section>
    </div>`;
  document.getElementById("connect-gmail")?.addEventListener("click", event => WorkspacePages.run(event.target, async () => {
    const result = await WorkspacePages.request(gmailStatus.stored_authorisation ? "/api/gmail/reconnect" : "/api/gmail/authorize", {});
    if (result.authorization_url) location.href = result.authorization_url; else await route();
  }));
  document.querySelectorAll("[data-disconnect-gmail]").forEach(button => button.onclick = () => WorkspacePages.run(button, async () => {
    if (!await confirmDialog({title:"Disconnect Gmail?",body:"<p>Existing quotations and correspondence are retained.</p>",confirmLabel:"Disconnect"})) return;
    await api.remove("/api/gmail/accounts/" + button.dataset.disconnectGmail);
    await route();
  }));
  document.getElementById("reanalyze-master")?.addEventListener("click", event => WorkspacePages.run(event.target, async () => {
    await api.post(`/api/supplier-databases/${encodeURIComponent(database.id)}/reanalyze`);
    toast("Supplier master schema refreshed", "ok");
    await route();
  }));
};
