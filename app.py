import os, json, zipfile, re, io
from datetime import datetime
from functools import wraps
from flask import Flask, request, jsonify, send_file, render_template_string, session, redirect

import xml.etree.ElementTree as ET
from pyproj import Transformer

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'survey-secret-change-me-2024')
DATA_FILE  = os.path.join(os.path.dirname(__file__), 'data', 'projects.json')
LOG_FILE   = os.path.join(os.path.dirname(__file__), 'data', 'activity.json')
UPLOAD_DIR = os.path.join(os.path.dirname(__file__), 'uploads')
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)

APP_PASSWORD = os.environ.get('APP_PASSWORD', 'survey123')

# ── activity log ───────────────────────────────────────────────────────────
def load_log():
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE) as f:
            return json.load(f)
    return []

def write_log(action, detail=''):
    log = load_log()
    log.insert(0, {
        'ts':     datetime.now().strftime('%b %d  %I:%M%p'),
        'email':  session.get('email', 'unknown'),
        'action': action,
        'detail': detail
    })
    log = log[:500]  # keep last 500 entries
    with open(LOG_FILE, 'w') as f:
        json.dump(log, f, indent=2)

# ── auth ───────────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            if request.is_json or request.path.startswith('/api/'):
                return jsonify({'error': 'Unauthorized'}), 401
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = ''
    if request.method == 'POST':
        email    = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        if not email or '@' not in email:
            error = 'Please enter a valid email address'
        elif password != APP_PASSWORD:
            error = 'Wrong password — try again'
        else:
            session['logged_in'] = True
            session['email'] = email
            write_log('signed in')
            return redirect('/')
    return render_template_string(LOGIN_HTML, error=error)

@app.route('/logout')
def logout():
    write_log('signed out')
    session.clear()
    return redirect('/login')

@app.route('/api/log')
@login_required
def api_log():
    return jsonify(load_log())

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            return json.load(f)
    return {}

def save_data(d):
    with open(DATA_FILE, 'w') as f:
        json.dump(d, f, indent=2)

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
        code = sub(rec, 'Code')
        if not code: continue  # skip base station / uncoded reference points
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
            f'{indent}  <n>{p["name"]}</n>\n'
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
    proj = data.get(project_id, {})
    jobs = proj.get('jobs', {})
    if job_id:
        jobs = {job_id: jobs[job_id]} if job_id in jobs else {}
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<kml xmlns="http://www.opengis.net/kml/2.2">',
             '<Document>', f'  <n>{project_id}</n>', style_defs()]
    lines.append(f'  <Folder><n>{proj.get("name", project_id)}</n><open>1</open>')
    for jid, job in sorted(jobs.items()):
        pts = job.get('points', [])
        lines.append(f'    <Folder><n>{jid}  ({len(pts)} pts)</n>')
        by_code = {}
        for p in pts:
            by_code.setdefault(p['code'], []).append(p)
        for code_name, cpts in sorted(by_code.items()):
            lines.append(f'      <Folder><n>{code_name} ({len(cpts)})</n>')
            for p in cpts:
                lines.append(placemark_kml(p))
            lines.append('      </Folder>')
        lines.append('    </Folder>')
    lines += ['  </Folder>', '</Document>', '</kml>']
    return '\n'.join(lines)

@app.route('/')
@login_required
def index():
    return render_template_string(HTML)

@app.route('/api/projects')
@login_required
def api_projects():
    return jsonify(load_data())

@app.route('/api/job/<project_id>/<path:job_id>')
@login_required
def api_job(project_id, job_id):
    data = load_data()
    proj = data.get(project_id, {})
    job = proj.get('jobs', {}).get(job_id)
    if not job:
        return jsonify({'error': 'Not found'}), 404
    return jsonify(job)

@app.route('/api/upload', methods=['POST'])
@login_required
def api_upload():
    if 'file' not in request.files:
        return jsonify({'error': 'No file'}), 400
    f = request.files['file']
    fname = f.filename
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
    code_counts = {}
    for p in points:
        code_counts[p['code']] = code_counts.get(p['code'], 0) + 1
    data = load_data()
    if project_id not in data:
        data[project_id] = {'name': project_id, 'created': datetime.now().isoformat(), 'jobs': {}}
    job_id = fname  # use full filename so each file is always a unique card
    data[project_id]['jobs'][job_id] = {
        'filename': fname, 'uploaded': datetime.now().isoformat(),
        'uploaded_by': session.get('email', 'unknown'),
        'point_count': len(points), 'code_counts': code_counts,
        'line_names': line_names, 'epsg': epsg, 'points': points,
        'deleted': False, 'deleted_by': None, 'deleted_at': None
    }
    save_data(data)
    write_log('uploaded', f'{fname} → {project_id} ({len(points)} pts)')
    total = sum(j['point_count'] for j in data[project_id]['jobs'].values())
    return jsonify({'project_id': project_id, 'job_id': job_id,
                    'point_count': len(points), 'project_total': total,
                    'code_counts': code_counts, 'line_names': line_names})

@app.route('/api/kmz/<project_id>')
@login_required
def api_kmz_project(project_id):
    data = load_data()
    if project_id not in data:
        return jsonify({'error': 'Not found'}), 404
    kml = build_project_kmz(project_id, data)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('doc.kml', kml.encode('utf-8'))
    buf.seek(0)
    write_log('downloaded KMZ', f'{project_id} (full project)')
    return send_file(buf, mimetype='application/vnd.google-earth.kmz',
                     as_attachment=True, download_name=f'{project_id}.kmz')

@app.route('/api/kmz/<project_id>/<path:job_id>')
@login_required
def api_kmz_job(project_id, job_id):
    data = load_data()
    if project_id not in data:
        return jsonify({'error': 'Not found'}), 404
    kml = build_project_kmz(project_id, data, job_id=job_id)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('doc.kml', kml.encode('utf-8'))
    buf.seek(0)
    safe = re.sub(r'[^A-Za-z0-9_.-]', '_', job_id)
    write_log('downloaded KMZ', f'{job_id}')
    return send_file(buf, mimetype='application/vnd.google-earth.kmz',
                     as_attachment=True, download_name=f'{safe}.kmz')

@app.route('/api/delete/<project_id>/<path:job_id>', methods=['DELETE'])
@login_required
def api_delete_job(project_id, job_id):
    data = load_data()
    if project_id in data and job_id in data[project_id].get('jobs', {}):
        write_log('deleted', job_id)
        del data[project_id]['jobs'][job_id]
        if not data[project_id]['jobs']:
            del data[project_id]
        save_data(data)
    return jsonify({'ok': True})


LOGIN_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>Survey Jobs — Login</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f0f2f5;min-height:100vh;display:flex;align-items:center;justify-content:center}
.card{background:#fff;border-radius:16px;padding:36px 28px;width:100%;max-width:340px;box-shadow:0 2px 16px rgba(0,0,0,.08)}
.logo{text-align:center;margin-bottom:28px}
.logo-icon{width:56px;height:56px;background:#1a2332;border-radius:14px;display:flex;align-items:center;justify-content:center;margin:0 auto 12px;font-size:26px}
.logo h1{font-size:20px;font-weight:700;color:#1a1a2e}
.logo p{font-size:13px;color:#6b7280;margin-top:3px}
label{display:block;font-size:13px;font-weight:500;color:#374151;margin-bottom:5px}
input{width:100%;border:1px solid #d1d5db;border-radius:8px;padding:10px 12px;font-size:15px;outline:none;margin-bottom:14px}
input:focus{border-color:#2a7de1}
button{width:100%;background:#1a2332;color:#fff;border:none;border-radius:8px;padding:12px;font-size:15px;font-weight:600;cursor:pointer}
button:active{opacity:.9}
.error{color:#dc2626;font-size:13px;margin-bottom:12px;text-align:center;background:#fef2f2;padding:8px;border-radius:8px}
</style>
</head>
<body>
<div class="card">
  <div class="logo">
    <div class="logo-icon">📡</div>
    <h1>Survey Jobs</h1>
    <p>Pipeline Survey Management</p>
  </div>
  {% if error %}<div class="error">{{ error }}</div>{% endif %}
  <form method="POST">
    <label>Your Email</label>
    <input type="email" name="email" placeholder="you@example.com" autofocus autocomplete="email">
    <label>Crew Password</label>
    <input type="password" name="password" placeholder="Enter crew password" autocomplete="current-password">
    <button type="submit">Sign In</button>
  </form>
</div>
</body>
</html>"""

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>Survey Jobs</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f0f2f5;color:#1a1a2e;height:100vh;display:flex;flex-direction:column;overflow:hidden}
.topbar{background:#1a2332;color:#fff;padding:12px 14px;display:flex;align-items:center;gap:10px;flex-shrink:0;z-index:200}
.back-btn{background:none;border:none;color:#aaa;font-size:20px;cursor:pointer;padding:0 4px;line-height:1}
.topbar h1{font-size:16px;font-weight:600;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.logout-btn{background:none;border:1px solid #ffffff33;color:#aaa;border-radius:6px;padding:4px 8px;font-size:11px;cursor:pointer;white-space:nowrap}
.log-btn{background:none;border:1px solid #ffffff33;color:#aaa;border-radius:6px;padding:4px 8px;font-size:11px;cursor:pointer;white-space:nowrap}
.upload-btn{background:#2a7de1;color:#fff;border:none;border-radius:8px;padding:7px 12px;font-size:13px;font-weight:500;cursor:pointer;white-space:nowrap;flex-shrink:0}

/* ── LIST VIEW ── */
#listView{flex:1;overflow-y:auto;display:flex;flex-direction:column}
.search-bar{padding:10px 12px;background:#fff;border-bottom:1px solid #e5e7eb;flex-shrink:0}
.search-bar input{width:100%;border:1px solid #d1d5db;border-radius:8px;padding:8px 12px;font-size:14px;outline:none;background:#f9fafb}
.search-bar input:focus{border-color:#2a7de1;background:#fff}
.list-scroll{flex:1;overflow-y:auto;padding:10px}
.project-block{background:#fff;border-radius:12px;border:1px solid #e5e7eb;overflow:hidden;margin-bottom:12px}
.project-header{padding:11px 13px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #f3f4f6;cursor:pointer;user-select:none}
.project-arrow{font-size:10px;color:#9ca3af;transition:transform .2s;flex-shrink:0}
.project-arrow.open{transform:rotate(90deg)}
.project-icon{width:34px;height:34px;border-radius:8px;background:#e8f0fb;display:flex;align-items:center;justify-content:center;font-size:14px;flex-shrink:0}
.project-name{font-size:13px;font-weight:600;color:#1a1a2e}
.project-meta{font-size:11px;color:#6b7280;margin-top:1px}
.proj-kmz-btn{background:#2a7de1;color:#fff;border:none;border-radius:7px;padding:5px 10px;font-size:11px;font-weight:500;cursor:pointer;white-space:nowrap;flex-shrink:0}
.job-card{padding:10px 13px;border-bottom:1px solid #f3f4f6;display:flex;align-items:flex-start;gap:8px}
.job-card:last-child{border-bottom:none}
.job-info{flex:1;min-width:0;cursor:pointer}
.job-name{font-size:12px;font-weight:500;color:#374151;word-break:break-all}
.job-date{font-size:11px;color:#9ca3af;margin-top:2px}
.job-lines{font-size:11px;color:#6b7280;margin-top:2px}
.badges{display:flex;flex-wrap:wrap;gap:4px;margin-top:5px}
.badge{font-size:10px;padding:2px 7px;border-radius:10px;font-weight:500}
.badge-code{font-size:10px;padding:2px 7px;border-radius:10px;font-weight:500}
.job-actions{display:flex;flex-direction:column;gap:4px;flex-shrink:0}
.job-map-btn{background:#e8f0fb;color:#1a56db;border:1px solid #bfdbfe;border-radius:6px;padding:4px 8px;font-size:10px;font-weight:500;cursor:pointer;white-space:nowrap}
.job-kmz-btn{background:#f0fdf4;color:#166534;border:1px solid #bbf7d0;border-radius:6px;padding:4px 8px;font-size:10px;font-weight:500;cursor:pointer;white-space:nowrap}
.job-del-btn{background:#fff5f5;color:#dc2626;border:1px solid #fecaca;border-radius:6px;padding:4px 8px;font-size:10px;cursor:pointer}
.empty{text-align:center;padding:48px 24px;color:#9ca3af}
.empty p{font-size:14px;margin-top:8px}

/* ── MAP VIEW ── */
#mapView{flex:1;display:none;flex-direction:column;position:relative}
#map{flex:1;z-index:1}
.map-toolbar{position:absolute;bottom:80px;right:12px;display:flex;flex-direction:column;gap:8px;z-index:400}
.map-btn{width:44px;height:44px;border-radius:22px;border:none;cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:18px;box-shadow:0 2px 8px rgba(0,0,0,.25)}
.map-btn.active{background:#1a2332!important;color:#fff!important}
.locate-btn{background:#2a7de1;color:#fff}
.filter-bar{display:flex;gap:6px;flex-wrap:wrap}
.filter-pill{font-size:11px;padding:4px 10px;border-radius:12px;border:1.5px solid transparent;cursor:pointer;font-weight:500;opacity:.4;transition:opacity .15s}
.filter-pill.on{opacity:1}
.search-result-item{padding:10px 14px;border-bottom:1px solid #f3f4f6;cursor:pointer;font-size:13px}
.search-result-item:last-child{border-bottom:none}
.search-result-item:active{background:#f9fafb}

/* popup */
.leaflet-popup-content{font-size:13px;line-height:1.6;min-width:200px}
.leaflet-popup-content b{color:#1a1a2e}
.leaflet-popup-content hr{border:none;border-top:1px solid #e5e7eb;margin:6px 0}
.pop-title{font-size:14px;font-weight:600;margin-bottom:4px;color:#1a1a2e}

.drop-overlay{display:none;position:fixed;inset:0;background:rgba(42,125,225,.12);border:3px dashed #2a7de1;z-index:500;align-items:center;justify-content:center;font-size:20px;font-weight:600;color:#2a7de1}
.drop-overlay.active{display:flex}
.toast{position:fixed;bottom:80px;left:50%;transform:translateX(-50%);background:#1a2332;color:#fff;padding:10px 18px;border-radius:10px;font-size:13px;z-index:600;opacity:0;transition:opacity .25s;pointer-events:none;white-space:nowrap}
.toast.show{opacity:1}
.spinner{display:none;width:14px;height:14px;border:2px solid #fff4;border-top-color:#fff;border-radius:50%;animation:spin .7s linear infinite;margin-left:6px;vertical-align:middle}
@keyframes spin{to{transform:rotate(360deg)}}
input[type=file]{display:none}
</style>
</head>
<body>

<div class="drop-overlay" id="dropOverlay">Drop JXL files here</div>
<div class="toast" id="toast"></div>

<div class="topbar" id="topbar">
  <button class="back-btn" id="backBtn" style="display:none" onclick="showList()">&#8592;</button>
  <h1 id="topTitle">Survey Jobs</h1>
  <button class="upload-btn" id="uploadBtn" onclick="document.getElementById('fileInput').click()">
    + Upload <span class="spinner" id="spinner"></span>
  </button>
  <button class="log-btn" id="logBtn" onclick="showLog()">Log</button>
  <button class="logout-btn" id="logoutBtn" onclick="window.location='/logout'">Sign out</button>
</div>

<!-- LIST -->
<div id="listView">
  <div class="search-bar">
    <input type="text" id="searchInput" placeholder="Search projects or jobs..." oninput="renderList()">
  </div>
  <div class="list-scroll" id="listScroll"></div>
</div>

<!-- MAP -->
<div id="mapView">
  <div style="position:absolute;top:8px;left:8px;right:8px;z-index:400;display:flex;flex-direction:column;gap:6px">
    <div style="display:flex;gap:6px">
      <div style="flex:1;display:flex;background:#fff;border-radius:10px;box-shadow:0 2px 8px rgba(0,0,0,.2);overflow:hidden">
        <input type="text" id="mapSearch" placeholder="Search point number..." oninput="searchPoint(this.value)"
          style="flex:1;border:none;outline:none;padding:8px 12px;font-size:13px;background:transparent">
        <button onclick="clearMapSearch()" style="border:none;background:transparent;padding:0 10px;color:#9ca3af;font-size:16px;cursor:pointer">&#x2715;</button>
      </div>
    </div>
    <div class="filter-bar" id="filterBar"></div>
    <div id="searchResults" style="display:none;background:#fff;border-radius:10px;box-shadow:0 2px 8px rgba(0,0,0,.2);overflow:hidden;max-height:180px;overflow-y:auto"></div>
  </div>
  <div id="map"></div>
  <div class="map-toolbar">
    <button class="map-btn locate-btn" onclick="locateMe()" title="Find me">&#x2316;</button>
    <button class="map-btn" id="fitBtn" onclick="fitPoints()" title="Zoom to all points" style="background:#fff;color:#1a2332;font-size:18px;display:none">&#x26F6;</button>
    <button class="map-btn" id="satelliteBtn" onclick="toggleSatellite()" title="Satellite" style="background:#fff;color:#1a2332;font-size:14px;font-weight:600">SAT</button>
    <button class="map-btn" id="measureBtn" onclick="toggleMeasure()" title="Measure distance" style="background:#fff;color:#1a2332;font-size:20px">&#x21B9;</button>
  </div>
  <div id="measureBar" style="display:none;position:absolute;bottom:140px;left:12px;right:12px;background:#1a2332;color:#fff;border-radius:10px;padding:10px 14px;z-index:400;display:none;align-items:center;justify-content:space-between">
    <span id="measureTxt">Tap two points to measure</span>
    <button onclick="clearMeasure()" style="background:none;border:1px solid #ffffff44;color:#fff;border-radius:6px;padding:3px 8px;font-size:11px;cursor:pointer">Clear</button>
  </div>
</div>

<!-- LOG -->
<div id="logView" style="display:none;flex:1;flex-direction:column;overflow:hidden">
  <div style="flex:1;overflow-y:auto;padding:10px" id="logScroll"></div>
</div>

<input type="file" id="fileInput" accept=".jxl,.jobxml,.xml" multiple onchange="uploadFiles(this.files)">

<script>
let DB = {};
let MAP = null;
let markers = [];
let locMarker = null;
let activeFilters = {};
let currentPoints = [];

const KNOWN_COLORS = {
  weld:    '#e24b4a',
  fitting: '#f59e0b',
  topo:    '#22c55e',
};
const EXTRA_PALETTE = [
  '#8b5cf6','#06b6d4','#f97316','#ec4899',
  '#14b8a6','#6366f1','#84cc16','#f43f5e',
  '#0ea5e9','#a855f7','#10b981','#fb923c'
];
const codeColorMap = {};

function codeColor(code){
  if(codeColorMap[code]) return codeColorMap[code];
  const c = (code||'').toLowerCase();
  if(['weld','tie-in','seam','butt'].some(w=>c.includes(w))) return codeColorMap[code]=KNOWN_COLORS.weld;
  if(['fit','elbow','tee','valve','bend','flange','cap','coupl','reducer'].some(w=>c.includes(w))) return codeColorMap[code]=KNOWN_COLORS.fitting;
  if(['topo','ground','surface','contour','shot','toe','top'].some(w=>c.includes(w))) return codeColorMap[code]=KNOWN_COLORS.topo;
  const used = Object.values(codeColorMap);
  const next = EXTRA_PALETTE.find(c=>!used.includes(c)) || EXTRA_PALETTE[used.length % EXTRA_PALETTE.length];
  return codeColorMap[code] = next;
}

function makeIcon(code){
  const color = codeColor(code);
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 18 18">
    <circle cx="9" cy="9" r="8" fill="${color}" stroke="#fff" stroke-width="2.5"/>
  </svg>`;
  return L.divIcon({
    html: svg, className: '', iconSize:[18,18], iconAnchor:[9,9], popupAnchor:[0,-12]
  });
}

async function loadProjects(){
  const r = await fetch('/api/projects');
  DB = await r.json();
  renderList();
}

function badgeClass(code){
  return 'badge-code';
}

function badgeStyle(code){
  const color = codeColor(code);
  const r=parseInt(color.slice(1,3),16), g=parseInt(color.slice(3,5),16), b=parseInt(color.slice(5,7),16);
  return `background:rgba(${r},${g},${b},.12);color:${color};border:1px solid rgba(${r},${g},${b},.35)`;
}

function fmtDate(iso){
  if(!iso) return '';
  return new Date(iso).toLocaleDateString('en-US',{month:'short',day:'numeric',year:'numeric'});
}

function renderList(){
  const q = document.getElementById('searchInput').value.toLowerCase();
  const el = document.getElementById('listScroll');
  const projects = Object.entries(DB).filter(([pid,proj])=>{
    if(!q) return true;
    if(pid.toLowerCase().includes(q)) return true;
    return Object.keys(proj.jobs||{}).some(jid=>jid.toLowerCase().includes(q));
  });

  if(!projects.length){
    el.innerHTML = `<div class="empty"><div style="font-size:40px">📡</div>
      <p>${Object.keys(DB).length?'No results':'Upload a JXL file to get started'}</p></div>`;
    return;
  }

  el.innerHTML = projects.sort((a,b)=>a[0].localeCompare(b[0])).map(([pid,proj])=>{
    const jobs = Object.entries(proj.jobs||{});
    const totalPts = jobs.reduce((s,[,j])=>s+j.point_count,0);
    const filteredJobs = q ? jobs.filter(([jid])=>jid.toLowerCase().includes(q)||pid.toLowerCase().includes(q)) : jobs;

    const jobCards = filteredJobs.sort((a,b)=>b[1].uploaded.localeCompare(a[1].uploaded)).map(([jid,job])=>{
      const badges = Object.entries(job.code_counts||{}).map(([code,cnt])=>
        `<span class="badge badge-code" style="${badgeStyle(code)}">${cnt} ${code}</span>`).join('');
      const lines = job.line_names&&job.line_names.length
        ? `<div class="job-lines">&#128205; ${job.line_names.join(', ')}</div>` : '';
      const safeJid = encodeURIComponent(jid);
      const uploader = job.uploaded_by ? `<div class="job-date" style="color:#9ca3af">&#128100; ${job.uploaded_by}</div>` : '';
      return `<div class="job-card">
        <div class="job-info" onclick="openJobMap('${pid}','${safeJid}')">
          <div class="job-name">${jid}</div>
          <div class="job-date">${fmtDate(job.uploaded)} &middot; ${job.point_count} pts</div>
          ${uploader}
          ${lines}
          <div class="badges">${badges}</div>
        </div>
        <div class="job-actions">
          <button class="job-map-btn" onclick="openJobMap('${pid}','${safeJid}')">Map</button>
          <button class="job-kmz-btn" onclick="dlJobKmz('${pid}','${jid}')">KMZ</button>
          <button class="job-del-btn" onclick="delJob('${pid}','${jid}')">Del</button>
        </div>
      </div>`;
    }).join('');

    const safePid = pid.replace(/'/g,"\\'");
    const allLines = [...new Set(jobs.flatMap(([,j])=>j.line_names||[]))].join(', ');
    return `<div class="project-block">
      <div class="project-header" onclick="toggleProject('${safePid}')">
        <div style="display:flex;align-items:center;gap:9px;flex:1;min-width:0">
          <div class="project-arrow" id="arrow-${pid}">&#9654;</div>
          <div class="project-icon">&#128193;</div>
          <div style="min-width:0">
            <div class="project-name">${pid}${allLines ? `<span style="font-weight:400;color:#6b7280;font-size:12px;margin-left:6px">${allLines}</span>` : ''}</div>
            <div class="project-meta">${jobs.length} job${jobs.length!==1?'s':''} &middot; ${totalPts} pts total</div>
          </div>
        </div>
        <div style="display:flex;gap:5px;flex-shrink:0" onclick="event.stopPropagation()">
          <button class="job-map-btn" onclick="openProjectMap('${safePid}')">Map</button>
          <button class="proj-kmz-btn" onclick="dlProjectKmz('${safePid}')">KMZ</button>
          <button class="job-del-btn" onclick="delProject('${safePid}')">Del</button>
        </div>
      </div>
      <div class="job-list" id="jobs-${pid}" style="display:none">
        ${jobCards}
      </div>
    </div>`;
  }).join('');
}

function toggleProject(pid){
  const jobs = document.getElementById(`jobs-${pid}`);
  const arrow = document.getElementById(`arrow-${pid}`);
  if(!jobs) return;
  const open = jobs.style.display === 'block';
  jobs.style.display = open ? 'none' : 'block';
  arrow.classList.toggle('open', !open);
}

async function openProjectMap(pid){
  toast('Loading map...');
  const data = DB[pid];
  if(!data) return;
  const allPoints = Object.values(data.jobs||{}).flatMap(j=>j.points||[]);
  if(!allPoints.length){ toast('No points in this project'); return; }
  currentPoints = allPoints;
  activeFilters = {};
  showMapView(pid);
}

async function openJobMap(pid, jobIdEncoded){
  const jid = decodeURIComponent(jobIdEncoded);
  toast('Loading map...');
  const r = await fetch(`/api/job/${encodeURIComponent(pid)}/${encodeURIComponent(jid)}`);
  const job = await r.json();
  if(job.error){ toast('Error: '+job.error); return; }
  currentPoints = job.points || [];
  activeFilters = {};
  showMapView(jid);
}

function showMapView(title){
  document.getElementById('listView').style.display = 'none';
  document.getElementById('logView').style.display = 'none';
  document.getElementById('mapView').style.display = 'flex';
  document.getElementById('backBtn').style.display = 'block';
  document.getElementById('uploadBtn').style.display = 'none';
  document.getElementById('logBtn').style.display = 'none';
  document.getElementById('logoutBtn').style.display = 'none';
  document.getElementById('topTitle').textContent = title;
  document.getElementById('mapSearch').value = '';
  document.getElementById('searchResults').style.display = 'none';
  if(!MAP){
    MAP = L.map('map', {zoomControl:true});
    initLayers();
  }
  // reset measure
  if(measuring){ measuring=false; MAP.off('click',onMeasureClick); clearMeasure(); }
  document.getElementById('measureBtn').classList.remove('active');
  document.getElementById('measureBar').style.display = 'none';
  document.getElementById('fitBtn').style.display = 'none';
  plotPoints();
  renderFilterBar();
}

let visiblePts = [];
let currentPopupIdx = 0;

function buildPopupHtml(p, idx, total){
  const color = codeColor(p.code);
  const rows = [`<div class="pop-title" style="color:${color}">${p.name}</div>`,
                `<b>Code:</b> ${p.code}`];
  if(p.ts) rows.push(`<b>Time:</b> ${p.ts.replace('T',' ')}`);
  rows.push(`<b>Elev:</b> ${p.elev.toFixed(3)} m`);
  if(p.horiz){
    try{ rows.push(`<b>Precision H/V:</b> ${parseFloat(p.horiz).toFixed(4)} / ${parseFloat(p.vert).toFixed(4)} m`); }catch(e){}
  }
  if(p.attrs && Object.keys(p.attrs).length){
    rows.push('<hr/>');
    Object.entries(p.attrs).forEach(([k,v])=>rows.push(`<b>${k}:</b> ${v}`));
  }
  const isApple = /iPhone|iPad|iPod|Mac/.test(navigator.userAgent);
  const navUrl = isApple
    ? `maps://maps.apple.com/?daddr=${p.lat},${p.lon}`
    : `https://www.google.com/maps/dir/?api=1&destination=${p.lat},${p.lon}`;
  const navLabel = isApple ? '&#x1F6A7; Navigate in Apple Maps' : '&#x1F6A7; Navigate in Google Maps';
  const nav = `<div style="display:flex;align-items:center;justify-content:space-between;margin-top:10px;padding-top:8px;border-top:1px solid #e5e7eb">
    <button onclick="navigatePoint(${idx-1})" style="background:#f3f4f6;border:none;border-radius:6px;padding:4px 10px;cursor:pointer;font-size:14px" ${idx===0?'disabled style="opacity:.3;background:#f3f4f6;border:none;border-radius:6px;padding:4px 10px;font-size:14px"':''}>&#8592;</button>
    <span style="font-size:11px;color:#6b7280">${idx+1} of ${total}</span>
    <button onclick="navigatePoint(${idx+1})" style="background:#f3f4f6;border:none;border-radius:6px;padding:4px 10px;cursor:pointer;font-size:14px" ${idx===total-1?'disabled style="opacity:.3;background:#f3f4f6;border:none;border-radius:6px;padding:4px 10px;font-size:14px"':''}>&#8594;</button>
  </div>
  <a href="${navUrl}" target="_blank"
    style="display:block;margin-top:8px;background:#1a73e8;color:#fff;text-align:center;padding:8px;border-radius:8px;font-size:13px;font-weight:500;text-decoration:none">
    ${navLabel}
  </a>`;
  return rows.join('<br/>') + nav;
}

function navigatePoint(idx){
  if(idx < 0 || idx >= visiblePts.length) return;
  currentPopupIdx = idx;
  const m = markers[idx];
  const p = visiblePts[idx];
  MAP.setView([p.lat, p.lon], Math.max(MAP.getZoom(), 18));
  m.setPopupContent(buildPopupHtml(p, idx, visiblePts.length));
  m.openPopup();
}

function plotPoints(){
  markers.forEach(m=>MAP.removeLayer(m));
  markers = [];
  visiblePts = currentPoints.filter(p=> activeFilters[p.code] !== false);
  if(!visiblePts.length) return;

  visiblePts.forEach((p, idx)=>{
    const m = L.marker([p.lat, p.lon], {icon: makeIcon(p.code)})
      .bindPopup(buildPopupHtml(p, idx, visiblePts.length), {maxWidth:300})
      .addTo(MAP);
    m.on('popupopen', ()=>{ currentPopupIdx = idx; });
    m.on('click', (e)=>{
      if(measuring){
        L.DomEvent.stopPropagation(e);
        onMeasureSnap({lat: p.lat, lng: p.lon}, p.name);
      }
    });
    markers.push(m);
  });

  if(markers.length){
    const group = L.featureGroup(markers);
    MAP.fitBounds(group.getBounds().pad(0.15));
  }
}

function renderFilterBar(){
  const counts = {};
  currentPoints.forEach(p=>counts[p.code]=(counts[p.code]||0)+1);
  if(!Object.keys(activeFilters).length){
    Object.keys(counts).forEach(code=>activeFilters[code]=true);
  }
  document.getElementById('filterBar').innerHTML = Object.entries(counts)
    .map(([code,cnt])=>{
      const color = codeColor(code);
      const on = activeFilters[code] !== false;
      const r=parseInt(color.slice(1,3),16), g=parseInt(color.slice(3,5),16), b=parseInt(color.slice(5,7),16);
      const bg = on ? `rgba(${r},${g},${b},.15)` : 'rgba(128,128,128,.08)';
      const bc = on ? color : '#aaa';
      const tc = on ? color : '#aaa';
      return `<span class="filter-pill ${on?'on':''}"
        style="background:${bg};color:${tc};border-color:${bc}"
        onclick="toggleFilter('${code.replace(/'/g,"\\'")}')">
        &#9679; ${code} ${cnt}</span>`;
    }).join('');
}

function toggleFilter(code){
  activeFilters[code] = activeFilters[code] === false ? true : false;
  renderFilterBar();
  plotPoints();
}

function locateMe(){
  if(!navigator.geolocation){ toast('GPS not available on this device'); return; }
  toast('Finding your location...');
  navigator.geolocation.getCurrentPosition(pos=>{
    const {latitude:lat, longitude:lon} = pos.coords;
    if(locMarker) MAP.removeLayer(locMarker);
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 20 20">
      <circle cx="10" cy="10" r="9" fill="#2a7de1" stroke="#fff" stroke-width="2"/>
      <circle cx="10" cy="10" r="4" fill="#fff"/>
    </svg>`;
    locMarker = L.marker([lat,lon],{
      icon: L.divIcon({html:svg,className:'',iconSize:[20,20],iconAnchor:[10,10]})
    }).addTo(MAP).bindPopup('You are here');
    MAP.setView([lat,lon], 17);
    document.getElementById('fitBtn').style.display = 'flex';
    toast('Found you!');
  }, err=>{
    toast('Could not get location — check browser permissions');
  },{enableHighAccuracy:true,timeout:10000});
}

function fitPoints(){
  if(!markers.length) return;
  const group = L.featureGroup(markers);
  MAP.fitBounds(group.getBounds().pad(0.15));
  document.getElementById('fitBtn').style.display = 'none';
}

// ── satellite toggle ────────────────────────────────────────────────────────
let streetLayer = null;
let satLayer = null;
let isSat = false;

function initLayers(){
  streetLayer = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{
    attribution:'&copy; OpenStreetMap', maxZoom:22
  });
  satLayer = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',{
    attribution:'&copy; Esri', maxZoom:22
  });
  streetLayer.addTo(MAP);
}

function toggleSatellite(){
  isSat = !isSat;
  if(isSat){
    MAP.removeLayer(streetLayer);
    satLayer.addTo(MAP);
  } else {
    MAP.removeLayer(satLayer);
    streetLayer.addTo(MAP);
  }
  document.getElementById('satelliteBtn').classList.toggle('active', isSat);
}

// ── measure tool ────────────────────────────────────────────────────────────
let measuring = false;
let measurePts = [];
let measureLayers = [];

function toggleMeasure(){
  measuring = !measuring;
  document.getElementById('measureBtn').classList.toggle('active', measuring);
  const bar = document.getElementById('measureBar');
  bar.style.display = measuring ? 'flex' : 'none';
  if(!measuring){ clearMeasure(); MAP.off('click', onMeasureClick); return; }
  document.getElementById('measureTxt').textContent = 'Tap a point or anywhere on map';
  MAP.on('click', onMeasureClick);
  // also hook into marker clicks when measuring
  markers.forEach((m, idx)=>{
    m.on('click', (e)=>{
      if(!measuring) return;
      L.DomEvent.stopPropagation(e);
      const p = visiblePts[idx];
      onMeasureSnap({lat: p.lat, lng: p.lon}, p.name);
    });
  });
}

let lastSnapTime = 0;

function onMeasureSnap(latlng, label){
  lastSnapTime = Date.now();
  measurePts.push(L.latLng(latlng.lat, latlng.lng));
  const dot = L.circleMarker([latlng.lat, latlng.lng],
    {radius:6, color:'#ff6b00', fillColor:'#ff6b00', fillOpacity:1, weight:2})
    .bindTooltip(label||'', {permanent:true, direction:'top', offset:[0,-8],
      className:'', opacity:1})
    .addTo(MAP);
  measureLayers.push(dot);
  if(measurePts.length >= 2){
    const line = L.polyline(measurePts, {color:'#ff6b00', weight:2.5, dashArray:'6 4'}).addTo(MAP);
    measureLayers.push(line);
    const d = totalDist(measurePts);
    const ft = (d * 3.28084).toFixed(1);
    const m  = d.toFixed(1);
    document.getElementById('measureTxt').textContent = `${ft} ft  (${m} m)  — ${measurePts.length} pts`;
  } else {
    document.getElementById('measureTxt').textContent = 'Tap another point or spot';
  }
}

function onMeasureClick(e){
  if(!measuring) return;
  if(Date.now() - lastSnapTime < 300) return; // pin tap already handled it
  onMeasureSnap(e.latlng, '');
}

function totalDist(pts){
  let d = 0;
  for(let i=1;i<pts.length;i++) d += pts[i-1].distanceTo(pts[i]);
  return d;
}

function clearMeasure(){
  measureLayers.forEach(l=>MAP.removeLayer(l));
  measureLayers = []; measurePts = [];
  if(measuring) document.getElementById('measureTxt').textContent = 'Tap the map to start measuring';
}

// ── point search ────────────────────────────────────────────────────────────
function searchPoint(q){
  const res = document.getElementById('searchResults');
  if(!q.trim()){ res.style.display='none'; return; }
  const matches = visiblePts.filter(p=>
    p.name.toLowerCase().includes(q.toLowerCase())
  ).slice(0, 8);
  if(!matches.length){ res.style.display='none'; return; }
  res.style.display = 'block';
  res.innerHTML = matches.map((p,i)=>{
    const color = codeColor(p.code);
    const idx = visiblePts.indexOf(p);
    return `<div class="search-result-item" onclick="jumpToPoint(${idx})">
      <span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:${color};margin-right:8px;vertical-align:middle"></span>
      <b>${p.name}</b> <span style="color:#9ca3af;font-size:11px;margin-left:6px">${p.code}</span>
    </div>`;
  }).join('');
}

function jumpToPoint(idx){
  document.getElementById('searchResults').style.display = 'none';
  document.getElementById('mapSearch').value = '';
  navigatePoint(idx);
}

function clearMapSearch(){
  document.getElementById('mapSearch').value = '';
  document.getElementById('searchResults').style.display = 'none';
}

function showList(){
  document.getElementById('mapView').style.display = 'none';
  document.getElementById('logView').style.display = 'none';
  document.getElementById('listView').style.display = 'flex';
  document.getElementById('backBtn').style.display = 'none';
  document.getElementById('uploadBtn').style.display = 'block';
  document.getElementById('logBtn').style.display = 'block';
  document.getElementById('logoutBtn').style.display = 'block';
  document.getElementById('topTitle').textContent = 'Survey Jobs';
}

async function uploadFiles(files){
  if(!files.length) return;
  document.getElementById('spinner').style.display = 'inline-block';
  let uploaded=0, errors=[];
  for(const file of files){
    const fd = new FormData(); fd.append('file',file);
    try{
      const r = await fetch('/api/upload',{method:'POST',body:fd});
      const d = await r.json();
      if(d.error){ errors.push(`${file.name}: ${d.error}`); continue; }
      DB = await (await fetch('/api/projects')).json();
      renderList(); uploaded++;
    }catch(e){ errors.push(file.name); }
  }
  document.getElementById('spinner').style.display = 'none';
  document.getElementById('fileInput').value = '';
  toast(errors.length ? 'Errors: '+errors.join(', ') : `✓ ${uploaded} file${uploaded!==1?'s':''} uploaded`);
}

function dlProjectKmz(pid){
  window.location.href=`/api/kmz/${encodeURIComponent(pid)}`;
  toast('Downloading project KMZ...');
}
function dlJobKmz(pid,jid){
  window.location.href=`/api/kmz/${encodeURIComponent(pid)}/${encodeURIComponent(jid)}`;
  toast('Downloading job KMZ...');
}
async function showLog(){
  document.getElementById('listView').style.display = 'none';
  document.getElementById('mapView').style.display = 'none';
  document.getElementById('logView').style.display = 'flex';
  document.getElementById('backBtn').style.display = 'block';
  document.getElementById('uploadBtn').style.display = 'none';
  document.getElementById('logBtn').style.display = 'none';
  document.getElementById('logoutBtn').style.display = 'none';
  document.getElementById('topTitle').textContent = 'Activity Log';
  const r = await fetch('/api/log');
  const log = await r.json();
  const el = document.getElementById('logScroll');
  if(!log.length){ el.innerHTML='<div class="empty"><p>No activity yet</p></div>'; return; }
  el.innerHTML = log.map(e=>`
    <div style="display:flex;gap:10px;padding:10px 4px;border-bottom:1px solid #f3f4f6;align-items:flex-start">
      <div style="min-width:120px;font-size:11px;color:#9ca3af;padding-top:1px">${e.ts}</div>
      <div style="flex:1;min-width:0">
        <div style="font-size:12px;font-weight:500;color:#374151">${e.email}</div>
        <div style="font-size:12px;color:#6b7280">${e.action}${e.detail?' — <span style="color:#374151">'+e.detail+'</span>':''}</div>
      </div>
    </div>`).join('');
}

async function delProject(pid){
  if(!confirm(`Delete entire project ${pid} and all its jobs?`)) return;
  const jobs = Object.keys(DB[pid]?.jobs||{});
  for(const jid of jobs){
    await fetch(`/api/delete/${encodeURIComponent(pid)}/${encodeURIComponent(jid)}`,{method:'DELETE'});
  }
  DB = await (await fetch('/api/projects')).json();
  renderList(); toast('Project deleted');
}

async function delJob(pid,jid){
  if(!confirm(`Delete ${jid}?`)) return;
  await fetch(`/api/delete/${encodeURIComponent(pid)}/${encodeURIComponent(jid)}`,{method:'DELETE'});
  DB = await (await fetch('/api/projects')).json();
  renderList(); toast('Deleted');
}

function toast(msg,dur=2500){
  const t=document.getElementById('toast');
  t.textContent=msg; t.classList.add('show');
  setTimeout(()=>t.classList.remove('show'),dur);
}

document.addEventListener('dragover',e=>{e.preventDefault();document.getElementById('dropOverlay').classList.add('active')});
document.addEventListener('dragleave',e=>{if(!e.relatedTarget)document.getElementById('dropOverlay').classList.remove('active')});
document.addEventListener('drop',e=>{
  e.preventDefault();
  document.getElementById('dropOverlay').classList.remove('active');
  const files=[...e.dataTransfer.files].filter(f=>f.name.match(/\.(jxl|jobxml|xml)$/i));
  if(files.length) uploadFiles(files);
});

loadProjects();
</script>
</body>
</html>"""

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f'\n  Survey Jobs running at http://localhost:{port}\n')
    app.run(debug=False, host='0.0.0.0', port=port)
