from __future__ import annotations

import json
import re
import time
from pathlib import Path

import requests

from .media import Cancelled

YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


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


def youtube_credentials(settings):
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    data = settings.secret("youtube_credentials")
    if not data:
        raise ValueError("Connect YouTube in Settings before enabling automatic uploads.")
    credentials = Credentials.from_authorized_user_info(data, YOUTUBE_SCOPES)
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        settings.save(secrets={"youtube_credentials": json.loads(credentials.to_json())})
    if not credentials.valid:
        raise ValueError("YouTube authorization expired. Reconnect in Settings.")
    return credentials


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
    upload = service.videos().insert(part="snippet,status", body={
        "snippet": {"title": clip["title"][:100], "description": clip["description"][:4900],
                    "categoryId": "20", "tags": ["gaming", "shorts"]},
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
        code = value.get("error", {}).get("code", response.status_code)
        raise RuntimeError(f"Facebook API error {code}. Check Page permissions, token validity, video requirements, and publishing limits.")
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
    from urllib.parse import urlparse
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
        "title": clip["title"], "description": clip["description"], "is_ai_generated": True}, timeout=(15, 60)))
    wait_facebook(base, video_id, token, cancel, published=True)
    return {"remote_id": video_id, "url": f"https://www.facebook.com/reel/{video_id}", "message": "Published to your Facebook Page."}
