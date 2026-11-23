(() => {
  const API = (window.VOICE_API_BASE || '').replace(/\/$/, '');
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const state = { projects: [], current: null, pollTimer: null, recorder: null, recordChunks: [], recordStarted: 0, recordTicker: null, undo: [], redo: [], timelinePeaks: [], timelineRanges: [], timelineActive: false, timelineSplits: [], timelineSelection: null, timelineDragging: false, timelineUndo: [], timelineRedo: [], originalObjectUrl: null, finalObjectUrl: null };

  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    let response = await fetch(`${API}${path}`, {...options, headers, credentials:'include'});
    if (response.status === 401 && !path.endsWith('/health') && path !== '/api/auth') {
      const entered = window.prompt('This AI Voice Editing Studio is protected. Enter the access token supplied by its administrator:');
      if (entered) {
        const authResponse = await fetch(`${API}/api/auth`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({token:entered.trim()}), credentials:'include'});
        if (authResponse.ok) {
          response = await fetch(`${API}${path}`, {...options, headers, credentials:'include'});
        } else response = authResponse;
      }
    }
    const type = response.headers.get('content-type') || '';
    const body = type.includes('application/json') ? await response.json() : await response.blob();
    if (!response.ok) throw new Error(body?.detail || body?.error || `Request failed (${response.status})`);
    return body;
  }
  function uploadWithProgress(path, form, onProgress) {
    return new Promise((resolve, reject) => {
      const send = () => {
        const xhr = new XMLHttpRequest();
        xhr.open('POST', `${API}${path}`);
        xhr.withCredentials = true;
        xhr.responseType = 'json';
        xhr.upload.onprogress = event => { if (event.lengthComputable) onProgress(Math.round(event.loaded / event.total * 100)); };
        xhr.onerror = () => reject(new Error('Upload connection failed. Check your network and retry.'));
        xhr.onload = async () => {
          if (xhr.status === 401) {
            const entered = window.prompt('This studio is protected. Enter the access token supplied by its administrator:');
            if (entered) {
              try {
                const auth = await fetch(`${API}/api/auth`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:entered.trim()}),credentials:'include'});
                if (!auth.ok) return reject(new Error('Invalid access token.'));
                return send();
              } catch (error) { return reject(error); }
            } else return reject(new Error('Upload was cancelled.'));
          }
          const data = xhr.response || {};
          if (xhr.status >= 200 && xhr.status < 300) resolve(data);
          else reject(new Error(data.detail || `Upload failed (${xhr.status}).`));
        };
        xhr.send(form);
      };
      send();
    });
  }
  function toast(message, error = false) {
    const el = $('#toast'); el.textContent = message; el.style.background = error ? '#49252a' : ''; el.style.borderColor = error ? '#a34d54' : '';
    el.classList.add('show'); clearTimeout(toast.timer); toast.timer = setTimeout(() => el.classList.remove('show'), 3200);
  }
  function formatTime(seconds) {
    if (!Number.isFinite(Number(seconds))) return '—';
    const s = Math.max(0, Math.floor(Number(seconds)));
    return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
  }
  function formatSize(bytes) {
    if (!Number.isFinite(Number(bytes))) return '—';
    const mb = Number(bytes) / 1024 / 1024;
    return mb >= 1024 ? `${(mb / 1024).toFixed(2)} GB` : `${mb.toFixed(1)} MB`;
  }
  function escapeHTML(value = '') {
    return String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }
  function showView(name) {
    $$('.view').forEach(v => v.classList.toggle('active', v.id === `view-${name}`));
    $$('.nav-item').forEach(b => b.classList.toggle('active', b.dataset.view === name));
    $('#crumb-current').textContent = ({home:'Overview',projects:'My projects',editor:'Audio editor',reports:'Analysis reports',settings:'Settings',about:'About Studio'})[name] || name;
    $('#sidebar').classList.remove('open');
    if (name === 'projects') loadProjects();
    if (name === 'reports') renderReportHistory();
  }
  function openDialog() {
    $('#new-project-name').value = 'New voiceover project';
    $('#project-dialog').showModal();
    $('#new-project-name').focus();
  }
  async function refreshProjects() {
    state.projects = await api('/api/projects');
    $('#stat-projects').textContent = state.projects.length;
    $('#stat-analyzed').textContent = state.projects.filter(p => ['review','done'].includes(p.status)).length;
    $('#stat-activity').textContent = state.projects.length ? 'In progress' : 'Ready';
    renderProjectCards();
  }
  function renderProjectCards() {
    const container = $('#recent-projects');
    const items = state.projects.slice(0, 3);
    if (!items.length) {
      container.innerHTML = `<div class="empty-card"><div class="empty-wave">〰</div><strong>Your workspace is clear</strong><p>Create a project to start matching your voice recording to a script.</p><button class="text-button" id="empty-create">Create your first project →</button></div>`;
      $('#empty-create')?.addEventListener('click', openDialog);
    } else container.innerHTML = items.map(projectCard).join('');
    $('#all-projects').innerHTML = state.projects.length ? state.projects.map(projectCard).join('') : '<div class="empty-card"><strong>No projects yet</strong><p>Your saved projects will appear here.</p></div>';
    $$('.project-card').forEach(card => card.addEventListener('click', () => openProject(card.dataset.id)));
  }
  function projectCard(p) {
    const label = p.status === 'done' ? 'EXPORTED' : p.status === 'review' ? 'ANALYZED' : p.status === 'analyzing' || p.status === 'queued' ? 'PROCESSING' : p.status === 'error' ? 'ERROR' : 'DRAFT';
    return `<button class="project-card" data-id="${escapeHTML(p.id)}"><div class="project-card-top"><span class="project-card-icon">♫</span><span class="status-pill">${label}</span></div><strong>${escapeHTML(p.name)}</strong><small>${p.audio_duration ? formatTime(p.audio_duration) + ' recording · ' : ''}${new Date(p.updated_at || p.created_at).toLocaleDateString()}</small></button>`;
  }
  async function createProject(name) {
    const project = await api('/api/projects', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name})});
    await refreshProjects();
    await openProject(project.id);
    toast('Project created. Add your script and recording.');
  }
  async function loadProjects() { try { await refreshProjects(); } catch (e) { toast(e.message, true); } }
  async function openProject(id) {
    try {
      stopPolling();
      const project = await api(`/api/projects/${id}`);
      state.current = project; state.undo = []; state.redo = []; state.timelineRanges = project.timeline_ranges || []; state.timelineActive = Array.isArray(project.timeline_ranges); state.timelineSplits = project.timeline_splits || []; state.timelineSelection = null; state.timelineUndo = []; state.timelineRedo = [];
      $('#editor-title').textContent = project.name;
      $('#editor-subtitle').textContent = project.audio_original_name ? `${project.audio_original_name} · ${project.script_text?.length || 0} script characters` : 'Add your script and recording to begin.';
      $('#script-text').value = project.script_text || '';
      updateScriptCount();
      $('#script-file').value = '';
      if (project.audio_filename) await showAudio(project);
      else { $('#audio-loaded').classList.add('hidden'); $('#audio-drop').classList.remove('hidden'); $('#original-player').removeAttribute('src'); }
      $('#project-status').textContent = String(project.status || 'draft').toUpperCase();
      $('#processing-panel').classList.toggle('hidden', !['queued','analyzing'].includes(project.status));
      $('#report-section').classList.toggle('hidden', !project.analysis);
      $('#final-output').classList.toggle('hidden', !project.last_export);
      $('#analyze-btn').disabled = !(project.script_text?.trim() && project.audio_filename) || ['queued','analyzing'].includes(project.status);
      updateStepper(project.status, !!project.analysis, !!project.last_export);
      if (project.analysis) renderAnalysis(project.analysis);
      if (project.last_export) await showFinalFile(project.last_export);
      if (['queued','analyzing'].includes(project.status)) startPolling();
      showView('editor');
    } catch (e) { toast(e.message, true); }
  }
  function updateScriptCount() {
    const text = $('#script-text').value;
    const sentences = text.split(/(?<=[.!?।؟])\s+|\n+/u).filter(s => s.trim()).length;
    $('#script-count').textContent = `${text.length.toLocaleString()} characters · ${sentences} sentences`;
    $('#analyze-btn').disabled = !(text.trim() && state.current?.audio_filename) || ['queued','analyzing'].includes(state.current?.status);
  }
  async function saveScript(showToast = true) {
    if (!state.current) return;
    const text = $('#script-text').value;
    await api(`/api/projects/${state.current.id}/script`, {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({script_text:text})});
    state.current.script_text = text; state.current.analysis = null; state.current.last_export = null;
    $('#report-section').classList.add('hidden');
    $('#project-status').textContent = state.current.audio_filename ? 'READY' : 'DRAFT';
    if (showToast) toast('Script saved.');
    updateStepper('ready', false, false);
    updateScriptCount();
  }
  async function uploadScript(file) {
    if (!state.current || !file) return;
    const form = new FormData(); form.append('file', file);
    const result = await api(`/api/projects/${state.current.id}/script-file`, {method:'POST',body:form});
    $('#script-text').value = result.script_text;
    state.current.script_text = result.script_text; state.current.analysis = null; state.current.last_export = null;
    $('#report-section').classList.add('hidden'); updateScriptCount(); updateStepper('ready', false, false);
    toast(`Script loaded: ${file.name}`);
  }
  async function uploadAudio(file) {
    if (!state.current || !file) return;
    const form = new FormData(); form.append('file', file);
    $('#upload-progress').classList.remove('hidden');$('#upload-progress-bar').style.width='0%';$('#upload-progress-text').textContent='Uploading 0%';
    const result = await uploadWithProgress(`/api/projects/${state.current.id}/audio`, form, percent => {$('#upload-progress-bar').style.width=`${percent}%`;$('#upload-progress-text').textContent=`Uploading ${percent}%`;});
    state.current.audio_filename = true; state.current.audio_original_name = result.filename; state.current.audio_duration = result.duration; state.current.audio_size = result.size; state.current.analysis = null; state.current.last_export = null;
    const project = await api(`/api/projects/${state.current.id}`); state.current = project;
    state.timelineRanges=[];state.timelineActive=false;state.timelineSplits=[];state.timelineSelection=null;state.timelineUndo=[];state.timelineRedo=[];
    await showAudio(project); $('#upload-progress').classList.add('hidden'); $('#report-section').classList.add('hidden');
    $('#editor-subtitle').textContent = `${result.filename} · ${formatTime(result.duration)} · ${formatSize(result.size)}`;
    updateScriptCount(); updateStepper('ready', false, false);
    toast('Original recording uploaded and preserved.');
  }
  async function showAudio(project) {
    $('#audio-drop').classList.add('hidden'); $('#audio-loaded').classList.remove('hidden');
    $('#audio-name').textContent = project.audio_original_name || project.audio_filename;
    $('#audio-meta').textContent = `${formatTime(project.audio_duration)} · ${formatSize(project.audio_size)}`;
    if (state.originalObjectUrl) { URL.revokeObjectURL(state.originalObjectUrl); state.originalObjectUrl = null; }
    $('#original-player').crossOrigin='use-credentials';$('#working-player').crossOrigin='use-credentials';
    $('#original-player').src = `${API}/api/projects/${project.id}/audio`;
    $('#original-player').load();
    $('#working-player').src = `${API}/api/projects/${project.id}/working-audio`;
    $('#working-player').load();
    drawWaveform();
  }
  async function drawWaveform() {
    const canvas = $('#waveform'); const ctx = canvas.getContext('2d');
    const dpr = window.devicePixelRatio || 1; const rect = canvas.getBoundingClientRect();
    canvas.width = Math.max(1, Math.floor(rect.width*dpr)); canvas.height = Math.max(1, Math.floor(rect.height*dpr));
    ctx.setTransform(dpr,0,0,dpr,0,0); const w = rect.width, h = rect.height; ctx.clearRect(0,0,w,h);
    try {
      const data = await api(`/api/projects/${state.current.id}/waveform?points=${Math.max(400,Math.floor(w*2))}`);
      state.timelinePeaks = data.peaks || []; renderWaveform();
    } catch (e) { ctx.strokeStyle='#3d9b92';ctx.globalAlpha=.8; for(let i=0;i<w;i+=5){const a=7+Math.abs(Math.sin(i*.11)*18);ctx.beginPath();ctx.moveTo(i,(h-a)/2);ctx.lineTo(i,(h+a)/2);ctx.stroke();}ctx.globalAlpha=1; }
  }
  function renderWaveform(){
    const canvas=$('#waveform'),ctx=canvas.getContext('2d'),w=canvas.clientWidth,h=canvas.clientHeight;
    const dpr=window.devicePixelRatio||1;ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,w,h);
    const peaks=state.timelinePeaks||[],duration=Number(state.current?.audio_duration)||0;if(!peaks.length||!duration)return;
    const bw=w/peaks.length;
    peaks.forEach((peak,i)=>{const t=(i/peaks.length)*duration;const removed=state.timelineActive&&!state.timelineRanges.some(r=>t>=r.start&&t<=r.end);const bar=Math.max(2,peak*h*.82);ctx.fillStyle=removed?'#5a6572':(i%5===0?'#6de0bd':'#3d9b92');ctx.globalAlpha=removed?.24:.78;ctx.fillRect(i*bw,(h-bar)/2,Math.max(1,bw-1),bar);});
    ctx.globalAlpha=1;
    for(const t of state.timelineSplits){const x=t/duration*w;ctx.strokeStyle='#f2bd64';ctx.setLineDash([3,3]);ctx.beginPath();ctx.moveTo(x,0);ctx.lineTo(x,h);ctx.stroke();ctx.setLineDash([]);}
    if(state.timelineSelection){const a=state.timelineSelection.start/duration*w,b=state.timelineSelection.end/duration*w;ctx.fillStyle='rgba(105,190,255,.18)';ctx.fillRect(a,0,Math.max(1,b-a),h);ctx.strokeStyle='#72b8f2';ctx.strokeRect(a+.5,.5,Math.max(1,b-a-1),h-1);}
  }
  function canvasTime(clientX){const r=$('#waveform').getBoundingClientRect();return Math.max(0,Math.min(Number(state.current?.audio_duration)||0,(clientX-r.left)/r.width*(Number(state.current?.audio_duration)||0)));}
  function updateTimelineLabel(){const s=state.timelineSelection;$('#timeline-selection').textContent=s?`Selection ${s.start.toFixed(2)}s – ${s.end.toFixed(2)}s · ${Math.max(0,s.end-s.start).toFixed(2)}s`:'Drag across the waveform to select audio';renderWaveform();}
  async function commitTimeline(nextRanges,nextSplits=state.timelineSplits){
    const old={ranges:structuredClone(state.timelineRanges),splits:[...state.timelineSplits],active:state.timelineActive};state.timelineUndo.push(old);state.timelineRedo=[];
    const result=await api(`/api/projects/${state.current.id}/timeline`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({ranges:nextRanges,splits:nextSplits})});
    state.timelineRanges=result.ranges;state.timelineActive=true;state.timelineSplits=nextSplits;state.current.timeline_ranges=result.ranges;state.current.timeline_splits=nextSplits;state.current.last_export=null;$('#final-output').classList.add('hidden');renderWaveform();toast('Timeline edit saved. Original audio remains unchanged.');
  }
  async function undoTimeline(){const prev=state.timelineUndo.pop();if(!prev)return toast('No timeline edit to undo.');state.timelineRedo.push({ranges:structuredClone(state.timelineRanges),splits:[...state.timelineSplits],active:state.timelineActive});await commitTimelineNoHistory(prev.ranges,prev.splits,prev.active);}
  async function redoTimeline(){const next=state.timelineRedo.pop();if(!next)return toast('No timeline edit to redo.');state.timelineUndo.push({ranges:structuredClone(state.timelineRanges),splits:[...state.timelineSplits],active:state.timelineActive});await commitTimelineNoHistory(next.ranges,next.splits,next.active);}
  async function commitTimelineNoHistory(ranges,splits,active){
    if(active){const result=await api(`/api/projects/${state.current.id}/timeline`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({ranges,splits})});state.timelineRanges=result.ranges;state.timelineActive=true;state.timelineSplits=splits;state.current.timeline_ranges=result.ranges;state.current.timeline_splits=splits;}
    else{await api(`/api/projects/${state.current.id}/timeline`,{method:'DELETE'});state.timelineRanges=[];state.timelineActive=false;state.timelineSplits=[];delete state.current.timeline_ranges;delete state.current.timeline_splits;}
    state.current.last_export=null;renderWaveform();
  }

  function updateStepper(status, analyzed, exported) {
    const n = exported ? 4 : analyzed ? 3 : ['queued','analyzing'].includes(status) ? 2 : 1;
    $$('.stepper .step').forEach((step,i)=>{step.classList.toggle('active',i+1===n);step.classList.toggle('done',i+1<n);});
  }
  async function startAnalysis() {
    if (!state.current) return;
    try { await saveScript(false); } catch(e) { toast(e.message,true); return; }
    try {
      const queued = await api(`/api/projects/${state.current.id}/analyze`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({language:$('#speech-language').value||'auto'})});
      state.current.job_id=queued.job_id;state.current.status='queued'; $('#project-status').textContent='QUEUED'; $('#processing-panel').classList.remove('hidden'); $('#processing-state').textContent='QUEUED'; $('#processing-title').textContent='Analysis queued'; $('#processing-copy').textContent='Preparing multilingual transcription and word-level timestamps.'; $('#report-section').classList.add('hidden'); $('#analyze-btn').disabled=true; updateStepper('queued',false,false); startPolling();
    } catch(e) { toast(e.message,true); }
  }
  function startPolling() {
    stopPolling(); let tries=0;
    state.pollTimer=setInterval(async()=>{
      if(!state.current){stopPolling();return;}
      try{
        if(state.current.job_id){const job=await api(`/api/jobs/${state.current.job_id}`);$('#processing-state').textContent=String(job.status||'').toUpperCase();$('#processing-title').textContent=job.stage||'Processing audio';$('#processing-copy').textContent=`${job.progress||0}% · ${job.status==='running'?'Working through the recording':'Waiting for a worker'}`;}
        const result=await api(`/api/projects/${state.current.id}/analysis`);
        if(!state.current.job_id) $('#processing-state').textContent=String(result.status||'').toUpperCase();
        if(result.status==='queued'||result.status==='analyzing'){
          $('#processing-title').textContent=result.status==='queued'?'Analysis queued':'Listening and aligning words';
          $('#processing-copy').textContent='This runs on the audio server. Large recordings may take several minutes.';
          tries++; if(tries%8===0) await refreshProjects();
        } else if(result.status==='review'&&result.analysis){
          stopPolling(); state.current.analysis=result.analysis;state.current.status='review';$('#processing-panel').classList.add('hidden');$('#project-status').textContent='REVIEW';$('#analyze-btn').disabled=false;$('#report-section').classList.remove('hidden');renderAnalysis(result.analysis);updateStepper('review',true,false);await refreshProjects();$('#report-section').scrollIntoView({behavior:'smooth',block:'start'});toast('Analysis completed. Review the proposed edits.');
        } else if(result.status==='error'){
          stopPolling();$('#processing-title').textContent='Analysis could not complete';$('#processing-copy').textContent=result.error||'Check the audio engine and server logs.';$('#processing-spinner')?.classList.add('hidden');$('#processing-state').textContent='ERROR';$('#analyze-btn').disabled=false;toast(result.error||'Analysis failed.',true);await refreshProjects();
        }
      }catch(e){stopPolling();$('#processing-copy').textContent=e.message;$('#processing-state').textContent='CONNECTION ERROR';$('#analyze-btn').disabled=false;toast(e.message,true);}
    },2000);
  }
  function stopPolling(){if(state.pollTimer){clearInterval(state.pollTimer);state.pollTimer=null;}}
  function renderAnalysis(report) {
    $('#match-score').textContent=`${report.matching_percentage ?? 0}%`;
    const stats=[['SCRIPT LINES',report.script_sentence_count||0],['MATCHED',report.matched_sentences||0],['REVIEW',report.review_sentences||0],['MISSING',report.missing_sentences||0],['BEST TAKES',report.best_takes_auto_selected||0],['STUTTERS',report.possible_stutters||0],['LANGUAGE',report.detected_language||'auto'],['REMOVED AUDIO',formatTime(report.timeline_removed_seconds||0)],['EST. CLEAN LENGTH',formatTime(report.estimated_final_duration)]];
    $('#report-stats').innerHTML=stats.map(([label,value])=>`<div class="report-stat"><strong>${value}</strong><small>${label}</small></div>`).join('');
    $('#sentence-list').innerHTML=(report.sentences||[]).map(row=>{
      const time=row.start==null?'No match':`${formatTime(row.start)}–${formatTime(row.end)}`;
      const tag=row.status==='matched'?'Matched':row.status==='review'?'Review required':'Missing';
      return `<div class="sentence-row ${row.status}"><input type="checkbox" data-index="${row.index}" ${row.include?'checked':''} ${row.start==null?'disabled':''} aria-label="Include sentence ${row.index+1}"/><div class="sentence-body"><div class="sentence-script">${escapeHTML(row.script)}</div><div class="sentence-recognized">Heard: ${escapeHTML(row.recognized||'No confident speech match')}</div><div class="sentence-meta"><span class="sentence-tag">${tag}</span><span>${Math.round((row.coverage||0)*100)}% words</span><span>${Math.round((row.confidence||0)*100)}% confidence</span></div>${(row.take_candidates||[]).length>1?`<div class="take-options">${row.take_candidates.map((take,i)=>`<button class="take-option ${row.selected_take&&Math.abs(row.selected_take.start-take.start)<.01?'active':''}" data-take-index="${row.index}" data-take-start="${take.start}" data-take-end="${take.end}">Take ${i+1} · ${Math.round(take.confidence*100)}%${take.complete?' · complete':' · partial'}</button>`).join('')}</div>`:''}</div><button class="sentence-time" data-play-start="${row.start ?? ''}" data-play-end="${row.end ?? ''}" ${row.start==null?'disabled':''}>▶ ${time}</button></div>`;
    }).join('')||'<div class="no-issues">No script sentences were detected. Check the script text.</div>';
    $$('#sentence-list input[type=checkbox]').forEach(input=>input.addEventListener('change',()=>changeSentence(Number(input.dataset.index),input.checked)));
    $$('#sentence-list [data-take-index]').forEach(button=>button.addEventListener('click',()=>changeSentence(Number(button.dataset.takeIndex),true,{start:Number(button.dataset.takeStart),end:Number(button.dataset.takeEnd)})));
    $$('#sentence-list [data-play-start]').forEach(button=>button.addEventListener('click',()=>playOriginalAt(Number(button.dataset.playStart),Number(button.dataset.playEnd))));
    const quality = report.audio_quality || {};
    if (quality.mean_volume_db !== undefined && quality.mean_volume_db !== null) {
      const qualityRows = [
        `Mean level: ${quality.mean_volume_db.toFixed(1)} dB`,
        `Peak level: ${quality.peak_volume_db == null ? 'Unavailable' : `${quality.peak_volume_db.toFixed(1)} dB`}`,
        `Near-clipping samples: ${quality.near_clipping_samples ?? 0}`
      ];
      $('#issue-list').dataset.quality = qualityRows.join(' · ');
    }
    const issues=report.issues||[];
    const issueMarkup = issues.length ? issues.map(issue => `<div class="issue-item"><div class="issue-item-head"><span>${escapeHTML((issue.category||'issue').replaceAll('_',' ').toUpperCase())}</span><button class="issue-play" data-play-start="${issue.start}" data-play-end="${issue.end}">▶ ${formatTime(issue.start)}–${formatTime(issue.end)}</button></div><p>${escapeHTML(issue.text||'Audio issue detected')}</p><small>${issue.duration?`${issue.duration.toFixed(1)} seconds · `:''}Review before editing</small></div>`).join('') : '<div class="no-issues">No unmatched words or long pauses were flagged.</div>';
    const qualityMarkup = $('#issue-list').dataset.quality ? `<div class="issue-item"><div class="issue-item-head"><span>AUDIO LEVEL ANALYSIS</span></div><p>${escapeHTML($('#issue-list').dataset.quality)}</p><small>Objective signal measurement, not a speech intelligibility score</small></div>` : '';
    $('#issue-list').innerHTML = issueMarkup + qualityMarkup;
    $$('#issue-list [data-play-start]').forEach(button=>button.addEventListener('click',()=>playOriginalAt(Number(button.dataset.playStart),Number(button.dataset.playEnd))));
    updateSelectedCount();
    if(report.detected_language) $('#editor-subtitle').textContent=`Detected speech language: ${report.detected_language.toUpperCase()} · ${report.script_sentence_count} script sentences`;
  }
  function updateSelectedCount(){const count=$$('#sentence-list input[type=checkbox]:checked').length;$('#selected-count').textContent=`${count} selected`;}
  function playOriginalAt(seconds, end){const player=$('#working-player');if(!player.src||!Number.isFinite(seconds))return;player.dataset.stopAt=Number.isFinite(end)?String(end):'';player.currentTime=Math.max(0,seconds);player.play().catch(()=>toast('Tap the original audio player to start playback.',true));}
  async function changeSentence(index, include, take=null) {
    const row=state.current?.analysis?.sentences?.[index]; if(!row)return;
    state.undo.push({index,include:row.include});state.redo=[];
    try{
      await api(`/api/projects/${state.current.id}/sentences/${index}`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({include,...(take?{take_start:take.start,take_end:take.end}:{})})});
      row.include=include;row.manual_decision=true;if(take){const c=(row.take_candidates||[]).find(x=>Math.abs(x.start-take.start)<.01&&Math.abs(x.end-take.end)<.01);if(c){row.start=c.start;row.end=c.end;row.audio_ranges=[{start:c.start,end:c.end}];row.recognized=c.recognized;row.confidence=c.confidence;row.coverage=c.coverage;row.selected_take=c;}}renderAnalysis(state.current.analysis);updateSelectedCount();
    }catch(e){toast(e.message,true);renderAnalysis(state.current.analysis);}
  }
  async function undoDecision(){const item=state.undo.pop();if(!item)return toast('Nothing to undo.');const row=state.current.analysis.sentences[item.index];state.redo.push({index:item.index,include:row.include});try{await api(`/api/projects/${state.current.id}/sentences/${item.index}`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({include:item.include})});row.include=item.include;renderAnalysis(state.current.analysis);toast('Last sentence decision undone.');}catch(e){toast(e.message,true);}}
  async function redoDecision(){const item=state.redo.pop();if(!item)return toast('Nothing to redo.');const row=state.current.analysis.sentences[item.index];state.undo.push({index:item.index,include:row.include});try{await api(`/api/projects/${state.current.id}/sentences/${item.index}`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({include:item.include})});row.include=item.include;renderAnalysis(state.current.analysis);toast('Sentence decision restored.');}catch(e){toast(e.message,true);}}
  async function buildExport(){
    if(!state.current)return;
    const payload={format:$('#export-format').value,bitrate:$('#export-bitrate').value,noise_reduction:$('#noise-reduction').value,pause_mode:$('#pause-mode').value,normalize:$('#normalize-audio').checked,filename:$('#output-name').value.trim()||'edited-voiceover',sample_rate:$('#sample-rate').value,channels:Number($('#audio-channels').value)};
    const btn=$('#export-btn');btn.disabled=true;btn.textContent='Building audio…';
    try{
      const result=await api(`/api/projects/${state.current.id}/export`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      state.current.last_export=result;await showFinalFile(result);$('#final-output').classList.remove('hidden');$('#project-status').textContent='EXPORTED';updateStepper('done',true,true);await refreshProjects();$('#final-output').scrollIntoView({behavior:'smooth',block:'center'});toast('Final audio generated. Original recording remains unchanged.');
    }catch(e){toast(e.message,true);}finally{btn.disabled=false;btn.innerHTML='Build final audio <span>→</span>';}
  }
  async function showFinalFile(item){
    if(!state.current)return;
    $('#final-name').textContent=item.filename;
    $('#final-meta').textContent=`${formatTime(item.duration)} · ${formatSize(item.size)} · ${item.selected_sentences||0} sentences`;
    const downloadBlob = await api(`/api/projects/${state.current.id}/export/download`);
    if (state.finalObjectUrl) URL.revokeObjectURL(state.finalObjectUrl);
    state.finalObjectUrl = URL.createObjectURL(downloadBlob);
    $('#download-final').href=state.finalObjectUrl;$('#download-final').download=item.filename;
    $('#final-player').src=state.finalObjectUrl;$('#final-output').classList.remove('hidden');
  }
  async function deleteCurrentProject(){if(!state.current)return;if(!confirm(`Delete “${state.current.name}” and all of its local files? This cannot be undone.`))return;try{await api(`/api/projects/${state.current.id}`,{method:'DELETE'});state.current=null;await refreshProjects();showView('projects');toast('Project deleted.');}catch(e){toast(e.message,true);}}
  async function renderReportHistory(){try{await refreshProjects();const rows=state.projects.filter(p=>['review','done'].includes(p.status));$('#report-history').innerHTML=rows.length?rows.map(projectCard).join(''):'<div class="empty-card"><strong>No analysis reports yet</strong><p>Run script and audio analysis in a project to create a report.</p></div>';$$('#report-history .project-card').forEach(card=>card.addEventListener('click',()=>openProject(card.dataset.id)));}catch(e){toast(e.message,true);}}
  async function checkHealth(){try{const result=await api('/api/health');if(result.engine_installed&&result.ffmpeg_available){$('#system-status-text').textContent='Audio engine connected';$('.system-status').classList.add('online');}else{$('#system-status-text').textContent='Backend ready · AI engine setup needed';$('.system-status').classList.remove('online');}}catch{$('#system-status-text').textContent='Backend not connected';$('.system-status').classList.remove('online');}}
  async function startRecording(){
    if(!navigator.mediaDevices?.getUserMedia||!window.MediaRecorder){toast('Recording is not supported in this browser.',true);return;}
    try{
      const stream=await navigator.mediaDevices.getUserMedia({audio:true});
      const type=['audio/webm;codecs=opus','audio/webm','audio/mp4'].find(t=>MediaRecorder.isTypeSupported(t))||'';
      state.recorder=new MediaRecorder(stream,type?{mimeType:type}:undefined);state.recordChunks=[];
      state.recorder.ondataavailable=e=>{if(e.data.size)state.recordChunks.push(e.data);};
      state.recorder.onstop=async()=>{stream.getTracks().forEach(t=>t.stop());clearInterval(state.recordTicker);$('#record-time').classList.add('hidden');const blob=new Blob(state.recordChunks,{type:state.recorder.mimeType||'audio/webm'});const ext=blob.type.includes('mp4')?'m4a':'webm';const file=new File([blob],`studio-recording-${Date.now()}.${ext}`,{type:blob.type});try{await uploadAudio(file);}catch(e){$('#upload-progress').classList.add('hidden');toast(e.message,true);}};
      state.recorder.start(800);state.recordStarted=Date.now();$('#record-toggle').classList.add('recording');$('#record-toggle').innerHTML='<i></i> Stop recording';$('#record-time').classList.remove('hidden');state.recordTicker=setInterval(()=>$('#record-time').textContent=formatTime((Date.now()-state.recordStarted)/1000),500);toast('Recording started. Keep this tab open.');
    }catch(e){toast(`Microphone access failed: ${e.message}`,true);}
  }
  function stopRecording(){if(state.recorder&&state.recorder.state!=='inactive'){state.recorder.stop();$('#record-toggle').classList.remove('recording');$('#record-toggle').innerHTML='<i></i> Start recording';}}

  // Navigation and primary actions
  $$('.nav-item').forEach(btn=>btn.addEventListener('click',()=>showView(btn.dataset.view)));
  $$('[data-view]').filter(el=>!el.classList.contains('nav-item')).forEach(btn=>btn.addEventListener('click',()=>showView(btn.dataset.view)));
  $('#mobile-menu').addEventListener('click',()=>$('#sidebar').classList.toggle('open'));
  ['#create-project','#create-project-two'].forEach(sel=>$(sel).addEventListener('click',openDialog));
  $('#open-recent').addEventListener('click',()=>state.projects[0]?openProject(state.projects[0].id):openDialog());
  $('#project-form').addEventListener('submit',async e=>{e.preventDefault();const name=$('#new-project-name').value.trim();if(!name)return;$('#project-dialog').close();try{await createProject(name);}catch(err){toast(err.message,true);}});
  $('#script-text').addEventListener('input',updateScriptCount);
  $('#save-script').addEventListener('click',()=>saveScript().catch(e=>toast(e.message,true)));
  $('#clear-script').addEventListener('click',async()=>{if(!state.current)return;if(confirm('Clear the saved script from this project?')){try{$('#script-text').value='';await saveScript();toast('Saved script cleared.');}catch(e){toast(e.message,true);}}});
  $('#script-file').addEventListener('change',e=>uploadScript(e.target.files[0]).catch(err=>toast(err.message,true)));
  $('#audio-file').addEventListener('change',e=>uploadAudio(e.target.files[0]).catch(err=>{ $('#upload-progress').classList.add('hidden');toast(err.message,true); }).finally(()=>e.target.value=''));
  $('#replace-audio').addEventListener('click',()=>$('#audio-file').click());
  $('#remove-audio').addEventListener('click',async()=>{if(!state.current||!confirm('Remove this recording from the active project? The original file will be kept in the project archive.'))return;try{await api(`/api/projects/${state.current.id}/audio`,{method:'DELETE'});state.current.audio_filename=null;state.current.analysis=null;state.current.last_export=null;state.timelineRanges=[];state.timelineActive=false;state.timelineSplits=[];state.timelineSelection=null;state.timelineUndo=[];state.timelineRedo=[];$('#audio-loaded').classList.add('hidden');$('#audio-drop').classList.remove('hidden');$('#original-player').removeAttribute('src');$('#working-player').removeAttribute('src');$('#report-section').classList.add('hidden');$('#project-status').textContent='DRAFT';updateScriptCount();toast('Recording detached. Original source is archived.');}catch(e){toast(e.message,true);}});
  $('#audio-drop').addEventListener('dragover',e=>{e.preventDefault();$('#audio-drop').style.borderColor='var(--mint)';});
  $('#audio-drop').addEventListener('dragleave',()=>$('#audio-drop').style.borderColor='');
  $('#audio-drop').addEventListener('drop',e=>{e.preventDefault();$('#audio-drop').style.borderColor='';const file=e.dataTransfer.files[0];if(file)uploadAudio(file).catch(err=>{$('#upload-progress').classList.add('hidden');toast(err.message,true);});});
  $('#analyze-btn').addEventListener('click',startAnalysis);
  $('#waveform').addEventListener('pointerdown',e=>{if(!state.current?.audio_duration)return;state.timelineDragging=true;const t=canvasTime(e.clientX);state.timelineSelection={start:t,end:t};$('#waveform').setPointerCapture?.(e.pointerId);updateTimelineLabel();});
  $('#waveform').addEventListener('pointermove',e=>{if(!state.timelineDragging||!state.timelineSelection)return;const t=canvasTime(e.clientX),a=state.timelineSelection.start;state.timelineSelection={start:Math.min(a,t),end:Math.max(a,t)};updateTimelineLabel();});
  $('#waveform').addEventListener('pointerup',e=>{if(!state.timelineDragging)return;state.timelineDragging=false;const t=canvasTime(e.clientX),a=state.timelineSelection?.start??t;state.timelineSelection={start:Math.min(a,t),end:Math.max(a,t)};updateTimelineLabel();});
  $('#trim-selection').addEventListener('click',async()=>{const s=state.timelineSelection;if(!s||s.end-s.start<.08)return toast('Select a longer waveform range first.',true);try{await commitTimeline([{start:s.start,end:s.end}],[]);}catch(e){toast(e.message,true);}});
  $('#delete-selection').addEventListener('click',async()=>{const s=state.timelineSelection;if(!s||s.end-s.start<.02)return toast('Select a waveform range first.',true);const duration=Number(state.current.audio_duration)||0;const base=state.timelineActive?state.timelineRanges:[{start:0,end:duration}];const next=[];for(const r of base){if(s.end<=r.start||s.start>=r.end)next.push(r);else{if(s.start>r.start)next.push({start:r.start,end:s.start});if(s.end<r.end)next.push({start:s.end,end:r.end});}}try{await commitTimeline(next,state.timelineSplits.filter(t=>t<s.start||t>s.end));}catch(e){toast(e.message,true);}});
  $('#split-playhead').addEventListener('click',async()=>{const t=Number($('#working-player').currentTime)||0;if(t<=0||t>=Number(state.current?.audio_duration||0))return toast('Move the playhead inside the recording first.',true);try{await commitTimeline(state.timelineRanges,[...new Set([...state.timelineSplits,t])].sort((a,b)=>a-b));toast(`Split marker added at ${t.toFixed(2)}s.`);}catch(e){toast(e.message,true);}});
  $('#reset-timeline').addEventListener('click',async()=>{if(!state.current)return;try{state.timelineUndo.push({ranges:structuredClone(state.timelineRanges),splits:[...state.timelineSplits],active:state.timelineActive});state.timelineRedo=[];await api(`/api/projects/${state.current.id}/timeline`,{method:'DELETE'});state.timelineRanges=[];state.timelineActive=false;state.timelineSplits=[];delete state.current.timeline_ranges;delete state.current.timeline_splits;state.current.last_export=null;renderWaveform();toast('Timeline restored to original full length.');}catch(e){toast(e.message,true);}});

  $('#timeline-undo').addEventListener('click',undoTimeline);
  $('#timeline-redo').addEventListener('click',redoTimeline);
  async function restoreWorkingAudio(action){try{const result=await api(`/api/projects/${state.current.id}/working-audio/${action}`,{method:'POST'});state.current.working_audio_filename=result.filename;state.current.last_export=null;$('#working-player').src=`${API}/api/projects/${state.current.id}/working-audio?t=${Date.now()}`;$('#working-player').load();state.timelinePeaks=(await api(`/api/projects/${state.current.id}/waveform?points=1800`)).peaks;renderWaveform();$('#final-output').classList.add('hidden');toast(action==='undo'?'Replacement undone.':'Replacement restored.');}catch(e){toast(e.message,true);}}
  $('#working-undo').addEventListener('click',()=>restoreWorkingAudio('undo'));
  $('#working-redo').addEventListener('click',()=>restoreWorkingAudio('redo'));
  $('#replace-selection').addEventListener('click',()=>{const s=state.timelineSelection;if(!s||s.end-s.start<.08)return toast('Select the part of the recording to replace first.',true);$('#replace-segment-file').click();});
  $('#replace-segment-file').addEventListener('change',async e=>{const file=e.target.files?.[0],s=state.timelineSelection;if(!file||!s)return;const form=new FormData();form.append('file',file);form.append('start',String(s.start));form.append('end',String(s.end));form.append('take_start','0');try{const result=await uploadWithProgress(`/api/projects/${state.current.id}/replace-segment`,form,p=>toast(`Replacing segment… ${p}%`));$('#working-player').src=`${API}/api/projects/${state.current.id}/working-audio?t=${Date.now()}`;$('#working-player').load();state.timelinePeaks=(await api(`/api/projects/${state.current.id}/waveform?points=1800`)).peaks;renderWaveform();state.current.working_audio_filename=result.working_audio;state.current.last_export=null;$('#final-output').classList.add('hidden');toast('Selected segment replaced. Original recording is preserved.');}catch(err){toast(err.message,true);}finally{e.target.value='';}});
  $('#export-btn').addEventListener('click',buildExport);
  $('#undo-edit').addEventListener('click',undoDecision);
  $('#redo-edit').addEventListener('click',redoDecision);
  $('#delete-project').addEventListener('click',deleteCurrentProject);
  $('#record-toggle').addEventListener('click',()=>state.recorder&&state.recorder.state==='recording'?stopRecording():startRecording());
  ['#original-player','#working-player'].forEach(sel=>$(sel).addEventListener('timeupdate',()=>{const player=$(sel);const end=Number(player.dataset.stopAt);if(Number.isFinite(end)&&end>0&&player.currentTime>=end){player.pause();delete player.dataset.stopAt;}}));
  window.addEventListener('resize',()=>{if(state.current?.audio_filename)drawWaveform();});
  document.addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='z'){e.preventDefault();undoDecision();}if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='y'){e.preventDefault();redoDecision();}if((e.ctrlKey||e.metaKey)&&e.shiftKey&&e.key.toLowerCase()==='z'){e.preventDefault();undoTimeline();}});

  // expose undo helpers for optional future UI controls
  window.voiceStudioUndo=undoDecision;window.voiceStudioRedo=redoDecision;
  if ('serviceWorker' in navigator) window.addEventListener('load',()=>navigator.serviceWorker.register('/sw.js').catch(()=>{}));
  checkHealth();refreshProjects().catch(e=>toast(e.message,true));
})();
