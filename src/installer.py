# Copyright (C) 2026 Jonas
# Installer and Auto-Updater for Sync-My-DTULearn

import os
import sys
import json
import shutil
import zipfile
import threading
import subprocess
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import urllib.request

# --- Configuration ---
GITHUB_OWNER = "jonas-m-w"
GITHUB_REPO = "sync-my-dtulearn"
APP_NAME = "Sync-My-DTULearn"
ZIP_NAME = "Sync-My-DTULearn_Windows.zip"
EXE_NAME = "Sync-My-DTULearn.exe"

DEFAULT_INSTALL_DIR = os.path.expanduser(f"~/AppData/Local/Programs/{APP_NAME}")
CONFIG_PATH = os.path.expanduser("~/.dtu_sync_install_path.json")


def get_stored_install_path():
    """Retrieves the prior installation path if recorded."""
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                path = data.get("install_dir")
                if path and os.path.exists(path):
                    return path
        except Exception:
            pass
    return DEFAULT_INSTALL_DIR


def save_install_path(path):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump({"install_dir": os.path.abspath(path)}, f, indent=2)
    except Exception:
        pass


def kill_running_process():
    """Ensures existing instances of the app are closed so files can be removed."""
    if sys.platform == "win32":
        try:
            subprocess.run(["taskkill", "/f", "/im", EXE_NAME], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


def create_desktop_shortcut(target_exe, shortcut_name):
    """Creates a Windows Desktop shortcut (.lnk) using PowerShell."""
    if sys.platform != "win32":
        return
    try:
        desktop = os.path.join(os.environ["USERPROFILE"], "Desktop")
        lnk_path = os.path.join(desktop, f"{shortcut_name}.lnk")
        ps_script = (
            f"$ws = New-Object -ComObject WScript.Shell; "
            f"$s = $ws.CreateShortcut('{lnk_path}'); "
            f"$s.TargetPath = '{target_exe}'; "
            f"$s.WorkingDirectory = '{os.path.dirname(target_exe)}'; "
            f"$s.Save()"
        )
        subprocess.run(["powershell", "-NoProfile", "-Command", ps_script], check=True, stdout=subprocess.DEVNULL)
    except Exception:
        pass


def get_icon_path():
    candidates = [
        os.path.join(getattr(sys, "_MEIPASS", ""), "app_icon.ico"),
        os.path.join(os.path.dirname(sys.executable), "app_icon.ico"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "app_icon.ico"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "app_icon.ico"),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None


class InstallerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} - Setup & Update")
        self.geometry("540x320")
        self.resizable(False, False)

        icon_path = get_icon_path()
        if icon_path:
            try:
                self.iconbitmap(default=icon_path)
            except Exception:
                pass

        self.detected_path = get_stored_install_path()
        self.var_dir = tk.StringVar(value=self.detected_path)
        self.var_shortcut = tk.BooleanVar(value=True)

        self.create_widgets()

    def create_widgets(self):
        frame = ttk.Frame(self, padding="20")
        frame.pack(fill=tk.BOTH, expand=True)

        lbl_header = ttk.Label(
            frame,
            text=f"Install or Update {APP_NAME}",
            font=("Helvetica", 12, "bold")
        )
        lbl_header.pack(anchor="w", pady=(0, 5))

        lbl_desc = ttk.Label(
            frame,
            text="This utility downloads the latest release from GitHub,\n"
                 "removes outdated program binaries, and installs the new version.",
            font=("Helvetica", 9)
        )
        lbl_desc.pack(anchor="w", pady=(0, 15))

        folder_frame = ttk.LabelFrame(frame, text="Installation Location", padding="10")
        folder_frame.pack(fill=tk.X, pady=(0, 10))

        ent_dir = ttk.Entry(folder_frame, textvariable=self.var_dir)
        ent_dir.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))

        btn_browse = ttk.Button(folder_frame, text="Browse...", command=self.browse_folder)
        btn_browse.pack(side=tk.RIGHT)

        cb_shortcut = ttk.Checkbutton(frame, text="Create Desktop Shortcut", variable=self.var_shortcut)
        cb_shortcut.pack(anchor="w", pady=(0, 15))

        self.lbl_status = ttk.Label(frame, text="Ready.", font=("Helvetica", 9, "italic"))
        self.lbl_status.pack(anchor="w", pady=(0, 4))

        self.progress = ttk.Progressbar(frame, orient=tk.HORIZONTAL, mode="determinate")
        self.progress.pack(fill=tk.X, pady=(0, 15))

        btn_box = ttk.Frame(frame)
        btn_box.pack(fill=tk.X)

        self.btn_install = ttk.Button(btn_box, text="Install / Update", command=self.start_install)
        self.btn_install.pack(side=tk.RIGHT, padx=(5, 0))

        self.btn_cancel = ttk.Button(btn_box, text="Exit", command=self.destroy)
        self.btn_cancel.pack(side=tk.RIGHT)

    def browse_folder(self):
        chosen = filedialog.askdirectory(initialdir=self.var_dir.get(), title="Select Install Folder")
        if chosen:
            self.var_dir.set(os.path.abspath(chosen))

    def set_ui_busy(self, is_busy: bool):
        state = tk.DISABLED if is_busy else tk.NORMAL
        self.btn_install.config(state=state)
        self.btn_cancel.config(state=state)

    def start_install(self):
        self.set_ui_busy(True)
        threading.Thread(target=self.run_installation, daemon=True).start()

    def run_installation(self):
        target_dir = os.path.abspath(self.var_dir.get().strip())
        zip_temp_path = os.path.join(os.environ.get("TEMP", os.getcwd()), ZIP_NAME)

        try:
            # 1. Download Latest Release directly via static CDN redirect (NO API RATE LIMIT)
            download_url = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest/download/{ZIP_NAME}"
            self.lbl_status.config(text="Connecting to GitHub...")

            opener = urllib.request.build_opener()
            opener.addheaders = [("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")]
            urllib.request.install_opener(opener)

            def report_progress(block_num, block_size, total_size):
                if total_size > 0:
                    percent = min(100.0, (block_num * block_size / total_size) * 100)
                    self.progress["value"] = percent
                    mb_curr = (block_num * block_size) / (1024 * 1024)
                    mb_tot = total_size / (1024 * 1024)
                    self.lbl_status.config(text=f"Downloading latest release: {mb_curr:.1f} MB / {mb_tot:.1f} MB ({percent:.0f}%)")

            urllib.request.urlretrieve(download_url, zip_temp_path, reporthook=report_progress)

            # 2. Stop running instances
            self.lbl_status.config(text="Closing existing instances...")
            kill_running_process()

            # 3. Clean previous binaries
            if os.path.exists(target_dir):
                self.lbl_status.config(text="Removing previous version files...")
                shutil.rmtree(target_dir, ignore_errors=True)

            os.makedirs(target_dir, exist_ok=True)

            # 4. Extract archive
            self.lbl_status.config(text="Extracting program files...")
            with zipfile.ZipFile(zip_temp_path, "r") as zip_ref:
                zip_ref.extractall(target_dir)

            if os.path.exists(zip_temp_path):
                os.remove(zip_temp_path)

            # 5. Locate executable and set up Desktop shortcut
            exe_path = os.path.join(target_dir, EXE_NAME)
            if not os.path.exists(exe_path):
                for root, dirs, files in os.walk(target_dir):
                    if EXE_NAME in files:
                        exe_path = os.path.join(root, EXE_NAME)
                        break

            if self.var_shortcut.get() and os.path.exists(exe_path):
                create_desktop_shortcut(exe_path, APP_NAME)

            save_install_path(target_dir)

            self.progress["value"] = 100
            self.lbl_status.config(text="Installation completed successfully!")

            launch = messagebox.askyesno(
                "Update Complete",
                f"Successfully installed the latest version!\n\nWould you like to launch {APP_NAME} now?"
            )
            if launch and os.path.exists(exe_path):
                subprocess.Popen([exe_path], cwd=os.path.dirname(exe_path))

            self.destroy()

        except Exception as e:
            self.lbl_status.config(text="Installation failed.")
            messagebox.showerror("Installation Error", f"Failed to complete installation:\n\n{e}")
            self.set_ui_busy(False)


if __name__ == "__main__":
    app = InstallerApp()
    app.mainloop()