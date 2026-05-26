import os
import shutil
import subprocess
import sys
import platform
import time
import webbrowser
import traceback
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent
BACKEND_DIR = ROOT / "backend"
FRONTEND_DIR = ROOT / "frontend"
BACKEND_HEALTH_URL = "http://127.0.0.1:8000/api/health"
FRONTEND_PORT = 5173
FRONTEND_PORT_CLEANUP_RANGE = range(5173, 5186)
FRONTEND_URL = f"http://127.0.0.1:{FRONTEND_PORT}"

APPDATA = Path(os.environ.get("APPDATA", str(Path.home()))) / "RunPodOneClickAIHub_V31"
APPDATA.mkdir(parents=True, exist_ok=True)
LOG_FILE = APPDATA / "launcher.log"
BACKEND_LOG = APPDATA / "backend.log"
FRONTEND_LOG = APPDATA / "frontend.log"

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

SYSTEM_REPORT = APPDATA / "system_check.txt"



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

def version_of(cmd):
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            creationflags=CREATE_NO_WINDOW,
            timeout=12,
        )
        out = (result.stdout or result.stderr or "").strip().splitlines()
        return result.returncode == 0, out[0] if out else "gefunden"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def write_system_report(npm_path: str | None = None) -> None:
    py_ok = sys.version_info.major == 3 and sys.version_info.minor in (10, 11, 12, 13, 14)
    node_path = shutil.which("node.exe") or shutil.which("node")
    npm_path = npm_path or shutil.which("npm.cmd") or shutil.which("npm")
    node_ok, node_ver = version_of([node_path, "--version"]) if node_path else (False, "not found")
    npm_ok, npm_ver = version_of([npm_path, "--version"]) if npm_path else (False, "not found")
    pip_ok, pip_ver = version_of([sys.executable, "-m", "pip", "--version"])
    lines = [
        "RunPod AI Hub V31.5 System check",
        "=========================================",
        f"Python: {'OK' if py_ok else 'MISSING/INVALID'} — {sys.executable}",
        f"Python Version: {sys.version.splitlines()[0]}",
        f"pip: {'OK' if pip_ok else 'FEHLT'} — {pip_ver}",
        f"Node.js: {'OK' if node_ok else 'FEHLT'} — {node_ver}",
        f"npm: {'OK' if npm_ok else 'FEHLT'} — {npm_ver}",
        "",
        "Falls etwas fehlt:",
        "- Python 3.10 bis 3.12/3.13/3.14 64-bit installieren und 'Add to PATH' activeieren.",
        "- Node.js LTS installieren. npm wird dabei mitinstalliert.",
        "- Danach START_HERE_VISIBLE.bat neu start.",
        "",
        f"AppData log folder: {APPDATA}",
    ]
    try:
        SYSTEM_REPORT.write_text("\n".join(lines), encoding="utf-8")
    except Exception:
        pass
    for line in lines:
        log(line)


def log(line: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(f"[{stamp}] {line}\n")


def run_hidden(cmd, cwd: Path, logfile: Path, env=None) -> None:
    log(f"RUN: {' '.join(map(str, cmd))} | cwd={cwd}")
    with logfile.open("a", encoding="utf-8", errors="replace") as f:
        f.write(f"\n--- RUN {' '.join(map(str, cmd))} ---\n")
        f.flush()
        subprocess.check_call(
            cmd,
            cwd=str(cwd),
            stdout=f,
            stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW,
            env=env,
        )
    log(f"DONE: {' '.join(map(str, cmd))}")


def popen_hidden(cmd, cwd: Path, logfile: Path, env=None):
    log(f"START: {' '.join(map(str, cmd))} | cwd={cwd}")
    f = logfile.open("a", encoding="utf-8", errors="replace")
    f.write(f"\n--- START {' '.join(map(str, cmd))} ---\n")
    f.flush()
    return subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        stdout=f,
        stderr=subprocess.STDOUT,
        creationflags=CREATE_NO_WINDOW,
        env=env,
    )


def find_npm() -> str:
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if npm:
        return npm

    common = [
        Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "nodejs" / "npm.cmd",
        Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "nodejs" / "npm.cmd",
        Path(os.environ.get("APPDATA", str(Path.home()))) / "npm" / "npm.cmd",
    ]
    for path in common:
        if path.exists():
            return str(path)

    raise RuntimeError("Node.js/npm wurde not found. Bitte Node.js LTS installieren (https://nodejs.org/), danach START_HERE_VISIBLE.bat neu start. Details: %APPDATA%\\RunPodOneClickAIHub_V31\\system_check.txt")




def ensure_supported_python() -> None:
    version = sys.version_info
    # V15: Auf dem Testsystem ist offenbar nur Python 3.14 installiert.
    # Die Requirements sind inzwischen auf moderne Versionen gesetzt und pip nutzt --only-binary,
    # deshalb erlauben wir 3.10 bis 3.14.
    if version.major != 3 or version.minor not in (10, 11, 12, 13, 14):
        raise RuntimeError(
            f"Unsupported Python {version.major}.{version.minor}.{version.micro}. "
            "Please use Python 3.10 to 3.14 64-bit."
        )



def kill_processes_on_port(port: int) -> None:
    """Beendet alte Backend/Frontend-Prozesse, die den lokalen Port blockieren.
    V22: taskkill bekommt ein Timeout, damit der Launcher nicht beim Aufraeumen haengen bleibt.
    """
    if os.name != "nt":
        return
    try:
        result = subprocess.run(
            ["cmd", "/c", f"netstat -ano | findstr :{port}"],
            capture_output=True,
            text=True,
            creationflags=CREATE_NO_WINDOW,
            timeout=8,
        )
        pids = set()
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 5:
                local_addr = parts[1]
                pid = parts[-1]
                if local_addr.endswith(f":{port}") and pid.isdigit() and pid != "0":
                    pids.add(pid)
        for pid in sorted(pids):
            log(f"Killing old process on port {port}: PID {pid}")
            try:
                subprocess.run(
                    ["taskkill", "/PID", pid, "/F", "/T"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=CREATE_NO_WINDOW,
                    timeout=8,
                )
            except subprocess.TimeoutExpired:
                log(f"taskkill timeout on port {port}, PID {pid}; continuing anyway")
    except subprocess.TimeoutExpired:
        log(f"netstat cleanup timeout on port {port}; continuing anyway")
    except Exception as exc:
        log(f"Could not clean port {port}: {type(exc).__name__}: {exc}")


def wait_for_url(url: str, seconds: int) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        try:
            with urlopen(url, timeout=2) as response:
                if 200 <= response.status < 500:
                    return True
        except Exception:
            time.sleep(1)
    return False


def main() -> int:
    # V10 schreibt nur noch in RunPodOneClickAIHub_V31, damit alte Logs nicht mehr verwirren.
    for _log_file in (LOG_FILE, BACKEND_LOG, FRONTEND_LOG):
        try:
            _log_file.write_text("", encoding="utf-8")
        except Exception:
            pass
    log("=== RunPod AI Hub launcher start — V31.5 STABILITY LAUNCHER ===")
    log(f"Python: {sys.executable}")
    log(f"Python version: {sys.version}")
    ensure_supported_python()
    log(f"Root: {ROOT}")

    if not (BACKEND_DIR / "main.py").exists():
        raise RuntimeError(f"backend/main.py wurde not found: {BACKEND_DIR}")
    if not (FRONTEND_DIR / "package.json").exists():
        raise RuntimeError(f"frontend/package.json wurde not found: {FRONTEND_DIR}")

    npm = find_npm()
    log(f"npm: {npm}")
    write_system_report(npm)

    # Vor jedem Start alte Instanzen entfernen, damit die Ports stabil bleiben.
    kill_processes_on_port(8000)
    for _port in FRONTEND_PORT_CLEANUP_RANGE:
        kill_processes_on_port(_port)
    # V22: Kein globales taskkill node.exe mehr.
    # Das konnte auf manchen Windows-Systemen haengen bleiben und andere Node-Prozesse erwischen.
    # Wir raeumen nur noch gezielt die Ports 8000 und 5173-5185 auf.
    log("Extra cleanup skipped: no global node.exe taskkill in V25")
    time.sleep(1)
    log("Port cleanup finished. Frontend will be forced to http://127.0.0.1:5173 by package.json and vite.config.js")

    if platform.architecture()[0] != "64bit":
        raise RuntimeError("Please install 64-bit Python. Many Windows packages are not available for 32-bit Python.")

    # V17: Keine venv mehr erstellen. Portable/StabilityMatrix-Python hängt oft bei `python -m venv`.
    # Stattdessen installieren wir die Backend-Abhängigkeiten isoliert in backend/.pydeps
    # und start uvicorn mit erweitertem PYTHONPATH. Das vermeidet venv-Probleme komplett.
    pydeps = BACKEND_DIR / ".pydeps_v25"
    pydeps.mkdir(exist_ok=True)
    run_hidden([sys.executable, "-m", "pip", "--version"], BACKEND_DIR, BACKEND_LOG)
    deps_marker = pydeps / ".installed_v25"
    if not deps_marker.exists():
        run_hidden([
            sys.executable,
            "-m",
            "pip",
            "install",
            "--target",
            str(pydeps),
            "--only-binary=:all:",
            "--prefer-binary",
            "--no-cache-dir",
            "-r",
            "requirements.txt",
        ], BACKEND_DIR, BACKEND_LOG)
        deps_marker.write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
    else:
        log(f"Backend dependencies already installed in {pydeps}; skipping pip install")
        with BACKEND_LOG.open("a", encoding="utf-8", errors="replace") as f:
            f.write(f"\n--- SKIP pip install: existing {deps_marker} ---\n")

    app_env = os.environ.copy()
    old_pythonpath = app_env.get("PYTHONPATH", "")
    app_env["PYTHONPATH"] = str(pydeps) + (os.pathsep + old_pythonpath if old_pythonpath else "")

    npm_marker = FRONTEND_DIR / "node_modules" / ".installed_v25"
    if not npm_marker.exists():
        run_hidden([npm, "install"], FRONTEND_DIR, FRONTEND_LOG)
        npm_marker.parent.mkdir(exist_ok=True)
        npm_marker.write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
    else:
        log("Frontend dependencies already installed; skipping npm install")
        with FRONTEND_LOG.open("a", encoding="utf-8", errors="replace") as f:
            f.write("\n--- SKIP npm install: node_modules already prepared ---\n")

    backend_proc = popen_hidden([
        sys.executable,
        "-m",
        "uvicorn",
        "main:app",
        "--host",
        "127.0.0.1",
        "--port",
        "8000",
    ], BACKEND_DIR, BACKEND_LOG, env=app_env)

    time.sleep(3)
    if backend_proc.poll() is not None:
        raise RuntimeError(f"Backend could not be started. Exit code: {backend_proc.returncode}. Details are in backend.log")

    if not wait_for_url(BACKEND_HEALTH_URL, 45):
        raise RuntimeError("Backend health URL is not responding. Details are in backend.log")

    frontend_proc = popen_hidden([
        "cmd",
        "/c",
        "npx",
        "vite",
        "--host",
        "127.0.0.1",
        "--port",
        "5173",
        "--strictPort",
    ], FRONTEND_DIR, FRONTEND_LOG)

    time.sleep(3)
    if frontend_proc.poll() is not None:
        raise RuntimeError(f"Frontend could not be started. Exit code: {frontend_proc.returncode}. Details are in frontend.log")

    if not wait_for_url(FRONTEND_URL, 45):
        raise RuntimeError("Frontend URL is not responding. Details are in frontend.log")

    open_browser_robust(FRONTEND_URL)
    log(f"Frontend ready. URL: {FRONTEND_URL}")
    show_safe_launcher_window(FRONTEND_URL)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        log(f"ERROR: {type(exc).__name__}: {exc}")
        # Kein 'Drücken zum Schließen' mehr. Error stehen in:
        # %APPDATA%\\RunPodOneClickAIHub_V31\\launcher.log
        raise SystemExit(1)
