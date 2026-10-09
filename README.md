# DTU Learn Course Synchronizer (`sync-my-dtulearn`)

An open-source desktop utility for students at the Technical University of Denmark (DTU) to synchronize and mirror course documents, lecture slides, and exercise files from DTU Learn (`learn.inside.dtu.dk`) to their local disk.

---

## Disclaimer & Responsible Use

- **Independent Project**: This software is developed independently by a student at DTU. It is **not** an official product of, supported by, or affiliated with the Technical University of Denmark (DTU) or D2L Brightspace.
- **No Content Sharing**: This tool **does not host, redistribute, or share course materials**. It functions strictly as a local download assistant that retrieves only the documents and courses that the logged-in student has legitimate, authenticated access to.
- **Responsible Infrastructure Use**: The application places no continuous or automated background load on DTU’s IT infrastructure. It communicates with DTU Learn servers **solely upon explicit user action** (launching login, clicking *Check Status*, or starting a sync). Automatic periodic polling is deliberately disabled.
- **Contact & Concerns**: If you are a student, lecturer, or member of DTU IT administration with questions or concerns regarding this tool, please reach out directly to **[s263327@dtu.dk](mailto:s263327@dtu.dk)**.

---

## What the Software Does

1. **In-App Authentication**: Opens a built-in login window powered by the native Microsoft Edge WebView2 runtime. You authenticate directly through DTU's official ADFS / MitID single-sign-on portal with two-factor authentication (2FA).
2. **API-Driven Course Discovery**: Communicates directly with Brightspace's Valence REST API on `learn.inside.dtu.dk` to query the list of course offerings you are officially enrolled in.
3. **Pre-Scan & Differential Comparison**: Traverses the course module structure and compares remote modification timestamps (`LastModifiedDate`) against local files and a local `.sync_manifest.json` state index.
4. **Selective Download**:
   - Downloads **only** new files or documents modified since your last sync run.
   - Skips already up-to-date files without re-downloading them.
   - Converts external web links into double-clickable Windows `.url` Internet Shortcuts.
   - Generates browser launcher stubs (`.html`) for interactive external assignments (e.g., FeedbackFruits).
5. **Issue Review Dialog**: If any file fails to download or has irregular metadata, an interactive review window allows you to permanently mark specific items to be ignored in future scans.

---

## Security, Privacy & Data Safety

Data protection and account safety are core design priorities of this application:

- **Zero Data Leaves Your Machine**: No analytics, telemetry, crash reporting, or external servers are contacted. The only host the application ever communicates with is **`https://learn.inside.dtu.dk`**.
- **No Password Access**: Your username and password are entered directly into DTU’s official login page inside the sandboxed WebView2 engine. The application itself never sees, reads, or records your password.
- **Windows DPAPI Encryption**: Captured session tokens (`d2lSessionVal`) are encrypted using the native Windows Data Protection API (`CryptProtectData`). The session file (`~/.dtu_sync_session.bin`) is cryptographically tied to your specific Windows user login context. Other local users or processes cannot decrypt the tokens.
- **Path Traversal Sandboxing**: Every directory name and file path is sanitized and verified using strict canonical prefix boundary checks (`safe_join`). Files are strictly prevented from writing outside your designated download directory.
- **Atomic Writes**: Downloads stream into temporary `.part` files and rename only upon clean completion. Canceling a sync run leaves no corrupted or half-written documents on your machine.

---

## How to Download & Run

### Option 1: Standalone Windows Executable (Recommended)
*No Python installation required.*

1. Go to the repository's **[Releases](../../releases)** page.
2. Download the latest `DTU_Learn_Sync_Windows.zip` archive.
3. Extract the ZIP archive to a folder of your choice.
4. Run **`DTU_Learn_Sync.exe`**.

### Option 2: Run from Source
*For developers or users who prefer inspecting the code directly.*

1. Clone this repository:
   ```bash
   git clone https://github.com/YOUR_USERNAME/sync-my-dtulearn.git
   cd sync-my-dtulearn
   ```
2. Create and activate a virtual environment:
   ```bash
   python -m venv .venv
   .venv\Scripts\activate
   ```
3. Install the dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. Launch the application:
   ```bash
   python src/sync_dtu_gui.py
   ```

---

## License

This program is free software: you can redistribute it and/or modify it under the terms of the **[GNU General Public License v3.0](LICENSE)** (GPLv3).

Anyone is free to inspect, fork, and adapt the software, with the condition that all derivative works and redistributed modifications **must remain free and open source** under the exact same GPLv3 terms.

---

## Acknowledgments & Credits

- **Inspiration**: The concept is inspired by **[Sync-my-L2P](https://github.com/RobertKrajewski/Sync-my-L2P)**, originally developed by Robert Krajewski and fellow student contributors for RWTH Aachen University.
- **Development**: Vibe-coded and iteratively architected with the assistance of **Gemini 3.8 Flash (Thinking)**, and verified, reviewed, and tested against real DTU Learn course contents by the author.

Feedback, bug reports, and contributions are welcome! Please open an issue on GitHub or email **[s263327@dtu.dk](mailto:s263327@dtu.dk)**.