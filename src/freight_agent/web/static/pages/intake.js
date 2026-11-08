window.IntakePages=(()=>{
 const {e,request,frame,heading}=WorkspacePages;
 async function list(){
  heading('Mailbox','New email is checked, verified as a quotation, and processed automatically.');
  frame('<section class="panel"><h2>Live processing</h2><p id="mailbox-live" aria-live="polite">Checking mailbox status…</p><div id="mailbox-status"></div></section>');
  const refresh=async()=>{const [messages,status]=await Promise.all([request('/api/gmail/inbox'),request('/api/gmail/status')]);const active=messages.filter(m=>!['not_a_quotation','ignored'].includes(m.processing_status));const live=document.getElementById('mailbox-live');if(!live)return;live.textContent=status.queue.reading_message_id?'Reading a quotation now…':status.queue.queued?`${status.queue.queued} email(s) waiting.`:'Waiting for new email.';document.getElementById('mailbox-status').innerHTML=active.map(m=>`<div class="workspace-gap row spread"><strong>${e(m.subject||'Incoming email')}</strong><span class="supplier-status">${e(String(m.processing_status).replaceAll('_',' '))}</span></div>`).join('')||'<p>No quotation emails are being processed.</p>';};
  await refresh();clearInterval(window.mailboxPoll);window.mailboxPoll=setInterval(()=>refresh().catch(()=>{}),2500);
 }
 async function documentPage(){location.hash='#/notifications';}
 async function mail(){location.hash='#/notifications';}
 return {list,document:documentPage,mail};
})();
