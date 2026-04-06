import os, json, zipfile, re
from datetime import datetime
from flask import Flask, request, jsonify, send_file, render_template_string
import xml.etree.ElementTree as ET
from pyproj import Transformer

app = Flask(__name__)
DATA_FILE = os.path.join(os.path.dirname(__file__), 'data', 'projects.json')
UPLOAD_DIR = os.path.join(os.path.dirname(__file__), 'uploads')
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)

# ── persistence ────────────────────────────────────────────────────────────
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            return json.load(f)
    return {}

def save_data(d):
    with open(DATA_FILE, 'w') as f:
        json.dump(d, f, indent=2)

# ── JXL parsing ────────────────────────────────────────────────────────────
_tc = {}
def grid_to_latlon(north, east, epsg=6583):
    if epsg not in _tc:
        _tc[epsg] = Transformer.from_crs(f'EPSG:{epsg}', 'EPSG:4326', always_xy=True)
    lon, lat = _tc[epsg].transform(east, north)
    return lat, lon

def parse_jxl(filepath):
    tree = ET.parse(filepath)
    root = tree.getroot()
    NS = (root.tag.split('}')[0] + '}') if root.tag.startswith('{') else ''

    def sub(el, *tags):
        cur = el
        for t in tags:
            cur = cur.find(f'{NS}{t}')
            if cur is None: return None
        return cur.text.strip() if (cur is not None and cur.text) else None

    fb = root.find(f'{NS}FieldBook')
    if fb is None:
        raise ValueError('No FieldBook found')

    epsg = 6583
    cs = root.find(f'.//{NS}CoordinateSystemRecord')
    if cs is not None:
        e = sub(cs, 'ProjectedCoordinateReferenceSystemEPSG')
        if e:
            try: epsg = int(e)
            except: pass

    # Collect all line names from attributes
    line_names = set()
    points = []
    for rec in fb:
        if rec.tag.replace(NS, '') != 'PointRecord': continue
        deleted = sub(rec, 'Deleted')
        if deleted and deleted.lower() == 'true': continue

        lat = lon = elev = None
        wlat = sub(rec, 'WGS84', 'Latitude')
        wlon = sub(rec, 'WGS84', 'Longitude')
        if wlat and wlon:
            lat, lon = float(wlat), float(wlon)
            elev = float(sub(rec, 'ComputedGrid', 'Elevation') or 0)
        else:
            n_s = sub(rec, 'ComputedGrid', 'North')
            e_s = sub(rec, 'ComputedGrid', 'East')
            z_s = sub(rec, 'ComputedGrid', 'Elevation')
            if n_s and e_s:
                try:
                    lat, lon = grid_to_latlon(float(n_s), float(e_s), epsg)
                    elev = float(z_s) if z_s else 0.0
                except: continue

        if lat is None: continue

        name = sub(rec, 'Name') or rec.get('ID', 'Unknown')
        code = sub(rec, 'Code') or 'UNCODED'
        ts   = rec.get('TimeStamp', '')

        attrs = {}
        feats = rec.find(f'{NS}Features')
        if feats is not None:
            for feat in feats:
                for attr in feat.findall(f'{NS}Attribute'):
                    an = sub(attr, 'Name'); av = sub(attr, 'Value')
                    if an and av and av not in ('0', '65535'):
                        attrs[an] = av
                        if an == 'LineName': line_names.add(av)

        horiz = sub(rec, 'Precision', 'Horizontal')
        vert  = sub(rec, 'Precision', 'Vertical')

        points.append({
            'name': name, 'code': code, 'lat': lat, 'lon': lon,
            'elev': elev, 'attrs': attrs,
            'horiz': horiz, 'vert': vert, 'ts': ts
        })

    return points, list(line_names), epsg

# ── KMZ building ───────────────────────────────────────────────────────────
STYLES = {
    'weld':    ('ff1400ff', 'http://maps.google.com/mapfiles/kml/paddle/red-circle.png'),
    'fitting': ('ff00d7ff', 'http://maps.google.com/mapfiles/kml/paddle/ylw-circle.png'),
    'topo':    ('ff00aa00', 'http://maps.google.com/mapfiles/kml/paddle/grn-circle.png'),
    'misc':    ('ffaaaaaa', 'http://maps.google.com/mapfiles/kml/paddle/wht-circle.png'),
}

def style_id(code):
    c = code.lower()
    if any(w in c for w in ['weld','tie-in','seam','butt']): return 'weld'
    if any(w in c for w in ['fit','elbow','tee','valve','bend','flange','cap','coupl','reducer']): return 'fitting'
    if any(w in c for w in ['topo','ground','surface','contour','shot','toe','top']): return 'topo'
    return 'misc'

def placemark_kml(p, indent='      '):
    rows = [f"<b>Point:</b> {p['name']}", f"<b>Code:</b> {p['code']}"]
    if p.get('ts'): rows.append(f"<b>Time:</b> {p['ts'].replace('T',' ')}")
    rows.append(f"<b>Lat:</b> {p['lat']:.8f}  <b>Lon:</b> {p['lon']:.8f}")
    rows.append(f"<b>Elevation:</b> {p['elev']:.3f} m")
    if p.get('horiz'):
        try: rows.append(f"<b>Precision H/V:</b> {float(p['horiz']):.4f} / {float(p['vert']):.4f} m")
        except: pass
    if p.get('attrs'):
        rows.append('<hr/>')
        for k, v in p['attrs'].items(): rows.append(f"<b>{k}:</b> {v}")
    desc = '<br/>'.join(rows)
    sid = style_id(p['code'])
    return (f'{indent}<Placemark>\n'
            f'{indent}  <name>{p["name"]}</name>\n'
            f'{indent}  <description><![CDATA[{desc}]]></description>\n'
            f'{indent}  <styleUrl>#{sid}</styleUrl>\n'
            f'{indent}  <Point><coordinates>{p["lon"]},{p["lat"]},{p["elev"]}</coordinates></Point>\n'
            f'{indent}</Placemark>')

def style_defs():
    lines = []
    for sid, (color, href) in STYLES.items():
        lines.append(f'  <Style id="{sid}"><IconStyle><color>{color}</color><scale>1.1</scale>'
                     f'<Icon><href>{href}</href></Icon></IconStyle>'
                     f'<LabelStyle><scale>0.8</scale></LabelStyle></Style>')
    return '\n'.join(lines)

def build_project_kmz(project_id, data, job_id=None):
    """Build KMZ for whole project or a single job."""
    proj = data.get(project_id, {})
    jobs = proj.get('jobs', {})
    if job_id:
        jobs = {job_id: jobs[job_id]} if job_id in jobs else {}

    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<kml xmlns="http://www.opengis.net/kml/2.2">',
             '<Document>', f'  <name>{project_id}</name>', style_defs()]

    folder_label = proj.get('name', project_id)
    lines.append(f'  <Folder><name>{folder_label}</name><open>1</open>')

    for jid, job in sorted(jobs.items()):
        pts = job.get('points', [])
        lines.append(f'    <Folder><name>{jid}  ({len(pts)} pts)</name>')
        by_code = {}
        for p in pts:
            by_code.setdefault(p['code'], []).append(p)
        for code_name, cpts in sorted(by_code.items()):
            lines.append(f'      <Folder><name>{code_name} ({len(cpts)})</name>')
            for p in cpts:
                lines.append(placemark_kml(p))
            lines.append('      </Folder>')
        lines.append('    </Folder>')

    lines += ['  </Folder>', '</Document>', '</kml>']
    return '\n'.join(lines)

# ── routes ─────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template_string(HTML)

@app.route('/api/projects')
def api_projects():
    return jsonify(load_data())

@app.route('/api/upload', methods=['POST'])
def api_upload():
    if 'file' not in request.files:
        return jsonify({'error': 'No file'}), 400
    f = request.files['file']
    fname = f.filename
    # Project ID = first 9 chars of filename (digits, letters, underscores)
    base = os.path.splitext(fname)[0]
    project_id = re.sub(r'[^A-Za-z0-9_-]', '', base)[:9].rstrip('_-')

    save_path = os.path.join(UPLOAD_DIR, fname)
    f.save(save_path)

    try:
        points, line_names, epsg = parse_jxl(save_path)
    except Exception as e:
        return jsonify({'error': str(e)}), 400

    if not points:
        return jsonify({'error': 'No points found in file'}), 400

    # Count by code
    code_counts = {}
    for p in points:
        code_counts[p['code']] = code_counts.get(p['code'], 0) + 1

    data = load_data()
    if project_id not in data:
        data[project_id] = {
            'name': project_id,
            'created': datetime.now().isoformat(),
            'jobs': {}
        }

    job_id = os.path.splitext(fname)[0]
    data[project_id]['jobs'][job_id] = {
        'filename': fname,
        'uploaded': datetime.now().isoformat(),
        'point_count': len(points),
        'code_counts': code_counts,
        'line_names': line_names,
        'epsg': epsg,
        'points': points
    }
    save_data(data)

    total = sum(j['point_count'] for j in data[project_id]['jobs'].values())
    return jsonify({
        'project_id': project_id,
        'job_id': job_id,
        'point_count': len(points),
        'project_total': total,
        'code_counts': code_counts,
        'line_names': line_names
    })

@app.route('/api/kmz/<project_id>')
def api_kmz_project(project_id):
    data = load_data()
    if project_id not in data:
        return jsonify({'error': 'Not found'}), 404
    kml = build_project_kmz(project_id, data)
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('doc.kml', kml.encode('utf-8'))
    buf.seek(0)
    return send_file(buf, mimetype='application/vnd.google-earth.kmz',
                     as_attachment=True, download_name=f'{project_id}.kmz')

@app.route('/api/kmz/<project_id>/<path:job_id>')
def api_kmz_job(project_id, job_id):
    data = load_data()
    if project_id not in data:
        return jsonify({'error': 'Not found'}), 404
    kml = build_project_kmz(project_id, data, job_id=job_id)
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('doc.kml', kml.encode('utf-8'))
    buf.seek(0)
    safe = re.sub(r'[^A-Za-z0-9_.-]', '_', job_id)
    return send_file(buf, mimetype='application/vnd.google-earth.kmz',
                     as_attachment=True, download_name=f'{safe}.kmz')

@app.route('/api/delete/<project_id>/<path:job_id>', methods=['DELETE'])
def api_delete_job(project_id, job_id):
    data = load_data()
    if project_id in data and job_id in data[project_id].get('jobs', {}):
        del data[project_id]['jobs'][job_id]
        if not data[project_id]['jobs']:
            del data[project_id]
        save_data(data)
    return jsonify({'ok': True})

# ── HTML (single page app) ─────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>Survey Jobs</title>
<style>
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f0f2f5;min-height:100vh;color:#1a1a2e}
.topbar{background:#1a2332;color:#fff;padding:14px 16px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100}
.topbar h1{font-size:17px;font-weight:600;letter-spacing:.3px}
.upload-btn{background:#2a7de1;color:#fff;border:none;border-radius:8px;padding:7px 14px;font-size:13px;font-weight:500;cursor:pointer}
.search-bar{padding:10px 14px;background:#fff;border-bottom:1px solid #e5e7eb}
.search-bar input{width:100%;border:1px solid #d1d5db;border-radius:8px;padding:8px 12px;font-size:14px;outline:none;background:#f9fafb}
.search-bar input:focus{border-color:#2a7de1;background:#fff}
.section-label{padding:12px 14px 6px;font-size:11px;font-weight:600;color:#6b7280;text-transform:uppercase;letter-spacing:.8px}
.project-block{margin:0 10px 14px;background:#fff;border-radius:12px;border:1px solid #e5e7eb;overflow:hidden}
.project-header{padding:12px 14px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #f3f4f6;cursor:pointer}
.project-header-left{display:flex;align-items:center;gap:10px}
.project-icon{width:36px;height:36px;border-radius:8px;background:#e8f0fb;display:flex;align-items:center;justify-content:center;font-size:15px}
.project-name{font-size:14px;font-weight:600;color:#1a1a2e}
.project-meta{font-size:11px;color:#6b7280;margin-top:1px}
.project-kmz-btn{background:#2a7de1;color:#fff;border:none;border-radius:7px;padding:5px 10px;font-size:11px;font-weight:500;cursor:pointer;white-space:nowrap}
.job-card{padding:11px 14px;border-bottom:1px solid #f3f4f6;display:flex;align-items:flex-start;justify-content:space-between;gap:8px}
.job-card:last-child{border-bottom:none}
.job-name{font-size:12px;font-weight:500;color:#374151;word-break:break-all}
.job-date{font-size:11px;color:#9ca3af;margin-top:2px}
.job-lines{font-size:11px;color:#6b7280;margin-top:2px}
.badges{display:flex;flex-wrap:wrap;gap:4px;margin-top:5px}
.badge{font-size:10px;padding:2px 7px;border-radius:10px;font-weight:500}
.badge-weld{background:#fde8e8;color:#9b1c1c}
.badge-fitting{background:#fef3c7;color:#78350f}
.badge-topo{background:#d1fae5;color:#065f46}
.badge-misc{background:#f3f4f6;color:#4b5563}
.job-actions{display:flex;flex-direction:column;gap:5px;flex-shrink:0}
.job-kmz-btn{background:#f0f7ff;color:#2a7de1;border:1px solid #bfdbfe;border-radius:6px;padding:4px 8px;font-size:10px;font-weight:500;cursor:pointer;white-space:nowrap}
.job-del-btn{background:#fff5f5;color:#dc2626;border:1px solid #fecaca;border-radius:6px;padding:4px 8px;font-size:10px;cursor:pointer}
.empty{text-align:center;padding:48px 24px;color:#9ca3af}
.empty-icon{font-size:40px;margin-bottom:12px}
.empty p{font-size:14px}
.drop-overlay{display:none;position:fixed;inset:0;background:rgba(42,125,225,.12);border:3px dashed #2a7de1;z-index:200;align-items:center;justify-content:center;font-size:20px;font-weight:600;color:#2a7de1}
.drop-overlay.active{display:flex}
.toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:#1a2332;color:#fff;padding:10px 18px;border-radius:10px;font-size:13px;z-index:300;opacity:0;transition:opacity .25s;pointer-events:none}
.toast.show{opacity:1}
.spinner{display:none;width:16px;height:16px;border:2px solid #fff4;border-top-color:#fff;border-radius:50%;animation:spin .7s linear infinite;margin-left:8px}
@keyframes spin{to{transform:rotate(360deg)}}
input[type=file]{display:none}
</style>
</head>
<body>

<div class="drop-overlay" id="dropOverlay">Drop JXL files here</div>
<div class="toast" id="toast"></div>

<div class="topbar">
  <h1>Survey Jobs</h1>
  <button class="upload-btn" onclick="document.getElementById('fileInput').click()">
    + Upload JXL <span class="spinner" id="spinner"></span>
  </button>
</div>

<div class="search-bar">
  <input type="text" id="searchInput" placeholder="Search projects or jobs..." oninput="render()">
</div>

<input type="file" id="fileInput" accept=".jxl,.jobxml,.xml" multiple onchange="uploadFiles(this.files)">

<div id="content"></div>

<script>
let DB = {};

async function loadProjects(){
  const r = await fetch('/api/projects');
  DB = await r.json();
  render();
}

function badgeClass(code){
  const c = code.toLowerCase();
  if(['weld','tie-in','seam','butt'].some(w=>c.includes(w))) return 'badge-weld';
  if(['fit','elbow','tee','valve','bend','flange','cap','coupl','reducer'].some(w=>c.includes(w))) return 'badge-fitting';
  if(['topo','ground','surface','contour','shot','toe','top'].some(w=>c.includes(w))) return 'badge-topo';
  return 'badge-misc';
}

function fmtDate(iso){
  if(!iso) return '';
  const d = new Date(iso);
  return d.toLocaleDateString('en-US',{month:'short',day:'numeric',year:'numeric'});
}

function render(){
  const q = document.getElementById('searchInput').value.toLowerCase();
  const content = document.getElementById('content');
  const projects = Object.entries(DB).filter(([pid, proj])=>{
    if(!q) return true;
    if(pid.toLowerCase().includes(q)) return true;
    return Object.keys(proj.jobs||{}).some(jid=>jid.toLowerCase().includes(q));
  });

  if(!projects.length){
    content.innerHTML = `<div class="empty">
      <div class="empty-icon">📡</div>
      <p>${Object.keys(DB).length ? 'No results' : 'No jobs yet — upload a JXL file to get started'}</p>
    </div>`;
    return;
  }

  content.innerHTML = projects.sort((a,b)=>a[0].localeCompare(b[0])).map(([pid, proj])=>{
    const jobs = Object.entries(proj.jobs||{});
    const totalPts = jobs.reduce((s,[,j])=>s+j.point_count,0);
    const filteredJobs = q ? jobs.filter(([jid])=>jid.toLowerCase().includes(q)||pid.toLowerCase().includes(q)) : jobs;

    const jobCards = filteredJobs.sort((a,b)=>b[1].uploaded.localeCompare(a[1].uploaded)).map(([jid, job])=>{
      const badges = Object.entries(job.code_counts||{}).map(([code,cnt])=>
        `<span class="badge ${badgeClass(code)}">${cnt} ${code}</span>`).join('');
      const lines = job.line_names && job.line_names.length
        ? `<div class="job-lines">📍 ${job.line_names.join(', ')}</div>` : '';
      return `<div class="job-card">
        <div style="flex:1;min-width:0">
          <div class="job-name">${jid}</div>
          <div class="job-date">${fmtDate(job.uploaded)} · ${job.point_count} pts</div>
          ${lines}
          <div class="badges">${badges}</div>
        </div>
        <div class="job-actions">
          <button class="job-kmz-btn" onclick="dlJobKmz('${pid}','${jid}')">KMZ</button>
          <button class="job-del-btn" onclick="delJob('${pid}','${jid}')">Del</button>
        </div>
      </div>`;
    }).join('');

    return `<div class="project-block">
      <div class="project-header" onclick="">
        <div class="project-header-left">
          <div class="project-icon">🗂</div>
          <div>
            <div class="project-name">${pid}</div>
            <div class="project-meta">${jobs.length} job${jobs.length!==1?'s':''} · ${totalPts} total pts</div>
          </div>
        </div>
        <button class="project-kmz-btn" onclick="event.stopPropagation();dlProjectKmz('${pid}')">Full KMZ</button>
      </div>
      ${jobCards}
    </div>`;
  }).join('');
}

async function uploadFiles(files){
  if(!files.length) return;
  const spinner = document.getElementById('spinner');
  spinner.style.display = 'inline-block';
  let uploaded = 0, errors = [];
  for(const file of files){
    const fd = new FormData();
    fd.append('file', file);
    try{
      const r = await fetch('/api/upload', {method:'POST', body:fd});
      const d = await r.json();
      if(d.error){ errors.push(`${file.name}: ${d.error}`); continue; }
      DB = await (await fetch('/api/projects')).json();
      render();
      uploaded++;
    } catch(e){ errors.push(file.name); }
  }
  spinner.style.display = 'none';
  document.getElementById('fileInput').value = '';
  if(errors.length) toast('Errors: '+errors.join(', '), 4000);
  else toast(`✓ ${uploaded} file${uploaded!==1?'s':''} uploaded`);
}

function dlProjectKmz(pid){
  window.location.href = `/api/kmz/${encodeURIComponent(pid)}`;
  toast('Downloading project KMZ...');
}

function dlJobKmz(pid, jid){
  window.location.href = `/api/kmz/${encodeURIComponent(pid)}/${encodeURIComponent(jid)}`;
  toast('Downloading job KMZ...');
}

async function delJob(pid, jid){
  if(!confirm(`Delete ${jid}?`)) return;
  await fetch(`/api/delete/${encodeURIComponent(pid)}/${encodeURIComponent(jid)}`, {method:'DELETE'});
  DB = await (await fetch('/api/projects')).json();
  render();
  toast('Deleted');
}

function toast(msg, dur=2200){
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(()=>t.classList.remove('show'), dur);
}

// Drag and drop
document.addEventListener('dragover', e=>{e.preventDefault(); document.getElementById('dropOverlay').classList.add('active')});
document.addEventListener('dragleave', e=>{if(!e.relatedTarget) document.getElementById('dropOverlay').classList.remove('active')});
document.addEventListener('drop', e=>{
  e.preventDefault();
  document.getElementById('dropOverlay').classList.remove('active');
  const files = [...e.dataTransfer.files].filter(f=>f.name.match(/\.(jxl|jobxml|xml)$/i));
  if(files.length) uploadFiles(files);
});

loadProjects();
</script>
</body>
</html>"""

if __name__ == '__main__':
    print('\n  Survey Jobs app running!')
    print('  Open in your browser: http://localhost:5000\n')
    import os; app.run(debug=False, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
