export function renderMdmCredentialsPortal() {
  return String.raw`<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="color-scheme" content="light">
  <meta name="description" content="Your private MDM credential connection status.">
  <title>MDM credentials</title>
  <style>
    :root{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#22332b;background:#f5f3eb;font-synthesis:none}*{box-sizing:border-box}body{margin:0;min-width:320px}.shell{max-width:740px;margin:auto;padding:30px 24px 48px}.brand{display:flex;align-items:center;gap:11px;padding-bottom:24px;border-bottom:1px solid #dce0d4;font-size:14px;font-weight:600}.mark{display:grid;place-items:center;width:34px;height:34px;border:1px solid #bccbb5;border-radius:11px;background:#edf0e6;color:#315b42;font:italic 25px Georgia,serif}.card{margin-top:38px;padding:34px;border:1px solid #dce0d4;border-radius:22px;background:#fffef9}.eyebrow{margin:0;color:#667267;font-size:11px;letter-spacing:1.5px;text-transform:uppercase}h1{margin:14px 0 23px;font:400 clamp(32px,7vw,46px)/1.15 "Iowan Old Style",Palatino,Georgia,serif;letter-spacing:-1px}.status{display:inline-block;margin:0 0 15px;padding:7px 11px;border-radius:20px;background:#f5ecd9;color:#795c29;font-size:12px;font-weight:600}.explanation{max-width:530px;margin:0;color:#667267;font-size:15px;line-height:1.8}.note{margin:26px 0 0;padding-top:20px;border-top:1px solid #dce0d4;color:#667267;font-size:12px;line-height:1.8}.skip{position:fixed;top:-80px;left:16px;padding:12px;background:#22332b;color:white}.skip:focus{top:12px}a:focus-visible{outline:3px solid #8ca97a;outline-offset:4px}@media(max-width:480px){.shell{padding:23px 20px 32px}.card{margin-top:28px;padding:25px 22px;border-radius:18px}.explanation{font-size:14px}}@media(prefers-reduced-motion:reduce){*{scroll-behavior:auto!important;animation:none!important;transition:none!important}}
  </style>
</head>
<body>
  <a class="skip" href="#main">Skip to content</a>
  <div class="shell">
    <header class="brand"><span class="mark" aria-hidden="true">c</span>Private portal</header>
    <main id="main" class="card">
      <p class="eyebrow">Microsoft Intune</p>
      <h1>MDM credentials.</h1>
      <p class="status" role="status">Not connected</p>
      <p class="explanation">Microsoft organization administrator sign-in is still needed. The Intune account connection has not been configured here.</p>
      <p class="note">Your existing devices and apps are unchanged.</p>
    </main>
  </div>
</body>
</html>`;
}

export function renderPortal() {
  return String.raw`<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="color-scheme" content="light">
  <meta name="description" content="Your private PatchDoctor status and controls.">
  <title>Control room</title>
  <style>
    :root{--paper:#f5f3eb;--card:#fffef9;--ink:#22332b;--muted:#667267;--green:#315b42;--soft:#e8eee3;--line:#dce0d4;--amber:#795c29;--amber-bg:#f5ecd9;--red:#93463b;--red-bg:#f8eae4;--radius:22px;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:var(--ink);background:var(--paper);font-synthesis:none}
    *{box-sizing:border-box}body{margin:0;min-width:320px}button,a,input{font:inherit}button{cursor:pointer}button:disabled{cursor:not-allowed;opacity:.48}button,a{-webkit-tap-highlight-color:transparent}button:focus-visible,a:focus-visible,summary:focus-visible{outline:3px solid #8ca97a;outline-offset:4px}a{color:var(--green)}[hidden]{display:none!important}
    .skip{position:fixed;left:16px;top:-80px;padding:12px;background:var(--ink);color:white;z-index:20}.skip:focus{top:12px}.shell{max-width:1120px;margin:auto;padding:0 36px 40px}.masthead{display:flex;align-items:center;justify-content:space-between;gap:20px;padding:27px 0;border-bottom:1px solid var(--line)}.brand{display:flex;align-items:center;gap:12px;font-weight:650;letter-spacing:-.3px}.brand-mark{display:grid;place-items:center;width:35px;height:35px;border:1px solid #bccbb5;border-radius:11px;color:var(--green);background:#edf0e6;font:italic 26px Georgia,serif}.eyebrow{font-size:11px;letter-spacing:1.8px;text-transform:uppercase;font-weight:650;color:var(--muted)}.protection{font-size:12px;color:var(--muted);display:flex;align-items:center;gap:7px}.dot{width:6px;height:6px;flex:none;background:#9aa095;border-radius:50%}.protection.good .dot{background:var(--green)}.protection.warn .dot{background:var(--amber)}
    .intro{padding:43px 0 30px;display:flex;align-items:flex-end;justify-content:space-between;gap:25px}h1,h2,h3,p{margin:0}h1{font-family:"Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif;font-size:clamp(36px,5vw,58px);font-weight:400;line-height:1.12;letter-spacing:-1.9px;margin:10px 0 13px}.intro p{font-size:15px;line-height:1.6;color:var(--muted);max-width:510px}.refresh-area{text-align:right;flex:none;padding-bottom:4px}.text-button{border:0;background:none;color:var(--green);font-size:13px;font-weight:650;padding:8px 0;text-underline-offset:4px}.text-button:hover{text-decoration:underline}.checked{font-size:11px;color:var(--muted);line-height:1.5;min-height:16px}
    .tabs{display:flex;gap:34px;border-bottom:1px solid #cfd6c8;margin-bottom:31px}.tab{border:0;border-bottom:3px solid transparent;background:none;color:var(--muted);padding:16px 2px 15px;font-weight:600;font-size:15px;display:flex;align-items:center;gap:9px}.tab[aria-selected="true"]{color:var(--ink);border-bottom-color:var(--green)}.tab-number{font-size:10px;letter-spacing:.5px;font-weight:500;opacity:.65}.columns{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:25px;align-items:start}.panel{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:29px}.section-label{display:flex;align-items:center;gap:9px;margin-bottom:18px}.section-line{width:20px;height:1px;background:#9aac8f}h2{font-family:"Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif;font-size:31px;font-weight:400;letter-spacing:-.8px;line-height:1.2}.lead{font-size:14px;line-height:1.75;color:var(--muted);margin-top:12px;max-width:580px}.setup-header{padding-bottom:21px}.steps{list-style:none;padding:0;margin:0}.step{display:flex;gap:15px;padding:19px 0;border-top:1px solid var(--line)}.step-marker{width:26px;height:26px;border:1px solid #ccd3c3;border-radius:50%;display:grid;place-items:center;flex:none;font-size:11px;color:var(--muted);margin-top:1px}.step.done .step-marker{background:var(--green);border-color:var(--green);color:white}.step.next .step-marker{border-color:var(--green);color:var(--green);box-shadow:0 0 0 4px var(--soft)}.step-body{flex:1;min-width:0}.step-heading{display:flex;align-items:center;justify-content:space-between;gap:12px}.step h3{font-size:14px;font-weight:600;line-height:1.5}.step p{font-size:12px;line-height:1.65;color:var(--muted);margin-top:4px}.next-label{font-size:9px;text-transform:uppercase;letter-spacing:1px;color:var(--green);white-space:nowrap}.setup-action{padding-top:6px}.button{display:inline-flex;align-items:center;justify-content:center;gap:10px;min-height:44px;padding:12px 18px;border-radius:11px;border:1px solid var(--green);background:var(--green);color:#fffef9;font-size:13px;font-weight:600;text-decoration:none;transition:background .15s,transform .15s}.button:hover:not(:disabled){background:#254b34}.button.secondary{background:transparent;color:var(--green);border-color:#c8d2be}.button.secondary:hover:not(:disabled){background:var(--soft)}.button.quiet{background:transparent;color:var(--muted);border-color:var(--line)}.button.wide{width:100%}.arrow{font-size:17px;font-weight:400}.action-note{font-size:11px;line-height:1.6;color:var(--muted);margin-top:10px}.side-stack{display:grid;gap:17px}.price-panel{background:#eaf0e2;border-color:#d8e1cf}.price{font-family:"Iowan Old Style",Georgia,serif;font-size:48px;font-weight:400;letter-spacing:-1.7px;line-height:1.05;margin:20px 0 6px}.price span{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;font-size:13px;letter-spacing:0;color:var(--muted);margin-left:6px}.price-caption{font-size:12px;line-height:1.75;color:#52614e}.price-rule{border-top:1px solid #ced9c4;padding-top:15px;margin-top:19px;display:flex;justify-content:space-between;font-size:12px}.small-heading{font-size:14px;font-weight:650;margin-bottom:10px}.notes{font-size:12px;line-height:1.8;color:var(--muted)}.notes p+p{margin-top:12px}.fine-link{font-size:11px;display:inline-block;margin-top:12px;text-underline-offset:3px}.side-note{padding:0 9px;font-size:11px;line-height:1.75;color:var(--muted)}
    .notice{font-size:12px;line-height:1.65;padding:13px 15px;border-radius:11px;margin-bottom:18px;background:var(--amber-bg);color:var(--amber);border:1px solid #e5d8bd}.notice.error{background:var(--red-bg);border-color:#ebd1c7;color:var(--red)}.notice.success{background:var(--soft);border-color:#d5dfcc;color:var(--green)}.message{margin-top:14px;white-space:pre-wrap;overflow-wrap:anywhere}.doctor-top{display:flex;justify-content:space-between;gap:16px;align-items:center;margin-bottom:20px}.badge{display:inline-flex;align-items:center;gap:7px;padding:6px 10px;border-radius:30px;font-size:10px;font-weight:650;color:var(--muted);background:#edf0e8;white-space:nowrap}.badge.good{background:var(--soft);color:var(--green)}.badge.attention{background:var(--amber-bg);color:var(--amber)}.doctor-copy{font-size:14px;color:var(--muted);line-height:1.8;margin-top:12px}.controls{display:flex;gap:9px;flex-wrap:wrap;margin:23px 0 11px}.controls .button{padding:10px 13px;min-height:42px}.limits{display:grid;grid-template-columns:1fr 1fr;border-top:1px solid var(--line);margin-top:25px;padding-top:21px;gap:20px 12px}.limit strong{display:block;font-size:21px;font-weight:500;letter-spacing:-.6px}.limit span{display:block;font-size:10px;line-height:1.6;color:var(--muted);margin-top:4px}.list-header{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:31px 0 13px}.list-header h3{font-size:14px;font-weight:650}.count{font-size:11px;color:var(--muted)}.case-list{border-top:1px solid var(--line)}.case{padding:19px 0;border-bottom:1px solid var(--line)}.case-top{display:flex;align-items:center;justify-content:space-between;gap:10px}.case-title{font-size:13px;font-weight:650;overflow-wrap:anywhere}.case-targets,.case-time{font-size:11px;line-height:1.7;color:var(--muted);margin-top:7px}.case-summary{font-size:12px;line-height:1.75;margin-top:10px;white-space:pre-wrap;overflow-wrap:anywhere}.case details{margin-top:10px}.case details p{margin-top:8px;font-size:11px;line-height:1.75;color:var(--muted);overflow-wrap:anywhere}.empty{padding:23px 0;color:var(--muted);font-size:12px;line-height:1.8}.events{list-style:none;margin:17px 0 0;padding:0}.event{padding:13px 0;border-top:1px solid var(--line);display:flex;gap:10px}.event-marker{width:5px;height:5px;border-radius:50%;background:#9aac8f;margin-top:7px;flex:none}.event-title{font-size:12px;line-height:1.5}.event-time{font-size:10px;line-height:1.6;color:var(--muted);margin-top:3px}details summary{font-size:11px;color:var(--green);cursor:pointer;line-height:1.8}details .definition{display:grid;grid-template-columns:1fr auto;gap:10px;font-size:11px;margin-top:15px;color:var(--muted)}.definition strong{font-weight:500;color:var(--ink)}.footer{display:flex;align-items:center;justify-content:space-between;gap:15px;border-top:1px solid var(--line);margin-top:35px;padding-top:19px;font-size:10px;line-height:1.7;color:var(--muted)}.footer-mark{font-family:Georgia,serif;font-style:italic;font-size:16px;color:#8b9b7f}.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
    @media(max-width:850px){.columns{grid-template-columns:minmax(0,1fr) 255px;gap:17px}.panel{padding:23px}.shell{padding:0 25px 30px}.tabs{margin-bottom:23px}.controls{gap:8px}.controls .button{font-size:12px}}
    @media(max-width:660px){.shell{padding:0 20px 28px}.masthead{padding:21px 0}.brand{font-size:14px}.protection{font-size:10px}.intro{padding:31px 0 22px;display:block}.intro p{font-size:13px;max-width:340px}h1{margin-top:10px;letter-spacing:-1.3px}.refresh-area{display:flex;align-items:center;justify-content:space-between;flex-direction:row-reverse;margin-top:17px;text-align:left;padding:0}.tabs{gap:0;justify-content:space-between;margin-bottom:21px}.tab{width:50%;justify-content:center;font-size:14px;padding:14px 0}.columns{grid-template-columns:1fr;gap:18px}.panel{padding:24px 21px;border-radius:18px}h2{font-size:29px}.lead{font-size:13px}.section-label{margin-bottom:15px}.step{gap:13px;padding:17px 0}.step p{font-size:12px}.side-stack{gap:15px}.price-panel{display:grid;grid-template-columns:1fr 1fr;column-gap:18px;align-items:start}.price-panel .eyebrow{grid-column:1/-1}.price{font-size:42px;margin:16px 0 8px}.price-caption{grid-column:1/-1}.price-rule{grid-column:1/-1;margin-top:14px;padding-top:12px}.notes p+p{margin-top:9px}.doctor-top{align-items:flex-start;margin-bottom:17px}.doctor-top .badge{margin-top:4px;max-width:150px;white-space:normal;line-height:1.5}.controls{display:grid;grid-template-columns:1fr 1fr}.controls .button{font-size:12px;padding:11px 10px}.footer{font-size:10px;align-items:flex-start}.footer p{max-width:270px}.case-top{align-items:flex-start}.case-top .badge{max-width:150px;white-space:normal;line-height:1.5}.side-note{padding:0 4px}}
    @media(prefers-reduced-motion:reduce){*{scroll-behavior:auto!important;transition:none!important;animation:none!important}}
  </style>
</head>
<body>
  <a class="skip" href="#main">Skip to content</a>
  <div class="shell">
    <header class="masthead">
      <div class="brand"><span class="brand-mark" aria-hidden="true">c</span>Control room</div>
      <div id="protection" class="protection"><span class="dot" aria-hidden="true"></span><span id="protection-text">Checking access</span></div>
    </header>
    <main id="main">
      <div class="intro">
        <div><div class="eyebrow">Your private workspace</div><h1>PatchDoctor.</h1><p>Review security repairs and their recorded checks.</p></div>
        <div class="refresh-area"><button type="button" id="refresh" class="text-button">Refresh repair status ↻</button><p class="checked" id="checked">Waiting for the first repair check</p></div>
      </div>
      <section id="panel-doctor" aria-label="PatchDoctor status and controls">
        <div id="status-error" class="notice error" role="alert" hidden></div>
        <div class="columns">
          <div class="panel">
            <div class="section-label"><span class="section-line" aria-hidden="true"></span><span class="eyebrow">PatchDoctor</span></div>
            <div class="doctor-top"><h2>Careful repairs.<br>A clear record.</h2><span id="doctor-badge" class="badge">Checking connection</span></div>
            <p class="doctor-copy" id="doctor-copy">Loading the repair service’s current state.</p>
            <p class="action-note" id="doctor-self-test" role="status">Self-test: Not confirmed</p>
            <div class="controls" aria-label="PatchDoctor controls">
              <button type="button" class="button" data-control="arm" disabled>Enable repairs</button>
              <button type="button" class="button secondary" data-control="pause" disabled>Pause new repairs</button>
              <button type="button" class="button secondary" data-control="self_test" disabled>Run self-test</button>
              <button type="button" class="button quiet" data-control="recheck" disabled>Recheck findings</button>
            </div>
            <p class="action-note">Pausing stops new repair starts. An operation already in progress may still finish. Model allowances are estimates; AWS compute is billed separately.</p>
            <div id="doctor-message" class="notice message" role="status" hidden></div>
            <div class="limits" aria-label="Configured repair limits">
              <div class="limit"><strong id="limit-attempt">—</strong><span>model allowance per attempt</span></div><div class="limit"><strong id="limit-case">—</strong><span>model allowance per case</span></div><div class="limit"><strong id="limit-starts">—</strong><span>repair starts per day</span></div><div class="limit"><strong id="limit-minutes">—</strong><span>maximum minutes per attempt</span></div>
            </div>
            <div class="list-header"><h3>Repair cases</h3><span id="case-count" class="count"></span></div>
            <div id="cases" class="case-list" aria-live="polite"><p class="empty">Loading repair cases…</p></div>
          </div>
          <aside class="side-stack" aria-label="Repair activity and details">
            <div class="panel"><h3 class="small-heading">Recent activity</h3><p class="notes">A small, private record of what happened.</p><div id="events" aria-live="polite"><p class="empty">Loading activity…</p></div></div>
            <div class="panel notes"><details><summary>How repairs work</summary><p>Only verified critical cases qualify for automatic repair. A self-test must pass before repairs can be enabled.</p><p>The agent can apply a fix and perform a necessary restart. Its operation records survive a worker restart. An unresolved vendor issue stays blocked or mitigated until it can be verified.</p><div class="definition"><span>Triage model</span><strong id="triage-model">Not loaded</strong><span>Repair model</span><strong id="repair-model">Not loaded</strong></div></details></div>
            <p class="side-note">Activity shows event types and tool names only. Model text, commands, and credentials stay out of this view.</p>
          </aside>
        </div>
      </section>
      <p id="announcement" class="sr-only" role="status" aria-live="polite"></p>
    </main>
    <footer class="footer"><p>Private by design. Real status, with room for “not yet.”</p><span class="footer-mark" aria-hidden="true">make yourself at home.</span></footer>
  </div>
  <script>
  (function () {
    'use strict';
    var state = {status: null, refreshing: false, actionBusy: false, uncertain: false};
    var byId = function (id) { return document.getElementById(id); };
    var text = function (value, maximum) { return typeof value === 'string' ? value.slice(0, maximum || 500) : ''; };
    var node = function (tag, className, value) { var element = document.createElement(tag); if (className) element.className = className; if (value !== undefined) element.textContent = value; return element; };
    function showMessage(id, message, kind) { var element = byId(id); element.textContent = message; element.className = 'notice message' + (kind ? ' ' + kind : ''); element.hidden = !message; }
    function formatTime(value) { var normalized = typeof value === 'number' && value > 0 && value < 1000000000000 ? value * 1000 : value; var date = new Date(normalized); return value && Number.isFinite(date.getTime()) ? date.toLocaleString([], {month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}) : 'Time unavailable'; }
    function amount(value, suffix) { return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? (suffix === 'money' ? '$' : '') + value.toLocaleString() : '—'; }
    async function api(path, body) {
      var controller = new AbortController(); var timer = setTimeout(function () { controller.abort(); }, 15000);
      try {
        var response = await fetch(path, {method:body === undefined ? 'GET' : 'POST', credentials:'same-origin', cache:'no-store', redirect:'error', signal:controller.signal, headers:body === undefined ? {Accept:'application/json'} : {Accept:'application/json','Content-Type':'application/json'}, body:body === undefined ? undefined : JSON.stringify(body)});
        if (!response.ok) { var reason = ''; try { var failure = await response.json(); reason = text(failure.message,400); } catch (_) {} var error = new Error(reason || (response.status === 401 ? 'Your sign-in needs to be renewed. Reload this page.' : response.status === 403 ? 'This action is available only to the owner.' : 'The service returned HTTP ' + response.status + '. Refresh the current state before trying again.')); error.confirmed = true; throw error; }
        if ((response.headers.get('content-type') || '').indexOf('application/json') < 0) throw new Error('The service returned an unexpected response. Reload to check your sign-in.');
        return await response.json();
      } catch (error) { if (error.confirmed || error.message === 'The service returned an unexpected response. Reload to check your sign-in.') throw error; throw new Error('The request could not be confirmed. Check your connection and refresh the current state.'); }
      finally { clearTimeout(timer); }
    }
    function renderStatus() {
      var data = state.status; var doctor = data && data.doctor;
      var protectedAccess = data && data.access && data.access.protected === true;
      byId('protection').className = 'protection' + (protectedAccess ? ' good' : data ? ' warn' : '');
      byId('protection-text').textContent = protectedAccess ? 'Access protected' : data ? 'Protection not confirmed' : 'Access status unavailable';
      var connected = !!doctor && doctor.connected === true; var armed = doctor && doctor.armed;
      var badge = byId('doctor-badge'); badge.className = 'badge';
      if (!doctor) { badge.textContent = 'Status unavailable'; byId('doctor-copy').textContent = 'The repair service’s state is not available yet. Refresh to check the connection.'; }
      else if (!connected) { badge.textContent = 'Not connected'; byId('doctor-copy').textContent = 'PatchDoctor is not connected to this portal yet. Its current repair state cannot be confirmed here.'; }
      else if (armed === true) { badge.textContent = 'Repairs enabled'; badge.classList.add('good'); byId('doctor-copy').textContent = 'Verified critical cases can start a repair. Findings, actions, and follow-up checks are recorded for you.'; }
      else if (armed === false) { badge.textContent = 'New repairs paused'; byId('doctor-copy').textContent = 'New automatic repairs are paused. You can run the self-test, review cases, or enable repairs when the service’s checks allow it.'; }
      else { badge.textContent = 'Repair mode unknown'; byId('doctor-copy').textContent = 'The service is connected, but its repair mode could not be confirmed. Refresh before changing it.'; }
      var proof = doctor && doctor.selfTest || {}; var commissioning = 'Not run';
      if (!connected) commissioning = 'Not confirmed';
      else if (proof.canArm === true) commissioning = 'Passed';
      else if (proof.haikuPassed !== true && proof.haikuCheckedAt) commissioning = 'Haiku check failed';
      else if (proof.haikuPassed === true) {
        if (proof.opusStatus === 'running' || proof.opusStatus === 'awaiting-native-closure') commissioning = 'Haiku passed · Opus running';
        else if (['pending','starting','start-uncertain','daily-budget-wait'].indexOf(proof.opusStatus) >= 0) commissioning = 'Haiku passed · Opus queued';
        else if (['failed','blocked','budget-exhausted'].indexOf(proof.opusStatus) >= 0) commissioning = 'Haiku passed · Opus check failed';
        else commissioning = 'Haiku passed · Opus not run';
      }
      byId('doctor-self-test').textContent = 'Self-test: ' + commissioning;
      var limits = doctor && doctor.limits || {};
      byId('limit-attempt').textContent = amount(limits.attemptUsd,'money'); byId('limit-case').textContent = amount(limits.caseUsd,'money'); byId('limit-starts').textContent = amount(limits.startsPerDay); byId('limit-minutes').textContent = amount(limits.minutes);
      byId('triage-model').textContent = text(doctor && doctor.triageModel,80) || 'Not loaded'; byId('repair-model').textContent = text(doctor && doctor.model,80) || 'Not loaded';
      document.querySelectorAll('[data-control]').forEach(function (button) { var action = button.dataset.control; button.disabled = !connected || typeof armed !== 'boolean' || state.actionBusy || state.uncertain || (action === 'arm' && (armed || proof.canArm !== true)) || (action === 'pause' && !armed); });
    }
    function caseStatus(value) { if (value === 'resolved') return ['Verified closed','good']; if (value === 'running') return ['Repair in progress','']; if (value === 'awaiting-native-closure' || value === 'patched' || value === 'mitigated') return ['Awaiting verification','']; if (value === 'self-test-passed') return ['Self-test passed','good']; if (typeof value === 'string' && (value.indexOf('blocked') === 0 || ['failed','budget-exhausted'].indexOf(value) >= 0)) return ['Needs attention','attention']; if (['pending','starting','start-uncertain','daily-budget-wait'].indexOf(value) >= 0) return ['Waiting','']; return ['State not confirmed','']; }
    function renderCases(data) {
      if (!data || !Array.isArray(data.cases) || !data.cases.every(function (item) { return item && typeof item === 'object'; })) throw new Error('Repair cases are unavailable.');
      var list = byId('cases'); list.replaceChildren(); byId('case-count').textContent = data.cases.length + (data.cases.length === 1 ? ' case' : ' cases');
      if (!data.cases.length) { list.append(node('p','empty','No cases are listed. New verified critical findings will appear here; this is not a full security scan.')); return; }
      data.cases.forEach(function (item) { if (!item || typeof item !== 'object') return; var article = node('article','case'); var top = node('div','case-top'); top.append(node('h4','case-title',text(item.cve,80) || 'Security case')); var label = caseStatus(item.status); top.append(node('span','badge ' + label[1],label[0])); article.append(top); var targets = Array.isArray(item.targets) ? item.targets.map(function (target) { return target && text(target.name,100) + (text(target.region,40) ? ' · ' + text(target.region,40) : ''); }).filter(Boolean).join(' / ') : ''; if (targets) article.append(node('p','case-targets',targets)); article.append(node('p','case-time',formatTime(item.updatedAt))); if (text(item.summary)) article.append(node('p','case-summary',text(item.summary))); var triageLabels = {waiting_for_vendor:'Waiting for the vendor; finding stays open.',not_applicable:'Vendor evidence indicates this package is not affected.',already_mitigated:'Verified mitigation; finding stays open.',needs_investigation:'Opus investigation required.'}; var triage = typeof item.triage === 'string' ? item.triage : item.triage && (item.triage.summary || item.triage.reason || triageLabels[item.triage.category]); if (text(triage) || text(item.id)) { var detail = node('details'); detail.append(node('summary','','Case details')); if (text(triage)) detail.append(node('p','',text(triage))); if (text(item.id)) detail.append(node('p','','Reference ' + text(item.id,12))); article.append(detail); } list.append(article); });
    }
    function renderEvents(data) {
      if (!data || !Array.isArray(data.events) || !data.events.every(function (item) { return item && typeof item === 'object'; })) throw new Error('Activity is unavailable.');
      var container = byId('events'); container.replaceChildren(); if (!data.events.length) { container.append(node('p','empty','No activity is listed yet. Completed checks and repair steps will appear here.')); return; }
      var list = node('ol','events'); var labels = {tool_use:'Tool used',usage:'Usage recorded',checkpoint_acknowledged:'Progress saved',checkpoint_restored:'Progress restored',case_finished:'Case updated',worker_failed:'Worker stopped',repair_started:'Repair started',self_test:'Self-test',triage:'Triage checked'}; var tools = ['Read','Edit','Write','Bash','Agent','WebFetch','WebSearch','Glob','Grep'];
      data.events.slice(0,15).forEach(function (event) { if (!event || typeof event !== 'object') return; var item = node('li','event'); var marker = node('span','event-marker'); marker.setAttribute('aria-hidden','true'); var body = node('div'); var title = labels[event.type] || 'Activity recorded'; if (tools.indexOf(event.tool) >= 0) title += ' · ' + event.tool; body.append(node('p','event-title',title),node('p','event-time',formatTime(event.timestamp))); item.append(marker,body); list.append(item); }); container.append(list);
    }
    async function refresh() {
      if (state.refreshing || state.actionBusy) return; state.refreshing = true; byId('refresh').disabled = true;
      var results = await Promise.allSettled([api('/api/status'),api('/api/doctor/cases'),api('/api/doctor/events')]);
      if (results[0].status === 'fulfilled' && results[0].value && results[0].value.doctor) { state.status = results[0].value; state.uncertain = false; byId('status-error').hidden = true; byId('checked').textContent = 'Checked ' + new Date().toLocaleTimeString([], {hour:'numeric',minute:'2-digit'}); }
      else { state.status = null; byId('status-error').textContent = 'Current status could not be loaded. ' + (results[0].status === 'rejected' ? results[0].reason.message : 'Please refresh to try again.'); byId('status-error').hidden = false; byId('checked').textContent = 'Current state not confirmed'; }
      try { if (results[1].status !== 'fulfilled') throw results[1].reason; renderCases(results[1].value); } catch (_) { byId('case-count').textContent = ''; byId('cases').replaceChildren(node('p','empty','Repair cases could not be loaded. Refresh to try again.')); }
      try { if (results[2].status !== 'fulfilled') throw results[2].reason; renderEvents(results[2].value); } catch (_) { byId('events').replaceChildren(node('p','empty','Activity could not be loaded. Refresh to try again.')); }
      state.refreshing = false; byId('refresh').disabled = false; renderStatus();
    }
    document.querySelectorAll('[data-control]').forEach(function (button) { button.addEventListener('click', async function () {
      if (state.actionBusy || button.disabled) return; var action = button.dataset.control; state.actionBusy = true; renderStatus(); showMessage('doctor-message','Sending your request…','');
      try { var result = await api('/api/doctor/control',{action:action,mutationId:crypto.randomUUID()}); if (!result || result.ok === false || result.acknowledged === false || result.refused === true || result.error || !(result.ok === true || result.acknowledged === true || text(result.message) || text(result.outcome) || text(result.status))) throw new Error(text(result && (result.message || result.error),300) || 'The service did not confirm this action.'); var outcome = text(result.message,400) || text(result.outcome,120) || text(result.status,120); showMessage('doctor-message',outcome || 'The service acknowledged your request. Refreshing its current state.','success'); byId('announcement').textContent = outcome || 'Request acknowledged.'; }
      catch (error) { state.uncertain = true; showMessage('doctor-message',text(error.message,400) + ' Refresh before sending another action.','error'); }
      finally { state.actionBusy = false; renderStatus(); await refresh(); }
    }); });
    byId('refresh').addEventListener('click',refresh);
    renderStatus(); refresh();
    setInterval(function () { if (document.visibilityState === 'visible') refresh(); },30000);
  })();
  </script>
</body>
</html>`;
}
