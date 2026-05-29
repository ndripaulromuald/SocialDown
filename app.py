from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import yt_dlp
import os
import tempfile
import threading
import time
import uuid
import re

app = Flask(__name__)
CORS(app)

DOWNLOAD_DIR = tempfile.mkdtemp()
download_cache = {}  # {task_id: {"status": ..., "file": ..., "title": ..., "error": ...}}


# ─── Nettoyage automatique des fichiers > 10 min ───────────────────────────────
def clean_old_files():
    while True:
        time.sleep(300)
        now = time.time()
        to_delete = []
        for tid, data in list(download_cache.items()):
            if now - data.get("created_at", now) > 600:
                fpath = data.get("file")
                if fpath and os.path.exists(fpath):
                    os.remove(fpath)
                to_delete.append(tid)
        for tid in to_delete:
            download_cache.pop(tid, None)


threading.Thread(target=clean_old_files, daemon=True).start()


# ─── Détection du réseau social depuis l'URL ──────────────────────────────────
PLATFORM_PATTERNS = {
    "TikTok":      r"tiktok\.com|vm\.tiktok\.com|vt\.tiktok\.com",
    "YouTube":     r"youtube\.com|youtu\.be",
    "Instagram":   r"instagram\.com|instagr\.am",
    "Facebook":    r"facebook\.com|fb\.watch|fb\.com",
    "Twitter/X":   r"twitter\.com|x\.com|t\.co",
    "Snapchat":    r"snapchat\.com|story\.snapchat\.com",
    "Pinterest":   r"pinterest\.(com|fr|ca|co\.uk)",
    "Reddit":      r"reddit\.com|redd\.it",
    "Twitch":      r"twitch\.tv|clips\.twitch\.tv",
    "Dailymotion": r"dailymotion\.com|dai\.ly",
    "Vimeo":       r"vimeo\.com",
    "SoundCloud":  r"soundcloud\.com",
    "Tumblr":      r"tumblr\.com",
    "LinkedIn":    r"linkedin\.com",
    "Bilibili":    r"bilibili\.com|b23\.tv",
    "Likee":       r"likee\.video|l\.likee\.video",
    "Triller":     r"triller\.co",
    "Kwai":        r"kwai\.com|kwaicorp\.com",
}


def detect_platform(url: str) -> str:
    for name, pattern in PLATFORM_PATTERNS.items():
        if re.search(pattern, url, re.IGNORECASE):
            return name
    return "Web"


def is_valid_url(url: str) -> bool:
    return bool(re.match(r"https?://\S+", url))


# ─── Téléchargement en arrière-plan ───────────────────────────────────────────
def do_download(task_id: str, url: str, quality: str):
    out_template = os.path.join(DOWNLOAD_DIR, f"{task_id}_%(title).60s.%(ext)s")

    ydl_opts = {
        "outtmpl": out_template,
        "quiet": True,
        "no_warnings": True,
        # Contourner les restrictions géographiques basiques
        "geo_bypass": True,
        # Limite de taille : 500 Mo pour éviter les abus
        "max_filesize": 500 * 1024 * 1024,
    }

    if quality == "audio":
        ydl_opts["format"] = "bestaudio/best"
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    elif quality == "720p":
        ydl_opts["format"] = (
            "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]"
            "/bestvideo[height<=720]+bestaudio/best[height<=720]/best"
        )
    elif quality == "480p":
        ydl_opts["format"] = (
            "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]"
            "/bestvideo[height<=480]+bestaudio/best[height<=480]/best"
        )
    else:
        # "best" par défaut
        ydl_opts["format"] = (
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
        )

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get("title", "video")
            platform = detect_platform(url)

        # Trouver le fichier généré
        for fname in os.listdir(DOWNLOAD_DIR):
            if fname.startswith(task_id):
                fpath = os.path.join(DOWNLOAD_DIR, fname)
                download_cache[task_id].update({
                    "status": "done",
                    "file": fpath,
                    "filename": fname.replace(f"{task_id}_", ""),
                    "title": title,
                    "platform": platform,
                })
                return

        download_cache[task_id].update({
            "status": "error",
            "error": "Fichier introuvable après téléchargement.",
        })

    except yt_dlp.utils.UnsupportedError:
        download_cache[task_id].update({
            "status": "error",
            "error": "Ce site n'est pas supporté par yt-dlp.",
        })
    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        # Simplifier les messages d'erreur courants
        if "Private video" in msg or "private" in msg.lower():
            msg = "Cette vidéo est privée."
        elif "age" in msg.lower():
            msg = "Cette vidéo est restreinte par âge."
        elif "removed" in msg.lower() or "deleted" in msg.lower():
            msg = "Cette vidéo a été supprimée."
        download_cache[task_id].update({"status": "error", "error": msg})
    except Exception as e:
        download_cache[task_id].update({"status": "error", "error": str(e)})


# ─── Routes API ───────────────────────────────────────────────────────────────
@app.route("/")
def index():
    with open(os.path.join(os.path.dirname(__file__), "static", "index.html"), encoding="utf-8") as f:
        return f.read()


@app.route("/api/detect", methods=["POST"])
def detect():
    """Détecte le réseau social avant de lancer le téléchargement."""
    data = request.json or {}
    url = (data.get("url") or "").strip()
    if not url or not is_valid_url(url):
        return jsonify({"error": "URL invalide."}), 400
    return jsonify({"platform": detect_platform(url)})


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.json or {}
    url = (data.get("url") or "").strip()
    quality = data.get("quality", "best")

    if not url:
        return jsonify({"error": "URL manquante."}), 400
    if not is_valid_url(url):
        return jsonify({"error": "URL invalide. Elle doit commencer par http:// ou https://"}), 400

    task_id = str(uuid.uuid4())[:8]
    download_cache[task_id] = {
        "status": "processing",
        "file": None,
        "title": None,
        "platform": detect_platform(url),
        "error": None,
        "created_at": time.time(),
    }

    t = threading.Thread(target=do_download, args=(task_id, url, quality))
    t.daemon = True
    t.start()

    return jsonify({"task_id": task_id, "platform": download_cache[task_id]["platform"]})


@app.route("/api/status/<task_id>")
def check_status(task_id):
    task = download_cache.get(task_id)
    if not task:
        return jsonify({"error": "Tâche introuvable."}), 404
    return jsonify({
        "status": task["status"],
        "title": task.get("title"),
        "platform": task.get("platform"),
        "error": task.get("error"),
    })


@app.route("/api/file/<task_id>")
def get_file(task_id):
    task = download_cache.get(task_id)
    if not task or task["status"] != "done" or not task.get("file"):
        return jsonify({"error": "Fichier non disponible."}), 404
    return send_file(
        task["file"],
        as_attachment=True,
        download_name=task.get("filename", "video.mp4"),
    )


if __name__ == "__main__":
    print("🚀 Social Downloader lancé sur http://localhost:5000")
    app.run(debug=False, port=5000)
