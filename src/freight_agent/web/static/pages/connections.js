window.ConnectionsPage = async function() {
  view.className = "supplier-work";
  skeleton();
  const [databases, gmailStatus] = await Promise.all([WorkspacePages.request("/api/supplier-databases"), WorkspacePages.request("/api/gmail/status")]);
  WorkspacePages.heading("Connections", "Manage supplier CSV destinations and the Gmail account used for explicit email delivery.");
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
        <div><dt>Source connection</dt><dd>${esc(database.csv_filename || "Source configured")} · configured</dd></div>
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

  view.innerHTML = `<p id="workspace-notice" role="status" aria-live="polite"></p>
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
      <div class="supplier-card-actions">${gmailStatus.accounts.some(a=>a.status==='connected')?gmailStatus.accounts.filter(a=>a.status==='connected').map(a=>`<button class="btn neutral" data-disconnect-gmail="${esc(a.id)}">Disconnect Gmail</button>`).join(''):`<button class="btn" id="connect-gmail">${gmailStatus.stored_authorisation?'Reconnect Gmail':'Connect Gmail'}</button>`}<a class="supplier-btn supplier-btn-neutral" href="#/inbox">Receive messages</a></div>
    </section>
    <div class="supplier-db-grid">${cards}</div>

    <section class="supplier-connect-panel" id="supplier-connect-panel" hidden>
      <div class="supplier-header">
        <div><span class="supplier-kicker">New connection</span><h2>Connect a data source</h2></div>
        <button class="supplier-icon-btn" id="supplier-connect-close" aria-label="Close connection form">×</button>
      </div>
      <form id="supplier-connect-form" class="supplier-form">
        <label>Choose a CSV file<input type="file" id="supplier-csv-upload" accept=".csv"></label><p class="meta">Or connect an existing local CSV by its location.</p><label>CSV file location<input type="text" id="supplier-file-path" placeholder="data/supplier_databases/logistics_supplier_database.csv"></label>
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

  document.getElementById('connect-gmail')?.addEventListener('click',event=>WorkspacePages.run(event.target,async()=>{const result=await WorkspacePages.request(gmailStatus.stored_authorisation?'/api/gmail/reconnect':'/api/gmail/authorize',{});if(result.authorization_url)location.href=result.authorization_url;else await route();}));
  document.querySelectorAll('[data-disconnect-gmail]').forEach(button=>button.onclick=()=>WorkspacePages.run(button,async()=>{if(!await confirmDialog({title:'Disconnect Gmail?',body:'<p>Existing sources and correspondence are retained.</p>',confirmLabel:'Disconnect'}))return;await api.remove('/api/gmail/accounts/'+button.dataset.disconnectGmail);await route();}));
  const panel = document.getElementById("supplier-connect-panel");
  const open = document.getElementById("supplier-connect-open");
  const close = () => { panel.hidden = true; state.supplierPreview = null; document.getElementById("supplier-submit").disabled = true; };
  open.addEventListener("click", () => { panel.hidden = false; panel.scrollIntoView({ behavior: "smooth", block: "start" }); });
  document.getElementById("supplier-connect-close").addEventListener("click", close);
  document.getElementById("supplier-cancel").addEventListener("click", close);

  for (const id of ['supplier-file-path','supplier-csv-upload']) document.getElementById(id).addEventListener('change',()=>{state.supplierPreview=null;document.getElementById('supplier-submit').disabled=true;});
  document.getElementById("supplier-preview-path").addEventListener("click", async () => {
    const pathInput = document.getElementById("supplier-file-path");
    const suppliedPath = pathInput.value.trim();
    const selectedFile = document.getElementById('supplier-csv-upload').files[0];
    if (!suppliedPath && !selectedFile) {
      document.getElementById("supplier-preview").innerHTML = '<p class="supplier-note bad">Enter the location of the CSV file.</p>';
      return;
    }
    document.getElementById("supplier-preview").innerHTML = '<p class="supplier-meta-text">Detecting CSV header and sample types…</p>';
    try {
      state.supplierPreview = selectedFile ? await api.multipart('/api/supplier-databases/preview', {}, selectedFile) : await api.post("/api/supplier-databases/preview-path", { path: suppliedPath });
      document.getElementById("supplier-preview").innerHTML = `
        <div class="supplier-preview">
          <div class="supplier-preview-head"><strong>${esc(state.supplierPreview.csv_filename)}</strong><span>${esc(state.supplierPreview.record_count)} sample records</span></div>
          <div class="supplier-meta-text">Review these detected fields before connecting the supplier database.</div>
          <div class="supplier-schema-table">${state.supplierPreview.schema.map((field) => `
            <div><strong>${esc(field.label)}</strong><span>${esc(field.name)}</span><code>${esc(field.type)}</code></div>`).join("")}</div>
        </div>`;
      document.getElementById("supplier-submit").disabled = false;
    } catch (error) {
      if (error.name === 'AbortError') return;
      document.getElementById("supplier-preview").innerHTML = `<p class="supplier-note bad">${esc(error.message)}</p>`;
    }
  });

  document.getElementById("supplier-connect-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const suppliedPath = document.getElementById("supplier-file-path").value.trim();
    const selectedFile = document.getElementById('supplier-csv-upload').files[0];
    if ((!suppliedPath && !selectedFile) || !state.supplierPreview) return;
    const submit = document.getElementById("supplier-submit");
    submit.disabled = true;
    submit.textContent = "Connecting…";
    try {
      if (selectedFile) {
        await api.multipart('/api/supplier-databases', {name:form.elements.name.value, description:form.elements.description.value,
          category:form.elements.category.value, routing_aliases:form.elements.routing_aliases.value, schema_json:JSON.stringify(state.supplierPreview.schema)}, selectedFile);
      } else await api.post("/api/supplier-databases/from-path", {
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
      if (error.name === 'AbortError') return;
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
      if (error.name === 'AbortError') return;
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

