const $ = (s) => document.querySelector(s);
const startBtn = $('#startBtn');
const stopBtn = $('#stopBtn');
const message = $('#message');

function esc(v){return String(v ?? '');}
function setMessage(text){ if(message) message.textContent=text; }
function fmt(ts){ if(!ts) return '-'; return new Date(ts*1000).toLocaleString('th-TH'); }

async function jsonFetch(url, options={}) {
  const res = await fetch(url, {cache:'no-store', ...options, headers:{'Content-Type':'application/json', ...(options.headers||{})}});
  const data = await res.json().catch(()=>({error:'invalid_json'}));
  if(!res.ok) throw new Error(data.message || data.error || `HTTP ${res.status}`);
  return data;
}

async function loadMe(){
  const data = await jsonFetch('/api/me');
  const user = Array.isArray(data.user) ? data.user[0] : data.user;
  const channel = Array.isArray(data.channel) ? data.channel[0] : data.channel;
  $('#username').textContent = user?.name || user?.username || user?.email || '-';
  $('#channel').textContent = channel?.slug || channel?.channel_slug || channel?.name || '-';
}

async function loadStatus(){
  try {
    const data = await jsonFetch('/api/live/status');
    $('#processStatus').textContent = data.process_alive ? data.status : 'offline';
    $('#startedAt').textContent = fmt(data.started_at);
    const pill = $('#livePill');
    if(data.process_alive){ pill.textContent='LIVE / SENDING'; pill.classList.add('live'); }
    else { pill.textContent='OFFLINE'; pill.classList.remove('live'); }
    if(data.last_error) $('#errorBox').textContent = data.last_error;
  } catch(e) {
    $('#errorBox').textContent = e.message;
  }
}

async function startLive(){
  startBtn.disabled=true; stopBtn.disabled=true; setMessage('กำลังรับ Stream Key และเริ่ม encoder...');
  try {
    await jsonFetch('/api/live/start',{method:'POST',body:'{}'});
    setMessage('เริ่มส่ง Live แล้ว กำลังรอ KICK ตรวจรับสัญญาณ...');
    await loadStatus();
  } catch(e){ setMessage(`เริ่ม Live ไม่สำเร็จ: ${e.message}`); }
  finally { startBtn.disabled=false; stopBtn.disabled=false; }
}

async function stopLive(){
  startBtn.disabled=true; stopBtn.disabled=true; setMessage('กำลังหยุด Live...');
  try { await jsonFetch('/api/live/stop',{method:'POST',body:'{}'}); setMessage('หยุด Live แล้ว'); await loadStatus(); }
  catch(e){ setMessage(`หยุด Live ไม่สำเร็จ: ${e.message}`); }
  finally { startBtn.disabled=false; stopBtn.disabled=false; }
}

if(startBtn){
  startBtn.addEventListener('click',startLive);
  stopBtn.addEventListener('click',stopLive);
  loadMe();
  loadStatus();
  setInterval(loadStatus,3000);
}
