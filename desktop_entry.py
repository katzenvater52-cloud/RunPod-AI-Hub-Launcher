"""
RunPod One-Click AI Hub - Desktop EXE Entry

This entry is used by PyInstaller. It runs the FastAPI backend and serves the
React production build from backend/static, so the installed EXE no longer needs
Vite at runtime.
"""
import os
import sys
import time
import threading
import webbrowser
from pathlib import Path


def resource_root() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


ROOT = resource_root()
BACKEND_DIR = ROOT / "backend"
STATIC_DIR = BACKEND_DIR / "static"
APPDATA = Path(os.environ.get("APPDATA", str(Path.home()))) / "RunPodOneClickAIHub_V31"
APPDATA.mkdir(parents=True, exist_ok=True)
LOG_FILE = APPDATA / "desktop.log"


def log(message: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"[{stamp}] {message}\n")
    except Exception:
        pass


def open_browser_robust(url: str) -> bool:
    """Open the local UI exactly once.

    V31.5 Stability Polish: older alpha launchers tried several Windows
    fallback methods in a row. That made some systems open 4-5 browser tabs.
    We now use one guarded launch attempt and keep the Safe Launcher window as
    the fallback if Windows/browser association blocks it.
    """
    lock_file = APPDATA / "browser_open.lock"
    now = time.time()
    try:
        if lock_file.exists():
            age = now - lock_file.stat().st_mtime
            if age < 20:
                log(f"Browser open suppressed by V31.5 guard ({age:.1f}s old): {url}")
                return True
        lock_file.write_text(str(now), encoding="utf-8")
    except Exception as exc:
        log(f"Browser open guard warning: {type(exc).__name__}: {exc}")

    log(f"Single browser open requested: {url}")

    try:
        if os.name == "nt":
            try:
                os.startfile(url)  # type: ignore[attr-defined]
                log("Browser launched once with os.startfile")
                return True
            except Exception as exc:
                log(f"os.startfile failed, trying webbrowser.open: {type(exc).__name__}: {exc}")

        opened = webbrowser.open(url, new=2, autoraise=True)
        log(f"webbrowser.open returned: {opened}")
        return bool(opened)
    except Exception as exc:
        log(f"Browser open failed: {type(exc).__name__}: {exc}")
        log(f"Please open manually: {url}")
        return False

def show_safe_launcher_window(url: str) -> None:
    """Always show a small fallback window with the local URL.

    This is the safest Windows behavior: even if auto-open is blocked by a
    browser, security suite, default-app issue or PyInstaller quirk, the user
    still gets a visible window with an Open button and the exact URL.
    """
    log(f"Showing safe launcher window for: {url}")
    try:
        import tkinter as tk
        from tkinter import messagebox

        win = tk.Tk()
        win.title("RunPod AI Hub")
        win.geometry("520x240")
        win.resizable(False, False)
        win.configure(bg="#07111f")
        try:
            win.attributes("-topmost", True)
            win.after(1500, lambda: win.attributes("-topmost", False))
        except Exception:
            pass

        tk.Label(
            win,
            text="RunPod AI Hub is running",
            bg="#07111f",
            fg="#6ee7b7",
            font=("Segoe UI", 18, "bold"),
        ).pack(pady=(22, 4))

        tk.Label(
            win,
            text="If the browser did not open automatically, use this safe launcher.",
            bg="#07111f",
            fg="#cbd5e1",
            font=("Segoe UI", 10),
            wraplength=460,
            justify="center",
        ).pack(pady=(0, 14))

        entry = tk.Entry(
            win,
            font=("Consolas", 11),
            bg="#020617",
            fg="#e5e7eb",
            insertbackground="#e5e7eb",
            relief="flat",
            justify="center",
        )
        entry.insert(0, url)
        entry.configure(state="readonly")
        entry.pack(fill="x", padx=38, ipady=8)

        def open_now() -> None:
            open_browser_robust(url)

        def copy_url() -> None:
            try:
                win.clipboard_clear()
                win.clipboard_append(url)
                messagebox.showinfo("Copied", "URL was copied to the clipboard.")
            except Exception as exc:
                log(f"Copy URL failed: {type(exc).__name__}: {exc}")

        btn_frame = tk.Frame(win, bg="#07111f")
        btn_frame.pack(pady=18)

        tk.Button(
            btn_frame,
            text="Open browser",
            command=open_now,
            bg="#10b981",
            fg="#02130d",
            activebackground="#34d399",
            activeforeground="#02130d",
            relief="flat",
            padx=22,
            pady=9,
            font=("Segoe UI", 10, "bold"),
        ).pack(side="left", padx=8)

        tk.Button(
            btn_frame,
            text="Copy URL",
            command=copy_url,
            bg="#1e293b",
            fg="#e5e7eb",
            activebackground="#334155",
            activeforeground="#ffffff",
            relief="flat",
            padx=22,
            pady=9,
            font=("Segoe UI", 10, "bold"),
        ).pack(side="left", padx=8)

        tk.Label(
            win,
            text="You can close this window. Backend/frontend keep running.",
            bg="#07111f",
            fg="#64748b",
            font=("Segoe UI", 9),
        ).pack(pady=(0, 8))

        win.mainloop()
    except Exception as exc:
        log(f"Safe launcher window failed: {type(exc).__name__}: {exc}")


def open_browser_later(url: str, delay: float = 0.5) -> None:
    def _open() -> None:
        import urllib.request
        health_url = url.rstrip("/") + "/api/health"
        index_url = url.rstrip("/") + "/"
        log(f"Waiting for backend/UI before browser open: {health_url}")

        health_ok = False
        for attempt in range(1, 121):
            time.sleep(delay)
            try:
                with urllib.request.urlopen(health_url, timeout=1.5) as response:
                    status = int(getattr(response, "status", 0) or 0)
                    if 200 <= status < 500:
                        health_ok = True
                        log(f"Backend ready for auto-open after attempt {attempt}: HTTP {status}")
                        break
            except Exception as exc:
                if attempt in (1, 10, 30, 60, 120):
                    log(f"Waiting for backend attempt {attempt}: {type(exc).__name__}: {exc}")
                continue

        if not health_ok:
            log(f"Backend health did not become ready. Manual URL: {index_url}")
            return

        # Give the SPA/static route a tiny moment after health is ready.
        for attempt in range(1, 21):
            try:
                with urllib.request.urlopen(index_url, timeout=1.5) as response:
                    status = int(getattr(response, "status", 0) or 0)
                    log(f"UI route check before open: HTTP {status}")
                    break
            except Exception as exc:
                if attempt in (1, 5, 10, 20):
                    log(f"Waiting for UI route attempt {attempt}: {type(exc).__name__}: {exc}")
                time.sleep(delay)

        open_browser_robust(index_url)
        show_safe_launcher_window(index_url)

    threading.Thread(target=_open, daemon=True).start()

def main() -> int:
    log("=== RunPod AI Hub Desktop EXE start — V31.5 STABILITY LAUNCHER ===")
    log(f"Executable: {sys.executable}")
    log(f"Resource root: {ROOT}")
    log(f"Backend dir: {BACKEND_DIR}")
    log(f"Static dir: {STATIC_DIR}")

    if not (BACKEND_DIR / "main.py").exists():
        raise RuntimeError(f"backend/main.py fehlt im EXE-Bundle: {BACKEND_DIR}")
    if not (STATIC_DIR / "index.html").exists():
        raise RuntimeError(
            "React production build fehlt: backend/static/index.html. "
            "Please run BUILD_EXE.bat so frontend/dist gets copied to backend/static."
        )

    sys.path.insert(0, str(BACKEND_DIR))
    os.chdir(str(BACKEND_DIR))

    import uvicorn
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles
    import main as backend_main

    app = backend_main.app

    assets_dir = STATIC_DIR / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

    @app.get("/")
    async def desktop_index():
        return FileResponse(str(STATIC_DIR / "index.html"))

    @app.get("/{full_path:path}")
    async def desktop_spa_fallback(full_path: str):
        # API routes are already registered before this fallback.
        maybe_file = STATIC_DIR / full_path
        if maybe_file.exists() and maybe_file.is_file():
            return FileResponse(str(maybe_file))
        return FileResponse(str(STATIC_DIR / "index.html"))

    url = "http://127.0.0.1:8000"
    open_browser_later(url)
    log(f"Starting uvicorn at {url}")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        log(f"ERROR: {type(exc).__name__}: {exc}")
        try:
            import traceback
            log(traceback.format_exc())
        except Exception:
            pass
        raise
