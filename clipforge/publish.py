from __future__ import annotations

import base64
import hashlib
import http.server
import json
import math
import re
import secrets
import time
import urllib.parse
import webbrowser
from pathlib import Path
from urllib.parse import urlparse

import requests

from .media import Cancelled

YOUTUBE_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]
TIKTOK_API_BASE = "https://open.tiktokapis.com/v2"
TIKTOK_AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TIKTOK_PRIVACY_LEVELS = ("SELF_ONLY", "PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR")
TIKTOK_UPLOAD_DOMAINS = ("tiktokapis.com", "tiktokv.com", "tiktokcdn.com")


def _safe_message(text: str) -> str:
    cleaned = re.sub(r"https?://\S+", "[service URL]", str(text or ""))
    return cleaned[:240].strip()


def connect_youtube(settings):
    from google_auth_oauthlib.flow import InstalledAppFlow
    client_file = Path(settings.get("youtube_client_file"))
    if not client_file.is_file():
        raise ValueError("Select a Google OAuth Desktop app client JSON file in Settings first.")
    flow = InstalledAppFlow.from_client_secrets_file(str(client_file), YOUTUBE_SCOPES)
    credentials = flow.run_local_server(port=0, open_browser=True, timeout_seconds=180,
        authorization_prompt_message="Complete the YouTube connection in your browser.",
        success_message="YouTube connected. You can return to ClipForge.", access_type="offline", prompt="consent")
    settings.save(secrets={"youtube_credentials": json.loads(credentials.to_json())})
    return check_youtube(settings)


def disconnect_youtube(settings):
    settings.save(secrets={"youtube_credentials": ""})


def youtube_credentials(settings):
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    data = settings.secret("youtube_credentials")
    if not data:
        raise ValueError("Connect YouTube in Settings before enabling automatic uploads.")
    scopes = data.get("scopes") or YOUTUBE_SCOPES if isinstance(data, dict) else YOUTUBE_SCOPES
    credentials = Credentials.from_authorized_user_info(data, scopes)
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        settings.save(secrets={"youtube_credentials": json.loads(credentials.to_json())})
    if not credentials.valid:
        raise ValueError("YouTube authorization expired. Reconnect in Settings.")
    return credentials


def check_youtube(settings):
    from googleapiclient.discovery import build
    import google_auth_httplib2
    import httplib2
    creds = youtube_credentials(settings)
    auth = google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=30))
    service = build("youtube", "v3", http=auth, cache_discovery=False)
    try:
        response = service.channels().list(part="snippet", mine=True).execute()
        items = response.get("items") or []
        if items:
            return items[0].get("snippet", {}).get("title") or "Connected YouTube channel"
    except Exception:
        return "Connected (reconnect to show channel name)"
    return "Connected YouTube account"


def upload_youtube(clip, settings, cancel, checkpoint):
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    import google_auth_httplib2
    import httplib2
    if cancel.is_set():
        raise Cancelled()
    auth = google_auth_httplib2.AuthorizedHttp(youtube_credentials(settings), http=httplib2.Http(timeout=90))
    service = build("youtube", "v3", http=auth, cache_discovery=False)
    privacy = settings.get("youtube_privacy")
    if privacy not in ("public", "unlisted", "private"):
        raise ValueError("Choose a valid YouTube visibility in Settings.")
    description = clip["description"]
    if "#shorts" not in description.lower():
        description = f"{description}\n#Shorts"
    upload = service.videos().insert(part="snippet,status", body={
        "snippet": {"title": clip["title"][:100], "description": description[:4900],
                    "categoryId": "20", "tags": ["gaming", "shorts", "Shorts"]},
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}},
        media_body=MediaFileUpload(clip["path"], mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True))
    result = None
    while result is None:
        if cancel.is_set():
            raise Cancelled()
        _, result = upload.next_chunk(num_retries=2)
    video_id = result["id"]
    checkpoint(video_id)
    actual_privacy = result.get("status", {}).get("privacyStatus", privacy)
    # API response confirms the upload and selected visibility, not future moderation outcomes.
    return {"remote_id": video_id, "url": f"https://www.youtube.com/watch?v={video_id}",
            "message": f"Uploaded ({actual_privacy}); YouTube may still be processing the video."}


def meta_response(response):
    try:
        value = response.json()
    except ValueError:
        raise RuntimeError(f"Facebook returned HTTP {response.status_code} without a valid response.") from None
    if not response.ok or "error" in value:
        err = value.get("error", {}) if isinstance(value.get("error"), dict) else {}
        code = err.get("code", response.status_code)
        detail = _safe_message(err.get("message", ""))
        suffix = f" ({detail})" if detail else ""
        raise RuntimeError(f"Facebook API error {code}{suffix}. Check Page permissions, token validity, video requirements, and publishing limits.")
    return value


def facebook_page(settings):
    page_id = settings.get("facebook_page_id").strip()
    token = settings.secret("facebook_page_token")
    version = settings.get("facebook_api_version")
    if not re.fullmatch(r"\d+", page_id) or not token:
        raise ValueError("Add your Facebook Page ID and Page access token in Settings.")
    if not re.fullmatch(r"v\d+\.\d+", version):
        raise ValueError("Facebook API version must look like v27.0.")
    return page_id, token, f"https://graph.facebook.com/{version}"


def disconnect_facebook(settings):
    settings.save(secrets={"facebook_page_token": ""})


def check_facebook(settings):
    page_id, token, base = facebook_page(settings)
    return meta_response(requests.get(f"{base}/{page_id}", params={"fields": "id,name"},
                                     headers={"Authorization": f"Bearer {token}"}, timeout=30)).get("name", page_id)


def wait_facebook(base, video_id, token, cancel, published=False):
    deadline = time.monotonic() + 360
    while time.monotonic() < deadline:
        if cancel.is_set():
            raise Cancelled()
        value = meta_response(requests.get(f"{base}/{video_id}", params={"fields": "status"},
            headers={"Authorization": f"Bearer {token}"}, timeout=(15, 45)))
        status = value.get("status", {})
        phases = [status.get(key, {}) for key in ("processing_phase", "uploading_phase", "publishing_phase")]
        if status.get("video_status") in ("error", "upload_failed", "expired") or any(p.get("status") == "error" for p in phases):
            raise RuntimeError("Facebook reported a video processing/publishing error. Check the Page's video library.")
        if published:
            if status.get("publishing_phase", {}).get("publish_status") == "published":
                return
        elif status.get("video_status") == "ready" or status.get("processing_phase", {}).get("status") == "completed":
            return
        if cancel.wait(3):
            raise Cancelled()
    raise RuntimeError("Facebook is still processing the video. Check its recorded video ID before retrying.")


def upload_facebook(clip, settings, cancel, checkpoint):
    page_id, token, base = facebook_page(settings)
    headers = {"Authorization": f"Bearer {token}"}
    created = meta_response(requests.post(f"{base}/{page_id}/video_reels", headers=headers,
                                        json={"upload_phase": "start"}, timeout=(15, 45)))
    video_id = str(created["video_id"])
    checkpoint(video_id)
    upload_url = created["upload_url"]
    parsed = urlparse(upload_url)
    if parsed.scheme != "https" or parsed.hostname != "rupload.facebook.com":
        raise RuntimeError("Facebook returned an unexpected upload host.")
    if cancel.is_set():
        raise Cancelled()
    path = Path(clip["path"])
    with path.open("rb") as media:
        meta_response(requests.post(upload_url, headers={"Authorization": f"OAuth {token}",
            "offset": "0", "file_size": str(path.stat().st_size), "Content-Type": "application/octet-stream"},
            data=media, timeout=(15, 240)))
    wait_facebook(base, video_id, token, cancel)
    meta_response(requests.post(f"{base}/{page_id}/video_reels", headers=headers, json={
        "upload_phase": "finish", "video_state": "PUBLISHED", "video_id": video_id,
        "title": clip["title"], "description": clip["description"]}, timeout=(15, 60)))
    wait_facebook(base, video_id, token, cancel, published=True)
    return {"remote_id": video_id, "url": f"https://www.facebook.com/reel/{video_id}", "message": "Published to your Facebook Page."}


def tiktok_response(response):
    try:
        value = response.json()
    except ValueError:
        raise RuntimeError(f"TikTok returned HTTP {response.status_code} without a valid response.") from None
    err = value.get("error") if isinstance(value.get("error"), dict) else {}
    code = err.get("code", "")
    if not response.ok or (code and code != "ok"):
        detail = _safe_message(err.get("message", ""))
        suffix = f" ({detail})" if detail else ""
        raise RuntimeError(f"TikTok API error {code or response.status_code}{suffix}. Check TikTok app permissions and token validity.")
    return value


def tiktok_token(settings):
    token = settings.secret("tiktok_access_token")
    if token:
        return token
    refresh = settings.secret("tiktok_refresh_token")
    client_key = settings.get("tiktok_client_key").strip()
    client_secret = settings.secret("tiktok_client_secret")
    if refresh and client_key and client_secret:
        resp = requests.post(f"{TIKTOK_API_BASE}/oauth/token/", data={
            "client_key": client_key, "client_secret": client_secret,
            "grant_type": "refresh_token", "refresh_token": refresh,
        }, timeout=30)
        data = tiktok_response(resp)
        access = data.get("access_token")
        if access:
            settings.save(secrets={
                "tiktok_access_token": access,
                "tiktok_refresh_token": data.get("refresh_token", refresh),
            })
            return access
    raise ValueError("Connect TikTok (or add a TikTok access token) in Settings before enabling automatic TikTok posting.")


def disconnect_tiktok(settings):
    settings.save(secrets={"tiktok_access_token": "", "tiktok_refresh_token": ""})


def connect_tiktok(settings, redirect_port=8765):
    client_key = settings.get("tiktok_client_key").strip()
    client_secret = settings.secret("tiktok_client_secret")
    if not client_key or not client_secret:
        raise ValueError("Add your TikTok Client Key and Client Secret in Settings first.")
    redirect_uri = f"http://127.0.0.1:{redirect_port}/callback"
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    captured = {}

    class CallbackHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            params = urllib.parse.parse_qs(parsed.query)
            captured["code"] = (params.get("code") or [""])[0]
            captured["state"] = (params.get("state") or [""])[0]
            captured["error"] = (params.get("error") or [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<h3>TikTok connected. You can return to ClipForge.</h3>")

        def log_message(self, format, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", redirect_port), CallbackHandler)
    server.timeout = 180
    auth_query = urllib.parse.urlencode({
        "client_key": client_key, "response_type": "code",
        "scope": "video.upload,video.publish", "redirect_uri": redirect_uri,
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
    })
    webbrowser.open(f"{TIKTOK_AUTH_URL}?{auth_query}")
    try:
        server.handle_request()
    finally:
        server.server_close()

    if captured.get("error"):
        raise RuntimeError(f"TikTok authorization declined: {_safe_message(captured['error'])}")
    if not captured.get("code") or captured.get("state") != state:
        raise RuntimeError("TikTok authorization did not complete or state verification failed.")

    resp = requests.post(f"{TIKTOK_API_BASE}/oauth/token/", data={
        "client_key": client_key, "client_secret": client_secret,
        "code": captured["code"], "grant_type": "authorization_code",
        "redirect_uri": redirect_uri, "code_verifier": verifier,
    }, timeout=30)
    data = tiktok_response(resp)
    access = data.get("access_token")
    if not access:
        raise RuntimeError("TikTok did not return an access token.")
    settings.save(secrets={
        "tiktok_access_token": access,
        "tiktok_refresh_token": data.get("refresh_token", ""),
    })
    return check_tiktok(settings)


def check_tiktok(settings):
    token = tiktok_token(settings)
    resp = requests.post(f"{TIKTOK_API_BASE}/post/publish/creator_info/query/",
                         headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"},
                         json={}, timeout=30)
    data = tiktok_response(resp).get("data", {})
    return data.get("creator_nickname") or data.get("creator_username") or "Connected TikTok account"


def _is_trusted_tiktok_upload_url(upload_url: str) -> bool:
    parsed = urlparse(upload_url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and any(host == d or host.endswith("." + d) for d in TIKTOK_UPLOAD_DOMAINS)


def wait_tiktok(publish_id, token, cancel):
    deadline = time.monotonic() + 360
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"}
    while time.monotonic() < deadline:
        if cancel.is_set():
            raise Cancelled()
        value = tiktok_response(requests.post(f"{TIKTOK_API_BASE}/post/publish/status/fetch/",
                                              headers=headers, json={"publish_id": publish_id}, timeout=(15, 45)))
        status = value.get("data", {}).get("status", "")
        if status in ("PUBLISH_COMPLETE", "SEND_TO_USER_INBOX"):
            return status
        if status == "FAILED":
            reason = _safe_message(value.get("data", {}).get("fail_reason", "unknown reason"))
            raise RuntimeError(f"TikTok failed to process the video ({reason}).")
        if cancel.wait(3):
            raise Cancelled()
    raise RuntimeError("TikTok is still processing the video. Check its publish ID before retrying.")


def upload_tiktok(clip, settings, cancel, checkpoint):
    token = tiktok_token(settings)
    mode = (settings.get("tiktok_post_mode") or "inbox").strip().lower()
    if mode not in ("inbox", "direct"):
        raise ValueError("TikTok post mode must be 'inbox' (draft) or 'direct'.")
    privacy = (settings.get("tiktok_privacy") or "SELF_ONLY").strip().upper()
    if privacy not in TIKTOK_PRIVACY_LEVELS:
        raise ValueError("Choose a valid TikTok privacy level in Settings.")
    path = Path(clip["path"])
    file_size = path.stat().st_size
    chunk_size = file_size if file_size <= 64 * 1024 * 1024 else 10 * 1024 * 1024
    total_chunks = max(1, math.ceil(file_size / chunk_size))
    source_info = {
        "source": "FILE_UPLOAD",
        "video_size": file_size,
        "chunk_size": chunk_size,
        "total_chunk_count": total_chunks,
    }
    if mode == "direct":
        endpoint = f"{TIKTOK_API_BASE}/post/publish/video/init/"
        payload = {
            "post_info": {
                "title": clip["description"][:2200] or clip["title"][:150],
                "privacy_level": privacy,
                "disable_duet": False,
                "disable_comment": False,
                "disable_stitch": False,
            },
            "source_info": source_info,
        }
    else:
        endpoint = f"{TIKTOK_API_BASE}/post/publish/inbox/video/init/"
        payload = {"source_info": source_info}

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"}
    created = tiktok_response(requests.post(endpoint, headers=headers, json=payload, timeout=(15, 45))).get("data", {})
    publish_id = str(created.get("publish_id") or "")
    upload_url = str(created.get("upload_url") or "")
    if not publish_id or not upload_url:
        raise RuntimeError("TikTok did not return a publish ID and upload URL.")
    checkpoint(publish_id)
    if not _is_trusted_tiktok_upload_url(upload_url):
        raise RuntimeError("TikTok returned an unexpected upload host.")
    if cancel.is_set():
        raise Cancelled()

    with path.open("rb") as media:
        offset = 0
        while offset < file_size:
            if cancel.is_set():
                raise Cancelled()
            chunk = media.read(chunk_size)
            if not chunk:
                break
            end = offset + len(chunk) - 1
            put_resp = requests.put(
                upload_url,
                headers={
                    "Content-Type": "video/mp4",
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {offset}-{end}/{file_size}",
                },
                data=chunk,
                timeout=(15, 240),
            )
            if not put_resp.ok:
                raise RuntimeError(f"TikTok video chunk upload failed with HTTP {put_resp.status_code}.")
            offset = end + 1

    final_status = wait_tiktok(publish_id, token, cancel)
    msg = ("Published to TikTok." if final_status == "PUBLISH_COMPLETE"
           else "Uploaded to TikTok Creator Inbox (finish review in the TikTok app).")
    return {"remote_id": publish_id, "url": "", "message": msg}
