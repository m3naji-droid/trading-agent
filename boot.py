"""
مُشغّل الوكيل مع التحديث الذاتي:
عند كل تشغيل يحمّل آخر نسخة من GitHub ثم يشغّلها.
إذا تعذر التحميل يستخدم آخر نسخة ناجحة، أو النسخة المضمّنة في الصورة.
"""
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.request

REPO = os.getenv("GITHUB_REPO", "m3naji-droid/trading-agent")
BRANCH = os.getenv("GITHUB_BRANCH", "main")
BAKED, LIVE = "/srv/baked", "/srv/live"
KEEP = ("app/", "static/", "requirements.txt")


def log(msg):
    print(f"[boot] {msg}", flush=True)


def get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "trading-agent", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_latest():
    c = json.loads(get(f"https://api.github.com/repos/{REPO}/commits/{BRANCH}"))
    sha, date = c["sha"], c["commit"]["committer"]["date"][:16].replace("T", " ")
    data = get(f"https://codeload.github.com/{REPO}/tar.gz/{sha}", timeout=120)
    tmp = LIVE + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for m in tar.getmembers():
            parts = m.name.split("/", 1)
            if len(parts) < 2 or not parts[1].startswith(KEEP):
                continue
            rel = os.path.normpath(parts[1])
            if rel.startswith("..") or os.path.isabs(rel) or not (m.isfile() or m.isdir()):
                continue
            dest = os.path.join(tmp, rel)
            if m.isdir():
                os.makedirs(dest, exist_ok=True)
            else:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "wb") as f:
                    f.write(tar.extractfile(m).read())
    if not os.path.exists(os.path.join(tmp, "app", "main.py")):
        raise RuntimeError("النسخة المحمّلة ناقصة")
    with open(os.path.join(tmp, "VERSION"), "w") as f:
        f.write(f"{sha[:7]} · {date}")
    shutil.rmtree(LIVE, ignore_errors=True)
    os.rename(tmp, LIVE)
    log(f"تم تحميل النسخة {sha[:7]}")


def ensure_requirements(root):
    req = os.path.join(root, "requirements.txt")
    if not os.path.exists(req):
        return
    h = hashlib.sha256(open(req, "rb").read()).hexdigest()
    mark = "/srv/.req_hash"
    if os.path.exists(mark) and open(mark).read() == h:
        return
    log("تثبيت المكتبات المطلوبة…")
    if subprocess.call([sys.executable, "-m", "pip", "install", "-q", "--no-cache-dir", "-r", req]) == 0:
        open(mark, "w").write(h)


def main():
    try:
        fetch_latest()
    except Exception as e:
        log(f"تعذر التحميل من GitHub ({e})")
    root = LIVE
    if not os.path.exists(os.path.join(LIVE, "app", "main.py")):
        log("استخدام النسخة المضمّنة")
        shutil.rmtree(LIVE, ignore_errors=True)
        shutil.copytree(BAKED, LIVE)
        with open(os.path.join(LIVE, "VERSION"), "w") as f:
            f.write("baked")
    ensure_requirements(root)
    os.chdir(root)
    os.execvp(sys.executable, [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"])


if __name__ == "__main__":
    main()
