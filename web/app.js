// DustGuard หน้าเว็บ: เรียก API Gateway (API_URL มาจาก config.js)

const $ = (id) => document.getElementById(id);

function levelClass(pm) {
  if (pm <= 15) return 'lv1';
  if (pm <= 25) return 'lv2';
  if (pm <= 37.5) return 'lv3';
  if (pm <= 75) return 'lv4';
  return 'lv5';
}

function formatTime(iso) {
  if (!iso) return '-';
  const d = new Date(iso);
  return d.toLocaleString('th-TH', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
}

async function api(path, options) {
  const res = await fetch(API_URL + path, options);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

async function loadProvinces() {
  const select = $('province');
  try {
    const data = await api('/provinces');
    $('threshold').textContent = data.threshold;
    select.innerHTML = '<option value="">-- เลือกจังหวัด --</option>';
    for (const p of data.provinces) {
      const opt = document.createElement('option');
      opt.value = p;
      opt.textContent = p;
      select.appendChild(opt);
    }
  } catch (e) {
    select.innerHTML = '<option value="">โหลดรายชื่อจังหวัดไม่ได้</option>';
    console.error(e);
  }
}

async function loadReadings() {
  const tbody = $('readings');
  try {
    const data = await api('/readings');
    tbody.innerHTML = '';
    for (const r of data.readings) {
      const tr = document.createElement('tr');
      const name = document.createElement('td');
      name.textContent = r.province;
      const val = document.createElement('td');
      const badge = document.createElement('span');
      if (r.pm25 === null) {
        badge.className = 'badge na';
        badge.textContent = 'ไม่มีข้อมูล';
      } else {
        badge.className = 'badge ' + levelClass(r.pm25);
        badge.textContent = r.pm25;
      }
      val.appendChild(badge);
      const time = document.createElement('td');
      time.className = 'time';
      time.textContent = formatTime(r.ts);
      tr.append(name, val, time);
      tbody.appendChild(tr);
    }
  } catch (e) {
    tbody.innerHTML = '<tr><td colspan="3">โหลดค่าฝุ่นไม่ได้</td></tr>';
    console.error(e);
  }
}

$('subscribe-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const msg = $('form-msg');
  const btn = $('submit-btn');
  btn.disabled = true;
  msg.className = 'msg';
  msg.textContent = 'กำลังสมัคร…';
  try {
    const data = await api('/subscribe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email: $('email').value.trim(), province: $('province').value }),
    });
    msg.className = 'msg ok';
    msg.textContent = '✅ ' + data.message;
    $('subscribe-form').reset();
  } catch (e) {
    msg.className = 'msg err';
    msg.textContent = '❌ ' + e.message;
  } finally {
    btn.disabled = false;
  }
});

$('refresh-btn').addEventListener('click', loadReadings);

loadProvinces();
loadReadings();
