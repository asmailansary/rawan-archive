#!/usr/bin/env python3
"""
العامل (Worker): يفحص حسابات تيك توك وتيلونيم، ويحفظ الجديد فقط في Supabase.
يعمل تلقائياً عبر GitHub Actions (انظر .github/workflows/save.yml).

المتغيرات المطلوبة:
    SUPABASE_URL, SUPABASE_SERVICE_KEY
اختياري:
    TIKTOK_COOKIES_FILE  (مسار ملف cookies.txt)
"""
import json
import mimetypes
import os
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests
import yt_dlp

SB_URL = os.environ["SUPABASE_URL"].rstrip("/")
SB_KEY = os.environ["SUPABASE_SERVICE_KEY"]
BUCKET = "archive"
COOKIES = os.environ.get("TIKTOK_COOKIES_FILE")
APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "")
APIFY_ACTOR = "clockworks~tiktok-profile-scraper"
APIFY_ITEMS = {}   # id -> بيانات المنشور كما أرجعتها Apify (تُستخدم لمنشورات الصور)
RESULTS = 5     # عدد أحدث المنشورات التي تُطلب من Apify لكل حساب في كل تشغيل
H = {"apikey": SB_KEY}
if not SB_KEY.startswith("sb_"):  # المفاتيح القديمة (JWT) تُرسل أيضاً في Authorization
    H["Authorization"] = f"Bearer {SB_KEY}"

errors = 0      # أخطاء حقيقية: تجعل التشغيل يظهر ❌
warnings = 0    # تنبيهات: حساب بلا منشورات، أو موقع يحجب GitHub
saved = 0       # عدد الفيديوهات المحفوظة في هذا التشغيل
skipped = 0     # فيديوهات تم تخطيها لأنها ليست لهذا الحساب
nofile = 0      # منشورات بلا ملف فيديو (صور)


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def errors_add(e) -> None:
    global errors
    errors += 1
    log(f"خطأ: {e}")
    note("error", "Purge", e)


def note(level: str, title: str, msg) -> None:
    """يكتب رسالة تظهر ضمن تنبيهات التشغيل في GitHub (level: notice | warning | error)."""
    text = " ".join(str(msg).split())[:900]
    print(f"::{level} title={title}::{text}", flush=True)


# ------------------------- Supabase -------------------------
def existing_ids(table: str, account: str) -> set:
    ids, offset = set(), 0
    while True:
        r = requests.get(
            f"{SB_URL}/rest/v1/{table}",
            headers=H,
            params={"select": "id", "account": f"eq.{account}", "limit": 1000, "offset": offset},
            timeout=60,
        )
        r.raise_for_status()
        rows = r.json()
        ids.update(x["id"] for x in rows)
        if len(rows) < 1000:
            return ids
        offset += 1000


def insert(table: str, rows: list) -> None:
    if not rows:
        return
    r = requests.post(
        f"{SB_URL}/rest/v1/{table}",
        headers={**H, "Content-Type": "application/json",
                 "Prefer": "resolution=ignore-duplicates,return=minimal"},
        data=json.dumps(rows),
        timeout=60,
    )
    r.raise_for_status()


def upload(local: Path, remote: str) -> str:
    ctype = mimetypes.guess_type(str(local))[0] or "application/octet-stream"
    with open(local, "rb") as f:
        r = requests.post(
            f"{SB_URL}/storage/v1/object/{BUCKET}/{quote(remote)}",
            headers={**H, "Content-Type": ctype, "x-upsert": "true"},
            data=f,
            timeout=900,
        )
    r.raise_for_status()
    return remote


# ------------------------- تيك توك -------------------------
def list_tiktok_ids(user: str):
    """يرجع (قائمة المعرفات، نص الخطأ إن وُجد)."""
    opts = {"extract_flat": True, "quiet": True, "skip_download": True}
    if COOKIES:
        opts["cookiefile"] = COOKIES
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://www.tiktok.com/@{user}", download=False)
    except Exception as e:  # noqa: BLE001
        return [], str(e)
    ids = [e["id"] for e in (info or {}).get("entries", []) if e and e.get("id")]
    return ids, ("" if ids else "yt-dlp لم يُرجع أي عناصر")


def list_tiktok_ids_apify(user: str, limit: int):
    """يجلب أحدث منشورات الحساب عبر خدمة Apify (تستخدم عناوين لا يحجبها تيك توك)."""
    try:
        r = requests.post(
            f"https://api.apify.com/v2/acts/{APIFY_ACTOR}/run-sync-get-dataset-items",
            params={"token": APIFY_TOKEN},
            json={"profiles": [user], "resultsPerPage": limit,
                  "profileScrapeSections": ["videos"], "profileSorting": "latest",
                  "excludePinnedPosts": False,
                  "shouldDownloadVideos": False, "shouldDownloadCovers": False,
                  "shouldDownloadSlideshowImages": True},
            timeout=330,
        )
        if r.status_code >= 400:
            return [], f"Apify HTTP {r.status_code}: {r.text[:300]}"
        items = r.json()
    except Exception as e:  # noqa: BLE001
        return [], f"Apify error: {e}"
    ids = []
    for it in items if isinstance(items, list) else []:
        author = ((it.get("authorMeta") or {}).get("name") or "").lower()
        vid = str(it.get("id") or "")
        if vid.isdigit() and (not author or author == user.lower()):
            ids.append(vid)
            APIFY_ITEMS[vid] = it
    ids = sorted(set(ids), key=int, reverse=True)
    if ids:
        return ids, ""
    if isinstance(items, list) and items:
        first = items[0] if isinstance(items[0], dict) else {"value": items[0]}
        rest = {k: v for k, v in first.items() if k != "authorMeta"}   # بيانات الملف الشخصي لا تهمنا
        am = first.get("authorMeta") or {}
        pick = {k: am.get(k) for k in ("name", "verified", "privateAccount", "video", "videos", "fans", "heart", "following", "region", "commerceUserInfo") if k in am}
        sample = f"profile={json.dumps(pick, ensure_ascii=False)[:300]} rest={json.dumps(rest, ensure_ascii=False)[:250]}"
    else:
        sample = str(items)[:450]
    return [], f"Apify أرجع {len(items) if isinstance(items, list) else '?'} عنصراً بلا منشورات صالحة. أول عنصر: {sample}"


def list_tiktok_ids_browser(user: str):
    """بديل عند فشل yt-dlp: متصفح حقيقي يفتح الملف الشخصي ويجمع معرّفات الفيديوهات."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:  # noqa: BLE001
        return [], f"playwright غير مثبت: {e}"
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox"])
            ctx = browser.new_context(user_agent=ua, locale="en-US", viewport={"width": 1280, "height": 900})
            page = ctx.new_page()
            api_ids = set()

            def on_response(resp):
                # فقط منشورات الملف الشخصي، لا قائمة "الموصى بها"
                if "/api/post/item_list" in resp.url:
                    try:
                        for it in (resp.json().get("itemList") or []):
                            au = ((it.get("author") or {}).get("uniqueId") or "").lower()
                            if it.get("id") and (not au or au == user.lower()):
                                api_ids.add(str(it["id"]))
                    except Exception:  # noqa: BLE001
                        pass

            page.on("response", on_response)
            page.goto(f"https://www.tiktok.com/@{user}", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(5000)
            title = page.title()
            for _ in range(15):
                page.mouse.wheel(0, 5000)
                page.wait_for_timeout(1500)
            hrefs = page.eval_on_selector_all('a[href*="/video/"]', "els => els.map(e => e.href)")
            dom_ids = {m.group(1) for h in hrefs
                       for m in [re.search(rf"/@{re.escape(user)}/video/(\d+)", h, re.I)] if m}
            body = " ".join((page.inner_text("body") or "").split())[:160]
            embed_ids, embed_len = set(), 0
            try:
                page.goto(f"https://www.tiktok.com/embed/@{user}", wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(4000)
                html = page.content()
                embed_len = len(html)
                embed_ids = set(re.findall(rf"/@{re.escape(user)}/video/(\d+)", html, re.I))
            except Exception:  # noqa: BLE001
                pass
            browser.close()
        ids = sorted(api_ids | dom_ids | embed_ids, key=int, reverse=True)  # الأحدث أولاً
        return ids, f"title={title!r} api={len(api_ids)} dom={len(dom_ids)} embed={len(embed_ids)} embed_html={embed_len} body={body!r}"
    except Exception as e:  # noqa: BLE001
        return [], f"browser error: {e}"


def remove_posts(rows: list) -> None:
    """يحذف سجلات tiktok_posts مع ملفاتها (الفيديو والصورة المصغرة) من التخزين."""
    for k in range(0, len(rows), 50):
        chunk = rows[k:k + 50]
        paths = [p for row in chunk for p in (row.get("video_path"), row.get("thumb_path")) if p]
        if paths:
            requests.delete(f"{SB_URL}/storage/v1/object/{BUCKET}",
                            headers={**H, "Content-Type": "application/json"},
                            data=json.dumps({"prefixes": paths}), timeout=120).raise_for_status()
        ids = ",".join(row["id"] for row in chunk)
        requests.delete(f"{SB_URL}/rest/v1/tiktok_posts", headers={**H, "Prefer": "return=minimal"},
                        params={"id": f"in.({ids})"}, timeout=60).raise_for_status()


def purge_account(user: str) -> None:
    """حذف كل ما حُفظ لهذا الحساب (يُستخدم يدوياً لمرة واحدة عبر خيار purge)."""
    rows, offset = [], 0
    while True:
        r = requests.get(f"{SB_URL}/rest/v1/tiktok_posts", headers=H,
                         params={"select": "id,video_path,thumb_path", "account": f"eq.{user}",
                                 "limit": 1000, "offset": offset}, timeout=60)
        r.raise_for_status()
        batch = r.json()
        rows.extend(batch)
        if len(batch) < 1000:
            break
        offset += 1000
    remove_posts(rows)
    log(f"[تنظيف] حُذف {len(rows)} سجل لـ @{user}")
    note("notice", f"Purge @{user}", f"removed {len(rows)} rows")


def cleanup_foreign(user: str) -> None:
    """يحذف سجلات سبق حفظها خطأً: فيديوهات يملكها حساب آخر لكن نُسبت لهذا الحساب."""
    bad, offset = [], 0
    while True:
        r = requests.get(
            f"{SB_URL}/rest/v1/tiktok_posts",
            headers=H,
            params={"select": "id,video_path,thumb_path,info", "account": f"eq.{user}",
                    "limit": 1000, "offset": offset},
            timeout=60,
        )
        r.raise_for_status()
        rows = r.json()
        for row in rows:
            wp = (row.get("info") or {}).get("webpage_url") or ""
            m = re.search(r"/@([^/]+)/video/", wp)
            if m and m.group(1).lower() != user.lower():
                bad.append(row)
        if len(rows) < 1000:
            break
        offset += 1000
    if not bad:
        return
    remove_posts(bad)
    log(f"[تيك توك] نُظّف {len(bad)} سجل خاطئ كان منسوباً لـ @{user}")
    note("notice", f"Cleanup @{user}", f"removed {len(bad)} wrongly attributed videos")


def _image_urls(item: dict) -> list:
    """يستخرج روابط صور منشور الصور (Slideshow) من بيانات Apify."""
    found = []

    def walk(o, path):
        low = path.lower()
        if any(x in low for x in ("authormeta", "musicmeta", "avatar", "cover", "hashtag", "mention")):
            return
        if isinstance(o, dict):
            for k, v in o.items():
                walk(v, f"{path}.{k}")
        elif isinstance(o, list):
            for v in o:
                walk(v, path)
        elif isinstance(o, str) and o.startswith("http"):
            if any(x in low for x in ("image", "slideshow", "photo")) and o not in found:
                found.append(o)

    walk(item, "")
    return found


def save_photo_post(user: str, vid: str, tmp: Path):
    """يحفظ منشور صور: يحمّل الصور ويرفعها. يرجع True عند النجاح."""
    global saved
    item = APIFY_ITEMS.get(vid)
    if not item:
        return False
    urls = _image_urls(item)
    if not urls:
        note("notice", f"Photo {vid} keys", f"keys={list(item.keys())}")
        return False
    paths = []
    for n, u in enumerate(urls, 1):
        r = requests.get(u, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.tiktok.com/"}, timeout=120)
        if r.status_code != 200 or len(r.content) < 500:
            continue
        ct = r.headers.get("content-type", "")
        ext = ".png" if "png" in ct else ".webp" if "webp" in ct else ".jpg"
        f = tmp / f"{vid}_{n}{ext}"
        f.write_bytes(r.content)
        paths.append(upload(f, f"tiktok/{user}/{vid}_{n}{ext}"))
    if not paths:
        return False
    posted = item.get("createTimeISO")
    if not posted and item.get("createTime"):
        posted = datetime.fromtimestamp(int(item["createTime"]), tz=timezone.utc).isoformat()
    insert("tiktok_posts", [{
        "id": vid, "account": user, "caption": item.get("text"), "posted_at": posted,
        "video_path": None, "thumb_path": paths[0],
        "info": {"type": "photo", "images": paths,
                 "like_count": item.get("diggCount"), "view_count": item.get("playCount"),
                 "comment_count": item.get("commentCount"), "webpage_url": item.get("webVideoUrl")},
    }])
    log(f"[تيك توك] حُفظ منشور صور {vid} ({len(paths)} صورة)")
    saved += 1
    return True


def sync_tiktok(user: str, cap: int) -> None:
    global errors, warnings, saved, skipped, nofile
    user = user.lstrip("@")
    log(f"[تيك توك] فحص @{user}")
    try:
        cleanup_foreign(user)
    except Exception as e:  # noqa: BLE001
        log(f"[تيك توك] تعذّر التنظيف: {e}")
        note("warning", f"Cleanup @{user}", e)
    ids, err = [], ""
    if APIFY_TOKEN:
        ids, err = list_tiktok_ids_apify(user, RESULTS)
        log(f"[تيك توك] Apify: {len(ids)} منشور لـ @{user}" + (f" ({err})" if err else ""))
        note("notice", f"TikTok Apify @{user}", f"ids={len(ids)} {err}".strip())
    if not ids:
        ids, err2 = list_tiktok_ids(user)
        err = f"{err} | yt-dlp: {err2}".strip(" |")
    if not ids:
        log(f"[تيك توك] لم تُجلب منشورات لـ @{user}: {err}. أجرّب المتصفح...")
        ids, info = list_tiktok_ids_browser(user)
        log(f"[تيك توك] المتصفح: {len(ids)} معرّف. {info}")
        note("notice", f"TikTok browser @{user}", f"ids={len(ids)} {info}")
        err = f"{err} | browser: {info}"
    if not ids:
        warnings += 1
        log(f"[تيك توك] تنبيه: لا توجد منشورات ظاهرة لـ @{user}")
        note("warning", f"TikTok @{user}", err)
        return
    known = existing_ids("tiktok_posts", user)
    try:
        known |= existing_ids("deleted_posts", user)   # ما حذفتَه يدوياً لا يُحفظ ثانيةً
    except Exception:  # noqa: BLE001
        pass
    todo = [i for i in ids if i not in known]
    log(f"[تيك توك] @{user}: {len(ids)} منشور، الجديد {len(todo)}، سيُحفظ {min(len(todo), cap)} الآن")

    for vid in todo[:cap]:
        tmp = Path(tempfile.mkdtemp())
        try:
            opts = {
                "outtmpl": str(tmp / "%(id)s.%(ext)s"),
                "writethumbnail": True,
                "format": "best[ext=mp4]/best",
                "quiet": True,
                "no_warnings": True,
                "retries": 5,
            }
            if COOKIES:
                opts["cookiefile"] = COOKIES
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"https://www.tiktok.com/@{user}/video/{vid}", download=True)

            # حماية: لا نحفظ إلا فيديو يملكه هذا الحساب فعلاً
            owner = info.get("uploader") or ""
            if not owner:
                m = re.search(r"/@([^/]+)/video/", info.get("webpage_url") or "")
                owner = m.group(1) if m else ""
            if owner and owner.lower() != user.lower():
                log(f"[تيك توك] تخطي {vid}: يملكه @{owner} وليس @{user}")
                skipped += 1
                continue
            files = list(tmp.glob(f"{vid}.*"))
            video = next((f for f in files if f.suffix.lower() in (".mp4", ".webm", ".mov", ".mkv")), None)
            thumb = next((f for f in files if f.suffix.lower() in (".jpg", ".jpeg", ".webp", ".png")), None)
            if not video:
                if save_photo_post(user, vid, tmp):
                    continue
                # غالباً منشور صور (Slideshow) بلا ملف فيديو
                log(f"[تيك توك] تخطي {vid}: لا يوجد ملف فيديو (منشور صور؟)")
                nofile += 1
                continue

            vpath = upload(video, f"tiktok/{user}/{vid}{video.suffix.lower()}")
            tpath = upload(thumb, f"tiktok/{user}/{vid}{thumb.suffix.lower()}") if thumb else None

            ts = info.get("timestamp")
            posted = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else None
            keep = ("view_count", "like_count", "comment_count", "repost_count",
                    "duration", "webpage_url", "uploader", "track", "artist")
            insert("tiktok_posts", [{
                "id": vid,
                "account": user,
                "caption": info.get("description") or info.get("title"),
                "posted_at": posted,
                "video_path": vpath,
                "thumb_path": tpath,
                "info": {k: info.get(k) for k in keep},
            }])
            log(f"[تيك توك] حُفظ {vid}")
            saved += 1
        except Exception as e:  # noqa: BLE001
            try:
                if save_photo_post(user, vid, tmp):
                    continue
            except Exception as e2:  # noqa: BLE001
                e = f"{e} | photo: {e2}"
            errors += 1
            log(f"[تيك توك] فشل {vid}: {e}")
            note("error", f"TikTok {vid}", e)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        time.sleep(3)


# ------------------------- تيلونيم -------------------------
TELLONYM_API = "https://api.tellonym.me/profiles/name/{name}"
T_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json",
    "tellonym-client": "web:1.0.0",
}


T_TOKEN = {"v": None}


def _tellonym_token() -> str:
    """رمز الدخول: من TELLONYM_TOKEN مباشرة، أو بتسجيل الدخول بـ TELLONYM_EMAIL/TELLONYM_PASSWORD (حسابك أنت)."""
    if T_TOKEN["v"] is not None:
        return T_TOKEN["v"]
    tok = os.environ.get("TELLONYM_TOKEN", "").strip()
    email, pw = os.environ.get("TELLONYM_EMAIL", "").strip(), os.environ.get("TELLONYM_PASSWORD", "")
    if not tok and email and pw:
        try:
            from curl_cffi import requests as cr
            r = cr.post("https://api.tellonym.me/tokens/create", impersonate="chrome", timeout=30,
                        headers={"Accept": "application/json", "tellonym-client": "web:3.80.0"},
                        json={"email": email, "password": pw, "limit": 0})
            try:
                tok = (r.json() or {}).get("accessToken") or ""
            except Exception:  # noqa: BLE001
                tok = ""
            note("notice", "Tellonym login", f"status={r.status_code} token={'yes' if tok else 'no'} body={'' if tok else r.text[:150]}")
        except Exception as e:  # noqa: BLE001
            note("notice", "Tellonym login", f"error {e}")
    T_TOKEN["v"] = tok
    return tok


def _tget(url: str, params=None):
    """طلب GET يحاكي متصفح Chrome الحقيقي (بصمة TLS) ويرسل Bearer إن وُجد."""
    h = {"Accept": "application/json", "tellonym-client": "web:3.80.0"}
    tok = _tellonym_token()
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    try:
        from curl_cffi import requests as cr
        return cr.get(url, headers=h, params=params, impersonate="chrome", timeout=30)
    except ImportError:
        return requests.get(url, headers={**T_HEADERS, **h}, params=params, timeout=30)


def tellonym_via_browser(name: str):
    """يفتح صفحة الملف في متصفح حقيقي ويلتقط ردود الـ API التي تحمل الإجابات."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:  # noqa: BLE001
        return [], f"playwright غير مثبت: {e}"
    got, seen = {}, []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox"])
            ctx = browser.new_context(locale="en-US", viewport={"width": 1280, "height": 1600},
                                      user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                                 "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
            page = ctx.new_page()

            def on_resp(resp):
                if "tellonym.me" not in resp.url:
                    return
                seen.append(f"{resp.status} {resp.url[8:70]}")
                try:
                    data = resp.json()
                except Exception:  # noqa: BLE001
                    return
                for a in (data.get("answers") or []) if isinstance(data, dict) else []:
                    if isinstance(a, dict) and a.get("id") is not None:
                        got[str(a["id"])] = a

            page.on("response", on_resp)
            page.goto(f"https://tellonym.me/{name}", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(15000)
            seen.append("title=" + page.title()[:60] + " text=" + " ".join(page.inner_text("body").split())[:200])
            last = -1
            for _ in range(60):                      # تمرير الصفحة لتحميل الأقدم
                page.mouse.wheel(0, 4000)
                page.wait_for_timeout(1200)
                if len(got) == last:
                    break
                last = len(got)
            browser.close()
    except Exception as e:  # noqa: BLE001
        return list(got.values()), f"browser error: {e} | seen={seen[:12]}"
    return list(got.values()), f"seen={seen[:12]}"


def fetch_tellonym(name: str, limit: int = 25) -> list:
    tried, last_status = [], 200
    # الطريقة 1: slug -> id -> answers (كما في كود المستخدم)  |  الطريقة 2: profiles/name
    uid = None
    r = _tget(f"https://api.tellonym.me/accounts/slug/{quote(name)}")
    tried.append(f"slug={r.status_code}")
    if r.status_code == 200:
        try:
            uid = r.json().get("id")
        except Exception:  # noqa: BLE001
            pass
    if uid:
        base, extra = f"https://api.tellonym.me/answers/id/{uid}", {}
    else:
        base, extra = TELLONYM_API.format(name=quote(name)), {}
    items, pos = [], 0
    while True:
        r = _tget(base, {"limit": limit, "pos": pos, **extra})
        tried.append(f"{'answers' if uid else 'profile'}={r.status_code}")
        if r.status_code != 200:
            note("notice", "Tellonym probe", f"{' '.join(tried)} body={r.text[:150]}")
            if not items and uid:   # جرّب الطريقة الأخرى
                uid = None
                base = TELLONYM_API.format(name=quote(name))
                continue
            last_status = r.status_code
            break
        batch = r.json().get("answers") or []
        if not batch:
            break
        items.extend(batch)
        if len(batch) < limit:
            break
        pos += len(batch)
        time.sleep(1.5)
    note("notice", "Tellonym probe", " ".join(tried))
    if not items:
        try:
            from curl_cffi import requests as cr
            h = cr.get(f"https://tellonym.me/{quote(name)}", impersonate="chrome", timeout=30)
            t = h.text
            import re as _re
            nd = _re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', t, _re.S)
            note("notice", "Tellonym html", f"status={h.status_code} len={len(t)} next_data={'yes' if nd else 'no'} "
                 f"has_answers={'answer' in t.lower()} head={' '.join(t[:200].split())}")
            if nd:
                d = json.loads(nd.group(1))
                def find(o):
                    if isinstance(o, dict):
                        if isinstance(o.get("answers"), list) and o["answers"]:
                            return o["answers"]
                        for v in o.values():
                            r_ = find(v)
                            if r_:
                                return r_
                    elif isinstance(o, list):
                        for v in o:
                            r_ = find(v)
                            if r_:
                                return r_
                    return None
                found = find(d)
                if found:
                    items = found
                else:
                    note("notice", "Tellonym html keys", json.dumps(d.get("props", {}).get("pageProps", {}), ensure_ascii=False)[:600])
        except Exception as e:  # noqa: BLE001
            note("notice", "Tellonym html", f"error {e}")
    if not items:
        items, info = tellonym_via_browser(name)
        note("notice", "Tellonym browser", f"items={len(items)} {info}")
    if not items and last_status != 200:
        class _E(Exception):
            pass
        err = _E(f"HTTP {last_status}")
        err.response = type("R", (), {"status_code": last_status})()
        raise err
    return items


def sync_tellonym(name: str) -> None:
    global errors, warnings
    name = name.lstrip("@")
    log(f"[تيلونيم] فحص {name}")
    try:
        items = fetch_tellonym(name)
    except Exception as e:  # noqa: BLE001
        code = getattr(getattr(e, "response", None), "status_code", "?")
        if code == 403:
            warnings += 1
            log("[تيلونيم] تنبيه: الموقع رفض الطلب (403)، غالباً يحجب عناوين GitHub. تم التخطي.")
            note("warning", f"Tellonym {name}", "HTTP 403 - blocked from GitHub runner")
        else:
            errors += 1
            log(f"[تيلونيم] فشل الطلب (HTTP {code}) — قد تكون الواجهة تغيّرت")
        return

    known = existing_ids("tellonym_answers", name)
    rows = []
    for i in items:
        if str(i.get("id")) in known:
            continue
        rows.append({
            "id": str(i.get("id")),
            "account": name,
            "tell": i.get("tell"),
            "answer": i.get("answer"),
            "posted_at": i.get("createdAt"),
            "raw": i,
        })
    for k in range(0, len(rows), 200):
        insert("tellonym_answers", rows[k:k + 200])
    log(f"[تيلونيم] {name}: {len(rows)} جديد من أصل {len(items)}")


# ------------------------- التشغيل -------------------------
def main() -> None:
    global errors
    cfg = json.loads((Path(__file__).parent.parent / "config.json").read_text(encoding="utf-8"))
    global RESULTS
    cap = int(cfg.get("max_new_videos_per_run", 20))
    RESULTS = int(os.environ.get("APIFY_RESULTS") or cfg.get("apify_results_per_run", 5))
    if APIFY_TOKEN:
        cap = max(cap, RESULTS)
    for acc in [a.strip().lstrip("@") for a in os.environ.get("PURGE_ACCOUNTS", "").split(",") if a.strip()]:
        try:
            purge_account(acc)
        except Exception as e:  # noqa: BLE001
            errors_add(e)

    for u in cfg.get("tiktok", []):
        try:
            sync_tiktok(u, cap)
        except Exception as e:  # noqa: BLE001
            errors += 1
            log(f"[تيك توك] خطأ في {u}: {e}")
    for u in cfg.get("tellonym", []):
        try:
            sync_tellonym(u)
        except Exception as e:  # noqa: BLE001
            errors += 1
            log(f"[تيلونيم] خطأ في {u}: {e}")

    log(f"انتهى. محفوظ: {saved}، أخطاء: {errors}، تنبيهات: {warnings}")
    note("notice", "Summary", f"saved={saved} skipped_foreign={skipped} no_video_file={nofile} errors={errors} warnings={warnings}")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
