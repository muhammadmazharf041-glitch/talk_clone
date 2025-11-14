# app.py — Fixed / Ready-to-run version
import os
import uuid
import json
import datetime
from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    send_file,
    session,
    jsonify,
)
from werkzeug.utils import secure_filename

# --------------------
# Optional imports (TTS, Google Drive)
# --------------------
HAS_TTS = False
tts = None
try:
    # Coqui TTS optional — only used if installed in environment
    from TTS.api import TTS

    HAS_TTS = True
except Exception:
    HAS_TTS = False

HAS_DRIVE = False
drive_service = None
MediaFileUpload = None
try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    HAS_DRIVE = True
except Exception:
    HAS_DRIVE = False

# --------------------
# Config (env vars fallback)
# --------------------
APP_SECRET = os.environ.get("APP_SECRET", "supersecretkey")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "mazhar6294")
DATA_SUBS = os.environ.get("DATA_SUBS", "subscriptions.json")
DATA_INV = os.environ.get("DATA_INV", "invoices.json")

UPLOAD_FOLDER = os.path.join("static", "uploads")
OUTPUT_FOLDER = os.path.join("static", "outputs")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# Optional Drive env vars (path on disk to service account json)
GOOGLE_SA_FILE = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")  # e.g. /home/user/service_account.json
DRIVE_FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID")  # Google Drive folder ID (optional)

# TTS model name env
TTS_MODEL_NAME = os.environ.get(
    "TTS_MODEL_NAME", "tts_models/multilingual/multi-dataset/xtts_v2"
)

# Flask app
app = Flask(__name__)
app.secret_key = APP_SECRET
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["OUTPUT_FOLDER"] = OUTPUT_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 300 * 1024 * 1024  # 300MB

# --------------------
# JSON helpers
# --------------------
def load_json(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


subscriptions = load_json(DATA_SUBS)
invoices = load_json(DATA_INV)

# --------------------
# Initialize optional services
# --------------------
if HAS_TTS:
    try:
        # Use cpu by default (change gpu=True if you have GPU)
        tts = TTS(model_name=TTS_MODEL_NAME, progress_bar=False, gpu=False)
        app.logger.info(f"TTS loaded: {TTS_MODEL_NAME}")
    except Exception as e:
        app.logger.warning("TTS available but failed to load: %s", e)
        tts = None

if HAS_DRIVE and GOOGLE_SA_FILE and DRIVE_FOLDER_ID:
    try:
        creds = service_account.Credentials.from_service_account_file(
            GOOGLE_SA_FILE, scopes=["https://www.googleapis.com/auth/drive"]
        )
        drive_service = build("drive", "v3", credentials=creds, cache_discovery=False)
        app.logger.info("Drive service ready")
    except Exception as e:
        app.logger.warning("Drive setup failed: %s", e)
        drive_service = None

# --------------------
# Drive uploader helper (returns webViewLink or None)
# --------------------
def upload_file_to_drive(local_path, filename):
    if not drive_service or not DRIVE_FOLDER_ID or MediaFileUpload is None:
        return None
    try:
        file_metadata = {"name": filename, "parents": [DRIVE_FOLDER_ID]}
        media = MediaFileUpload(local_path, resumable=True)
        created = drive_service.files().create(
            body=file_metadata, media_body=media, fields="id,webViewLink"
        ).execute()
        file_id = created.get("id")
        try:
            drive_service.permissions().create(
                fileId=file_id, body={"role": "reader", "type": "anyone"}
            ).execute()
        except Exception:
            pass
        return created.get("webViewLink")
    except Exception as e:
        app.logger.warning("Drive upload failed: %s", e)
        return None


# --------------------
# Token/device helpers
# --------------------
def gen_token():
    return uuid.uuid4().hex[:12]


def check_device_allowed(token, device_id):
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
    # Ensure templates/index.html exists in templates/
    return render_template("index.html")


@app.route("/plans")
def plans():
    plans = [
        {"id": "plan_monthly", "title": "Monthly Basic", "price": 5000, "voices": "50 voices"},
        {"id": "plan_3m", "title": "3 Months", "price": 18000, "voices": "150 voices"},
        {"id": "plan_year", "title": "Yearly", "price": 50000, "voices": "Unlimited (year)"},
    ]
    return render_template("plans.html", plans=plans)


@app.route("/talkclone", methods=["GET", "POST"])
def talkclone():
    if request.method == "POST":
        user_name = request.form.get("user_name", "").strip()
        contact = request.form.get("contact", "").strip()
        plan = request.form.get("plan", "").strip()
        proof = request.files.get("proof")
        if not user_name or not contact or not plan or not proof:
            flash("Please fill all fields and upload payment proof.", "danger")
            return redirect(url_for("talkclone"))

        iid = uuid.uuid4().hex[:10]
        filename = secure_filename(proof.filename)
        local_path = os.path.join(app.config["UPLOAD_FOLDER"], f"proof_{iid}_{filename}")
        proof.save(local_path)

        invoices[iid] = {
            "user_name": user_name,
            "contact": contact,
            "plan": plan,
            "amount": request.form.get("amount", "0"),
            "proof_path": local_path,
            "proof_url": None,
            "status": "pending",
            "created_at": datetime.datetime.now().isoformat(),
        }
        save_json(DATA_INV, invoices)
        flash(f"Invoice submitted. Your ID: {iid}. Admin will approve soon.", "info")
        return redirect(url_for("talkclone"))

    return render_template("talkclone.html")


@app.route("/api/check_token", methods=["POST"])
def api_check_token():
    data = request.get_json() or {}
    token = data.get("token")
    device = data.get("device_id")
    ok, msg = check_device_allowed(token, device)
    return jsonify({"ok": ok, "message": msg})


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


@app.route("/generate", methods=["POST"])
def generate():
    token = request.form.get("token")
    device = request.form.get("device_id", str(uuid.uuid4().hex)[:8])
    text = request.form.get("text", "").strip()
    language = request.form.get("language", "en")
    sp_file = request.files.get("speaker")
    if not text:
        flash("Please enter text to generate.", "danger")
        return redirect(url_for("talkclone"))

    allowed, msg = check_device_allowed(token, device)
    if not allowed:
        flash(msg, "danger")
        return redirect(url_for("talkclone"))

    speaker_path = None
    if sp_file and sp_file.filename:
        fname = secure_filename(sp_file.filename)
        speaker_path = os.path.join(app.config["UPLOAD_FOLDER"], f"sp_{uuid.uuid4().hex[:8]}_{fname}")
        sp_file.save(speaker_path)

    out_name = f"out_{uuid.uuid4().hex[:10]}.wav"
    out_path = os.path.join(app.config["OUTPUT_FOLDER"], out_name)

    if HAS_TTS and tts:
        try:
            kwargs = {"text": text, "file_path": out_path}
            if speaker_path:
                kwargs["speaker_wav"] = speaker_path
            if language:
                kwargs["language"] = language
            if hasattr(tts, "tts_to_file"):
                tts.tts_to_file(**kwargs)
            else:
                audio = tts.tts(**kwargs)
                with open(out_path, "wb") as f:
                    f.write(audio)
        except Exception as e:
            app.logger.error("TTS generation failed: %s", e)
            flash("TTS generation failed: " + str(e), "danger")
            return redirect(url_for("talkclone"))
    else:
        flash("Server: TTS engine not available. Install Coqui TTS or run locally.", "danger")
        return redirect(url_for("talkclone"))

    public_url = None
    try:
        public_url = upload_file_to_drive(out_path, out_name) if drive_service else None
    except Exception as e:
        app.logger.warning("Drive upload failed: %s", e)
        public_url = None

    register_device(token, device)
    return render_template("success.html", out_filename=out_name, out_path=out_path, public_url=public_url)


@app.route("/download/<fname>")
def download(fname):
    path = os.path.join(app.config["OUTPUT_FOLDER"], fname)
    if not os.path.exists(path):
        flash("File not found.", "danger")
        return redirect(url_for("index"))
    return send_file(path, as_attachment=True)


@app.route("/lapsync", methods=["GET", "POST"])
def lapsync():
    if request.method == "POST":
        media = request.files.get("media")
        audios = request.files.getlist("audios")
        if not media:
            flash("Upload a media file (video/image).", "danger")
            return redirect(url_for("lapsync"))
        mid = uuid.uuid4().hex[:10]
        media_fn = secure_filename(media.filename)
        media_path = os.path.join(app.config["UPLOAD_FOLDER"], f"media_{mid}_{media_fn}")
        media.save(media_path)
        audio_paths = []
        for a in audios:
            if a and a.filename:
                an = secure_filename(a.filename)
                ap = os.path.join(app.config["UPLOAD_FOLDER"], f"lap_audio_{uuid.uuid4().hex[:8]}_{an}")
                a.save(ap)
                audio_paths.append(ap)

        job_id = uuid.uuid4().hex[:10]
        invoices[job_id] = {
            "uploader_name": request.form.get("name", "anon"),
            "contact": request.form.get("contact", ""),
            "media": media_path,
            "audios": audio_paths,
            "status": "uploaded",
            "created_at": datetime.datetime.now().isoformat(),
        }
        save_json(DATA_INV, invoices)
        flash("LapSync job uploaded — processing queued (demo).", "info")
        return redirect(url_for("lapsync"))

    return render_template("lapsync.html")


# --------------------
# Admin (login, approve)
# --------------------
@app.route("/admin", methods=["GET", "POST"])
def admin():
    logged = session.get("admin_logged", False)
    if request.method == "POST" and not logged:
        pwd = request.form.get("password", "")
        if pwd == ADMIN_PASSWORD:
            session["admin_logged"] = True
            flash("Welcome Admin!", "success")
            return redirect(url_for("admin"))
        else:
            flash("Wrong password.", "danger")
            return redirect(url_for("admin"))
    if not logged:
        return render_template("admin.html", logged=False)

    q = request.args.get("search", "").lower()
    invs = invoices.copy()
    subs = subscriptions.copy()
    if q:
        invs = {k: v for k, v in invs.items() if q in k.lower() or q in str(v.get("user_name", "")).lower()}
        subs = {k: v for k, v in subs.items() if q in k.lower() or q in str(v.get("user_name", "")).lower()}
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

    proof_path = inv.get("proof_path")
    proof_url = None
    try:
        if proof_path and drive_service:
            proof_url = upload_file_to_drive(proof_path, os.path.basename(proof_path))
    except Exception as e:
        app.logger.warning("Proof upload fail: %s", e)
    inv["proof_url"] = proof_url

    subscriptions[token] = {
        "user_name": inv.get("user_name"),
        "contact": inv.get("contact"),
        "plan": inv.get("plan"),
        "amount": inv.get("amount"),
        "approved_at": datetime.datetime.now().isoformat(),
        "expires_at": (datetime.datetime.now() + datetime.timedelta(days=30)).isoformat(),
        "devices": [],
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
    # For local testing use: python app.py
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)