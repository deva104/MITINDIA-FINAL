/* ClaimPilot AI — frontend logic. Defensive by default: every field may be
   null and every array may be empty on an early-exit payload. */

   const STAGES = [
    { key: 'stage0_duplicate',      label: 'Duplicate check' },
    { key: 'stage1_gemini',         label: 'AI assessment' },
    { key: 'stage2_deep_forensics', label: 'Image forensics' },
    { key: 'stage3_rules',          label: 'Cross-checks' }
  ];
  
  const LABELS = {
    insured_name_claim_form: 'Insured name (claim form)',
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
    registration_mismatch: 'Registration number mismatch',
    dl_expired_at_loss: 'Licence expired at time of loss',
    policy_not_active: 'Policy not active on accident date',
    damage_narrative_mismatch: 'Damage does not match description',
    no_damage_detected: 'No visible damage found',
    not_a_vehicle: 'Image is not a vehicle',
    duplicate_photo: 'Duplicate photo',
    duplicate: 'Duplicate check',
    ela: 'Error level analysis',
    exif: 'Capture metadata'
  };
  
  const staged = { documents: [], photos: [] };
  let activeId = null;
  
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
    return '—';
  }
  
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }
  
  function verdictPill(v) {
    const cls = { clean: 'pill-ok', suspicious: 'pill-warn', fail: 'pill-danger' }[v] || 'pill-mute';
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
      const r = await fetch('api/claims');
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const claims = normalise(await r.json());
      renderFeed(claims);          /* sorts newest-first in place */
      return claims;
    } catch (e) {
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
    return 'Analysed';
  }
  
  function statusPill(s) {
    if (s === 'analysed') return '<span class="pill pill-ok">analysed</span>';
    if (s === 'rejected') return '<span class="pill pill-danger">rejected</span>';
    if (s === 'error')    return '<span class="pill pill-warn">error</span>';
    return '<span class="pill pill-mute">' + esc(s || 'unknown') + '</span>';
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
          ${statusPill(c.status)}
        </div>
        <span class="claim-time">${esc(relTime(c.created_at))}</span>
        <p class="claim-sum">${esc(summarise(c))}</p>
      </div>`).join('');
  
    $('feed').querySelectorAll('.claim').forEach(el => {
      el.onclick = () => openClaim(el.dataset.id);
    });
  }
  
  async function openClaim(id) {
    try {
      const r = await fetch('api/claims/' + encodeURIComponent(id));
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const data = await r.json();
      render(normalise([data])[0] || data);
    } catch (e) {
      alert('Could not load claim ' + id);
    }
  }
  
  /* ---------- claim rendering ---------- */
  function render(p) {
    if (!p || !p.claim_id) return;
    activeId = p.claim_id;
    show('viewClaim');
  
    const status = p.status || 'unknown';
    const stopped = p.stopped_at || null;
  
    /* verdict band */
    const band = $('verdict');
    band.className = 'verdict ' + (
      status === 'analysed' ? 'is-ok' : status === 'rejected' ? 'is-rejected' : 'is-error'
    );
    $('verdictId').textContent = p.claim_id;
  
    let title = 'Analysed \u2014 estimate ready', sub = '';
    if (status === 'rejected' && stopped === 'duplicate') {
      title = 'Rejected \u2014 duplicate photo detected';
      sub = 'This image was already submitted with an earlier claim.';
    } else if (status === 'rejected' && stopped === 'not_a_vehicle') {
      title = 'Rejected \u2014 image is not a vehicle';
      sub = p.vehicle_description || 'The uploaded image does not show a motor vehicle.';
    } else if (status === 'error') {
      title = 'Analysis unavailable \u2014 escalated to human review';
      sub = 'Automated assessment could not complete. A human assessor will review this claim.';
    } else if (p.estimate && p.estimate.requires_survey) {
      sub = 'Estimate exceeds the automated settlement threshold.';
    }
    $('verdictTitle').textContent = title;
    $('verdictSub').textContent = sub;
  
    renderBars(p);
    renderEvidence(p);
    renderForensics(p);
    renderFlags(p);
    renderFields(p);
    renderDamage(p);
    renderEstimate(p);
    pollClaims();
  }
  
  function renderBars(p) {
    const timings = Array.isArray(p.timings) ? p.timings : [];
    const map = {};
    timings.forEach(t => { if (t && t.stage) map[t.stage] = Number(t.seconds) || 0; });
    const max = Math.max(0.01, ...Object.values(map));
  
    $('bars').innerHTML = STAGES.map(s => {
      if (!(s.key in map)) {
        const skipped = p.stopped_at === 'duplicate' && s.key === 'stage1_gemini';
        return `<div class="bar-row is-skipped">
          <span class="bar-name">${s.label}</span>
          <div class="bar-skip">${skipped ? 'not required' : 'not reached'}</div>
          <span class="bar-time">\u2014</span></div>`;
      }
      const sec = map[s.key];
      const pct = Math.max(2, (sec / max) * 100);
      const slim = pct < 12 ? ' slim' : '';
      return `<div class="bar-row">
        <span class="bar-name">${s.label}</span>
        <div class="bar-track"><div class="bar-fill${slim}" style="width:${pct}%"></div></div>
        <span class="bar-time">${sec.toFixed(2)}s</span></div>`;
    }).join('');
  
    const out = $('pipelineCallout');
    if (p.stopped_at === 'duplicate') {
      const s = (map['stage0_duplicate'] || 0).toFixed(2);
      out.textContent = `Rejected in ${s}s \u2014 no AI analysis performed`;
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
      box.innerHTML = '<p class="empty">No source images on this claim.</p>';
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
      return `<div class="row">
        <div class="row-top"><span class="row-title">${esc(human(f.check))}</span>${verdictPill(f.verdict)}</div>
        <p class="row-text">${esc(f.detail || '')}</p>${geo}</div>`;
    }).join('');
  }
  
  function renderFlags(p) {
    const flags = Array.isArray(p.flags) ? p.flags : [];
    if (!flags.length) {
      $('flags').innerHTML = '<p class="empty">No inconsistencies detected.</p>';
      return;
    }
    $('flags').innerHTML = flags.map(f => {
      const ev = Array.isArray(f.evidence) ? f.evidence : [];
      const chips = ev.length
        ? `<div class="chips">${ev.map(e => `<span class="chip">${esc(human(e))}</span>`).join('')}</div>`
        : '';
      return `<div class="row">
        <div class="row-top"><span class="row-title">${esc(human(f.rule))}</span>${sevPill(f.severity)}</div>
        <p class="row-text">${esc(f.message || '')}</p>${chips}</div>`;
    }).join('');
  }
  
  function renderFields(p) {
    const fields = Array.isArray(p.fields) ? p.fields : [];
    if (!fields.length) {
      $('fields').innerHTML = '<p class="empty">No documents were submitted with this claim.</p>';
      return;
    }
    $('fields').innerHTML = fields.map(f => {
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
    const dmg = Array.isArray(p.damage) ? p.damage : [];
    if (!dmg.length) {
      $('damage').innerHTML = '<p class="empty">No visible damage was detected.</p>';
      return;
    }
    const lines = (p.estimate && Array.isArray(p.estimate.line_items)) ? p.estimate.line_items : [];
    $('damage').innerHTML = dmg.map(d => {
      const li = lines.find(l => l && l.part === d.part);
      const price = li ? `<span class="row-price">${rupee(li.low)} \u2013 ${rupee(li.high)}</span>` : '';
      return `<div class="row">
        <div class="row-top"><span class="row-title">${esc(human(d.part))}</span>${sevPill(d.severity)}</div>
        <p class="row-text">${esc(human(d.damage_type))} \u00B7 confidence ${esc(confText(d.confidence))}</p>
        <p class="row-text">${esc(d.reasoning || '')}</p>${price}</div>`;
    }).join('');
  }
  
  function renderEstimate(p) {
    const e = p.estimate || {};
    const lines = Array.isArray(e.line_items) ? e.line_items : [];
  
    if (!lines.length && !e.total_high) {
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
  
    $('estimate').innerHTML = `
      <div class="est-total">${rupee(e.total_low)} \u2013 ${rupee(e.total_high)}</div>
      <p class="est-caption">Estimated repair cost range</p>
      <div class="est-lines">${body}</div>${unpriced}${survey}`;
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
        return `<li class="${cls}"><b>0${n + 1}</b>${s.label}</li>`;
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
      const r = await fetch('api/analyze', { method: 'POST', body: fd });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const p = await r.json();
      clearInterval(stageTimer);
      resetStaged();
      render(p);
    } catch (e) {
      clearInterval(stageTimer);
      show('viewUpload');
      $('uploadNote').textContent = 'Analysis failed: ' + e.message + '. Check the server and try again.';
    }
  }
  
  /* ---------- boot ---------- */
  document.querySelectorAll('.drop').forEach(wireDrop);
  $('btnAnalyse').onclick = analyse;
  $('btnNew').onclick = () => { activeId = null; resetStaged(); show('viewUpload'); pollClaims(); };
  $('btnReset').onclick = async () => {
    if (!confirm('Clear all claims and stored photo hashes?')) return;
    try {
      await fetch('api/demo/reset', { method: 'POST' });
      activeId = null;
      resetStaged();
      show('viewUpload');
      pollClaims();
    } catch (e) {
      alert('Reset failed.');
    }
  };
  
  health();
  setInterval(health, 15000);
  
  /* Open on the newest claim if one exists. Only the first poll auto-opens —
     the interval below must never steal focus from a claim being read. */
  (async () => {
    const claims = await pollClaims();
    if (claims.length) openClaim(claims[0].claim_id);
    setInterval(pollClaims, 3000);
  })();