import os
import uuid
import json
import datetime
from flask import Flask, render_template, request, redirect, url_for, flash, send_file, session, jsonify
from werkzeug.utils import secure_filename

# Optional TTS import (if you have Coqui TTS installed). If not available, app will still run but TTS endpoints will raise helpful message.
try:
    from TTS.api import TTS
    HAS_TTS = True
except Exception:
    HAS_TTS = False

# Optional Google drive uploader (requires setup) - we'll provide helper later
try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    HAS_DRIVE = True
except Exception:
    HAS_DRIVE = False

# --------------------
# Config
# --------------------
APP_SECRET = os.environ.get("APP_SECRET", "supersecretkey")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "mazhar6294")
DATA_SUBS = "subscriptions.json"
DATA_INV = "invoices.json"

UPLOAD_FOLDER = os.path.join("static", "uploads")
OUTPUT_FOLDER = os.path.join("static", "outputs")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# Optional Drive env vars
GOOGLE_SA_FILE = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")  # path to service_account.json if used
DRIVE_FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID")  # optional

app = Flask(__name__)
app.secret_key = APP_SECRET
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['OUTPUT_FOLDER'] = OUTPUT_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 300 * 1024 * 1024  # 300MB limit

# --------------------
# Load data helpers
# --------------------
def load_json(path):
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except:
            return {}

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

subscriptions = load_json(DATA_SUBS)
invoices = load_json(DATA_INV)

# --------------------
# Optional: initialize TTS if available
# --------------------
TTS_MODEL_NAME = os.environ.get("TTS_MODEL_NAME", "tts_models/multilingual/multi-dataset/xtts_v2")
tts = None
if HAS_TTS:
    try:
        # instantiate TTS (this may download model the first time)
        tts = TTS(model_name=TTS_MODEL_NAME, progress_bar=False, gpu=False)
        print("TTS loaded:", TTS_MODEL_NAME)
    except Exception as e:
        print("TTS available but failed to load:", e)
        tts = None

# --------------------
# Optional Drive uploader
# --------------------
drive_service = None
if HAS_DRIVE and GOOGLE_SA_FILE and DRIVE_FOLDER_ID:
    try:
        creds = service_account.Credentials.from_service_account_file(GOOGLE_SA_FILE, scopes=["https://www.googleapis.com/auth/drive"])
        drive_service = build("drive", "v3", credentials=creds, cache_discovery=False)
        print("Drive service ready")
    except Exception as e:
        print("Drive setup failed:", e)
        drive_service = None

def upload_file_to_drive(local_path, filename):
    """Uploads file to DRIVE_FOLDER_ID if drive_service is configured. Returns file webViewLink or None."""
    if not drive_service or not DRIVE_FOLDER_ID:
        return None
    file_metadata = {"name": filename, "parents": [DRIVE_FOLDER_ID]}
    media = MediaFileUpload(local_path, resumable=True)
    file = drive_service.files().create(body=file_metadata, media_body=media, fields="id,webViewLink").execute()
    file_id = file.get("id")
    # make file shareable link if needed (the folder is expected to be shareable)
    try:
        drive_service.permissions().create(fileId=file_id, body={"role":"reader","type":"anyone"}).execute()
    except Exception:
        pass
    webview = file.get("webViewLink")
    return webview

# --------------------
# Utilities: token/device logic
# --------------------
def gen_token():
    return uuid.uuid4().hex[:12]

def check_device_allowed(token, device_id):
    # returns (allowed:bool, message:str)
    subs = subscriptions.get(token)
    if not subs:
        return False, "Invalid token."
    devices = subs.get("devices", [])
    if device_id in devices:
        return True, "Device already registered."
    if len(devices) >= 3:
        return False, "Device limit reached (3 devices)."
    return True, "Allowed"

def register_device(token, device_id):
    subs = subscriptions.get(token)
    if not subs:
        return False
    devices = subs.get("devices", [])
    if device_id not in devices:
        devices.append(device_id)
        subs["devices"] = devices
        save_json(DATA_SUBS, subscriptions)
    return True

# --------------------
# Routes
# --------------------
@app.route("/")
def index():
    return render_template("index.html")

# Plans page
@app.route("/plans")
def plans():
    # sample plans (edit prices as you like)
    plans = [
        {"id":"plan_monthly","title":"Monthly Basic","price":5000,"voices":"50 voices"},
        {"id":"plan_3m","title":"3 Months","price":18000,"voices":"150 voices"},
        {"id":"plan_year","title":"Yearly","price":50000,"voices":"Unlimited (year)"}
    ]
    return render_template("plans.html", plans=plans)

# Talk clone page
@app.route("/talkclone", methods=["GET","POST"])
def talkclone():
    if request.method == "POST":
        # user fills: user_name, contact, plan, proof file (image), device_token (optional)
        user_name = request.form.get("user_name","").strip()
        contact = request.form.get("contact","").strip()
        plan = request.form.get("plan","").strip()
        proof = request.files.get("proof")
        if not user_name or not contact or not plan or not proof:
            flash("Please fill all fields and upload payment proof.", "danger")
            return redirect(url_for("talkclone"))
        iid = uuid.uuid4().hex[:10]
        filename = secure_filename(proof.filename)
        local_path = os.path.join(app.config['UPLOAD_FOLDER'], f"proof_{iid}_{filename}")
        proof.save(local_path)
        # store invoice (status: pending)
        invoices[iid] = {
            "user_name": user_name,
            "contact": contact,
            "plan": plan,
            "amount": request.form.get("amount", "0"),
            "proof_path": local_path,
            "proof_url": None,
            "status": "pending",
            "created_at": datetime.datetime.now().isoformat()
        }
        save_json(DATA_INV, invoices)
        flash(f"Invoice submitted. Your ID: {iid}. Admin will approve soon.", "info")
        return redirect(url_for("talkclone"))

    # GET
    return render_template("talkclone.html")

# API to check token/device for client-side usage
@app.route("/api/check_token", methods=["POST"])
def api_check_token():
    data = request.get_json() or {}
    token = data.get("token")
    device = data.get("device_id")
    ok, msg = check_device_allowed(token, device)
    return jsonify({"ok": ok, "message": msg})

# Register device (on first use after token approved)
@app.route("/api/register_device", methods=["POST"])
def api_register_device():
    data = request.get_json() or {}
    token = data.get("token")
    device = data.get("device_id")
    allowed, msg = check_device_allowed(token, device)
    if not allowed:
        return jsonify({"ok": False, "message": msg}), 403
    register_device(token, device)
    return jsonify({"ok": True, "message": "Device registered."})

# Endpoint to generate voice (server-side)
@app.route("/generate", methods=["POST"])
def generate():
    # fields: token, device_id, text, language (optional), speaker_file (optional)
    token = request.form.get("token")
    device = request.form.get("device_id", str(uuid.uuid4().hex)[:8])
    text = request.form.get("text","").strip()
    language = request.form.get("language","en")
    sp_file = request.files.get("speaker")
    if not text:
        return redirect(url_for("talkclone"))

    # verify subscription token
    allowed, msg = check_device_allowed(token, device)
    if not allowed:
        flash(msg, "danger")
        return redirect(url_for("talkclone"))

    # save speaker if provided
    speaker_path = None
    if sp_file:
        fname = secure_filename(sp_file.filename)
        speaker_path = os.path.join(app.config['UPLOAD_FOLDER'], f"sp_{uuid.uuid4().hex[:8]}_{fname}")
        sp_file.save(speaker_path)

    # generate output name
    out_name = f"out_{uuid.uuid4().hex[:10]}.wav"
    out_path = os.path.join(app.config['OUTPUT_FOLDER'], out_name)

    # Generate using TTS if available
    if HAS_TTS and tts:
        try:
            # For multi-speaker models we should pass speaker_wav or speaker_idx and language
            kwargs = {"text": text, "file_path": out_path}
            if speaker_path:
                kwargs["speaker_wav"] = speaker_path
            if language:
                kwargs["language"] = language
            # tts.tts_to_file may differ by version; handle both
            if hasattr(tts, "tts_to_file"):
                tts.tts_to_file(**kwargs)
            else:
                # fallback using old api
                audio = tts.tts(**kwargs)
                with open(out_path, "wb") as f:
                    f.write(audio)
        except Exception as e:
            flash("TTS generation failed: " + str(e), "danger")
            return redirect(url_for("talkclone"))
    else:
        # TTS not available — inform user
        flash("Server: TTS engine not available. Install Coqui TTS or run locally.", "danger")
        return redirect(url_for("talkclone"))

    # optional: upload to Drive (if configured)
    public_url = None
    try:
        public_url = upload_file_to_drive(out_path, out_name) if drive_service else None
    except Exception as e:
        print("Drive upload failed:", e)
        public_url = None

    # Register this device (persist)
    register_device(token, device)

    # Return result page
    return render_template("success.html", out_filename=out_name, out_path=out_path, public_url=public_url)

# Serve download direct
@app.route("/download/<fname>")
def download(fname):
    path = os.path.join(app.config['OUTPUT_FOLDER'], fname)
    if not os.path.exists(path):
        flash("File not found.", "danger")
        return redirect(url_for("index"))
    return send_file(path, as_attachment=True)

# Lapsync upload page (basic upload only)
@app.route("/lapsync", methods=["GET","POST"])
def lapsync():
    if request.method == "POST":
        media = request.files.get("media")
        audios = request.files.getlist("audios")
        if not media:
            flash("Upload a media file (video/image).", "danger")
            return redirect(url_for("lapsync"))
        mid = uuid.uuid4().hex[:10]
        media_fn = secure_filename(media.filename)
        media_path = os.path.join(app.config['UPLOAD_FOLDER'], f"media_{mid}_{media_fn}")
        media.save(media_path)
        audio_paths = []
        for a in audios:
            if a and a.filename:
                an = secure_filename(a.filename)
                ap = os.path.join(app.config['UPLOAD_FOLDER'], f"lap_audio_{uuid.uuid4().hex[:8]}_{an}")
                a.save(ap)
                audio_paths.append(ap)
        # For now just save job record
        job_id = uuid.uuid4().hex[:10]
        # store in invoices as jobs for admin view
        invoices[job_id] = {"uploader_name": request.form.get("name","anon"), "contact": request.form.get("contact",""), "media": media_path, "audios": audio_paths, "status":"uploaded", "created_at": datetime.datetime.now().isoformat()}
        save_json(DATA_INV, invoices)
        flash("LapSync job uploaded — processing queued (demo).", "info")
        return redirect(url_for("lapsync"))
    return render_template("lapsync.html")

# --------------------
# Admin routes (login + dashboard + approve)
# --------------------
@app.route("/admin", methods=["GET","POST"])
def admin():
    logged = session.get("admin_logged", False)
    if request.method == "POST" and not logged:
        pwd = request.form.get("password","")
        if pwd == ADMIN_PASSWORD:
            session["admin_logged"] = True
            flash("Welcome Admin!", "success")
            return redirect(url_for("admin"))
        else:
            flash("Wrong password.", "danger")
            return redirect(url_for("admin"))
    if not logged:
        return render_template("admin.html", logged=False)
    # logged in -> show invoices & subscriptions
    q = request.args.get("search","").lower()
    invs = invoices.copy()
    subs = subscriptions.copy()
    if q:
        invs = {k:v for k,v in invs.items() if q in k.lower() or q in str(v.get("user_name","")).lower()}
        subs = {k:v for k,v in subs.items() if q in k.lower() or q in str(v.get("user_name","")).lower()}
    return render_template("admin.html", logged=True, invoices=invs, subs=subs, query=q)

@app.route("/admin/approve/<iid>", methods=["POST"])
def admin_approve(iid):
    if not session.get("admin_logged"):
        flash("Unauthorized", "danger")
        return redirect(url_for("admin"))
    inv = invoices.get(iid)
    if not inv:
        flash("Invoice not found", "danger")
        return redirect(url_for("admin"))
    token = gen_token()
    inv["status"] = "approved"
    inv["token"] = token
    # if we uploaded proof locally, we can optionally upload to drive
    proof_path = inv.get("proof_path")
    proof_url = None
    try:
        if proof_path and drive_service:
            proof_url = upload_file_to_drive(proof_path, os.path.basename(proof_path))
    except Exception as e:
        print("Proof upload fail:", e)
    inv["proof_url"] = proof_url
    # create subscription record
    subscriptions[token] = {
        "user_name": inv.get("user_name"),
        "contact": inv.get("contact"),
        "plan": inv.get("plan"),
        "amount": inv.get("amount"),
        "approved_at": datetime.datetime.now().isoformat(),
        "expires_at": (datetime.datetime.now() + datetime.timedelta(days=30)).isoformat(),
        "devices": []
    }
    save_json(DATA_INV, invoices)
    save_json(DATA_SUBS, subscriptions)
    flash(f"Approved {inv.get('user_name')} — token: {token}", "success")
    return redirect(url_for("admin"))

@app.route("/admin/logout")
def admin_logout():
    session.pop("admin_logged", None)
    flash("Logged out", "info")
    return redirect(url_for("admin"))

# --------------------
# Run
# --------------------
if __name__ == "__main__":
    # debug-friendly
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)