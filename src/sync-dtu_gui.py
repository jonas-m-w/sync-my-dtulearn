# Copyright (C) 2026 Jonas s263327@dtu.dk
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License.

import ctypes
from ctypes import wintypes
import json
import multiprocessing
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
import requests
import webview

# Canonical Brightspace host at DTU
BASE_URL = "https://learn.inside.dtu.dk"

# Standard Valence API endpoints for DTU Inside Brightspace
API_VERSIONS = {"lp": "1.45", "le": "1.47"}

CONFIG_FILE = os.path.expanduser("~/.dtu_sync_config.json")
ENCRYPTED_SESSION_FILE = os.path.expanduser("~/.dtu_sync_session.bin")
DEFAULT_TARGET_DIR = os.path.expanduser("~/DTU_Courses")


# ----------------- SECURITY: Windows DPAPI Encryption -----------------
class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi_encrypt(data_bytes: bytes) -> bytes:
    if sys.platform != "win32":
        return data_bytes
    blob_in = DATA_BLOB(len(data_bytes), ctypes.cast(ctypes.create_string_buffer(data_bytes), ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    if ctypes.windll.crypt32.CryptProtectData(ctypes.byref(blob_in), "DTUSession", None, None, None, 0, ctypes.byref(blob_out)):
        encrypted = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)
        return encrypted
    raise RuntimeError("DPAPI encryption failed.")


def _dpapi_decrypt(encrypted_bytes: bytes) -> bytes:
    if sys.platform != "win32":
        return encrypted_bytes
    blob_in = DATA_BLOB(len(encrypted_bytes), ctypes.cast(ctypes.create_string_buffer(encrypted_bytes), ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    if ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        decrypted = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)
        return decrypted
    raise RuntimeError("DPAPI decryption failed.")


def save_session_cookies(cookies_dict):
    try:
        raw_json = json.dumps(cookies_dict).encode("utf-8")
        encrypted = _dpapi_encrypt(raw_json)
        with open(ENCRYPTED_SESSION_FILE, "wb") as f:
            f.write(encrypted)
    except Exception as e:
        print(f"[SECURITY] Failed to write session: {e}")


def load_session_cookies():
    if not os.path.exists(ENCRYPTED_SESSION_FILE):
        return None
    try:
        with open(ENCRYPTED_SESSION_FILE, "rb") as f:
            encrypted = f.read()
        decrypted = _dpapi_decrypt(encrypted)
        return json.loads(decrypted.decode("utf-8"))
    except Exception:
        return None


# ----------------- SECURITY: Path Sandboxing -----------------
def sanitize(name: str) -> str:
    clean = re.sub(r'[\/\\:*?"<>|]', "_", str(name or "")).strip()
    clean = re.sub(r"^\.+", "_", clean)
    return re.sub(r"\s+", " ", clean) or "unnamed"


def safe_join(base_dir: str, *paths: str) -> str:
    base_dir = os.path.abspath(base_dir)
    target = os.path.abspath(os.path.join(base_dir, *paths))
    if os.path.commonpath([base_dir, target]) != base_dir:
        raise ValueError(f"Path traversal detected: {target} is outside {base_dir}")
    return target


# ----------------- Configuration & Session Management -----------------
def load_config():
    defaults = {"target_dir": DEFAULT_TARGET_DIR, "excluded_courses": []}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return {**defaults, **json.load(f)}
        except Exception:
            pass
    return defaults


def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


def get_authenticated_session():
    cookies = load_session_cookies()
    if not cookies:
        return None, "No active session found. Please click 'Log In to DTU Learn' first."

    session = requests.Session()
    session.cookies.update(cookies)
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
    })

    enroll_url = f"{BASE_URL}/d2l/api/lp/{API_VERSIONS['lp']}/enrollments/myenrollments/?orgUnitTypeId=3"
    try:
        resp = session.get(enroll_url, timeout=6)
        if resp.status_code == 200:
            return session, None

        if resp.status_code in (401, 403):
            return None, "Session expired. Please click 'Log In to DTU Learn' to re-authenticate."
        return None, f"Authentication check failed (HTTP {resp.status_code})."
    except requests.exceptions.Timeout:
        return None, "Connection timed out. Please check your internet connection and retry."
    except Exception as e:
        return None, f"Network error during authentication: {e}"


# ----------------- Independent Webview Process -----------------
def _webview_worker():
    window = webview.create_window(
        title="DTU Learn Sign In",
        url="https://learn.inside.dtu.dk/d2l/home",
        width=850,
        height=720,
    )

    def watcher():
        while True:
            time.sleep(0.8)
            try:
                curr_url = window.get_current_url() or ""
                if "d2l/home" in curr_url.lower():
                    raw_cookies = window.get_cookies()
                    cookie_dict = {}
                    for c in raw_cookies:
                        for k, morsel in c.items():
                            cookie_dict[k] = morsel.value

                    if "d2lSessionVal" in cookie_dict or any("d2l" in k.lower() for k in cookie_dict):
                        save_session_cookies(cookie_dict)
                        time.sleep(0.5)
                        window.destroy()
                        break
            except Exception:
                break

    threading.Thread(target=watcher, daemon=True).start()
    webview.start(private_mode=False)


# ----------------- Robust Fault-Tolerant Sync Engine -----------------
class SyncWorker(threading.Thread):
    def __init__(self, msg_queue, target_dir, excluded_ids, cancel_event):
        super().__init__(daemon=True)
        self.q = msg_queue
        self.target_dir = os.path.abspath(target_dir)
        self.excluded_ids = set(excluded_ids)
        self.cancel_event = cancel_event

    def run(self):
        failed_items = []  # Detailed failure objects for post-sync prompt
        try:
            self.q.put(("status", "Step 1/4: Authenticating session..."))
            self.q.put(("log", ("[STEP 1] Validating session on DTU Inside...", "normal")))
            session, err = get_authenticated_session()
            if not session:
                self.q.put(("auth_status", False))
                self.q.put(("error", err))
                return

            if self.cancel_event.is_set():
                self.q.put(("log", ("[CANCELED] Sync stopped by user.", "orange")))
                self.q.put(("status", "Sync canceled."))
                return

            self.q.put(("auth_status", True))
            self.q.put(("log", ("[INFO] Session active and validated.", "green")))

            self.q.put(("status", "Step 2/4: Fetching course list..."))
            self.q.put(("log", ("[STEP 2] Querying enrolled courses...", "normal")))

            try:
                url = f"{BASE_URL}/d2l/api/lp/{API_VERSIONS['lp']}/enrollments/myenrollments/?orgUnitTypeId=3"
                resp = session.get(url, timeout=15)
                resp.raise_for_status()
                items = resp.json().get("Items", [])
            except Exception as e:
                self.q.put(("error", f"Failed to retrieve enrolled courses:\n{e}"))
                return

            all_courses = [
                {
                    "id": str(it.get("OrgUnit", {}).get("Id")),
                    "name": it.get("OrgUnit", {}).get("Name", "Unnamed"),
                    "code": it.get("OrgUnit", {}).get("Code", ""),
                }
                for it in items
            ]

            self.q.put(("course_list", all_courses))
            courses_to_sync = [c for c in all_courses if c["id"] not in self.excluded_ids]
            self.q.put(("log", (f"[INFO] Found {len(all_courses)} total courses ({len(courses_to_sync)} active for sync).", "normal")))

            # --- PASS 1: SCANNING ALL COURSES ---
            self.q.put(("status", "Step 3/4: Scanning online contents against local drive..."))
            self.q.put(("log", ("\n[STEP 3] Starting pre-sync scan across all selected courses...", "normal")))

            pending_downloads = []
            already_up_to_date_count = 0
            course_manifests = {}
            course_dirs = {}

            for c_idx, course in enumerate(courses_to_sync, 1):
                if self.cancel_event.is_set():
                    self.q.put(("log", ("[CANCELED] Scan stopped by user.", "orange")))
                    self.q.put(("status", "Sync canceled."))
                    return

                cid = course["id"]
                cname = sanitize(course["name"])
                folder_name = f"{course['code']}_{cname}" if course.get("code") else cname

                try:
                    c_dir = safe_join(self.target_dir, sanitize(folder_name))
                except ValueError as e:
                    self.q.put(("log", (f"[SECURITY ERROR] {e}", "red")))
                    continue

                os.makedirs(c_dir, exist_ok=True)
                course_dirs[cid] = c_dir

                m_file = os.path.join(c_dir, ".sync_manifest.json")
                manifest = {}
                if os.path.exists(m_file):
                    try:
                        with open(m_file, "r", encoding="utf-8") as f:
                            manifest = json.load(f)
                    except Exception:
                        manifest = {}
                course_manifests[cid] = manifest

                self.q.put(("log", (f"  Scanning course [{c_idx}/{len(courses_to_sync)}]: {course['name']}...", "normal")))
                toc_url = f"{BASE_URL}/d2l/api/le/{API_VERSIONS['le']}/{cid}/content/toc"
                try:
                    toc_resp = session.get(toc_url, timeout=15)
                    if toc_resp.status_code != 200:
                        self.q.put(("log", (f"  [WARN] Failed to load contents for {cname} (HTTP {toc_resp.status_code})", "orange")))
                        continue
                    toc_data = toc_resp.json()
                except Exception as e:
                    self.q.put(("log", (f"  [WARN] Error fetching table of contents: {e}", "red")))
                    continue

                to_dl, up_count = self.scan_modules(cid, course["name"], toc_data.get("Modules", []), c_dir, manifest, failed_items)
                pending_downloads.extend(to_dl)
                already_up_to_date_count += up_count

            total_pending = len(pending_downloads)
            self.q.put(("pending_count", total_pending))
            self.q.put(("log", (f"[SCAN COMPLETE] Found {total_pending} item(s) to process; {already_up_to_date_count} already up-to-date.\n", "green")))

            if total_pending == 0:
                self.q.put(("status", "All files already up to date."))
                self.q.put(("done", f"All files are up to date! ({already_up_to_date_count} verified on disk)."))
                if failed_items:
                    self.q.put(("show_failed_dialog", (failed_items, course_dirs)))
                return

            if self.cancel_event.is_set():
                self.q.put(("log", ("[CANCELED] Sync stopped before download phase.", "orange")))
                self.q.put(("status", "Sync canceled."))
                return

            # --- PASS 2: DOWNLOADING PENDING ITEMS ---
            self.q.put(("status", f"Step 4/4: Processing {total_pending} updated/new items..."))
            self.q.put(("log", (f"[STEP 4] Beginning sync of {total_pending} item(s)...", "normal")))
            self.q.put(("set_max_progress", total_pending))

            downloaded_count = 0

            for idx, item in enumerate(pending_downloads, 1):
                if self.cancel_event.is_set():
                    self.q.put(("log", (f"\n[CANCELED] Sync aborted by user after processing {downloaded_count} items.", "orange")))
                    self.q.put(("status", "Sync canceled by user."))
                    break

                cid = item["course_id"]
                course_name = item.get("course_name", "")
                topic_id = item["topic_id"]
                planned_file = item["planned_file"]
                remote_mod = item["remote_mod"]
                filename = item["filename"]
                raw_url = item["raw_url"]
                is_web_link = item.get("is_web_link", False)
                is_fbf = item.get("is_fbf", False)
                manifest = course_manifests[cid]

                action_label = "Updating" if item["exists"] else "Downloading"
                self.q.put(("file_progress", idx))
                self.q.put(("file_downloading", f"[{idx}/{total_pending}] {action_label}: {filename}"))
                self.q.put(("log", (f"  [{idx}/{total_pending}] {action_label}: {filename}", "normal")))

                # --- 1. DIRECT HANDLER: Web Links (.url) ---
                if is_web_link:
                    target_url = raw_url
                    if not target_url.startswith("http"):
                        target_url = f"{BASE_URL}{target_url}" if target_url.startswith("/") else f"{BASE_URL}/{target_url}"

                    shortcut_path = os.path.splitext(planned_file)[0] + ".url"
                    shortcut_content = f"[InternetShortcut]\nURL={target_url}\n"
                    try:
                        with open(shortcut_path, "w", encoding="utf-8") as f:
                            f.write(shortcut_content)

                        manifest[topic_id] = {
                            "mod_date": remote_mod,
                            "file_path": shortcut_path,
                            "filename": os.path.basename(shortcut_path),
                            "size": os.path.getsize(shortcut_path),
                        }
                        downloaded_count += 1
                        self.q.put(("file_counter_inc", None))
                        self.q.put(("log", (f"  [LINK] Created web shortcut: {os.path.basename(shortcut_path)}", "green")))
                        continue
                    except Exception as e:
                        self.q.put(("log", (f"  [FAIL] Could not write shortcut for {filename}: {e}", "orange")))
                        failed_items.append({
                            "course_id": cid,
                            "course_name": course_name,
                            "topic_id": topic_id,
                            "title": filename,
                            "remote_mod": remote_mod,
                            "reason": f"Shortcut creation failed: {e}",
                            "color": "orange"
                        })
                        continue

                # --- 2. DIRECT HANDLER: FeedbackFruits / LTI Tool (.html launcher) ---
                if is_fbf:
                    activity_url = f"{BASE_URL}/d2l/le/content/{cid}/viewContent/{topic_id}/View"
                    html_content = f"""<!DOCTYPE html>
<html>
<head>
    <meta http-equiv="refresh" content="0; url='{activity_url}'" />
    <title>Activity - Redirect</title>
</head>
<body style="font-family: sans-serif; padding: 20px;">
    <h2>Interactive Course Activity</h2>
    <p>This item is hosted directly on DTU Learn / FeedbackFruits.</p>
    <p><a href="{activity_url}">Click here to open the activity in your browser</a></p>
</body>
</html>"""
                    html_path = os.path.splitext(planned_file)[0] + ".html"
                    try:
                        with open(html_path, "w", encoding="utf-8") as f:
                            f.write(html_content)

                        manifest[topic_id] = {
                            "mod_date": remote_mod,
                            "file_path": html_path,
                            "filename": os.path.basename(html_path),
                            "size": os.path.getsize(html_path),
                        }
                        downloaded_count += 1
                        self.q.put(("file_counter_inc", None))
                        self.q.put(("log", (f"  [ACTIVITY] Created launcher stub: {os.path.basename(html_path)}", "green")))
                        continue
                    except Exception as e:
                        self.q.put(("log", (f"  [FAIL] Could not write launcher stub for {filename}: {e}", "orange")))
                        failed_items.append({
                            "course_id": cid,
                            "course_name": course_name,
                            "topic_id": topic_id,
                            "title": filename,
                            "remote_mod": remote_mod,
                            "reason": f"Activity stub creation failed: {e}",
                            "color": "orange"
                        })
                        continue

                # --- 3. STANDARD FILE DOWNLOADS ---
                download_candidates = [
                    f"{BASE_URL}/d2l/le/content/{cid}/topics/files/download/{topic_id}/DirectFileTopicDownload",
                    f"{BASE_URL}/d2l/api/le/{API_VERSIONS['le']}/{cid}/content/topics/{topic_id}/file",
                ]
                if raw_url and not raw_url.startswith("http"):
                    download_candidates.append(
                        f"{BASE_URL}{raw_url}" if raw_url.startswith("/") else f"{BASE_URL}/{raw_url}"
                    )

                success = False
                last_err = "No response"
                final_saved_file = planned_file

                for candidate_url in download_candidates:
                    if self.cancel_event.is_set():
                        break
                    try:
                        ok, res, real_name = self._download_stream(session, candidate_url, planned_file)
                        if ok:
                            success = True
                            final_saved_file = res
                            manifest[topic_id] = {
                                "mod_date": remote_mod,
                                "file_path": final_saved_file,
                                "filename": real_name or filename,
                                "size": os.path.getsize(final_saved_file),
                            }
                            downloaded_count += 1
                            self.q.put(("file_counter_inc", None))
                            break
                        else:
                            last_err = res
                    except Exception as ex:
                        last_err = str(ex)

                if not success and not self.cancel_event.is_set():
                    self.q.put(("log", (f"  [FAIL] Failed downloading {filename} ({last_err})", "orange")))
                    failed_items.append({
                        "course_id": cid,
                        "course_name": course_name,
                        "topic_id": topic_id,
                        "title": filename,
                        "remote_mod": remote_mod,
                        "reason": str(last_err),
                        "color": "orange"
                    })

            # Save manifests even on partial download
            for cid, manifest in course_manifests.items():
                if cid in course_dirs:
                    m_path = os.path.join(course_dirs[cid], ".sync_manifest.json")
                    with open(m_path, "w", encoding="utf-8") as f:
                        json.dump(manifest, f, indent=2)

            if not self.cancel_event.is_set():
                failures_count = len(failed_items)
                summary_msg = f"Sync Finished! Downloaded: {downloaded_count} | Up-to-Date: {already_up_to_date_count} | Failures: {failures_count}."
                self.q.put(("log", (f"\n[SUMMARY] {summary_msg}", "green" if failures_count == 0 else "orange")))
                self.q.put(("status", "Sync completed."))
                self.q.put(("done", summary_msg))

                # Launch review dialog if failures occurred
                if failed_items:
                    self.q.put(("show_failed_dialog", (failed_items, course_dirs)))

        except Exception as unexpected_err:
            self.q.put(("log", (f"[FATAL ERROR] {unexpected_err}", "red")))
            self.q.put(("error", f"An unexpected error interrupted sync:\n{unexpected_err}"))
        finally:
            self.q.put(("cleanup_done", None))

    def scan_modules(self, course_id, course_name, modules, current_path, manifest, failed_items):
        to_download = []
        up_to_date_count = 0

        for mod in modules:
            if self.cancel_event.is_set():
                break

            mod_title = sanitize(mod.get("Title", "Untitled Module"))
            try:
                mod_path = safe_join(current_path, mod_title)
            except ValueError as e:
                self.q.put(("log", (f"  [SECURITY] Skipped module: {e}", "red")))
                continue

            os.makedirs(mod_path, exist_ok=True)

            for topic in mod.get("Topics", []):
                try:
                    if topic.get("IsHidden"):
                        continue

                    topic_id = str(topic.get("TopicId") or "")
                    if not topic_id:
                        continue

                    title = sanitize(topic.get("Title", "Untitled"))
                    raw_url = str(topic.get("Url") or "").strip()
                    topic_type = topic.get("Type")

                    # Identify Web Links (Type 3) and FeedbackFruits / LTI activities
                    is_web_link = (
                        topic_type == 3
                        or raw_url.startswith("http://")
                        or (raw_url.startswith("https://") and "inside.dtu.dk" not in raw_url and "dtu.dk" not in raw_url)
                    )
                    is_fbf = (
                        "feedbackfruits" in raw_url.lower()
                        or "feedbackfruits" in title.lower()
                        or "quicklink" in raw_url.lower()
                    )

                    # Assign appropriate extension
                    if is_web_link:
                        ext = ".url"
                    elif is_fbf:
                        ext = ".html"
                    else:
                        ext = os.path.splitext(raw_url)[1] if raw_url else ".pdf"
                        if not ext or len(ext) > 5:
                            ext = ".pdf"

                    planned_filename = f"{title}{ext}"

                    try:
                        planned_file = safe_join(mod_path, planned_filename)
                    except ValueError:
                        continue

                    remote_mod = str(topic.get("LastModifiedDate") or topic.get("StartDate") or "static_file")
                    cached_entry = manifest.get(topic_id)
                    already_exists = False
                    is_user_ignored = False

                    if isinstance(cached_entry, dict):
                        is_user_ignored = cached_entry.get("ignored", False)
                        cached_file = cached_entry.get("file_path")
                        cached_mod = cached_entry.get("mod_date")
                        if cached_file and os.path.exists(cached_file):
                            already_exists = True
                        elif os.path.exists(planned_file):
                            already_exists = True
                    else:
                        cached_mod = cached_entry
                        already_exists = os.path.exists(planned_file)

                    # Skip if up-to-date or flagged by user to ignore
                    if is_user_ignored or (already_exists and cached_mod == remote_mod):
                        up_to_date_count += 1
                    else:
                        to_download.append({
                            "course_id": course_id,
                            "course_name": course_name,
                            "topic_id": topic_id,
                            "planned_file": planned_file,
                            "filename": planned_filename,
                            "remote_mod": remote_mod,
                            "raw_url": raw_url,
                            "is_web_link": is_web_link,
                            "is_fbf": is_fbf,
                            "exists": already_exists,
                        })
                except Exception as item_err:
                    self.q.put(("log", (f"  [ERROR] Skipped topic in '{mod_title}': {item_err}", "red")))
                    failed_items.append({
                        "course_id": course_id,
                        "course_name": course_name,
                        "topic_id": str(topic.get("TopicId") or "unknown"),
                        "title": str(topic.get("Title") or "Corrupted Metadata"),
                        "remote_mod": "error",
                        "reason": f"Metadata parse error: {item_err}",
                        "color": "red"
                    })
                    continue

            if mod.get("Modules"):
                sub_dl, sub_up = self.scan_modules(course_id, course_name, mod["Modules"], mod_path, manifest, failed_items)
                to_download.extend(sub_dl)
                up_to_date_count += sub_up

        return to_download, up_to_date_count

    def _download_stream(self, session, url, planned_path):
        part_path = f"{planned_path}.part"
        try:
            with session.get(url, stream=True, timeout=25, allow_redirects=True) as r:
                if r.status_code != 200:
                    return False, f"HTTP {r.status_code}", None

                content_type = r.headers.get("Content-Type", "").lower()
                if "text/html" in content_type and not planned_path.endswith(".html"):
                    return False, "Returned HTML stub", None

                final_path = planned_path
                disp = r.headers.get("Content-Disposition", "")
                if "filename=" in disp:
                    match = re.findall(r'filename\*?=(?:UTF-8\'\')?["\']?([^"\';\r\n]+)', disp)
                    if match:
                        real_name = sanitize(match[0].strip())
                        local_dir = os.path.dirname(planned_path)
                        final_path = safe_join(local_dir, real_name)
                        part_path = f"{final_path}.part"

                with open(part_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=16384):
                        if self.cancel_event.is_set():
                            f.close()
                            if os.path.exists(part_path):
                                os.remove(part_path)
                            return False, "Aborted mid-stream", None
                        if chunk:
                            f.write(chunk)

            if os.path.exists(final_path):
                os.remove(final_path)
            os.rename(part_path, final_path)
            return True, final_path, os.path.basename(final_path)

        except Exception as e:
            if os.path.exists(part_path):
                try:
                    os.remove(part_path)
                except Exception:
                    pass
            return False, str(e), None


# ----------------- Dialog: Failed Items Review & Ignore -----------------
class FailedItemsDialog(tk.Toplevel):
    def __init__(self, parent, failed_items, course_dirs):
        super().__init__(parent)
        self.title("Sync Issues Review")
        self.geometry("720x460")
        self.minsize(580, 360)
        self.transient(parent)
        self.grab_set()

        self.failed_items = failed_items
        self.course_dirs = course_dirs
        self.check_vars = {}

        self.create_widgets()

    def create_widgets(self):
        header_frame = ttk.Frame(self, padding="10")
        header_frame.pack(fill=tk.X)

        lbl_title = ttk.Label(
            header_frame,
            text="The following items could not be downloaded or had errors:",
            font=("Helvetica", 10, "bold")
        )
        lbl_title.pack(anchor="w")

        lbl_desc = ttk.Label(
            header_frame,
            text="Select items you would like to permanently ignore in future scans (treated as up-to-date):",
            font=("Helvetica", 9)
        )
        lbl_desc.pack(anchor="w", pady=(3, 0))

        # Bulk selection toolbar
        btn_box = ttk.Frame(self, padding="10 5")
        btn_box.pack(fill=tk.X)

        ttk.Button(btn_box, text="Select All", command=self.select_all).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(btn_box, text="Deselect All", command=self.deselect_all).pack(side=tk.LEFT)

        # Scrollable list of failed items
        list_container = ttk.Frame(self, padding="10")
        list_container.pack(fill=tk.BOTH, expand=True)

        canvas = tk.Canvas(list_container, highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_container, orient=tk.VERTICAL, command=canvas.yview)
        scroll_frame = ttk.Frame(canvas)

        scroll_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=scroll_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        for idx, item in enumerate(self.failed_items):
            var = tk.BooleanVar(value=False)
            self.check_vars[idx] = (var, item)

            item_frame = ttk.Frame(scroll_frame, padding="2 4")
            item_frame.pack(fill=tk.X, anchor="w")

            cb = ttk.Checkbutton(item_frame, variable=var)
            cb.pack(side=tk.LEFT, padx=(0, 5))

            color_code = "#dc2626" if item.get("color") == "red" else "#d97706"
            txt_label = f"[{item['course_name']}] {item['title']}  —  {item['reason']}"
            lbl = tk.Label(item_frame, text=txt_label, fg=color_code, font=("Consolas", 8), anchor="w", justify=tk.LEFT)
            lbl.pack(side=tk.LEFT, fill=tk.X)

        # Bottom actions
        bottom_frame = ttk.Frame(self, padding="10")
        bottom_frame.pack(fill=tk.X)

        ttk.Button(bottom_frame, text="Ignore Selected in Future", command=self.save_ignored).pack(side=tk.RIGHT, padx=(5, 0))
        ttk.Button(bottom_frame, text="Close Without Ignoring", command=self.destroy).pack(side=tk.RIGHT)

    def select_all(self):
        for var, _ in self.check_vars.values():
            var.set(True)

    def deselect_all(self):
        for var, _ in self.check_vars.values():
            var.set(False)

    def save_ignored(self):
        ignored_count = 0
        grouped_by_course = {}

        for var, item in self.check_vars.values():
            if var.get():
                cid = item["course_id"]
                grouped_by_course.setdefault(cid, []).append(item)
                ignored_count += 1

        for cid, items in grouped_by_course.items():
            c_dir = self.course_dirs.get(cid)
            if not c_dir or not os.path.exists(c_dir):
                continue

            manifest_path = os.path.join(c_dir, ".sync_manifest.json")
            manifest = {}
            if os.path.exists(manifest_path):
                try:
                    with open(manifest_path, "r", encoding="utf-8") as f:
                        manifest = json.load(f)
                except Exception:
                    manifest = {}

            for it in items:
                topic_id = it["topic_id"]
                manifest[topic_id] = {
                    "mod_date": it.get("remote_mod", "ignored"),
                    "ignored": True,
                    "title": it["title"],
                }

            with open(manifest_path, "w", encoding="utf-8") as f:
                json.dump(manifest, f, indent=2)

        messagebox.showinfo("Ignored Updated", f"{ignored_count} item(s) will be ignored in future scans.")
        self.destroy()


# ----------------- GUI Application -----------------
class DTUSyncApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("DTU Learn Course Synchronizer")
        self.geometry("780x720")
        self.minsize(680, 540)

        self.config_data = load_config()
        self.msg_queue = queue.Queue()
        self.total_downloaded = 0
        self.course_vars = {}

        self.sync_cancel_event = threading.Event()
        self.worker = None

        self.create_widgets()
        self.after(100, self.process_queue)

        threading.Thread(target=self.check_auth_status_manual, daemon=True).start()

    def create_widgets(self):
        # 1. Settings & Directory
        settings_frame = ttk.LabelFrame(self, text="Settings & Directory", padding="10")
        settings_frame.pack(fill=tk.X, padx=10, pady=(10, 5))

        lbl_dir = ttk.Label(settings_frame, text="Download Folder:")
        lbl_dir.grid(row=0, column=0, sticky=tk.W, padx=(0, 5))

        self.var_dir = tk.StringVar(value=self.config_data["target_dir"])
        ent_dir = ttk.Entry(settings_frame, textvariable=self.var_dir)
        ent_dir.grid(row=0, column=1, sticky="ew", padx=5)

        btn_browse = ttk.Button(settings_frame, text="Browse...", command=self.browse_directory)
        btn_browse.grid(row=0, column=2, padx=(5, 0))
        settings_frame.columnconfigure(1, weight=1)

        # 2. Course Selection Checklist
        courses_box = ttk.LabelFrame(self, text="Course Selection (Uncheck to Exclude)", padding="10")
        courses_box.pack(fill=tk.X, padx=10, pady=5)

        container = ttk.Frame(courses_box)
        container.pack(fill=tk.BOTH, expand=True)

        self.canvas = tk.Canvas(container, height=110, highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(container, orient=tk.VERTICAL, command=self.canvas.yview)
        self.scrollable_frame = ttk.Frame(self.canvas)

        self.scrollable_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )
        self.canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.lbl_courses_empty = ttk.Label(self.scrollable_frame, text="Log in or click 'Start Sync' to populate courses...")
        self.lbl_courses_empty.pack(anchor="w")

        # 3. Actions Toolbar
        toolbar = ttk.Frame(self, padding="10")
        toolbar.pack(fill=tk.X, padx=5)

        self.btn_login = ttk.Button(toolbar, text="Log In to DTU Learn", command=self.trigger_login)
        self.btn_login.pack(side=tk.LEFT, padx=(0, 10))

        # Status Badge & Manual Check Button Frame
        status_badge_frame = ttk.Frame(toolbar)
        status_badge_frame.pack(side=tk.LEFT, padx=(0, 15))

        self.lbl_auth_badge = tk.Label(
            status_badge_frame,
            text="● Checking...",
            font=("Helvetica", 9, "bold"),
            fg="#888888",
            padx=4,
        )
        self.lbl_auth_badge.pack(side=tk.LEFT)

        self.btn_check_auth = ttk.Button(
            status_badge_frame,
            text="Check Status",
            width=12,
            command=self.trigger_manual_auth_check
        )
        self.btn_check_auth.pack(side=tk.LEFT, padx=(5, 0))

        # Start & Stop Buttons
        self.btn_sync = ttk.Button(toolbar, text="Start Sync", command=self.start_sync)
        self.btn_sync.pack(side=tk.LEFT, padx=(0, 5))

        self.btn_stop = ttk.Button(toolbar, text="Stop Sync", command=self.stop_sync, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT)

        # Counter display frame
        counter_frame = ttk.Frame(toolbar)
        counter_frame.pack(side=tk.RIGHT)

        self.lbl_counter = ttk.Label(counter_frame, text="Files downloaded: 0", font=("Helvetica", 9, "bold"))
        self.lbl_counter.pack(anchor="e")

        self.lbl_pending = ttk.Label(
            counter_frame,
            text="Pending download: -",
            font=("Helvetica", 8, "italic"),
            foreground="#555555"
        )
        self.lbl_pending.pack(anchor="e")

        # 4. Status Progress Bar
        status_frame = ttk.Frame(self, padding="10")
        status_frame.pack(fill=tk.X, padx=5)

        self.lbl_status = ttk.Label(status_frame, text="Ready.", anchor="w")
        self.lbl_status.pack(fill=tk.X, pady=(0, 3))

        self.lbl_current_file = ttk.Label(status_frame, text="", font=("Helvetica", 8, "italic"), foreground="#555")
        self.lbl_current_file.pack(fill=tk.X, pady=(0, 5))

        self.progress = ttk.Progressbar(status_frame, orient=tk.HORIZONTAL, mode="determinate")
        self.progress.pack(fill=tk.X)

        # 5. Activity Log (with Color Tagging)
        log_frame = ttk.LabelFrame(self, text="Activity Log", padding="10")
        log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        self.txt_log = scrolledtext.ScrolledText(log_frame, wrap=tk.WORD, font=("Consolas", 9))
        self.txt_log.pack(fill=tk.BOTH, expand=True)

        # Setup Color Tags
        self.txt_log.tag_config("normal", foreground="#1e293b")
        self.txt_log.tag_config("green", foreground="#16a34a")
        self.txt_log.tag_config("orange", foreground="#d97706")
        self.txt_log.tag_config("red", foreground="#dc2626")

    def browse_directory(self):
        chosen = filedialog.askdirectory(initialdir=self.var_dir.get(), title="Select Download Folder")
        if chosen:
            self.var_dir.set(chosen)
            self.config_data["target_dir"] = chosen
            save_config(self.config_data)

    def trigger_manual_auth_check(self):
        self.btn_check_auth.config(state=tk.DISABLED)
        self.lbl_auth_badge.config(text="● Checking...", fg="#888888")
        threading.Thread(target=self.check_auth_status_manual, daemon=True).start()

    def check_auth_status_manual(self):
        session, _ = get_authenticated_session()
        is_valid = session is not None
        self.msg_queue.put(("auth_status", is_valid))

        if is_valid and not self.course_vars:
            try:
                url = f"{BASE_URL}/d2l/api/lp/{API_VERSIONS['lp']}/enrollments/myenrollments/?orgUnitTypeId=3"
                resp = session.get(url, timeout=10)
                if resp.status_code == 200:
                    items = resp.json().get("Items", [])
                    courses = [
                        {
                            "id": str(it.get("OrgUnit", {}).get("Id")),
                            "name": it.get("OrgUnit", {}).get("Name"),
                            "code": it.get("OrgUnit", {}).get("Code", ""),
                        }
                        for it in items
                    ]
                    self.msg_queue.put(("course_list", courses))
            except Exception:
                pass

        self.msg_queue.put(("auth_check_finished", None))

    def update_auth_badge(self, is_logged_in: bool):
        if is_logged_in:
            self.lbl_auth_badge.config(text="● Logged In", fg="#1b873f")
        else:
            self.lbl_auth_badge.config(text="● Not Logged In", fg="#c82333")

    def trigger_login(self):
        self.btn_login.config(state=tk.DISABLED)
        self.lbl_status.config(text="Opening sign-in window...")

        def runner():
            try:
                p = multiprocessing.Process(target=_webview_worker)
                p.start()
                p.join()

                session, err = get_authenticated_session()
                if session:
                    self.msg_queue.put(("login_success", None))
                else:
                    self.msg_queue.put(("login_cancel", err))
            except Exception as e:
                self.msg_queue.put(("error", f"Login window process failed:\n{e}"))
                self.msg_queue.put(("login_cancel", None))

        threading.Thread(target=runner, daemon=True).start()

    def update_course_list_ui(self, courses):
        if not courses:
            return
        if self.lbl_courses_empty.winfo_exists():
            self.lbl_courses_empty.destroy()

        saved_excluded = set(self.config_data.get("excluded_courses", []))
        for c in courses:
            cid = c["id"]
            if cid in self.course_vars:
                continue

            var = tk.BooleanVar(value=cid not in saved_excluded)
            self.course_vars[cid] = var
            label = f"{c['code']} - {c['name']}" if c.get("code") else c["name"]
            cb = ttk.Checkbutton(
                self.scrollable_frame,
                text=label,
                variable=var,
                command=self.persist_excluded_courses
            )
            cb.pack(anchor="w", padx=5, pady=2)

    def persist_excluded_courses(self):
        self.config_data["excluded_courses"] = [cid for cid, var in self.course_vars.items() if not var.get()]
        save_config(self.config_data)

    def start_sync(self):
        self.btn_sync.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL)
        self.sync_cancel_event.clear()

        self.total_downloaded = 0
        self.lbl_counter.config(text="Files downloaded: 0")
        self.lbl_pending.config(text="Scanning...")
        self.progress["value"] = 0
        self.txt_log.delete("1.0", tk.END)

        target_dir = self.var_dir.get().strip() or DEFAULT_TARGET_DIR
        excluded = [cid for cid, var in self.course_vars.items() if not var.get()]

        self.worker = SyncWorker(self.msg_queue, target_dir, excluded, self.sync_cancel_event)
        self.worker.start()

    def stop_sync(self):
        if self.worker and self.worker.is_alive():
            self.lbl_status.config(text="Stopping sync... please wait.")
            self.btn_stop.config(state=tk.DISABLED)
            self.sync_cancel_event.set()

    def process_queue(self):
        try:
            while True:
                msg_type, payload = self.msg_queue.get_nowait()

                if msg_type == "auth_status":
                    self.update_auth_badge(payload)
                elif msg_type == "auth_check_finished":
                    self.btn_check_auth.config(state=tk.NORMAL)
                elif msg_type == "status":
                    self.lbl_status.config(text=payload)
                elif msg_type == "pending_count":
                    self.lbl_pending.config(text=f"Pending download: {payload}")
                elif msg_type == "file_downloading":
                    self.lbl_current_file.config(text=payload)
                elif msg_type == "file_counter_inc":
                    self.total_downloaded += 1
                    self.lbl_counter.config(text=f"Files downloaded: {self.total_downloaded}")
                elif msg_type == "set_max_progress":
                    self.progress["maximum"] = payload
                elif msg_type == "file_progress":
                    self.progress["value"] = payload
                elif msg_type == "course_list":
                    self.update_course_list_ui(payload)
                elif msg_type == "login_success":
                    self.btn_login.config(state=tk.NORMAL)
                    self.update_auth_badge(True)
                    self.lbl_status.config(text="Authenticated.")
                    messagebox.showinfo("Logged In", "Authentication successful! Your session is encrypted and ready.")
                    threading.Thread(target=self.check_auth_status_manual, daemon=True).start()
                elif msg_type == "login_cancel":
                    self.btn_login.config(state=tk.NORMAL)
                    self.update_auth_badge(False)
                    self.lbl_status.config(text="Login closed or canceled.")
                elif msg_type == "log":
                    if isinstance(payload, tuple):
                        text, tag = payload
                    else:
                        text, tag = payload, "normal"
                    self.txt_log.insert(tk.END, text + "\n", tag)
                    self.txt_log.see(tk.END)
                elif msg_type == "error":
                    messagebox.showerror("Sync Error", payload)
                    self.lbl_status.config(text="Sync stopped due to an error.")
                elif msg_type == "cleanup_done":
                    self.btn_sync.config(state=tk.NORMAL)
                    self.btn_stop.config(state=tk.DISABLED)
                    self.lbl_current_file.config(text="")
                elif msg_type == "done":
                    if payload:
                        messagebox.showinfo("Finished", payload)
                elif msg_type == "show_failed_dialog":
                    failed_items, course_dirs = payload
                    FailedItemsDialog(self, failed_items, course_dirs)
        except queue.Empty:
            pass

        self.after(100, self.process_queue)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    app = DTUSyncApp()
    app.mainloop()