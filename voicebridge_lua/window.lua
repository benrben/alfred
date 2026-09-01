-- Alfred (Hammerspoon front-end) — the full app window (hs.webview): its
-- embedded HTML/JS, the JS<->Lua message bridge, and the history/settings
-- panels it drives.
-- Split out of voicebridge.lua (see voicebridge.lua's loader comment for the
-- shared-module-scope convention every split file uses).

-- ---- Full app window (hs.webview) ----------------------------------------

WIN_W, WIN_H = 470, 650

-- The engine's resolved history.jsonl path (honours [history].dir via the
-- contract's `resolved.history`); literal fallback for older engines / no contract.
function historyPath()
  local c = VB.contract
  local h = c and type(c.resolved) == "table" and c.resolved.history
  if type(h) == "string" and #h > 0 then return h end
  return HOME .. "/.voicebridge/history/history.jsonl"
end

-- Pure: the oldest index parseHistory should walk back to, given the total
-- line count and the requested window (default 30). Factored out of
-- parseHistory to keep its own branch count low.
function historyStartIndex(n, limit)
  return math.max(1, n - (limit or 30) + 1)
end

-- Pure: does a decoded history record (plus the pcall `ok` flag that produced
-- it) carry real text? Factored out of parseHistory for the same reason.
function historyRecordValid(ok, rec)
  if not ok then return false end
  if type(rec) ~= "table" then return false end
  return rec.text and true or false
end

-- Pure: the history item shape parseHistory returns for one valid record.
-- (chars computed on its own line: a lizard Lua-parser quirk loses track of
-- the function boundary when a `#` length expression is immediately followed
-- by a closing `}` on the same physical line.)
function historyItemFrom(rec)
  local chars = rec.chars or #rec.text
  return { ts = rec.ts or "", chars = chars, text = rec.text }
end

-- Pure: newest-first history items from raw JSONL lines (most recent `limit`),
-- decoded by an injected `decodeFn`; skips lines that don't decode to a record
-- with text. Kept hs-free (readHistory injects hs.json.decode) for testing.
function parseHistory(lines, limit, decodeFn)
  lines = lines or {}
  local n = #lines
  local items = {}
  for i = n, historyStartIndex(n, limit), -1 do
    local ok, rec = pcall(decodeFn, lines[i])
    if historyRecordValid(ok, rec) then
      items[#items + 1] = historyItemFrom(rec)
    end
  end
  return items
end

function readHistory(limit)
  local f = io.open(historyPath(), "r")
  if not f then return {} end
  local lines = {}
  for line in f:lines() do lines[#lines + 1] = line end
  f:close()
  return parseHistory(lines, limit, hs.json.decode)
end

-- Pure window helpers (no hs.*), extracted so the window's logic is testable:
--   buildCaptureFlags  — mode flags + the translate toggle -> engine argv flags
--   normalizeBackend   — a "setBackend" value; "" / "default" / nil all mean
--                        "use config" (nil); otherwise the backend name
--   normalizeTranslate — a "setTranslate" value coerced to a strict boolean
function buildCaptureFlags(modeFlags, translate)
  local f = {}
  for _, a in ipairs(modeFlags or {}) do f[#f + 1] = a end
  f[#f + 1] = translate and "--translate" or "--no-translate"
  return f
end

function normalizeBackend(value)
  if value and value ~= "" and value ~= "default" then return value end
  return nil
end

function normalizeTranslate(value)
  return value and true or false
end

function windowCaptureFlags()
  return buildCaptureFlags(VB.winModeFlags, VB.winTranslate)
end

function windowToggleRecord()
  if VB.state == "idle" then
    VB.captureFlags = windowCaptureFlags()
    startRecording()
  elseif VB.state == "recording" then
    stopRecording()
  end
end

function pushWindowState()
  if not VB.win then return end
  local timer = (VB.state == "recording") and fmtTime(os.time() - VB.recStart) or ""
  VB.win:evaluateJavaScript(string.format(
    "window.vbState&&vbState(%q,%q,%f)", VB.state, timer, VB.level or 0))
end

-- Surface a delivered result. If the full app window is already open, refresh it
-- in place; otherwise show the lightweight Copy/Paste/Email toast panel. A quick
-- hotkey dictation must NOT summon the whole 470×650 window on every capture (the
-- toast panel is what the README documents), so we no longer force it open here.
function updateResult(text, llmFailed)
  VB.resultText = text or ""
  if VB.win then
    VB.win:evaluateJavaScript("window.vbResult&&vbResult(" .. jsStr(VB.resultText)
      .. "," .. tostring(llmFailed and true or false) .. ")")
    VB.win:evaluateJavaScript("window.vbHistory&&vbHistory(" .. hs.json.encode(readHistory(30)) .. ")")
    VB.win:show():bringToFront()
  else
    showResult(VB.resultText, llmFailed, resultPanelHandlers())
  end
end

function onWebMessage(message)
  local body = message and message.body
  if type(body) ~= "string" then return end
  local ok, d = pcall(hs.json.decode, body)
  if not ok or type(d) ~= "table" then return end
  local a = d.action
  if a == "record" then
    windowToggleRecord()
  elseif a == "setMode" then
    local m = MODES[d.index]; VB.winModeFlags = (m and m.flags) or {}
  elseif a == "setBackend" then
    VB.backend = normalizeBackend(d.value)
  elseif a == "setTranslate" then
    VB.winTranslate = normalizeTranslate(d.value)
  elseif a == "processText" then
    if type(d.text) == "string" and #d.text > 0 then
      VB.captureFlags = windowCaptureFlags()
      runEngine({ "text", d.text })
    end
  elseif a == "copy" or a == "recopy" then
    hs.pasteboard.setContents(d.text or VB.resultText or "")   -- toast shown in-window
  elseif a == "editIntents" then
    openConfig()
  elseif a == "reloadModes" then
    refreshModes()
  elseif a == "saveIntent" then
    if type(d.key) == "string" and #d.key > 0 then saveIntent(d.key, d.prompt or "") end
  elseif a == "setModel" then
    if d.backend == "claude" or d.backend == "codex" then setModel(d.backend, d.model or "") end
  elseif a == "paste" then
    if VB.win then VB.win:hide() end
    pasteText(d.text or VB.resultText or "", 0.12)   -- longer delay: window hides first
  end
end

WIN_HEAD = [==[<!DOCTYPE html><html><head><meta charset="utf-8"><title>Alfred</title>
<style>
 :root{color-scheme:dark;
  --text:#eceef2;--muted:#888d99;--faint:#5f636e;
  --card:#15171d;--card2:#1a1d24;--border:#262932;--border2:#30343e;
  --accent:#5b7cff;--accent2:#7a5cff;--rec:#ff4d57;--ok:#37d39b;}
 *{box-sizing:border-box;}
 body{margin:0;color:var(--text);-webkit-font-smoothing:antialiased;
  font:13px/1.45 -apple-system,system-ui,"SF Pro Text",sans-serif;
  background:radial-gradient(900px 320px at 50% -12%,rgba(91,124,255,.10),transparent 70%),linear-gradient(180deg,#0f1117,#0c0d11);}
 .wrap{padding:16px 16px 18px;display:flex;flex-direction:column;gap:14px;}
 header{display:flex;align-items:center;justify-content:space-between;}
 .brand{display:flex;align-items:center;gap:9px;font-size:15px;font-weight:650;letter-spacing:.2px;}
 .brand span:first-child{font-size:18px;}
 .pill{font-size:11px;color:var(--muted);background:var(--card2);border:1px solid var(--border);border-radius:999px;padding:3px 11px;}
 .pill.rec{color:#ffd9db;background:rgba(255,77,87,.16);border-color:rgba(255,77,87,.42);}
 .pill.busy{color:#ece3bf;background:rgba(220,180,60,.14);border-color:rgba(220,180,60,.35);}
 .rec{position:relative;display:flex;align-items:center;justify-content:center;gap:10px;width:100%;
  border:0;border-radius:14px;padding:15px;cursor:pointer;color:#fff;font-size:15px;font-weight:650;letter-spacing:.2px;
  background:linear-gradient(180deg,var(--accent),var(--accent2));
  box-shadow:0 6px 18px rgba(91,124,255,.30),inset 0 1px 0 rgba(255,255,255,.18);
  transition:transform .06s,box-shadow .2s,background .2s;}
 .rec:hover{box-shadow:0 9px 24px rgba(91,124,255,.42),inset 0 1px 0 rgba(255,255,255,.2);}
 .rec:active{transform:translateY(1px);}
 .rec .rec-dot{width:10px;height:10px;border-radius:50%;background:#fff;opacity:.95;}
 .rec .rec-time{font-variant-numeric:tabular-nums;font-weight:600;opacity:.92;min-width:40px;text-align:right;}
 .rec.recording{background:linear-gradient(180deg,var(--rec),#e23b46);box-shadow:0 6px 20px rgba(255,77,87,.42),inset 0 1px 0 rgba(255,255,255,.18);}
 .rec.recording .rec-dot{animation:pulse 1s ease-in-out infinite;}
 @keyframes pulse{0%,100%{transform:scale(1);opacity:1;}50%{transform:scale(1.55);opacity:.4;}}
 .meter{height:6px;background:var(--card2);border:1px solid var(--border);border-radius:6px;overflow:hidden;margin-top:-4px;}
 .meter-fill{height:100%;width:0;border-radius:6px;background:linear-gradient(90deg,#37d39b,#9fd84a 58%,#ffb74d 84%,#ff5d5d);transition:width .08s linear;}
 .grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;}
 .field{display:flex;flex-direction:column;gap:5px;}
 .flabel{display:flex;align-items:center;justify-content:space-between;font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;font-weight:600;}
 select,input[type=text],textarea{width:100%;background:var(--card);color:var(--text);border:1px solid var(--border);border-radius:10px;padding:9px 10px;font-size:13px;transition:border-color .15s,box-shadow .15s;}
 select{-webkit-appearance:none;appearance:none;cursor:pointer;padding-right:28px;
  background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='10' height='10'><polygon points='0,2 10,2 5,8' fill='%23888d99'/></svg>");
  background-repeat:no-repeat;background-position:right 10px center;}
 select:focus,input:focus,textarea:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px rgba(91,124,255,.22);}
 textarea{height:116px;resize:vertical;line-height:1.5;}
 textarea.typein{height:64px;line-height:1.4;}
 textarea.intent{height:80px;font-size:12px;line-height:1.4;color:#d6d9e0;}
 .switch{display:flex;align-items:center;gap:9px;cursor:pointer;user-select:none;}
 .switch input{display:none;}
 .track{width:38px;height:22px;border-radius:999px;background:var(--card2);border:1px solid var(--border2);position:relative;transition:background .18s,border-color .18s;}
 .track::after{content:"";position:absolute;top:2px;left:2px;width:16px;height:16px;border-radius:50%;background:#cfd2da;transition:transform .18s,background .18s;}
 .switch input:checked + .track{background:linear-gradient(180deg,var(--accent),var(--accent2));border-color:transparent;}
 .switch input:checked + .track::after{transform:translateX(16px);background:#fff;}
 .card{background:var(--card);border:1px solid var(--border);border-radius:12px;overflow:hidden;}
 .card-head{display:flex;align-items:center;justify-content:space-between;padding:9px 12px;border-bottom:1px solid var(--border);font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;font-weight:600;}
 .card-head .tools{display:flex;gap:6px;}
 .card textarea{border:0;border-radius:0;background:transparent;}
 .card textarea:focus{box-shadow:none;}
 .chip{background:var(--card2);color:var(--text);border:1px solid var(--border2);border-radius:7px;padding:5px 11px;font-size:12px;cursor:pointer;transition:background .15s,transform .06s;}
 .chip:hover{background:#23262e;} .chip:active{transform:translateY(1px);}
 .link{background:none;border:0;color:var(--accent);font-size:11px;cursor:pointer;padding:0;text-transform:none;letter-spacing:0;}
 .link:hover{text-decoration:underline;}
 .link.muted{color:var(--muted);}
 .ibtns{display:flex;gap:12px;}
 .hist{list-style:none;margin:0;padding:0;max-height:170px;overflow:auto;}
 .hist li{padding:9px 12px;cursor:pointer;border-bottom:1px solid var(--border);transition:background .12s;}
 .hist li:last-child{border-bottom:0;} .hist li:hover{background:var(--card2);}
 .hist .ht{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;}
 .hist .meta{font-size:10px;color:var(--faint);margin-top:3px;font-variant-numeric:tabular-nums;}
 .hist .empty{padding:16px 12px;color:var(--faint);text-align:center;}
 ::-webkit-scrollbar{width:9px;} ::-webkit-scrollbar-thumb{background:#2a2d36;border-radius:6px;border:2px solid transparent;background-clip:padding-box;}
 .toast{position:fixed;left:50%;bottom:18px;transform:translate(-50%,18px);background:rgba(20,22,28,.97);border:1px solid var(--border2);color:var(--text);padding:8px 16px;border-radius:999px;font-size:12px;opacity:0;pointer-events:none;transition:opacity .2s,transform .2s;box-shadow:0 10px 26px rgba(0,0,0,.55);}
 .toast.show{opacity:1;transform:translate(-50%,0);}
</style></head><body>
 <div class="wrap">
  <header>
   <div class="brand"><span>🎙️</span><span>Alfred</span></div>
   <span id="state" class="pill">Ready</span>
  </header>
  <button id="rec" class="rec"><span class="rec-dot"></span><span id="reclabel">Record</span><span id="rectime" class="rec-time"></span></button>
  <div class="meter"><div id="lvl" class="meter-fill"></div></div>
  <textarea id="typein" class="typein" placeholder="…or type here, then ⏎ to run  (⇧⏎ = new line)"></textarea>
  <div class="grid">
   <div class="field"><div class="flabel"><span>Format / intent</span><button id="editint" class="link">Edit prompt</button></div><select id="mode"></select></div>
   <div class="field"><div class="flabel"><span>LLM backend</span></div><select id="backend"></select></div>
  </div>
  <div class="grid">
   <div class="field"><div class="flabel"><span>Claude model</span></div><select id="claudemodel"></select></div>
   <div class="field"><div class="flabel"><span>Codex model</span></div><select id="codexmodel"></select></div>
  </div>
  <div class="field" id="intentbox" style="display:none"><div class="flabel"><span>Intent prompt</span><span class="ibtns"><button id="cancelint" class="link muted">Cancel</button><button id="saveint" class="link">Save &amp; close</button></span></div><textarea id="intent" class="intent" placeholder="Prompt for this format…"></textarea></div>
  <label class="switch"><input type="checkbox" id="translate"><span class="track"></span><span>Translate to English</span></label>
  <section class="card">
   <div class="card-head"><span>Result</span><div class="tools"><button id="copy" class="chip">Copy</button><button id="paste" class="chip">Paste</button></div></div>
   <textarea id="result" placeholder="Your cleaned text appears here…"></textarea>
  </section>
  <section class="card">
   <div class="card-head"><span>History</span><button id="reload" class="link">Refresh</button></div>
   <ul id="hist" class="hist"></ul>
  </section>
 </div>
 <div id="toast" class="toast"></div>
<script>window.VB_INIT=]==]

WIN_TAIL = [==[;
 const send=(action,extra)=>window.webkit.messageHandlers.vb.postMessage(JSON.stringify(Object.assign({action},extra||{})));
 const $=id=>document.getElementById(id);
 let WIN_MODES=[];
 let toastT; const toast=msg=>{const t=$('toast');t.textContent=msg;t.classList.add('show');clearTimeout(toastT);toastT=setTimeout(()=>t.classList.remove('show'),1200);};
 window.vbState=function(state,timer,level){
   const st=$('state'),r=$('rec');
   if(state==='recording'){st.textContent='● Recording';st.className='pill rec';r.classList.add('recording');$('reclabel').textContent='Stop';$('rectime').textContent=timer;}
   else if(state==='processing'){st.textContent='Working…';st.className='pill busy';r.classList.remove('recording');$('reclabel').textContent='Working…';$('rectime').textContent='';}
   else{st.textContent='Ready';st.className='pill';r.classList.remove('recording');$('reclabel').textContent='Record';$('rectime').textContent='';}
   $('lvl').style.width=state==='recording'?(Math.min(1,level*1.7)*100+'%'):'0';
 };
 window.vbResult=function(text){ $('result').value=text||''; };
 window.vbHistory=function(items){
   const ul=$('hist'); ul.innerHTML='';
   if(!items||!items.length){ul.innerHTML='<li class="empty">No history yet</li>';return;}
   items.forEach(it=>{
     const li=document.createElement('li');
     const t=document.createElement('div'); t.className='ht'; t.textContent=it.text||'';
     const m=document.createElement('div'); m.className='meta'; m.textContent=((it.ts||'').replace('T',' ').slice(0,16))+'  ·  '+(it.chars||0)+'c';
     li.appendChild(t); li.appendChild(m);
     li.onclick=()=>{ $('result').value=it.text; send('recopy',{text:it.text}); toast('Copied ✓'); };
     ul.appendChild(li);
   });
 };
 window.vbModes=function(modes,index){
   WIN_MODES=modes||[];
   const s=$('mode'); s.innerHTML='';
   WIN_MODES.forEach((m,i)=>{const o=document.createElement('option');o.value=i+1;o.textContent=m.label;s.appendChild(o);});
   s.value=index||1; syncEditor();
 };
 function curMode(){ return WIN_MODES[(parseInt($('mode').value,10)||1)-1]||{}; }
 function editorOpen(){ return $('intentbox').style.display!=='none'; }
 function openEditor(){ const m=curMode(); if(!m.key){ toast('This format uses your config prompt'); return; } $('intent').value=m.prompt||''; $('intentbox').style.display=''; $('intent').focus(); }
 function closeEditor(){ $('intentbox').style.display='none'; }
 function syncEditor(){ if(editorOpen()){ const m=curMode(); if(m.key){ $('intent').value=m.prompt||''; } else { closeEditor(); } } }
 function fillModelSelect(id,list,current){
   const s=$(id); if(!s) return; s.innerHTML='';
   const opts=['']; (list||[]).forEach(m=>{ if(opts.indexOf(m)<0) opts.push(m); });
   if(current && opts.indexOf(current)<0) opts.push(current);
   opts.forEach(m=>{const o=document.createElement('option');o.value=m;o.textContent=(m===''?'(default)':m);s.appendChild(o);});
   s.value=current||'';
 }
 window.vbSettings=function(s){ s=s||{}; fillModelSelect('claudemodel',s.claude_models,s.claude_model); fillModelSelect('codexmodel',s.codex_models,s.codex_model); };
 window.vbBackends=function(list,current){
   const s=$('backend'); if(!s) return; s.innerHTML='';
   const mk=(v,t)=>{const o=document.createElement('option');o.value=v;o.textContent=t;s.appendChild(o);};
   mk('default','Default (config)'); (list||[]).forEach(b=>mk(b,b)); s.value=current||'default';
 };
 const I=window.VB_INIT||{};
 vbModes(I.modes,I.modeIndex);
 vbBackends(I.backends,I.backend);
 $('translate').checked=I.translate!==false;
 vbResult(I.result||'');
 vbHistory(I.history||[]);
 vbState(I.state||'idle','',0);
 vbSettings(I.settings||{});
 $('rec').onclick=()=>send('record');
 $('mode').onchange=e=>{send('setMode',{index:parseInt(e.target.value,10)});syncEditor();};
 $('editint').onclick=openEditor;
 $('cancelint').onclick=closeEditor;
 $('saveint').onclick=()=>{const m=curMode(); if(m.key){send('saveIntent',{key:m.key,prompt:$('intent').value});toast('Saved ✓');} closeEditor();};
 $('backend').onchange=e=>send('setBackend',{value:e.target.value});
 $('claudemodel').onchange=e=>{send('setModel',{backend:'claude',model:e.target.value});toast('Saved ✓');};
 $('codexmodel').onchange=e=>{send('setModel',{backend:'codex',model:e.target.value});toast('Saved ✓');};
 $('translate').onchange=e=>send('setTranslate',{value:e.target.checked});
 $('copy').onclick=()=>{send('copy',{text:$('result').value});toast('Copied ✓');};
 $('paste').onclick=()=>send('paste',{text:$('result').value});
 $('reload').onclick=()=>{send('reloadModes');toast('Reloading…');};
 $('typein').addEventListener('keydown',e=>{ if(e.key==='Enter'&&!e.shiftKey){ e.preventDefault(); send('processText',{text:e.target.value}); e.target.value=''; }});
</script></body></html>]==]

function openWindow()
  if VB.win then VB.win:show():bringToFront(); return end
  local ctrl = hs.webview.usercontent.new("vb")
  ctrl:setCallback(onWebMessage)
  local f = hs.screen.mainScreen():frame()
  local w = hs.webview.new(
    { x = f.x + f.w - WIN_W - 40, y = f.y + 60, w = WIN_W, h = WIN_H },
    { developerExtrasEnabled = false }, ctrl)
  local m = hs.webview.windowMasks
  w:windowStyle(m.titled | m.closable | m.resizable | m.utility)
  w:allowTextEntry(true)
  w:level(hs.canvas.windowLevels.floating)
  w:closeOnEscape(true)
  w:deleteOnClose(true)
  w:windowCallback(function(action) if action == "closing" then VB.win = nil end end)
  VB.win = w
  local init = {
    modes = modesForJS(), modeIndex = 1,
    backends = BACKENDS,
    backend = VB.backend or "default",
    translate = VB.winTranslate ~= false,
    result = VB.resultText or "",
    history = readHistory(30),
    state = VB.state,
    settings = VB.settings or {},
  }
  w:html(WIN_HEAD .. hs.json.encode(init) .. WIN_TAIL)
  w:show():bringToFront()
end

-- Persist an intent's prompt to config (engine writes [intent.<key>]), then reload.
function saveIntent(key, prompt)
  local t = hs.task.new(PYTHON, function(code)
    dbg("saveIntent " .. tostring(key) .. " code=" .. tostring(code))
    refreshModes()
  end, { SCRIPT, "set-intent", key, "--prompt", prompt or "" })
  VB.saveTask = t
  t:setEnvironment(TASK_ENV)
  t:start()
end

-- Pure: does a "settings" task's (exit code, stdout) look like a valid,
-- decodable payload worth accepting? Factored out of refreshSettings' task
-- callback to keep its own branch count (and lizard-reported complexity) low.
function taskOutputReady(code, out)
  return code == 0 and out and #out > 0
end

-- Fetch backend/model settings + selectable model lists from the engine.
function refreshSettings()
  local t = hs.task.new(PYTHON, function(code, out)
    if taskOutputReady(code, out) then
      local ok, s = pcall(hs.json.decode, out)
      if ok and type(s) == "table" then
        VB.settings = s
        if VB.win then
          VB.win:evaluateJavaScript("window.vbSettings&&vbSettings(" .. hs.json.encode(s) .. ")")
        end
      end
    end
  end, { SCRIPT, "settings" })
  VB.settingsTask = t
  t:setEnvironment(TASK_ENV)
  t:start()
end

-- Persist a backend's model to config, then refresh (daemon re-reads per call).
function setModel(backend, model)
  local t = hs.task.new(PYTHON, function() refreshSettings() end,
    { SCRIPT, "set-model", backend, "--model", model or "" })
  VB.modelTask = t
  t:setEnvironment(TASK_ENV)
  t:start()
end

