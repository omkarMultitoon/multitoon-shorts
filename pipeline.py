#!/usr/bin/env python3
"""
MultiToon Network - free daily Hindi cartoon Shorts pipeline.

Steps: story (Gemini free API) -> images (Cloudflare Workers AI free, fallback
Pollinations) -> Hindi voice (edge-tts, fallback gTTS) -> subtitles (ASS) ->
vertical 1080x1920 MP4 (ffmpeg) -> YouTube upload (official API) or a ready
"manual" package -> history, error log, phone notification (ntfy).

Usage:
  python pipeline.py --slot morning            # normal run (uses UPLOAD_MODE)
  python pipeline.py --slot evening --mode build_only
  python pipeline.py --slot morning --mode upload_test   # public API upload test
  python pipeline.py --selftest                # no keys needed, fake story/voice
"""
import argparse
import asyncio
import base64
import datetime as dt
import hashlib
import io
import json
import os
import random
import re
import subprocess
import sys
import time
import traceback
import urllib.parse
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
OUT = ROOT / "output"
MUSIC = ROOT / "music"
HISTORY = DATA / "history.json"
ERRLOG = DATA / "errors.log"
TOPICS = ROOT / "topics.txt"

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
SLOTS = {"morning": (10, 0), "evening": (19, 0)}   # IST publish times
W, H, FPS = 1080, 1920, 30
GAP = 0.2                                          # seconds between lines
MIN_SEC, MAX_SEC = 30.0, 45.0                      # target video length

VOICES = {"male": "hi-IN-MadhurNeural", "female": "hi-IN-SwaraNeural"}
CHANNEL = os.environ.get("CHANNEL_NAME", "MultiToon Network")

FALLBACK_TOPICS = [
    "a clumsy village animal friend causes a funny mix-up before a festival",
    "two siblings try to hide a broken vase and the plan goes comically wrong",
    "a clever little animal outsmarts a bigger bossy animal in a kind, funny way",
    "a magic object grants silly wishes and teaches a small lesson",
    "kids at school find out that honesty is better than a funny fib",
    "a family kitchen disaster turns into the best snack ever",
    "a lazy character learns that sharing makes work easy",
    "a tiny seed, a rainy day and two friends who learn patience",
]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def log(msg):
    print(f"[{dt.datetime.now(IST):%H:%M:%S}] {msg}", flush=True)


def run(cmd, cwd=None):
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, cwd=cwd)
    if r.returncode != 0:
        raise RuntimeError(f"command failed: {' '.join(str(c) for c in cmd[:4])}\n{r.stderr[-1800:]}")
    return r.stdout


def duration(path):
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=nw=1:nk=1", path])
    return float(out.strip())


def load_history():
    try:
        return json.loads(HISTORY.read_text(encoding="utf-8"))
    except Exception:
        return {"items": []}


def save_history(h):
    DATA.mkdir(exist_ok=True)
    HISTORY.write_text(json.dumps(h, ensure_ascii=False, indent=1), encoding="utf-8")


def log_error(slot, err):
    DATA.mkdir(exist_ok=True)
    stamp = dt.datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")
    with open(ERRLOG, "a", encoding="utf-8") as f:
        f.write(f"--- {stamp} slot={slot}\n{err[-1500:]}\n")


def notify(title, message, priority="default", tags=""):
    """Free phone push via ntfy.sh. Never include secrets here."""
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        return
    try:
        requests.post(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": priority, "Tags": tags},
            timeout=20,
        )
    except Exception as e:  # notification must never crash the run
        log(f"notify failed: {e}")


def gh_output(**kv):
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        for k, v in kv.items():
            f.write(f"{k}={v}\n")


def next_publish(slot):
    """Next 10:00 / 19:00 IST that is at least 20 minutes away."""
    hh, mm = SLOTS[slot]
    now = dt.datetime.now(IST)
    t = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if t - now < dt.timedelta(minutes=20):
        t += dt.timedelta(days=1)
    return t


def norm(s):
    return re.sub(r"[^\w]+", "", s.lower())


def pick_font():
    pref = os.environ.get("FONT_NAME")
    if pref:
        return pref
    try:
        fams = run(["fc-list", ":lang=hi", "family"]).splitlines()
    except Exception:
        fams = []
    names = {f.split(",")[0].strip() for f in fams if f.strip()}
    for want in ("Noto Sans Devanagari", "Lohit Devanagari", "FreeSans", "Mangal"):
        if want in names:
            return want
    return sorted(names)[0] if names else "Noto Sans Devanagari"


# --------------------------------------------------------------------------
# 1. story (Gemini free API)
# --------------------------------------------------------------------------
def choose_topic(history, slot_key):
    done = {i.get("topic") for i in history["items"]}
    if TOPICS.exists():
        for line in TOPICS.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and line not in done:
                return line
    rnd = random.Random(slot_key)
    return rnd.choice(FALLBACK_TOPICS)


def story_prompt(topic, recent, fix=""):
    return f"""You write original Hindi cartoon Shorts for the YouTube channel "{CHANNEL}".
Write ONE new story. Idea: {topic}

Rules:
- Spoken Hindi in Devanagari, 80 to 100 words in total across all lines (about 35-42 seconds read aloud).
- Very simple everyday Hindi, short funny dialogues, one funny moment, a tiny moral at the end.
- Family friendly and kids-safe: no violence, nothing scary, no brands, no real people,
  no copyrighted characters (for example Chhota Bheem, Motu Patlu, Doraemon, Peppa Pig).
- 2 or 3 original characters, 4 or 5 scenes, each scene has 1 to 3 spoken lines, each line at most 14 words.
- Every line has "voice": "male" or "female".
- "image_prompt": English, describes only that scene's picture, no text or letters in the picture.
- "style": ONE English sentence describing the art style and the exact look of every character
  (colors, clothes, animal type) so the pictures stay consistent.
- "title": Hindi, catchy, at most 60 characters, no hashtags.
- "tags": 10 to 14 short search keywords (Hindi and English mix).
- "hashtags": 5 to 7 hashtags starting with #, include #Shorts and #HindiCartoon.
Do not repeat these recent titles: {recent}
{fix}
Return ONLY valid JSON with exactly this shape:
{{"title": "...", "summary": "one Hindi sentence", "moral": "one Hindi sentence",
 "style": "...", "tags": ["..."], "hashtags": ["#Shorts"],
 "scenes": [{{"image_prompt": "...", "lines": [{{"voice": "male", "text": "..."}}]}}]}}"""


def gemini_json(prompt):
    key = os.environ["GEMINI_API_KEY"]
    model = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 1.0, "responseMimeType": "application/json"}}
    last = None
    for attempt in range(4):
        r = requests.post(url, headers={"x-goog-api-key": key}, json=body, timeout=120)
        if r.status_code in (429, 500, 503):
            last = f"HTTP {r.status_code}"
            time.sleep(15 * (attempt + 1))
            continue
        if r.status_code != 200:
            raise RuntimeError(f"Gemini error {r.status_code}: {r.text[:300]}")
        parts = r.json()["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
        return json.loads(text)
    raise RuntimeError(f"Gemini unavailable after retries ({last}). Daily free limit may be used up.")


def validate_story(s):
    probs = []
    try:
        scenes = s["scenes"]
        if not 4 <= len(scenes) <= 6:
            probs.append("use 4 or 5 scenes")
        lines = [l for sc in scenes for l in sc["lines"]]
        words = sum(len(l["text"].split()) for l in lines)
        if not 70 <= words <= 110:
            probs.append(f"total words is {words}, must be 80-100")
        if any(len(l["text"].split()) > 16 for l in lines):
            probs.append("a line is longer than 14 words")
        if not all(re.search(r"[ऀ-ॿ]", l["text"]) for l in lines):
            probs.append("every line must be in Devanagari Hindi")
        if any(l.get("voice") not in ("male", "female") for l in lines):
            probs.append('voice must be "male" or "female"')
        if not all(sc.get("image_prompt") for sc in scenes):
            probs.append("each scene needs image_prompt")
        for k in ("title", "style", "tags", "hashtags", "summary", "moral"):
            if not s.get(k):
                probs.append(f"missing {k}")
    except Exception as e:
        probs.append(f"bad structure: {e}")
    return probs


def make_story(topic, history, fake=False):
    if fake:
        return SELFTEST_STORY
    recent = "; ".join(i["title"] for i in history["items"][-30:]) or "none yet"
    fix = ""
    for attempt in range(3):
        s = gemini_json(story_prompt(topic, recent, fix))
        probs = validate_story(s)
        titles = {norm(i["title"]) for i in history["items"]}
        if norm(s.get("title", "")) in titles:
            probs.append("title already used, write a different story")
        if not probs:
            return s
        log(f"story problems: {probs}")
        fix = "Fix these problems from the last try: " + "; ".join(probs)
    raise RuntimeError("Could not get a valid story after 3 tries: " + "; ".join(probs))


# --------------------------------------------------------------------------
# 2. images (Cloudflare Workers AI free, fallback Pollinations)
# --------------------------------------------------------------------------
def to_jpeg(data, path):
    from PIL import Image
    im = Image.open(io.BytesIO(data)).convert("RGB")
    if min(im.size) < 400:
        raise RuntimeError("image too small")
    im.save(path, "JPEG", quality=92)


def cf_image(prompt, path):
    acct = os.environ["CF_ACCOUNT_ID"]
    model = os.environ.get("CF_IMAGE_MODEL", "@cf/black-forest-labs/flux-1-schnell")
    r = requests.post(
        f"https://api.cloudflare.com/client/v4/accounts/{acct}/ai/run/{model}",
        headers={"Authorization": f"Bearer {os.environ['CF_API_TOKEN']}"},
        json={"prompt": prompt[:1800], "steps": 6},
        timeout=180,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Cloudflare image {r.status_code}: {r.text[:200]}")
    if "json" in r.headers.get("content-type", ""):
        data = base64.b64decode(r.json()["result"]["image"])
    else:
        data = r.content
    to_jpeg(data, path)


def pollinations_image(prompt, path, seed):
    url = ("https://image.pollinations.ai/prompt/" + urllib.parse.quote(prompt[:900]) +
           f"?width=768&height=1344&nologo=true&seed={seed}")
    r = requests.get(url, timeout=180)
    if r.status_code != 200:
        raise RuntimeError(f"Pollinations {r.status_code}")
    to_jpeg(r.content, path)


def make_image(scene, style, path, seed, fake=False, idx=0):
    if fake:
        return fake_image(path, idx)
    prompt = (f"{style}. {scene['image_prompt']}. Bright colorful 2D cartoon illustration, "
              f"clean outlines, centered subject, no text, no letters, no watermark.")
    errs = []
    if os.environ.get("CF_ACCOUNT_ID") and os.environ.get("CF_API_TOKEN"):
        for attempt in range(2):
            try:
                return cf_image(prompt, path)
            except Exception as e:
                errs.append(f"cloudflare: {e}")
                time.sleep(5)
    for attempt in range(2):
        try:
            return pollinations_image(prompt, path, seed)
        except Exception as e:
            errs.append(f"pollinations: {e}")
            time.sleep(8)
    raise RuntimeError("All image sources failed: " + " | ".join(errs[-3:]))


# --------------------------------------------------------------------------
# 3. voice (edge-tts, fallback gTTS) - one file per line, exact timing
# --------------------------------------------------------------------------
async def _edge(text, voice, path):
    import edge_tts
    await edge_tts.Communicate(text, voice).save(str(path))


def tts_all(lines, wd, fake=False):
    paths = []
    if fake:
        for i, l in enumerate(lines):
            p = wd / f"line{i:02d}.wav"
            d = max(1.4, len(l["text"]) * 0.07)
            run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency={200 + 40 * (i % 4)}:duration={d}",
                 "-af", "volume=0.3", p])
            paths.append(p)
        return paths, "fake"
    try:
        for i, l in enumerate(lines):
            p = wd / f"line{i:02d}.mp3"
            for attempt in range(3):
                try:
                    asyncio.run(_edge(l["text"], VOICES[l["voice"]], p))
                    if p.stat().st_size < 2000:
                        raise RuntimeError("empty audio")
                    break
                except Exception:
                    if attempt == 2:
                        raise
                    time.sleep(3 * (attempt + 1))
            paths.append(p)
        return paths, "edge-tts"
    except Exception as e:
        log(f"edge-tts failed ({e}); using gTTS for the whole video")
    from gtts import gTTS
    paths = []
    for i, l in enumerate(lines):
        p = wd / f"line{i:02d}.mp3"
        for attempt in range(3):
            try:
                gTTS(text=l["text"], lang="hi").save(str(p))
                break
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(4 * (attempt + 1))
        paths.append(p)
    return paths, "gTTS"


# --------------------------------------------------------------------------
# 4. subtitles + video
# --------------------------------------------------------------------------
def ass_time(t):
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def build_ass(cues, font, path, total):
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Sub,{font},70,&H0000E5FF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,7,2,2,80,80,210,1
Style: Brand,{font},46,&H99FFFFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,3,0,8,60,60,80,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev = [f"Dialogue: 0,{ass_time(0)},{ass_time(total)},Brand,,0,0,0,,{CHANNEL}"]
    for s, e, text in cues:
        clean = text.replace("{", "(").replace("}", ")").replace("\n", " ")
        ev.append(f"Dialogue: 1,{ass_time(s)},{ass_time(e)},Sub,,0,0,0,,{clean}")
    Path(path).write_text(head + "\n".join(ev) + "\n", encoding="utf-8")


def scene_clip(img, dur, idx, out_name, wd):
    frames = max(2, int(round(dur * FPS)))
    zin = idx % 2 == 0
    z = f"1.0+0.10*on/{frames}" if zin else f"1.10-0.10*on/{frames}"
    fc = (
        "[0:v]split=2[bg][fg];"
        f"[bg]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
        "boxblur=30:5,eq=brightness=-0.10[b];"
        f"[fg]scale={W}:-2:flags=lanczos[f];"
        "[b][f]overlay=(W-w)/2:(H-h)/2,scale=1620:2880,"
        f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}:fps={FPS}[v]"
    )
    run(["ffmpeg", "-y", "-loop", "1", "-framerate", FPS, "-t", f"{dur:.3f}", "-i", img,
         "-filter_complex", fc, "-map", "[v]", "-frames:v", frames, "-r", FPS,
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", out_name],
        cwd=wd)


def pick_music(key):
    # music can sit in a "music" folder OR in the main folder with a name starting "music"
    pool = list(MUSIC.glob("*")) + list(ROOT.glob("music*.*"))
    files = sorted(p for p in pool if p.is_file() and p.suffix.lower() in (".mp3", ".wav", ".m4a", ".ogg"))
    if not files:
        return None
    return files[int(hashlib.md5(key.encode()).hexdigest(), 16) % len(files)]


def build_video(story, voice_paths, img_paths, key, out_dir, font):
    wd = out_dir / "work"
    wd.mkdir(parents=True, exist_ok=True)
    flat = [l for sc in story["scenes"] for l in sc["lines"]]
    durs = [duration(p) for p in voice_paths]

    # timeline: line i starts at cum; scene length = sum of its lines
    cues, scene_len, cum, li = [], [], 0.0, 0
    for sc in story["scenes"]:
        start = cum
        for _ in sc["lines"]:
            cues.append((cum, cum + durs[li], flat[li]["text"]))
            cum += durs[li] + GAP
            li += 1
        scene_len.append(cum - start)
    scene_len[-1] += 0.6
    total = cum + 0.6

    # voice track
    inputs, chains = [], []
    for i, p in enumerate(voice_paths):
        inputs += ["-i", p]
        chains.append(f"[{i}:a]aresample=44100,aformat=channel_layouts=mono,apad=pad_dur={GAP}[a{i}]")
    cat = "".join(f"[a{i}]" for i in range(len(voice_paths)))
    fc = ";".join(chains) + f";{cat}concat=n={len(voice_paths)}:v=0:a=1,loudnorm=I=-16:TP=-1.5:LRA=11[v]"
    run(["ffmpeg", "-y", *inputs, "-filter_complex", fc, "-map", "[v]", "-ar", "44100", "voice.wav"], cwd=wd)

    # scene clips + concat
    names = []
    for i, (img, d) in enumerate(zip(img_paths, scene_len)):
        n = f"scene{i}.mp4"
        scene_clip(img, d, i, n, wd)
        names.append(n)
    (wd / "list.txt").write_text("".join(f"file '{n}'\n" for n in names))
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "list.txt", "-c", "copy", "silent.mp4"], cwd=wd)

    # subtitles
    build_ass(cues, font, wd / "subs.ass", total)

    # final: burn subs + mix voice (+ music)
    music = pick_music(key)
    fonts_dir = os.environ.get("FONTS_DIR", "/usr/share/fonts")
    vf = f"ass=subs.ass:fontsdir={fonts_dir}"
    final = out_dir / "video.mp4"
    cmd = ["ffmpeg", "-y", "-i", "silent.mp4", "-i", "voice.wav"]
    if music:
        cmd += ["-stream_loop", "-1", "-i", music]
        af = (f"[2:a]volume=0.10,afade=t=in:d=1,afade=t=out:st={max(0, total - 1.5):.2f}:d=1.5[m];"
              "[1:a][m]amix=inputs=2:duration=first:normalize=0[a]")
    else:
        af = "[1:a]anull[a]"
    cmd += ["-filter_complex", f"[0:v]{vf}[v];{af}", "-map", "[v]", "-map", "[a]",
            "-t", f"{total:.3f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", final]
    run(cmd, cwd=wd)
    return final, total


# --------------------------------------------------------------------------
# 5. metadata + YouTube upload
# --------------------------------------------------------------------------
def make_meta(story):
    tags = []
    size = 0
    for t in story["tags"]:
        t = str(t).replace("<", "").replace(">", "").strip()
        if t and size + len(t) < 420:
            tags.append(t)
            size += len(t) + 1
    hashtags = " ".join(h if h.startswith("#") else "#" + h for h in story["hashtags"][:7])
    if "#shorts" not in hashtags.lower():
        hashtags = "#Shorts " + hashtags
    title = story["title"].strip()[:80] + " #Shorts"
    desc = (f"{story['summary']}\n\n{story['moral']}\n\n"
            f"👍 पसंद आए तो Like करें और {CHANNEL} को Subscribe करें 🔔\n\n{hashtags}")
    return {"title": title, "description": desc, "tags": tags}


def youtube_client():
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    creds = Credentials(
        None,
        refresh_token=os.environ["YT_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["YT_CLIENT_ID"],
        client_secret=os.environ["YT_CLIENT_SECRET"],
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def upload_youtube(video, meta, publish_dt=None):
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload
    yt = youtube_client()
    status = {"selfDeclaredMadeForKids": os.environ.get("MADE_FOR_KIDS", "false").lower() == "true"}
    if publish_dt:
        status["privacyStatus"] = "private"
        status["publishAt"] = publish_dt.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        status["privacyStatus"] = "public"
    body = {"snippet": {"title": meta["title"], "description": meta["description"],
                        "tags": meta["tags"], "categoryId": os.environ.get("YT_CATEGORY", "24"),
                        "defaultLanguage": "hi", "defaultAudioLanguage": "hi"},
            "status": status}
    media = MediaFileUpload(str(video), mimetype="video/mp4", resumable=True, chunksize=4 * 1024 * 1024)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp, tries = None, 0
    while resp is None:
        try:
            _, resp = req.next_chunk()
        except HttpError as e:
            if e.resp.status in (500, 502, 503, 504) and tries < 5:
                tries += 1
                time.sleep(2 ** tries)
                continue
            raise
    return resp["id"]


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", choices=list(SLOTS), default="morning")
    ap.add_argument("--mode", choices=["normal", "build_only", "upload_test"], default="normal")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    fake = a.selftest
    mode = "build_only" if fake else a.mode
    upload_mode = os.environ.get("UPLOAD_MODE", "manual").lower()   # api | manual

    history = load_history()
    pub = next_publish(a.slot)
    key = f"{pub:%Y-%m-%d}-{a.slot}"
    if mode == "normal":
        if any(i["slot_key"] == key and i["status"] in ("uploaded", "scheduled", "ready") for i in history["items"]):
            log(f"{key} already done - skipping (duplicate protection)")
            gh_output(release="no", status="skipped")
            return
    out_dir = OUT / ("selftest" if fake else (key if mode == "normal" else f"test-{a.slot}"))
    if out_dir.exists():
        subprocess.run(["rm", "-rf", str(out_dir)])
    out_dir.mkdir(parents=True)

    try:
        topic = "selftest" if fake else choose_topic(history, key)
        log(f"slot={key} topic={topic}")
        story = None
        for attempt in range(3):
            story = make_story(topic, history, fake)
            wd = out_dir / "work"
            wd.mkdir(exist_ok=True)
            flat = [l for sc in story["scenes"] for l in sc["lines"]]
            vpaths, engine = tts_all(flat, wd, fake)
            total = sum(duration(p) + GAP for p in vpaths) + 0.6
            log(f"voice={engine} length={total:.1f}s")
            if fake or MIN_SEC <= total <= MAX_SEC or attempt == 2:
                break
            log("length outside 30-45s, asking for a new story")
            time.sleep(4)
        if not fake and not (25 <= total <= 59):
            raise RuntimeError(f"Video length {total:.0f}s is outside the allowed range")

        imgs = []
        for i, sc in enumerate(story["scenes"]):
            p = out_dir / f"scene{i}.jpg"
            make_image(sc, story["style"], p, seed=int(hashlib.md5(key.encode()).hexdigest(), 16) % 10**6,
                       fake=fake, idx=i)
            imgs.append(p)
            log(f"image {i + 1}/{len(story['scenes'])} ready")

        video, total = build_video(story, vpaths, imgs, key, out_dir, pick_font())
        meta = make_meta(story)
        (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        (out_dir / "upload_notes.txt").write_text(
            f"TITLE:\n{meta['title']}\n\nDESCRIPTION:\n{meta['description']}\n\nTAGS:\n{', '.join(meta['tags'])}\n",
            encoding="utf-8")
        log(f"video built: {video} ({total:.1f}s)")

        status, vid = "ready", None
        if mode == "normal" and upload_mode == "api":
            vid = upload_youtube(video, meta, pub)
            status = "scheduled"
            notify("MultiToon: scheduled", f"{pub:%d %b %H:%M} IST\n{meta['title']}\nhttps://youtu.be/{vid}", tags="white_check_mark")
        elif mode == "upload_test":
            vid = upload_youtube(video, meta, None)
            status = "test_uploaded"
            notify("MultiToon: upload test", f"Open YouTube Studio and check video {vid} for a lock notice.\nhttps://youtu.be/{vid}")
        elif mode == "normal":
            notify("MultiToon: video ready", f"Upload for {pub:%d %b %H:%M} IST: {meta['title']}", tags="movie_camera")

        if mode == "normal":
            history["items"].append({"slot_key": key, "topic": topic, "title": story["title"],
                                     "status": status, "video_id": vid,
                                     "created": dt.datetime.now(IST).isoformat(timespec="minutes")})
            save_history(history)
        gh_output(release="yes" if (upload_mode == "manual" or mode == "build_only") and mode != "upload_test" else "no",
                  dir=str(out_dir), tag=f"short-{key}" if mode == "normal" else f"test-{a.slot}-{int(time.time())}",
                  title=meta["title"].replace("%", ""), status=status)
        log(f"done: {status}")
    except Exception as e:
        err = f"{type(e).__name__}: {e}\n{traceback.format_exc()[-900:]}"
        log("FAILED: " + str(e)[:500])
        if mode == "normal":
            log_error(a.slot, err)
        notify("MultiToon: FAILED", f"{key}: {str(e)[:300]}", priority="high", tags="rotating_light")
        gh_output(release="no", status="failed")
        sys.exit(1)


# --------------------------------------------------------------------------
# self-test data (no keys, no internet needed except ffmpeg)
# --------------------------------------------------------------------------
SELFTEST_STORY = {
    "title": "चिंटू और जादुई आम",
    "summary": "चिंटू बंदर को सबसे बड़ा आम चाहिए।",
    "moral": "हर काम ताक़त से नहीं, तरीके से होता है।",
    "style": "2D cartoon, brown monkey with red vest, orange squirrel",
    "tags": ["hindi cartoon", "hindi kahani", "funny cartoon"],
    "hashtags": ["#Shorts", "#HindiCartoon"],
    "scenes": [
        {"image_prompt": "monkey under mango tree", "lines": [
            {"voice": "male", "text": "चिंटू बंदर को पेड़ का सबसे बड़ा आम चाहिए था।"},
            {"voice": "female", "text": "चिंकी बोली, अरे वाह, कितना बड़ा आम!"}]},
        {"image_prompt": "monkey falls", "lines": [
            {"voice": "male", "text": "वो चढ़ा, फिसला, और धड़ाम! तीन बार गिरा।"}]},
        {"image_prompt": "squirrel points", "lines": [
            {"voice": "female", "text": "चिंटू, चढ़ने की ज़रूरत नहीं, डाल हिलाओ!"},
            {"voice": "male", "text": "अच्छा, ये तरीका भी है क्या?"}]},
        {"image_prompt": "mango on head", "lines": [
            {"voice": "male", "text": "चिंटू ने डाल हिलाई, और आम सीधा उसके सिर पर!"},
            {"voice": "female", "text": "हर काम ताक़त से नहीं, तरीके से होता है।"}]},
    ],
}


def fake_image(path, idx):
    from PIL import Image, ImageDraw
    cols = [(255, 190, 90), (120, 200, 255), (160, 230, 140), (255, 140, 160)]
    im = Image.new("RGB", (1024, 1024), cols[idx % 4])
    d = ImageDraw.Draw(im)
    d.ellipse((312, 250, 712, 650), fill=(139, 90, 43), outline=(0, 0, 0), width=8)
    d.ellipse((410, 400, 450, 440), fill="white")
    d.ellipse((574, 400, 614, 440), fill="white")
    d.arc((420, 470, 604, 600), 20, 160, fill="black", width=8)
    d.rectangle((0, 800, 1024, 1024), fill=(90, 160, 70))
    im.save(path, "JPEG", quality=90)


if __name__ == "__main__":
    main()
