window.SupplierIntelligencePage = async function (supplierId = null) {
  const { e, request, frame, heading } = WorkspacePages;
  const value = (row, key, fallback = 'Not provided') => {
    const found = row?.[key];
    return found === null || found === undefined || found === '' ? fallback : e(found);
  };
  const quoteRows = rows => (rows || []).map(quote => {
    const route = `${value(quote, 'Origin_City', '?')}${quote.Origin_Country ? `, ${e(quote.Origin_Country)}` : ''} → ${value(quote, 'Destination_City', '?')}${quote.Destination_Country ? `, ${e(quote.Destination_Country)}` : ''}`;
    return `<tr>
      <td><strong>${value(quote, 'Quotation_Reference', value(quote, 'Quotation_ID'))}</strong><small class="block opacity-60">${value(quote, 'Quotation_ID')}</small></td>
      <td>${route}<small class="block opacity-60">${value(quote, 'Origin_Port', '')}${quote.Origin_Port || quote.Destination_Port ? ' → ' : ''}${value(quote, 'Destination_Port', '')}</small></td>
      <td>${value(quote, 'Transport_Mode')} · ${value(quote, 'Service_Type')}<small class="block opacity-60">${value(quote, 'Cargo_Type')} · ${value(quote, 'Fuel_Type')} · ${value(quote, 'Equipment_Type', value(quote, 'Container_Type'))}</small></td>
      <td><strong>${value(quote, 'Quoted_Price')} ${value(quote, 'Currency', '')}</strong><small class="block opacity-60">${value(quote, 'Pricing_Basis')}</small></td>
      <td>${value(quote, 'Delivery_Time', value(quote, 'Transit_Time_Min_Days'))}</td>
      <td>${value(quote, 'Payment_Terms')}<small class="block opacity-60">Valid until ${value(quote, 'Quotation_Valid_Until')}</small></td>
      <td><span class="badge badge-sm badge-ghost">${value(quote, 'Record_Data_Status', 'Unverified')}</span><small class="block mt-1 opacity-60">${value(quote, 'Data_Source')}</small></td>
    </tr>`;
  }).join('');
  const quoteTable = rows => rows?.length
    ? `<div class="overflow-x-auto"><table class="table table-zebra table-sm min-w-[1000px]"><thead><tr><th>Quotation</th><th>Route</th><th>Mode and cargo</th><th>Price</th><th>Delivery</th><th>Payment and validity</th><th>Record source</th></tr></thead><tbody>${quoteRows(rows)}</tbody></table></div>`
    : '<div class="alert alert-info">No quotation rows have been recorded for this supplier.</div>';

  if (supplierId) {
    const result = await request(`/api/supplier-intelligence/${encodeURIComponent(supplierId)}`);
    const supplier = result.supplier;
    heading(supplier.Supplier_Name, `Supplier profile · ${supplier.Supplier_ID}`);
    frame(`<div class="mb-4 flex flex-wrap items-center gap-3"><a class="btn btn-sm btn-ghost" href="#/suppliers">← Supplier directory</a><a class="btn btn-sm btn-outline" href="#/comparison">Compare quotations</a></div>
      <div class="alert alert-info mb-4"><span>${e(result.data_note)}</span></div>
      <section class="card bg-base-100 border border-base-300 shadow-sm mb-4"><div class="card-body">
        <div class="flex flex-wrap items-start justify-between gap-3"><div><p class="text-xs uppercase tracking-wide opacity-60">Supplier master record</p><h2 class="card-title">${value(supplier, 'Supplier_Name')}</h2><p>${value(supplier, 'Supplier_ID')} · ${value(supplier, 'Category')}</p></div><span class="badge badge-outline">${value(supplier, 'Verification_Status', 'Verification not recorded')}</span></div>
        <dl class="grid gap-3 sm:grid-cols-2 lg:grid-cols-3"><div><dt class="text-sm opacity-60">Primary lanes / regions</dt><dd>${value(supplier, 'Primary_Lanes_Regions')}</dd></div><div><dt class="text-sm opacity-60">Contract type</dt><dd>${value(supplier, 'Service_Contract_Type')}</dd></div><div><dt class="text-sm opacity-60">Compliance status</dt><dd>${value(supplier, 'Compliance_Status')}</dd></div><div><dt class="text-sm opacity-60">Default payment terms</dt><dd>${value(supplier, 'Supplier_Default_Payment_Terms', value(supplier, 'Payment_Terms'))}</dd></div><div><dt class="text-sm opacity-60">Primary contact</dt><dd>${value(supplier, 'Primary_Contact_Email')}</dd></div><div><dt class="text-sm opacity-60">Data source</dt><dd>${value(supplier, 'Data_Source')}</dd></div></dl>
      </div></section>
      <section class="card bg-base-100 border border-base-300 shadow-sm"><div class="card-body"><div class="flex flex-wrap items-center justify-between gap-2"><h2 class="card-title">Quotation records</h2><span class="badge badge-primary">${supplier.quotations?.length || 0}</span></div><p class="text-sm opacity-70">Each quotation is stored as a separate row with its source and evidence details.</p>${quoteTable(supplier.quotations)}</div></section>`);
    return;
  }

  heading('Supplier Master', 'Browse supplier profiles and their quotation rows from one unified database.');
  const initial = await request('/api/supplier-intelligence');
  const categories = [...new Set((initial.suppliers || []).map(item => item.Category).filter(Boolean))].sort();
  frame(`<div class="card bg-base-100 border border-base-300 shadow-sm mb-4"><div class="card-body">
      <div class="flex flex-wrap items-start justify-between gap-3"><div><p class="text-xs uppercase tracking-wide opacity-60">One canonical file</p><h2 class="card-title">Supplier and quotation records</h2><p>${e(initial.data_note)}</p></div><a class="btn btn-primary" href="#/comparison">Compare quotes</a></div>
      <div class="grid gap-3 md:grid-cols-3"><label class="form-control"><span class="label-text">Search supplier or quote</span><input id="supplier-search" class="input input-bordered" type="search" placeholder="Name, route, price, reference"></label><label class="form-control"><span class="label-text">Category</span><select id="supplier-category-filter" class="select select-bordered"><option value="">All categories</option>${categories.map(item => `<option value="${e(item)}">${e(item)}</option>`).join('')}</select></label><label class="form-control"><span class="label-text">Transport mode</span><select id="supplier-mode-filter" class="select select-bordered"><option value="">All modes</option><option>Ocean</option><option>Air</option><option>Road</option><option>Rail</option></select></label></div>
      <p id="supplier-result-count" class="text-sm opacity-70" aria-live="polite"></p><div id="supplier-directory-grid" class="grid gap-3 md:grid-cols-2 xl:grid-cols-3"></div>
    </div></div>`);

  const render = async () => {
    const params = new URLSearchParams();
    const search = document.getElementById('supplier-search').value.trim();
    const category = document.getElementById('supplier-category-filter').value;
    const mode = document.getElementById('supplier-mode-filter').value;
    if (search) params.set('query', search);
    if (category) params.set('category', category);
    if (mode) params.set('mode', mode);
    const result = await request(`/api/supplier-intelligence?${params}`);
    const suppliers = result.suppliers || [];
    document.getElementById('supplier-result-count').textContent = `${suppliers.length} supplier record(s) · one master database`;
    document.getElementById('supplier-directory-grid').innerHTML = suppliers.map(supplier => {
      const quotations = supplier.quotations || [];
      const quoteSummary = quotations.slice(0, 2).map(quote => `${e(quote.Origin_City || '?')} → ${e(quote.Destination_City || '?')} · ${e(quote.Quoted_Price || 'price not stated')} ${e(quote.Currency || '')}`).join('<br>');
      return `<article class="card bg-base-100 border border-base-300 shadow-sm"><div class="card-body gap-3">
        <div class="flex items-start justify-between gap-2"><div><p class="text-xs opacity-60">${value(supplier, 'Supplier_ID')}</p><h3 class="card-title">${value(supplier, 'Supplier_Name')}</h3></div><span class="badge badge-sm badge-ghost">${value(supplier, 'Verification_Status', 'Verification not recorded')}</span></div>
        <p>${value(supplier, 'Category')}</p><p class="text-sm opacity-70">${value(supplier, 'Primary_Lanes_Regions')}</p>
        <div class="rounded-box bg-base-200 p-3 text-sm"><strong>${quotations.length} quotation row(s)</strong><p>${quoteSummary || 'No quotation records recorded.'}</p></div>
        <div class="card-actions justify-end"><a class="btn btn-sm btn-outline" href="#/suppliers/${encodeURIComponent(supplier.Supplier_ID)}">Open supplier record</a></div>
      </div></article>`;
    }).join('') || (!initial.suppliers?.length && !search && !category && !mode
      ? '<div class="alert alert-info">No supplier records yet. Supplier profiles will appear after a quotation record is approved and imported.</div>'
      : '<div class="alert alert-info">No supplier records match this search.</div>');
  };
  let timer;
  document.getElementById('supplier-search').addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(() => render().catch(showError), 160); });
  for (const id of ['supplier-category-filter', 'supplier-mode-filter']) document.getElementById(id).addEventListener('change', () => render().catch(showError));
  function showError(error) {
    const grid = document.getElementById('supplier-directory-grid');
    if (grid) grid.innerHTML = `<div class="alert alert-error">${e(error.message || 'Could not load supplier records.')}</div>`;
  }
  await render().catch(showError);
};
