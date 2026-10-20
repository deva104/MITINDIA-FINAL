/* InsureIT — frontend logic. Defensive by default: every field may be
   null and every array may be empty on an early-exit payload. */

   const STAGES = [
    { key: 'stage0_duplicate',      label: 'Duplicate check' },
    { key: 'stage0_5_classify',     label: 'Document classification' },
    { key: 'stage1_gemini',         label: 'AI assessment' },
    { key: 'stage2_deep_forensics', label: 'Image forensics' },
    { key: 'stage3_rules',          label: 'Cross-checks' }
  ];
  
  const LABELS = {
    insured_name_claim_form: 'Insured name (claim form)',
    driver_name_claim_form: 'Driver name (claim form)',
    owner_name_rc: 'Owner name (RC)',
    holder_name_dl: 'Holder name (licence)',
    registration_number_claim_form: 'Registration no. (claim form)',
    registration_number_rc: 'Registration no. (RC)',
    chassis_number: 'Chassis number',
    engine_number: 'Engine number',
    vehicle_make_model: 'Make and model',
    dl_number: 'Licence number',
    dl_expiry_date: 'Licence expiry',
    policy_number: 'Policy number',
    policy_start_date: 'Policy start',
    policy_end_date: 'Policy end',
    accident_date: 'Accident date',
    accident_time: 'Accident time',
    accident_location: 'Accident location',
    accident_description: 'Accident description',
    name_mismatch: 'Name mismatch across documents',
    insured_owner_mismatch: 'Insured is not the registered owner',
    driver_licence_mismatch: 'Driver does not match the licence holder',
    driver_not_owner: 'Driver is not the registered owner',
    registration_mismatch: 'Registration number mismatch',
    dl_expired_at_loss: 'Licence expired at time of loss',
    policy_not_active: 'Policy not active on accident date',
    damage_narrative_mismatch: 'Damage does not match description',
    no_damage_detected: 'No visible damage found',
    not_a_vehicle: 'Image is not a vehicle',
    duplicate_photo: 'Duplicate photo',
    duplicate: 'Duplicate check',
    ela: 'Error level analysis',
    exif: 'Capture metadata',
    ai_generated: 'AI-generation check'
  };

  const DOCUMENT_LABELS = {
    rc: 'Registration certificate',
    driving_licence: 'Driving licence',
    claim_form: 'Claim form',
    policy: 'Insurance policy'
  };
  
  const staged = { documents: [], photos: [] };
  let activeId = null;
  let activeClaim = null;
  let pendingOverride = null;
  
  /* ---------- helpers ---------- */
  const $ = id => document.getElementById(id);
  
  function human(key) {
    if (!key) return '';
    if (LABELS[key]) return LABELS[key];
    const s = String(key).replace(/_/g, ' ').trim();
    return s.charAt(0).toUpperCase() + s.slice(1);
  }
  
  function rupee(n) {
    const v = Number(n);
    if (!isFinite(v)) return '—';
    return '\u20B9' + v.toLocaleString('en-IN');
  }
  
  function relTime(iso) {
    const t = new Date(iso).getTime();
    if (!isFinite(t)) return '';
    const s = Math.max(0, Math.floor((Date.now() - t) / 1000));
    if (s < 60) return s + 's ago';
    if (s < 3600) return Math.floor(s / 60) + ' min ago';
    if (s < 86400) return Math.floor(s / 3600) + ' hr ago';
    return Math.floor(s / 86400) + 'd ago';
  }
  
  function confText(c) {
    if (typeof c === 'number' && isFinite(c)) return Math.round(c * 100) + '%';
    if (typeof c === 'string' && c) return c;
    return 'not reported';
  }
  
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }
  
  function verdictPill(v) {
    const cls = {
      clean: 'pill-ok',
      informational: 'pill-mute',
      info: 'pill-mute',
      suspicious: 'pill-warn',
      inconclusive: 'pill-warn',
      unavailable: 'pill-mute',
      fail: 'pill-danger'
    }[v] || 'pill-mute';
    const txt = v === 'not_applicable' ? 'not applicable' : (v || 'unknown');
    return `<span class="pill ${cls}">${esc(txt)}</span>`;
  }
  
  function sevPill(s) {
    const k = String(s || '').toLowerCase();
    const cls = (k === 'severe' || k === 'high') ? 'pill-danger'
              : (k === 'moderate' || k === 'medium') ? 'pill-warn'
              : 'pill-ok';
    return `<span class="pill ${cls}">${esc(k || 'unknown')}</span>`;
  }

  function list(value) {
    return Array.isArray(value) ? value : [];
  }

  function assessorName() {
    return String($('assessorName').value || '').trim();
  }

  function loadAssessorName() {
    try {
      return localStorage.getItem('claimpilot.assessor') || '';
    } catch (e) {
      return '';
    }
  }

  function saveAssessorName(value) {
    try {
      localStorage.setItem('claimpilot.assessor', value);
    } catch (e) {
      /* The operator can still work when storage is blocked. */
    }
  }

  /* Claims, uploads, overrides and demo reset need the admin token. It is
     asked for once and kept in localStorage (for this page only if storage is
     blocked), and asked for again when the server answers 401. */
  const ADMIN_TOKEN_KEY = 'claimpilot.adminToken';
  let adminToken = (function () {
    try {
      return localStorage.getItem(ADMIN_TOKEN_KEY) || '';
    } catch (e) {
      return '';
    }
  })();

  function rememberAdminToken(value) {
    adminToken = value;
    try {
      if (value) localStorage.setItem(ADMIN_TOKEN_KEY, value);
      else localStorage.removeItem(ADMIN_TOKEN_KEY);
    } catch (e) {
      /* Kept in memory only. */
    }
  }

  /* The token is asked for in the feed panel, not with prompt(): embedded
     browsers refuse prompt(), and the poll must never wipe what is typed. */
  let tokenWaiters = [];

  function showTokenForm(note) {
    $('claimCount').textContent = '–';
    if ($('tokenForm')) {
      $('tokenNote').textContent = note;
      return;
    }
    $('feed').innerHTML = `<form class="feed-empty token-form" id="tokenForm" autocomplete="off">
        <span class="label">Assessor access</span>
        <span id="tokenNote">${esc(note)}</span>
        <div class="token-row">
          <input id="tokenInput" type="password" autocomplete="current-password"
                 placeholder="Admin token" aria-label="Admin token">
          <button type="submit" class="btn btn-primary" id="tokenUnlock" disabled>Unlock</button>
        </div>
      </form>`;
    $('tokenInput').oninput = event => {
      $('tokenUnlock').disabled = !event.target.value.trim();
    };
    $('tokenForm').onsubmit = event => {
      event.preventDefault();
      const value = String($('tokenInput').value || '').trim();
      if (!value) return;
      rememberAdminToken(value);
      const waiters = tokenWaiters;
      tokenWaiters = [];
      waiters.forEach(resolve => resolve());
      pollClaims().then(claims => {
        if (claims.length && !activeId) openClaim(claims[0].claim_id);
      });
    };
    $('tokenInput').focus();
  }

  const TOKEN_ASK = 'Claims are shown to assessors only. Enter the admin token.';
  const TOKEN_REJECTED = 'That token was not accepted. Enter the admin token.';

  function requestToken(note) {
    showTokenForm(note);
    return new Promise(resolve => tokenWaiters.push(resolve));
  }

  function isTokenError(e) {
    return /^Admin token/.test(String((e && e.message) || ''));
  }

  /* interactive = true waits for the operator to enter the token; polling
     passes false and only shows the form. */
  /* True from a 401 until a request succeeds, so the poll keeps saying why. */
  let tokenRejected = false;

  async function adminFetch(url, options, interactive = true) {
    for (let attempt = 0; attempt < 3; attempt++) {
      if (!adminToken) {
        if (!interactive) {
          throw new Error(tokenRejected ? 'Admin token rejected' : 'Admin token required');
        }
        await requestToken(tokenRejected ? TOKEN_REJECTED : TOKEN_ASK);
      }
      const sent = adminToken;
      const headers = Object.assign({}, (options && options.headers) || {}, {
        'X-Admin-Token': adminToken
      });
      const r = await fetch(url, Object.assign({}, options || {}, { headers }));
      if (r.status !== 401) {
        tokenRejected = false;
        return r;
      }
      tokenRejected = true;
      /* A token entered meanwhile by another request is kept. */
      if (adminToken === sent) rememberAdminToken('');
    }
    throw new Error('Admin token rejected');
  }

  function updateOverrideControls() {
    const disabled = !assessorName();
    document.querySelectorAll('.override-control').forEach(control => {
      control.disabled = disabled;
      control.title = disabled ? 'Enter an assessor name first' : '';
    });
  }

  function whatsappBadge(p) {
    if (!p || p.source !== 'whatsapp') return '';
    const digits = String(p.from_number || '').replace(/\D/g, '');
    const suffix = digits ? digits.slice(-4) : '••••';
    return `<span class="source-badge">WhatsApp ···${esc(suffix)}</span>`;
  }

  /* One indicative line per part, as the server computed it. Estimates saved
     before per-part dedup carry no totals, so their items are summed. */
  function indicativeRange(estimate) {
    if (estimate && estimate.indicative_high != null) {
      return {
        low: Number(estimate.indicative_low) || 0,
        high: Number(estimate.indicative_high) || 0
      };
    }
    const items = list(estimate && estimate.unconfirmed);
    return {
      low: items.reduce((sum, item) => sum + (Number(item && item.low) || 0), 0),
      high: items.reduce((sum, item) => sum + (Number(item && item.high) || 0), 0)
    };
  }

  /* Every empty state says whether nothing was found or nothing was checked. */
  function stoppedText(p) {
    if (p.status === 'error') return 'the AI assessment was unavailable';
    if (p.stopped_at === 'duplicate') return 'the claim stopped at the duplicate check';
    if (p.stopped_at === 'not_a_vehicle') return 'the photos did not show a vehicle';
    return p.stopped_at ? 'the claim stopped at ' + human(p.stopped_at) : '';
  }

  function extractionOf(p) {
    return (p && p.extraction && typeof p.extraction === 'object') ? p.extraction : null;
  }

  function documentCount(n) {
    return n + ' document' + (n === 1 ? '' : 's');
  }
  
  /* Backend may return raw payloads or DB rows holding payload_json. */
  function normalise(data) {
    if (!Array.isArray(data)) return [];
    return data.map(c => {
      if (c && typeof c === 'object' && typeof c.payload_json === 'string') {
        try { return JSON.parse(c.payload_json); } catch (e) { return null; }
      }
      return c;
    }).filter(c => c && c.claim_id);
  }
  
  /* ---------- views ---------- */
  function show(view) {
    ['viewUpload', 'viewWorking', 'viewClaim'].forEach(v => {
      $(v).classList.toggle('hidden', v !== view);
    });
    $('main').scrollTop = 0;
  }
  
  /* ---------- health ---------- */
  async function health() {
    const el = $('health');
    try {
      const r = await fetch('api/health');
      if (!r.ok) throw new Error();
      el.className = 'health ok';
      el.querySelector('span').textContent = 'Online';
    } catch (e) {
      el.className = 'health down';
      el.querySelector('span').textContent = 'Offline';
    }
  }
  
  /* ---------- feed ---------- */
  async function pollClaims() {
    try {
      /* Never waits for the token: the form stays up until it is entered. */
      const r = await adminFetch('api/claims', null, false);
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const claims = normalise(await r.json());
      renderFeed(claims);          /* sorts newest-first in place */
      return claims;
    } catch (e) {
      if (isTokenError(e)) {
        showTokenForm(/rejected/.test(e.message) ? TOKEN_REJECTED : TOKEN_ASK);
        return [];
      }
      $('feed').innerHTML =
        '<div class="feed-empty">Cannot reach the claim service. Check the server is running.</div>';
      return [];
    }
  }
  
  function summarise(c) {
    if (c.status === 'rejected') {
      return c.stopped_at === 'duplicate'
        ? 'Duplicate photo — rejected'
        : 'Not a vehicle — rejected';
    }
    if (c.status === 'error') return 'Escalated to human review';
    const e = c.estimate || {};
    if (e.total_high) return rupee(e.total_low) + ' – ' + rupee(e.total_high);
    if (list(e.unconfirmed).length) {
      const range = indicativeRange(e);
      return 'Indicative ' + rupee(range.low) + ' – ' + rupee(range.high);
    }
    return 'Analysed';
  }
  
  function statusPill(s) {
    if (s === 'analysed') return '<span class="pill pill-ok">analysed</span>';
    if (s === 'rejected') return '<span class="pill pill-danger">rejected</span>';
    if (s === 'error')    return '<span class="pill pill-warn">error</span>';
    return '<span class="pill pill-mute">' + esc(s || 'unknown') + '</span>';
  }

  function claimPill(c) {
    if (c && c.status === 'error') return statusPill('error');
    if (!c || !c.decision) return statusPill(c && c.status);
    if (c.decision === 'auto_assess') return '<span class="pill pill-ok">auto-assessed</span>';
    if (c.decision === 'needs_review') return '<span class="pill pill-warn">needs review</span>';
    if (c.decision === 'reject') return '<span class="pill pill-danger">rejected</span>';
    return statusPill(c.status);
  }
  
  function renderFeed(claims) {
    claims.sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
    $('claimCount').textContent = claims.length;
  
    if (!claims.length) {
      $('feed').innerHTML =
        '<div class="feed-empty">No claims yet. Upload a bundle or send one over WhatsApp.</div>';
      return;
    }
  
    $('feed').innerHTML = claims.map(c => `
      <div class="claim ${c.claim_id === activeId ? 'is-active' : ''}" data-id="${esc(c.claim_id)}">
        <div class="claim-top">
          <span class="claim-id">${esc(c.claim_id)}</span>
          ${claimPill(c)}
        </div>
        <span class="claim-time">${esc(relTime(c.created_at))}${c.plate ? ` · <span class="mono">${esc(c.plate)}</span>` : ''}</span>
        <p class="claim-sum">${esc(summarise(c))}</p>
        ${c.source === 'whatsapp' ? `<div class="claim-source">${whatsappBadge(c)}</div>` : ''}
      </div>`).join('');
  
    $('feed').querySelectorAll('.claim').forEach(el => {
      el.onclick = () => openClaim(el.dataset.id);
    });
  }
  
  async function openClaim(id) {
    try {
      const r = await adminFetch('api/claims/' + encodeURIComponent(id));
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const data = await r.json();
      render(normalise([data])[0] || data);
    } catch (e) {
      if (isTokenError(e)) return;
      notify('Could not load claim ' + id + '. Click it again to retry.');
    }
  }

  /* Embedded browsers suppress alert, confirm and prompt dialogs, so
     failures are said in the page. */
  function notify(message) {
    let el = $('pageNotice');
    if (!el) {
      el = document.createElement('div');
      el.id = 'pageNotice';
      el.className = 'page-notice hidden';
      el.setAttribute('role', 'alert');
      document.body.appendChild(el);
    }
    el.textContent = message;
    el.classList.remove('hidden');
    clearTimeout(notify.timer);
    notify.timer = setTimeout(() => el.classList.add('hidden'), 6000);
  }
  
  /* ---------- claim rendering ---------- */
  function render(p) {
    if (!p || !p.claim_id) return;
    activeId = p.claim_id;
    activeClaim = p;
    show('viewClaim');
  
    const status = p.status || 'unknown';
    const stopped = p.stopped_at || null;
    const escalated = status === 'error';

    /* verdict band */
    const band = $('verdict');
    const decision = p.decision || null;
    const decisionClass = {
      auto_assess: 'is-ok',
      needs_review: 'is-error',
      reject: 'is-rejected'
    }[decision];
    band.className = 'verdict ' + (escalated ? 'is-error' : (decisionClass || (
      status === 'analysed' ? 'is-ok' : status === 'rejected' ? 'is-rejected' : 'is-error'
    )));
    $('verdictId').textContent = p.claim_id;
    const source = $('verdictSource');
    if (p.source === 'whatsapp') {
      const digits = String(p.from_number || '').replace(/\D/g, '');
      source.textContent = 'WhatsApp ···' + (digits ? digits.slice(-4) : '••••');
      source.classList.remove('hidden');
    } else {
      source.textContent = '';
      source.classList.add('hidden');
    }
    const confidence = $('verdictConfidence');
    if (typeof p.confidence === 'number' && isFinite(p.confidence)) {
      confidence.textContent = 'model confidence ' + p.confidence.toFixed(2);
      confidence.classList.remove('hidden');
    } else if (list(p.damage).length) {
      confidence.textContent = 'model confidence not reported';
      confidence.classList.remove('hidden');
    } else {
      confidence.textContent = '';
      confidence.classList.add('hidden');
    }
  
    let title = 'Analysed \u2014 estimate ready', sub = '';
    if (escalated) {
      title = 'Analysis unavailable \u2014 escalated to human review';
      sub = 'Automated assessment could not complete. A human assessor will review this claim.';
    } else if (status === 'rejected' && stopped === 'duplicate') {
      title = 'Rejected \u2014 duplicate photo detected';
      sub = 'This image was already submitted with an earlier claim.';
    } else if (status === 'rejected' && stopped === 'not_a_vehicle') {
      title = 'Rejected \u2014 image is not a vehicle';
      sub = p.vehicle_description || 'The uploaded image does not show a motor vehicle.';
    } else if (p.estimate && p.estimate.requires_survey) {
      sub = p.estimate.survey_reason || 'Estimate exceeds the automated settlement threshold.';
    }
    if (!escalated && decision === 'auto_assess') {
      title = 'Auto-assessed';
    } else if (!escalated && decision === 'needs_review') {
      title = 'Needs review';
      sub = list(p.decision_reasons)[0] || 'A human assessor should review this claim.';
    }
    const systemTitle = title;
    const finalDecision = p.final_decision;
    if (finalDecision && finalDecision.value) {
      const finalLabels = {
        approved: 'Approved',
        rejected: 'Rejected',
        more_info: 'More information requested'
      };
      const finalLabel = finalLabels[finalDecision.value] || human(finalDecision.value);
      title = finalLabel;
      const at = finalDecision.at ? new Date(finalDecision.at) : null;
      const timeLabel = at && isFinite(at.getTime())
        ? at.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
        : '';
      sub = `System: ${systemTitle}  →  ${finalLabel} by ${finalDecision.actor || 'assessor'}${timeLabel ? ', ' + timeLabel : ''}`;
      band.className = 'verdict ' + (
        finalDecision.value === 'approved' ? 'is-ok'
          : finalDecision.value === 'rejected' ? 'is-rejected' : 'is-error'
      );
    }
    $('verdictTitle').textContent = title;
    $('verdictStatus').textContent = band.classList.contains('is-ok') ? 'Clear'
      : band.classList.contains('is-rejected') ? 'Reject'
      : escalated ? 'Escalated' : 'Review';
    $('verdictSub').textContent = sub;
  
    renderUndetermined(p);
    renderDocuments(p);
    renderCase(p);
    renderBars(p);
    renderVehicles(p);
    renderEvidence(p);
    renderForensics(p);
    renderFlags(p);
    renderFields(p);
    if (!list(p.vehicles).length) renderDamage(p);
    renderEstimate(p);
    renderAudit(p);
    updateOverrideControls();
    pollClaims();
  }

  function renderUndetermined(p) {
    const items = list(p.undetermined).filter(x => x && typeof x === 'object');
    const card = $('undeterminedCard');
    if (!items.length) {
      card.classList.add('hidden');
      $('undetermined').innerHTML = '';
      return;
    }
    $('undetermined').innerHTML = items.map(item => `
      <div class="undetermined-row">
        <b>${esc(item.item || 'Unknown item')}</b>
        <span class="undetermined-reason">${esc(item.reason || '')}</span>
        ${item.impact ? `<span class="undetermined-impact">· ${esc(item.impact)}</span>` : ''}
      </div>`).join('');
    card.classList.remove('hidden');
  }

  /* ---------- case: registry record, assessor, timeline ---------- */
  const EVENT_LABELS = {
    submitted: 'Claim submitted',
    assessor_assigned: 'Assessor assigned',
    survey_scheduled: 'Survey scheduled',
    documents_added: 'Documents added',
    more_info_requested: 'More information requested',
    approved: 'Approved',
    rejected: 'Not approved',
    assessment_updated: 'Assessment updated'
  };

  function kv(rows) {
    return '<dl class="kv">' + rows.filter(r => r && r[1] != null && r[1] !== '').map(([k, v, cls]) =>
      `<dt>${esc(k)}</dt><dd${cls ? ` class="${cls}"` : ''}>${v}</dd>`).join('') + '</dl>';
  }

  function shortDate(value) {
    if (!value) return '';
    const d = new Date(String(value).length <= 10 ? value + 'T00:00:00' : value);
    return isNaN(d) ? String(value) : d.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' });
  }

  function renderCase(p) {
    const r = p.registry;
    if (r && r.plate) {
      const policy = r.policy_type === 'third_party_only' ? 'Third party only' : 'Comprehensive';
      const mobile = r.owner_mobile && p.from_number
        ? (String(r.owner_mobile).slice(-10) === String(p.from_number).replace(/\D/g, '').slice(-10)
          ? ['WhatsApp number', 'matches the registered mobile', 'ok']
          : ['WhatsApp number', 'not the registered mobile', 'warn'])
        : null;
      $('registry').innerHTML = kv([
        ['Plate', `<span class="mono">${esc(r.plate)}</span>`],
        ['Owner', esc(r.owner_name)],
        ['Vehicle', esc([r.colour, r.make, r.model, r.variant].filter(Boolean).join(' '))],
        ['Body · fuel', esc([r.body_type, r.fuel].filter(Boolean).join(' · '))],
        ['RTO · city', esc([r.rto, r.city].filter(Boolean).join(' · '))],
        ['Chassis · engine', `<span class="mono">…${esc(r.chassis_last5 || '')} · …${esc(r.engine_last5 || '')}</span>`],
        ['Insurer', esc(r.insurer)],
        ['Policy', `<span class="mono">${esc(r.policy_number)}</span> · ${esc(policy)}`],
        ['Valid', `${esc(shortDate(r.policy_start))} – ${esc(shortDate(r.policy_end))}`],
        ['IDV · NCB', `${rupee(r.idv)} · ${esc(r.ncb_percent)}%`],
        mobile
      ]);
    } else {
      $('registry').innerHTML = '<p class="empty">Not checked — no registration number was confirmed or read for this claim.</p>';
    }
    const a = p.assessor || {};
    $('caseDetails').innerHTML = kv([
      ['Tracking ID', `<span class="mono">${esc(p.tracking_id || p.claim_id)}</span>`],
      ['Assessor', a.name ? esc(a.name + (a.city ? ' · ' + a.city : '') + (a.phone ? ' · ' + a.phone : '')) : 'not assigned'],
      ['Next step', esc(p.next_step || '')]
    ]);
    const events = list(p.events);
    $('timeline').innerHTML = events.length
      ? events.map(e => `<li><span class="when">${esc(new Date(e.created_at).toLocaleString(undefined, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }))}</span>
          <span class="what">${esc(EVENT_LABELS[e.event] || e.event)}</span>
          <span class="detail">${esc(e.detail || '')}</span></li>`).join('')
      : '<li><span class="what">No timeline events recorded for this claim.</span></li>';
  }

  function renderDocuments(p) {
    const required = list(p.required_documents);
    const unclassified = list(p.unclassified).filter(
      item => item && typeof item === 'object'
    );
    const card = $('documentsCard');
    if (!required.length && !unclassified.length) {
      card.classList.add('hidden');
      $('documentChecklist').innerHTML = '';
      $('unclassifiedImages').classList.add('hidden');
      return;
    }

    /* One source of truth: the pipeline's document_check. Older claims
       without it fall back to documents_received. */
    const check = p.document_check && typeof p.document_check === 'object' ? p.document_check : null;
    const received = (check && check.received) || p.documents_received || {};
    const unchecked = !check && 'document_check' in p;
    $('documentChecklist').innerHTML = required.map(docType => {
      const count = Number(received[docType] || 0);
      let state = 'Missing';
      if (count) state = '✓ Received' + (count > 1 ? ` (${count})` : '');
      else if (unchecked) state = 'Not checked';
      else if (check && check.unverified) state = 'Not established — a document could not be read';
      return `<div class="document-row">
        <span>${esc(DOCUMENT_LABELS[docType] || human(docType))}</span>
        <span class="document-state ${count ? 'is-received' : 'is-missing'}">
          ${esc(state)}
        </span>
      </div>`;
    }).join('');

    const unclassifiedBox = $('unclassifiedImages');
    if (unclassified.length) {
      unclassifiedBox.innerHTML = `
        <div class="unclassified-title">Unclassified images</div>
        <div class="unclassified-list">${unclassified.map(item => `
          <div class="unclassified-item">
            ${item.url ? `<img src="${esc(item.url)}" alt="${esc(item.filename || 'Unclassified image')}">` : ''}
            <span>${esc(item.filename || 'Image')}</span>
          </div>`).join('')}</div>`;
      unclassifiedBox.classList.remove('hidden');
    } else {
      unclassifiedBox.innerHTML = '';
      unclassifiedBox.classList.add('hidden');
    }
    card.classList.remove('hidden');
  }

  function vehicleStatusPill(status) {
    const config = {
      claimable: ['pill-ok', 'Claimable'],
      no_damage: ['pill-mute', 'No damage'],
      all_duplicate: ['pill-danger', 'Already claimed'],
      not_vehicle: ['pill-mute', 'Not a vehicle']
    }[status] || ['pill-mute', human(status || 'unknown')];
    return `<span class="pill ${config[0]}">${esc(config[1])}</span>`;
  }

  function damageOverrideControls(item) {
    if (!item || !item.damage_id) return '';
    const confirmed = item.confirmed === true;
    const rejected = item.rejected === true;
    const confirm = !confirmed && !rejected
      ? `<button class="btn btn-confirm is-prominent override-control"
           data-action="confirm_damage" data-target="${esc(item.damage_id)}">Confirm</button>`
      : '';
    const reject = !rejected
      ? `<button class="btn btn-reject-damage override-control"
           data-action="reject_damage" data-target="${esc(item.damage_id)}">Reject</button>`
      : '';
    const options = ['minor', 'moderate', 'severe'].map(value =>
      `<option value="${value}" ${value === item.severity ? 'selected' : ''}>${human(value)}</option>`
    ).join('');
    return `<div class="damage-override-actions">
      ${confirm}${reject}
      <select class="severity-override override-control"
        data-action="set_severity" data-target="${esc(item.damage_id)}"
        data-current="${esc(item.severity || 'moderate')}">${options}</select>
    </div>`;
  }

  function vehicleDamageRows(vehicle, p) {
    const estimate = vehicle.estimate || {};
    const damage = list(vehicle.damage);
    const lines = list(estimate.line_items);
    const unconfirmed = list(estimate.unconfirmed);
    const unpriced = list(estimate.unpriced);
    if (!damage.length) {
      if (p && p.status === 'error') {
        return '<p class="empty">Damage not assessed — the AI assessment was unavailable.</p>';
      }
      return '<p class="empty">No visible damage was detected for this vehicle.</p>';
    }
    return damage.map(item => {
      const line = lines.find(value =>
        value && (
          (value.damage_id && value.damage_id === item.damage_id)
          || (!value.damage_id && value.part === item.part)
        )
      );
      const uncertain = unconfirmed.find(value =>
        value && (
          (value.damage_id && value.damage_id === item.damage_id)
          || (!value.damage_id && value.part === item.part)
        )
      );
      const manual = unpriced.some(value => value && value.part === item.part);
      const also = list(line && line.also_reported).map(value =>
        `<span class="also-chip">also: ${esc(human(value).toLowerCase())}</span>`
      ).join('');
      let price = 'Included in panel price';
      if (item.rejected) price = 'Excluded by assessor';
      else if (line) price = `${rupee(line.low)} – ${rupee(line.high)}`;
      else if (uncertain && uncertain.counts_toward_indicative === false && uncertain.covered_by) {
        price = uncertain.covered_by === 'confirmed line for this part'
          ? 'Same panel — already priced'
          : 'Same panel — counted once';
      } else if (uncertain && uncertain.low != null) {
        price = `${rupee(uncertain.low)} – ${rupee(uncertain.high)} indicative`;
      } else if (manual) price = 'Manual pricing';
      const classes = [
        'vehicle-damage-row',
        item.confirmed === false ? 'is-unconfirmed' : '',
        item.rejected === true ? 'is-rejected' : ''
      ].filter(Boolean).join(' ');
      const note = item.rejected
        ? 'rejected by assessor — not priced'
        : item.confirmed === false ? 'unconfirmed — not priced' : '';
      return `<div class="${classes}">
        <div>
          <div class="vehicle-damage-name">${esc(human(item.part))}</div>
          <div class="vehicle-damage-meta">${esc(human(item.damage_type))}${also ? `<div>${also}</div>` : ''}</div>
          ${note ? `<div class="unconfirmed-note">${note}</div>` : ''}
          ${damageOverrideControls(item)}
        </div>
        ${sevPill(item.severity)}
        <span class="vehicle-damage-price">${price}</span>
      </div>`;
    }).join('');
  }

  function renderVehicles(p) {
    const vehicles = list(p.effective_vehicles).length
      ? list(p.effective_vehicles).filter(v => v && typeof v === 'object')
      : list(p.vehicles).filter(v => v && typeof v === 'object');
    const section = $('vehicles');
    const banner = $('multiVehicle');
    const legacyDamage = $('damageCard');
    if (!vehicles.length) {
      section.classList.add('hidden');
      section.innerHTML = '';
      banner.classList.add('hidden');
      banner.innerHTML = '';
      legacyDamage.classList.remove('hidden');
      return;
    }

    legacyDamage.classList.add('hidden');
    const claimableCount = Number.isInteger(p.claimable_count)
      ? p.claimable_count
      : vehicles.filter(v => v.status === 'claimable').length;
    if (claimableCount > 1) {
      const primary = vehicles[0] && vehicles[0].label
        ? vehicles[0].label : 'vehicle group 0';
      banner.textContent = `${claimableCount} vehicles detected — a claim covers one vehicle. Headline estimate refers to ${primary}.`;
      banner.classList.remove('hidden');
    } else {
      banner.textContent = '';
      banner.classList.add('hidden');
    }

    section.innerHTML = vehicles.map(vehicle => {
      const names = list(vehicle.photos);
      const urls = list(vehicle.photo_urls);
      const excluded = new Set(
        list(vehicle.excluded_photos).map(name => String(name).toLowerCase())
      );
      const photos = names.map((name, index) => {
        const url = urls[index];
        if (!url) return '';
        const isExcluded = excluded.has(String(name).toLowerCase());
        return `<div class="vehicle-photo ${isExcluded ? 'is-excluded' : ''}">
          <img src="${esc(url)}" alt="${esc(name || vehicle.label || 'Vehicle photo')}">
          ${isExcluded ? '<small>previously claimed</small>' : ''}
        </div>`;
      }).join('');
      const estimate = vehicle.estimate || {};
      const range = indicativeRange(estimate);
      const total = estimate.total_high
        ? `${rupee(estimate.total_low)} – ${rupee(estimate.total_high)}`
          + (range.high ? ` + ${rupee(range.low)} – ${rupee(range.high)} indicative` : '')
        : range.high
          ? `No confirmed estimate · ${rupee(range.low)} – ${rupee(range.high)} indicative`
          : list(estimate.unconfirmed).length
            ? 'No confirmed estimate · unconfirmed findings need manual pricing'
            : p.status === 'error' ? 'Not assessed' : 'No estimate';
      return `<div class="card vehicle-card">
        <div class="vehicle-head">
          <h2 class="vehicle-title">${esc(vehicle.label || 'Vehicle')}</h2>
          ${vehicleStatusPill(vehicle.status)}
        </div>
        ${photos ? `<div class="vehicle-photos">${photos}</div>` : ''}
        <div class="vehicle-damage">${vehicleDamageRows(vehicle, p)}</div>
        <div class="vehicle-total"><span>Group estimate</span><strong>${total}</strong></div>
      </div>`;
    }).join('');
    section.classList.remove('hidden');
  }
  
  function renderBars(p) {
    const timings = Array.isArray(p.timings) ? p.timings : [];
    const map = {};
    timings.forEach(t => {
      if (t && t.stage) {
        map[t.stage] = {
          seconds: Number(t.seconds) || 0,
          cached: t.cached === true,
          via: typeof t.via === 'string' ? t.via : ''
        };
      }
    });
    const max = Math.max(0.01, ...Object.values(map).map(value => value.seconds));
  
    $('bars').innerHTML = STAGES.map((s, i) => {
      const chip = `<span class="num-chip">0${i + 1}</span>`;
      if (!(s.key in map)) {
        const skipped = p.stopped_at === 'duplicate' && s.key === 'stage1_gemini';
        return `<div class="bar-row is-skipped">${chip}
          <div class="bar-label"><span class="bar-name">${s.label}</span></div>
          <div class="bar-skip">${skipped ? 'not required' : 'not reached'}</div>
          <span class="bar-time">\u2014</span></div>`;
      }
      const timing = map[s.key];
      const sec = timing.seconds;
      const pct = Math.max(2, (sec / max) * 100);
      const slim = pct < 12 ? ' slim' : '';
      const cached = timing.cached === true;
      const via = timing.via && timing.via !== 'cached' ? esc(timing.via) : '';
      const note = cached ? 'cached' : via;
      return `<div class="bar-row ${cached ? 'is-cached' : ''}">${chip}
        <div class="bar-label"><span class="bar-name">${s.label}</span>${note ? `<span class="bar-via">${note}</span>` : ''}</div>
        <div class="bar-track"><div class="bar-fill${slim}" style="width:${pct}%"></div></div>
        <span class="bar-time">${sec.toFixed(2)}s</span></div>`;
    }).join('');
  
    const out = $('pipelineCallout');
    if (p.stopped_at === 'duplicate') {
      const s = ((map['stage0_duplicate'] || {}).seconds || 0).toFixed(2);
      out.textContent = `Rejected at the duplicate check in ${s}s \u2014 no damage assessment performed`;
      out.classList.remove('hidden');
    } else {
      out.classList.add('hidden');
    }
  }
  
  function renderEvidence(p) {
    const f = (p.forensics || []).filter(x => x && x.check === 'ela');
    const box = $('evidence'), sw = $('imgSwitch');
    const originals = f.map(x => x.original_url).filter(Boolean);
    const heats = f.map(x => x.heatmap_url).filter(Boolean);
  
    if (!originals.length && !heats.length) {
      sw.classList.add('hidden');
      const ran = (p.timings || []).some(t => t && t.stage === 'stage2_deep_forensics');
      const why = stoppedText(p);
      box.innerHTML = ran
        ? '<p class="empty">No photo was eligible for error-level analysis (JPEG vehicle photos only).</p>'
        : `<p class="empty">Image forensics not run${why ? ' — ' + esc(why) : ''}.</p>`;
      return;
    }
  
    const draw = urls => {
      box.innerHTML = urls.map(u => `<img src="${esc(u)}" alt="Claim evidence">`).join('');
    };
    draw(originals.length ? originals : heats);
  
    if (heats.length && originals.length) {
      sw.classList.remove('hidden');
      sw.querySelectorAll('button').forEach(b => {
        b.onclick = () => {
          sw.querySelectorAll('button').forEach(x => x.classList.remove('is-active'));
          b.classList.add('is-active');
          draw(b.dataset.mode === 'heatmap' ? heats : originals);
        };
      });
    } else {
      sw.classList.add('hidden');
    }
  }
  
  function renderForensics(p) {
    const items = Array.isArray(p.forensics) ? p.forensics : [];
    if (!items.length) {
      $('forensics').innerHTML = '<p class="empty">No forensic checks were run.</p>';
      return;
    }
    $('forensics').innerHTML = items.map(f => {
      let geo = '';
      if (f.lat != null && f.lon != null) {
        geo = `<div class="chips"><span class="chip">${esc(f.lat)}, ${esc(f.lon)}</span>
          <a class="maplink" target="_blank" rel="noopener"
             href="https://www.google.com/maps?q=${encodeURIComponent(f.lat + ',' + f.lon)}">View on map</a></div>`;
      }
      const score = typeof f.score === 'number' && isFinite(f.score)
        ? `<span class="forensic-score">score ${f.score.toFixed(2)}</span>`
        : '';
      return `<div class="row">
        <div class="row-top"><span class="row-title">${esc(human(f.check))}</span>
          <span class="forensic-verdict">${score}${verdictPill(f.verdict)}</span></div>
        <p class="row-text">${esc(f.detail || '')}</p>${geo}</div>`;
    }).join('');
  }
  
  function flagsNotice(p) {
    const ex = extractionOf(p);
    const why = stoppedText(p);
    if (p.status === 'rejected' || (!ex && why)) {
      return `Cross-document checks not run${why ? ' — ' + why : ''}.`;
    }
    if (ex && ex.status === 'failed') {
      return 'Cross-document checks not run — documents could not be read.';
    }
    if (ex && ex.status === 'partial') {
      const failed = list(ex.failed).length;
      return `Some cross-document checks not run — ${documentCount(failed)} could not be read.`;
    }
    return '';
  }

  /* Information about the claim that is never a flag (e.g. the driver is
     not the registered owner, which is normal). */
  function notesHtml(p) {
    return list(p.notes).filter(n => n && typeof n === 'object').map(n => `<div class="row">
        <div class="row-top"><span class="row-title">${esc(human(n.rule))}</span><span class="pill pill-mute">info</span></div>
        <p class="row-text">${esc(n.message || '')}</p></div>`).join('');
  }

  function renderFlags(p) {
    const flags = Array.isArray(p.flags) ? p.flags : [];
    const notice = flagsNotice(p);
    const ex = extractionOf(p);
    if (!flags.length) {
      let text = notice;
      if (!text) {
        if (ex && ex.status === 'ok') {
          text = `No inconsistencies found across the ${documentCount(list(ex.read).length || Number(ex.documents) || 0)} read.`;
        } else if (ex && ex.status === 'not_attempted') {
          text = 'No cross-document checks — no documents were submitted.';
        } else {
          text = list(p.fields).length
            ? 'No inconsistencies found in the document fields read.'
            : 'No cross-document checks — no document fields were read.';
        }
      }
      $('flags').innerHTML = `<p class="empty">${esc(text)}</p>` + notesHtml(p);
      return;
    }
    $('flags').innerHTML = (notice ? `<p class="empty">${esc(notice)}</p>` : '') + notesHtml(p) + flags.map(f => {
      const ev = Array.isArray(f.evidence) ? f.evidence : [];
      const chips = ev.length
        ? `<div class="chips">${ev.map(e => `<span class="chip">${esc(human(e))}</span>`).join('')}</div>`
        : '';
      return `<div class="row">
        <div class="row-top"><span class="row-title">${esc(human(f.rule))}</span>${sevPill(f.severity)}</div>
        <p class="row-text">${esc(f.message || '')}</p>${chips}</div>`;
    }).join('');
  }
  
  function fieldsNotice(p) {
    const ex = extractionOf(p);
    if (!ex) return '';
    if (ex.status === 'failed') {
      return `${documentCount(Number(ex.documents) || list(ex.failed).length)} received — extraction failed: ${ex.error || 'unknown error'}`;
    }
    if (ex.status === 'partial') {
      return list(ex.failed).map(item =>
        `${item.name || 'A document'} could not be read: ${item.error || 'unknown error'}`
      ).join(' · ');
    }
    return '';
  }

  function renderFields(p) {
    const fields = Array.isArray(p.fields) ? p.fields : [];
    const ex = extractionOf(p);
    const notice = fieldsNotice(p);
    if (!fields.length) {
      let text = notice;
      if (!text) {
        const why = stoppedText(p);
        const anyReceived = Object.values(p.documents_received || {}).some(n => Number(n) > 0);
        if (ex && ex.status === 'not_attempted' && why && p.status === 'rejected') {
          text = `Document extraction not run — ${why}.`;
        } else if (ex && ex.status === 'not_attempted') {
          text = 'No documents were submitted with this claim.';
        } else if (!ex && anyReceived) {
          text = 'Documents were received but no fields were recorded for this claim.';
        } else if (!ex && why) {
          text = `Document extraction not run — ${why}.`;
        } else {
          text = 'No documents were submitted with this claim.';
        }
      }
      $('fields').innerHTML = `<p class="empty">${esc(text)}</p>`;
      return;
    }
    $('fields').innerHTML = (notice ? `<p class="empty">${esc(notice)}</p>` : '') + fields.map(f => {
      const empty = f.value == null || f.value === '';
      return `<div class="field ${empty ? 'is-null' : ''}">
        <span class="field-name">${esc(human(f.name))}</span>
        <span class="field-val">${esc(empty ? 'not found' : f.value)}</span>
        ${f.source_doc && !empty
          ? `<span class="field-src">${esc(f.source_doc)} \u00B7 confidence ${esc(confText(f.confidence))}</span>`
          : ''}
      </div>`;
    }).join('');
  }
  
  function renderDamage(p) {
    const dmg = Array.isArray(p.effective_damage)
      ? p.effective_damage : list(p.damage);
    if (!dmg.length) {
      const why = stoppedText(p);
      $('damage').innerHTML = why
        ? `<p class="empty">Damage not assessed — ${esc(why)}.</p>`
        : '<p class="empty">No visible damage was detected.</p>';
      return;
    }
    const effectiveEstimate = p.effective_estimate || p.estimate || {};
    const lines = list(effectiveEstimate.line_items);
    $('damage').innerHTML = dmg.map(d => {
      const li = lines.find(l => l && (
        (l.damage_id && l.damage_id === d.damage_id)
        || (!l.damage_id && l.part === d.part)
      ));
      const price = d.rejected
        ? '<span class="row-price">Excluded by assessor</span>'
        : li ? `<span class="row-price">${rupee(li.low)} \u2013 ${rupee(li.high)}</span>` : '';
      return `<div class="row ${d.rejected ? 'is-rejected' : ''}">
        <div class="row-top"><span class="row-title">${esc(human(d.part))}</span>${sevPill(d.severity)}</div>
        <p class="row-text">${esc(human(d.damage_type))} \u00B7 confidence ${esc(confText(d.confidence))}</p>
        <p class="row-text">${esc(d.reasoning || '')}</p>
        ${damageOverrideControls(d)}${price}</div>`;
    }).join('');
  }
  
  function renderEstimate(p) {
    const e = p.effective_estimate || p.estimate || {};
    const systemEstimate = p.estimate || {};
    const lines = Array.isArray(e.line_items) ? e.line_items : [];
    const unconfirmedItems = list(e.unconfirmed);
  
    if (!lines.length && !e.total_high && !unconfirmedItems.length
        && !list(p.effective_damage).length && !list(p.damage).length) {
      $('estimate').innerHTML = '<p class="empty">No estimate — this claim did not reach assessment.</p>';
      return;
    }
  
    const body = lines.map(l => `<div class="est-line">
        <span>${esc(human(l.part))} \u00B7 ${esc(l.severity)}</span>
        <span>${rupee(l.low)} \u2013 ${rupee(l.high)}</span></div>`).join('');
  
    const survey = e.requires_survey
      ? `<div class="callout"><b>Manual survey required</b>${esc(e.survey_reason || '')}</div>` : '';
  
    const unpriced = (Array.isArray(e.unpriced) && e.unpriced.length)
      ? `<p class="empty">${e.unpriced.length} item(s) had no rate card entry.</p>` : '';

    const indicative = indicativeRange(e);
    const headline = e.total_high
      ? `${rupee(e.total_low)} – ${rupee(e.total_high)}`
      : 'No confirmed estimate';
    const indicativeLine = indicative.high
      ? `<p class="est-indicative">+ ${rupee(indicative.low)} – ${rupee(indicative.high)} indicative, unconfirmed</p>`
      : unconfirmedItems.length && !e.total_high
        ? '<p class="est-indicative">Unconfirmed findings have no indicative price.</p>'
        : '';
    const differs = Number(e.total_low || 0) !== Number(systemEstimate.total_low || 0)
      || Number(e.total_high || 0) !== Number(systemEstimate.total_high || 0);
    const systemLine = differs
      ? `<p class="system-estimate">${Number(systemEstimate.total_high || 0)
          ? `System estimate: ${rupee(systemEstimate.total_low)} – ${rupee(systemEstimate.total_high)}`
          : 'System estimate: no confirmed findings'}</p>`
      : '';
  
    $('estimate').innerHTML = `
      <div class="est-total">${headline}</div>
      <p class="est-caption">${e.total_high ? 'Estimated repair cost range' : 'Confirmed findings only'}</p>
      ${systemLine}
      ${indicativeLine}
      <div class="est-lines">${body}</div>${unpriced}${survey}`;
  }

  function auditState(row, state) {
    state = state || {};
    const action = String(row.action || '');
    if (action === 'set_severity') return human(state.severity || 'unknown');
    if (action === 'confirm_damage') return state.confirmed ? 'confirmed' : 'unconfirmed';
    if (action === 'reject_damage') return state.rejected ? 'rejected' : 'included';
    if (action === 'set_decision') return human(state.value || 'none');
    if (action.startsWith('notification_')) return human(state.status || 'pending');
    return JSON.stringify(state);
  }

  function renderAudit(p) {
    const rows = list(p.overrides).filter(row => row && typeof row === 'object');
    const card = $('auditCard');
    if (!rows.length) {
      card.classList.add('hidden');
      $('auditTrail').innerHTML = '';
      return;
    }
    $('auditTrail').innerHTML = rows.map(row => {
      const date = row.created_at ? new Date(row.created_at) : null;
      const timestamp = date && isFinite(date.getTime())
        ? date.toLocaleString() : (row.created_at || '');
      return `<div class="audit-row">
        <span class="audit-time">${esc(timestamp)}</span>
        <div class="audit-main">
          <b>${esc(row.actor || 'System')}</b>
          <span class="audit-action">${esc(human(row.action))} · ${esc(row.target || 'claim')}</span>
          <span class="audit-change">${esc(auditState(row, row.before))} → ${esc(auditState(row, row.after))}</span>
          <span class="audit-reason">${esc(row.reason || '')}</span>
        </div>
      </div>`;
    }).join('');
    card.classList.remove('hidden');
  }

  function openOverride(action, target, value) {
    if (!activeClaim || !assessorName()) return;
    const labels = {
      confirm_damage: 'Confirm damage finding',
      reject_damage: 'Reject damage finding',
      set_severity: `Set severity to ${human(value)}`,
      set_decision: {
        approved: 'Approve claim',
        rejected: 'Reject claim',
        more_info: 'Request more information'
      }[value]
    };
    pendingOverride = { action, target, value };
    $('overrideModalTitle').textContent = labels[action] || 'Record override';
    $('overrideModalSummary').textContent =
      `This will be layered over the system assessment for ${activeClaim.claim_id}.`;
    $('overrideReason').value = '';
    $('overrideError').textContent = '';
    $('overrideError').classList.add('hidden');
    $('overrideSubmit').disabled = true;
    $('overrideModal').classList.remove('hidden');
    $('overrideReason').focus();
  }

  function closeOverride() {
    pendingOverride = null;
    $('overrideModal').classList.add('hidden');
  }

  async function submitOverride() {
    if (!pendingOverride || !activeClaim) return;
    const reason = $('overrideReason').value.trim();
    if (!reason) return;
    const button = $('overrideSubmit');
    button.disabled = true;
    $('overrideError').classList.add('hidden');
    try {
      const r = await adminFetch(
        'api/claims/' + encodeURIComponent(activeClaim.claim_id) + '/override',
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            actor: assessorName(),
            action: pendingOverride.action,
            target: pendingOverride.target,
            value: pendingOverride.value,
            reason
          })
        }
      );
      if (!r.ok) {
        let message = 'HTTP ' + r.status;
        try {
          const body = await r.json();
          message = body.detail || message;
        } catch (e) {}
        throw new Error(message);
      }
      const updated = await r.json();
      closeOverride();
      render(updated);
    } catch (e) {
      $('overrideError').textContent = e.message || 'Override failed';
      $('overrideError').classList.remove('hidden');
      button.disabled = false;
    }
  }
  
  /* ---------- upload ---------- */
  function wireDrop(el) {
    const kind = el.dataset.kind;
    const input = el.querySelector('input');
    el.querySelector('button').onclick = () => input.click();
    input.onchange = () => addFiles(kind, [...input.files]);
  
    el.addEventListener('dragover', ev => { ev.preventDefault(); el.classList.add('is-over'); });
    el.addEventListener('dragleave', () => el.classList.remove('is-over'));
    el.addEventListener('drop', ev => {
      ev.preventDefault();
      el.classList.remove('is-over');
      addFiles(kind, [...ev.dataTransfer.files]);
    });
  }
  
  function addFiles(kind, files) {
    staged[kind].push(...files);
    const box = document.querySelector(`.drop[data-kind="${kind}"] .thumbs`);
    files.forEach(f => {
      if (f.type.startsWith('image/')) {
        const img = document.createElement('img');
        img.className = 'thumb';
        img.src = URL.createObjectURL(f);
        box.appendChild(img);
      } else {
        const d = document.createElement('div');
        d.className = 'thumb thumb-doc';
        d.textContent = (f.name.split('.').pop() || 'file').toUpperCase();
        box.appendChild(d);
      }
    });
    const n = staged.documents.length + staged.photos.length;
    $('btnAnalyse').disabled = n === 0;
    $('uploadNote').textContent = n
      ? `${staged.documents.length} document(s), ${staged.photos.length} photo(s) ready.`
      : 'Add at least one document or photo.';
  }
  
  function resetStaged() {
    staged.documents = [];
    staged.photos = [];
    document.querySelectorAll('.thumbs').forEach(t => t.innerHTML = '');
    document.querySelectorAll('.drop input').forEach(i => i.value = '');
    $('btnAnalyse').disabled = true;
    $('uploadNote').textContent = 'Add at least one document or photo.';
  }
  
  /* Static stage progression — text weight only, no motion. */
  let stageTimer = null;
  function startWorking() {
    show('viewWorking');
    let i = 0;
    const paint = () => {
      $('workingStages').innerHTML = STAGES.map((s, n) => {
        const cls = n < i ? 'is-done' : n === i ? 'is-now' : '';
        return `<li class="${cls}"><span class="num-chip">0${n + 1}</span>${s.label}</li>`;
      }).join('');
    };
    paint();
    clearInterval(stageTimer);
    stageTimer = setInterval(() => { if (i < STAGES.length - 1) { i++; paint(); } }, 7000);
  }
  
  async function analyse() {
    const fd = new FormData();
    staged.documents.forEach(f => fd.append('documents', f, f.name));
    staged.photos.forEach(f => fd.append('photos', f, f.name));
    startWorking();
    try {
      const r = await adminFetch('api/analyze', { method: 'POST', body: fd });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const p = await r.json();
      clearInterval(stageTimer);
      resetStaged();
      render(p);
    } catch (e) {
      clearInterval(stageTimer);
      show('viewUpload');
      $('uploadNote').textContent = isTokenError(e)
        ? 'Analysis needs a valid admin token. Enter it in the claim feed panel.'
        : 'Analysis failed: ' + e.message + '. Check the server and try again.';
    }
  }
  
  /* ---------- boot ---------- */
  document.querySelectorAll('.drop').forEach(wireDrop);
  $('assessorName').value = loadAssessorName();
  $('assessorName').addEventListener('input', event => {
    saveAssessorName(event.target.value);
    updateOverrideControls();
  });
  document.addEventListener('click', event => {
    const control = event.target.closest('button[data-action]');
    if (!control || control.disabled) return;
    openOverride(
      control.dataset.action,
      control.dataset.target || 'claim',
      control.dataset.value || null
    );
  });
  document.addEventListener('change', event => {
    const control = event.target.closest('select.severity-override');
    if (!control || control.disabled) return;
    const value = control.value;
    control.value = control.dataset.current;
    openOverride(control.dataset.action, control.dataset.target, value);
  });
  $('overrideReason').addEventListener('input', event => {
    $('overrideSubmit').disabled = !event.target.value.trim();
  });
  $('overrideCancel').onclick = closeOverride;
  $('overrideSubmit').onclick = submitOverride;
  $('overrideModal').addEventListener('click', event => {
    if (event.target === $('overrideModal')) closeOverride();
  });
  updateOverrideControls();
  $('btnAnalyse').onclick = analyse;
  $('btnNew').onclick = () => {
    activeId = null;
    activeClaim = null;
    resetStaged();
    show('viewUpload');
    pollClaims();
  };
  /* Reset is confirmed in the page by typing RESET: embedded browsers make
     confirm() return false without showing anything. */
  function closeReset() {
    $('resetModal').classList.add('hidden');
  }
  $('btnReset').onclick = () => {
    $('resetInput').value = '';
    $('resetConfirm').disabled = true;
    $('resetError').classList.add('hidden');
    $('resetModal').classList.remove('hidden');
    $('resetInput').focus();
  };
  $('resetInput').addEventListener('input', event => {
    $('resetConfirm').disabled = event.target.value.trim() !== 'RESET';
  });
  $('resetCancel').onclick = closeReset;
  $('resetModal').addEventListener('click', event => {
    if (event.target === $('resetModal')) closeReset();
  });
  $('resetConfirm').onclick = async () => {
    if ($('resetInput').value.trim() !== 'RESET') return;
    $('resetConfirm').disabled = true;
    try {
      const r = await adminFetch('api/demo/reset', { method: 'POST' });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      closeReset();
      activeId = null;
      activeClaim = null;
      resetStaged();
      show('viewUpload');
      pollClaims();
    } catch (e) {
      $('resetError').textContent = 'Reset failed: ' + (e.message || 'unknown error');
      $('resetError').classList.remove('hidden');
      $('resetConfirm').disabled = false;
    }
  };
  
  health();
  setInterval(health, 15000);
  
  /* Open on the newest claim if one exists. Only the first poll auto-opens —
     the interval below must never steal focus from a claim being read. */
  (async () => {
    const claims = await pollClaims();   /* shows the token form when there is none */
    if (claims.length) openClaim(claims[0].claim_id);
    setInterval(() => pollClaims(), 3000);
  })();