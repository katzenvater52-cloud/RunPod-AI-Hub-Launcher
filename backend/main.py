import base64
import json
import os
import queue
import re
import shlex
import threading
import time
import shutil
import subprocess
import sys
import uuid
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

import paramiko
from cryptography.fernet import Fernet, InvalidToken
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from services.model_paths import get_default_base_path, get_model_path
from services.pod_scanner import scan_pod


app = FastAPI(title="RunPod One-Click Backend", version="0.6.0-v29")

app.add_middleware(
    CORSMiddleware,
    # Lokale Desktop-App: Vite kann beim Neustart kurz auf 5174/5175 ausweichen,
    # deshalb erlauben wir lokale Dev-Ports robust per Regex.
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_origin_regex=r"^http://(localhost|127\.0\.0\.1):(3000|5173|5174|5175|5176)$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


APP_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "RunPodOneClickModelHub"
CONFIG_FILE = APP_DIR / "settings.secure"
KEY_FILE = APP_DIR / "local.key"
LAUNCHER_APPDATA = Path(os.environ.get("APPDATA", str(Path.home()))) / "RunPodOneClickAIHub_V27"
SYSTEM_REPORT_FILE = LAUNCHER_APPDATA / "system_check.txt"



def _cmd_version(cmd: list[str]) -> dict:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
        text = (result.stdout or result.stderr or "").strip().splitlines()
        return {"ok": result.returncode == 0, "version": text[0] if text else "gefunden", "path": cmd[0]}
    except Exception as exc:
        return {"ok": False, "version": f"{type(exc).__name__}: {exc}", "path": cmd[0] if cmd else ""}


def build_system_info() -> dict:
    node = shutil.which("node.exe") or shutil.which("node")
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    python_ok = sys.version_info.major == 3 and sys.version_info.minor in (10, 11, 12, 13, 14)
    info = {
        "ok": True,
        "version": "V29 RunPod API + Serverless",
        "python": {
            "ok": python_ok,
            "version": sys.version.splitlines()[0],
            "path": sys.executable,
            "hint": "Python 3.10 to 3.14 64-bit is supported.",
        },
        "pip": _cmd_version([sys.executable, "-m", "pip", "--version"]),
        "node": _cmd_version([node, "--version"]) if node else {"ok": False, "version": "not found", "path": "", "hint": "Install Node.js LTS."},
        "npm": _cmd_version([npm, "--version"]) if npm else {"ok": False, "version": "not found", "path": "", "hint": "npm is included with Node.js LTS."},
        "logs_path": str(LAUNCHER_APPDATA),
        "system_report": str(SYSTEM_REPORT_FILE),
        "install_hints": [
            "Python 3.10+ 64-bit installieren und Add to PATH activeieren.",
            "Node.js LTS installieren; npm wird automatisch mitinstalliert.",
            "Danach START_HERE_VISIBLE.bat neu start.",
        ],
    }
    info["ready"] = bool(info["python"]["ok"] and info["pip"]["ok"] and info["node"]["ok"] and info["npm"]["ok"])
    return info


@app.get("/api/system-info")
def system_info():
    return build_system_info()

class SSHConfig(BaseModel):
    host: str = Field(..., min_length=3)
    port: int = Field(22, ge=1, le=65535)
    username: str = "root"
    password: Optional[str] = None
    private_key: Optional[str] = None
    private_key_path: Optional[str] = None
    private_key_passphrase: Optional[str] = None
    timeout: int = Field(20, ge=5, le=120)


class SSHCommandPayload(BaseModel):
    command: str


class SettingsPayload(BaseModel):
    runpod_api_key: Optional[str] = None
    civitai_token: Optional[str] = None
    hf_token: Optional[str] = None
    default_ssh_host: Optional[str] = None
    default_ssh_port: Optional[int] = None
    default_ssh_user: Optional[str] = "root"
    default_comfy_path: Optional[str] = "/workspace/ComfyUI"
    default_runpod_pod_id: Optional[str] = None
    default_serverless_endpoint_id: Optional[str] = None


class RunPodProxyRequest(BaseModel):
    pod_id: str = Field(..., min_length=3)
    port: int = Field(..., ge=1, le=65535)


class RunPodPodActionRequest(BaseModel):
    pod_id: str = Field(..., min_length=3)
    gpu_count: int = Field(1, ge=1, le=8)


class ServerlessRunRequest(BaseModel):
    endpoint_id: str = Field(..., min_length=3)
    prompt: str = Field(..., min_length=1)
    mode: str = "runsync"
    raw_input_json: Optional[str] = None


class ServerlessStatusRequest(BaseModel):
    endpoint_id: str = Field(..., min_length=3)
    job_id: str = Field(..., min_length=3)


class AppHealthRequest(BaseModel):
    ssh: SSHConfig
    app_id: str = "unknown"
    app_name: str = "AI App"
    port: int = Field(..., ge=1, le=65535)
    pod_id: Optional[str] = None


class AppActionRequest(BaseModel):
    ssh: SSHConfig
    app_id: str = "unknown"
    app_name: str = "AI App"
    port: int = Field(..., ge=1, le=65535)
    base_path: Optional[str] = None
    pod_id: Optional[str] = None


class StorageStatusRequest(BaseModel):
    ssh: SSHConfig
    paths: Optional[list[str]] = None


class ModelInstallRequest(BaseModel):
    ssh: SSHConfig
    model_name: str
    source: str = "direct"
    url: str
    target_folder: str = "checkpoints"
    hf_token: Optional[str] = None
    civitai_token: Optional[str] = None
    comfy_path: Optional[str] = None
    filename: Optional[str] = None
    restart_comfyui: bool = False

    # New universal AI Host Manager fields.
    # Kept optional so old frontend calls still work.
    target_ui: Optional[str] = None
    model_type: Optional[str] = None
    ui_base_path: Optional[str] = None


class AutoStopConfigRequest(BaseModel):
    enabled: bool = False
    pod_id: Optional[str] = None
    ssh: Optional[SSHConfig] = None
    inactivity_minutes: int = Field(120, ge=5, le=1440)
    gpu_idle_threshold: int = Field(5, ge=0, le=100)


class AutoStopPingRequest(BaseModel):
    reason: str = "user_activity"


@dataclass
class Job:
    id: str
    logs: "queue.Queue[str]"
    done: bool = False
    ok: bool = False
    error: Optional[str] = None
    progress: Optional[dict] = None
    last_progress_emit: float = 0.0


jobs: Dict[str, Job] = {}
autostop_state: Dict[str, dict] = {}

SAFE_FOLDERS = {
    "checkpoints",
    "loras",
    "vae",
    "clip",
    "unet",
    "controlnet",
    "upscale_models",
    "gguf",
    "diffusion_models",
    "text_encoders",
}




def ssh_capture(client: paramiko.SSHClient, command: str, timeout: int = 20) -> dict:
    """Run one SSH command and return stdout/stderr without raising on non-zero exit."""
    transport = client.get_transport()
    if transport is None:
        raise RuntimeError("SSH transport is not connected.")
    channel = transport.open_session()
    channel.settimeout(timeout)
    channel.exec_command(command)
    stdout = b""
    stderr = b""
    start = time.time()
    while True:
        if channel.recv_ready():
            stdout += channel.recv(4096)
        if channel.recv_stderr_ready():
            stderr += channel.recv_stderr(4096)
        if channel.exit_status_ready():
            while channel.recv_ready():
                stdout += channel.recv(4096)
            while channel.recv_stderr_ready():
                stderr += channel.recv_stderr(4096)
            break
        if time.time() - start > timeout:
            try:
                channel.close()
            except Exception:
                pass
            return {"exit_code": 124, "stdout": stdout.decode("utf-8", errors="replace"), "stderr": "Timeout"}
        time.sleep(0.05)
    return {
        "exit_code": channel.recv_exit_status(),
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
    }


def analyze_bind_output(raw: str, port: int) -> dict:
    lines = [line.strip() for line in (raw or "").splitlines() if line.strip()]
    matching = [line for line in lines if f":{port}" in line or f".{port}" in line]
    text = "\n".join(matching)
    if not matching:
        return {"state": "missing", "ok": False, "message": f"Kein Prozess lauscht auf Port {port}.", "lines": []}
    if "0.0.0.0:" in text or "[::]:" in text or ":::" in text or f"*:{port}" in text:
        return {"state": "public", "ok": True, "message": f"Port {port} lauscht auf 0.0.0.0 / alle Interfaces.", "lines": matching}
    if "127.0.0.1:" in text or "localhost:" in text:
        return {"state": "local_only", "ok": False, "message": f"Port {port} is listening only locally on 127.0.0.1. For RunPod proxy the app usually needs --listen / 0.0.0.0.", "lines": matching}
    return {"state": "unknown_bind", "ok": True, "message": f"Port {port} is listening; please check the bind address.", "lines": matching}

def windows_expand_ssh_path(path_value: str) -> str:
    path_value = path_value.strip().strip('"').strip("'")
    if path_value.startswith("~/.ssh/") or path_value.startswith("~\\.ssh\\"):
        rest = path_value[7:].replace("/", "\\")
        return str(Path.home() / ".ssh" / rest)
    if path_value == "~/.ssh" or path_value == "~\\.ssh":
        return str(Path.home() / ".ssh")
    return os.path.expandvars(os.path.expanduser(path_value)).replace("/", "\\")


def parse_runpod_ssh_command(command: str) -> dict:
    """
    Parses examples like:
    ssh root@194.14.47.19 -p 22932 -i ~/.ssh/id_ed25519
    ssh -p 22932 -i "C:\\Users\\andy\\.ssh\\id_ed25519" root@194.14.47.19
    """
    raw = command.strip()
    if not raw:
        raise ValueError("SSH-Befehl ist leer.")

    import re
    import shlex

    try:
        parts = shlex.split(raw, posix=False)
    except Exception:
        parts = raw.split()

    joined = " ".join(parts)

    user = "root"
    host = None
    port = 22
    key_path = None

    user_host_match = re.search(r"(?:(?P<user>[A-Za-z0-9_.-]+)@)?(?P<host>(?:\\d{1,3}\\.){3}\\d{1,3}|[A-Za-z0-9_.-]+)", joined)
    # Prefer token containing @ because command itself starts with "ssh"
    for token in parts:
        clean = token.strip().strip('"').strip("'")
        if "@" in clean and not clean.startswith("-"):
            before, after = clean.split("@", 1)
            if before:
                user = before
            host = after
            break

    if host is None:
        # Fallback: find IP after @ in raw string.
        m = re.search(r"@(?P<host>(?:\\d{1,3}\\.){3}\\d{1,3}|[A-Za-z0-9_.-]+)", raw)
        if m:
            host = m.group("host")

    if host is None:
        raise ValueError("Could not detect host/IP in the SSH command. Expected e.g. root@194.14.47.19")

    for i, token in enumerate(parts):
        clean = token.strip()
        if clean == "-p" and i + 1 < len(parts):
            port = int(parts[i + 1].strip().strip('"').strip("'"))
        elif clean.startswith("-p") and len(clean) > 2:
            port = int(clean[2:].strip())

        if clean == "-i" and i + 1 < len(parts):
            key_path = windows_expand_ssh_path(parts[i + 1])
        elif clean.startswith("-i") and len(clean) > 2:
            key_path = windows_expand_ssh_path(clean[2:])

    if not (1 <= int(port) <= 65535):
        raise ValueError("Port is invalid.")

    return {
        "host": host.strip(),
        "port": int(port),
        "username": user.strip() or "root",
        "private_key_path": key_path or "",
    }


def ensure_app_dir():
    APP_DIR.mkdir(parents=True, exist_ok=True)


def get_fernet() -> Fernet:
    ensure_app_dir()
    if KEY_FILE.exists():
        key = KEY_FILE.read_bytes()
    else:
        key = Fernet.generate_key()
        KEY_FILE.write_bytes(key)
    return Fernet(key)


def load_settings_full() -> dict:
    ensure_app_dir()
    if not CONFIG_FILE.exists():
        return {}
    f = get_fernet()
    try:
        raw = f.decrypt(CONFIG_FILE.read_bytes())
        return json.loads(raw.decode("utf-8"))
    except (InvalidToken, json.JSONDecodeError, OSError):
        return {}


def save_settings_full(data: dict) -> None:
    ensure_app_dir()
    f = get_fernet()
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    CONFIG_FILE.write_bytes(f.encrypt(payload))


def masked(value: Optional[str]) -> dict:
    if not value:
        return {"saved": False, "preview": ""}
    if len(value) <= 8:
        preview = "****"
    else:
        preview = value[:4] + "..." + value[-4:]
    return {"saved": True, "preview": preview}


def public_settings() -> dict:
    s = load_settings_full()
    return {
        "configured": bool(s.get("hf_token") or s.get("civitai_token") or s.get("runpod_api_key")),
        "storage_path": str(APP_DIR),
        "runpod_api_key": masked(s.get("runpod_api_key")),
        "civitai_token": masked(s.get("civitai_token")),
        "hf_token": masked(s.get("hf_token")),
        "default_ssh_host": s.get("default_ssh_host") or "",
        "default_ssh_port": s.get("default_ssh_port") or "",
        "default_ssh_user": s.get("default_ssh_user") or "root",
        "default_comfy_path": s.get("default_comfy_path") or "/workspace/ComfyUI",
        "default_runpod_pod_id": s.get("default_runpod_pod_id") or "",
        "default_serverless_endpoint_id": s.get("default_serverless_endpoint_id") or "",
    }


def merge_tokens(req: ModelInstallRequest) -> ModelInstallRequest:
    s = load_settings_full()

    if not req.hf_token:
        req.hf_token = (
            s.get("hf_token")
            or s.get("huggingface_token")
            or s.get("huggingface_api_key")
        )

    if not req.civitai_token:
        req.civitai_token = (
            s.get("civitai_token")
            or s.get("civitai_api_key")
            or s.get("civitai_key")
        )

    if not req.comfy_path:
        req.comfy_path = s.get("default_comfy_path") or "/workspace/ComfyUI"

    return req


def parse_progress_line(line: str) -> Optional[dict]:
    text = (line or "").replace("\r", " ").strip()
    if not text:
        return None
    percent_match = re.search(r"(\d{1,3})%", text)
    if not percent_match:
        return None
    percent = max(0, min(100, int(percent_match.group(1))))

    speed = None
    speed_match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*([KMG]B/s|KB/s|MB/s|GB/s|B/s)", text, re.I)
    if speed_match:
        speed = f"{speed_match.group(1)} {speed_match.group(2)}"

    eta = None
    eta_match = re.search(r"eta\s+([^\s]+)|ETA\s+([^\s]+)", text, re.I)
    if eta_match:
        eta = eta_match.group(1) or eta_match.group(2)
    else:
        tail = text.split()[-1] if text.split() else ""
        if re.match(r"^\d+[smhd:]", tail, re.I):
            eta = tail

    return {
        "percent": percent,
        "speed": speed or "calculating",
        "eta": eta or "calculating",
        "raw": text[-240:],
        "updated_at": time.time(),
    }


def log(job: Job, line: str) -> None:
    line = (line or "").strip()
    if not line:
        return
    progress = parse_progress_line(line)
    if progress:
        job.progress = progress
    stamp = time.strftime("%H:%M:%S")
    job.logs.put(f"[{stamp}] {line}")


def create_ssh_client(cfg: SSHConfig) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    kwargs = {
        "hostname": cfg.host,
        "port": cfg.port,
        "username": cfg.username,
        "timeout": cfg.timeout,
        "banner_timeout": cfg.timeout,
        "auth_timeout": cfg.timeout,
        "look_for_keys": False,
        "allow_agent": False,
    }

    if cfg.private_key or cfg.private_key_path:
        import io

        key = None
        last_error = None

        if cfg.private_key_path:
            key_path = windows_expand_ssh_path(cfg.private_key_path)
            if not os.path.exists(key_path):
                raise RuntimeError(f"Private-Key-Datei not found: {key_path}")
            for key_cls in (paramiko.RSAKey, paramiko.Ed25519Key, paramiko.ECDSAKey):
                try:
                    key = key_cls.from_private_key_file(key_path, password=cfg.private_key_passphrase)
                    break
                except Exception as exc:
                    last_error = exc
        else:
            key_stream = io.StringIO(cfg.private_key)
            for key_cls in (paramiko.RSAKey, paramiko.Ed25519Key, paramiko.ECDSAKey):
                key_stream.seek(0)
                try:
                    key = key_cls.from_private_key(key_stream, password=cfg.private_key_passphrase)
                    break
                except Exception as exc:
                    last_error = exc

        if key is None:
            raise RuntimeError(f"Private key could not be read: {last_error}")
        kwargs["pkey"] = key
    else:
        if not cfg.password:
            raise RuntimeError("Passwort, Private-Key-Text oder Private-Key-Pfad fehlt.")
        kwargs["password"] = cfg.password

    client.connect(**kwargs)
    return client


def run_command(client: paramiko.SSHClient, command: str, job: Optional[Job] = None, timeout: Optional[int] = None) -> int:
    if job:
        safe_command = mask_sensitive_command(command)
        log(job, f"$ {safe_command[:500]}{'...' if len(safe_command) > 500 else ''}")

    transport = client.get_transport()
    if transport is None:
        raise RuntimeError("SSH transport is not connected.")

    channel = transport.open_session()
    channel.set_combine_stderr(True)
    if timeout:
        channel.settimeout(timeout)
    channel.exec_command(command)

    buffer = b""

    def flush_parts(force: bool = False):
        nonlocal buffer
        while b"\n" in buffer or b"\r" in buffer:
            newline_pos = buffer.find(b"\n") if b"\n" in buffer else 10**9
            carriage_pos = buffer.find(b"\r") if b"\r" in buffer else 10**9
            pos = min(newline_pos, carriage_pos)
            part, buffer = buffer[:pos], buffer[pos + 1:]
            if job:
                text = part.decode("utf-8", errors="replace").strip()
                if text:
                    log(job, text)
        if force and buffer:
            if job:
                text = buffer.decode("utf-8", errors="replace").strip()
                if text:
                    log(job, text)
            buffer = b""

    while True:
        if channel.recv_ready():
            data = channel.recv(4096)
            if not data:
                break
            buffer += data
            flush_parts(False)

        if channel.exit_status_ready():
            while channel.recv_ready():
                buffer += channel.recv(4096)
                flush_parts(False)
            flush_parts(True)
            return channel.recv_exit_status()

        time.sleep(0.08)

def guess_filename(url: str, given: Optional[str]) -> str:
    if given:
        return os.path.basename(given.strip())
    clean = url.split("?")[0].rstrip("/")
    name = clean.split("/")[-1]
    if not name or "." not in name:
        name = "downloaded_model"
    return re.sub(r"[^A-Za-z0-9._+-]+", "_", name)



def is_civitai_url(url: str) -> bool:
    if not url:
        return False
    u = url.lower()
    return "civitai.com" in u or "civitai.red" in u


def build_civitai_download_url(url: str, civitai_token: Optional[str]) -> str:
    """
    Normalisiert CivitAI URLs und hängt automatisch den API-Token an.
    Unterstützt:
    - civitai.com
    - civitai.red

    Off:
    https://civitai.red/api/download/models/123?fileId=456

    wird:
    https://civitai.com/api/download/models/123?fileId=456&token=...
    """
    if not url:
        return url

    # .red immer sauber auf offizielles civitai.com normalisieren
    url = url.replace("civitai.red", "civitai.com")

    if not civitai_token:
        return url

    civitai_token = civitai_token.strip()
    if not civitai_token:
        return url

    # Wenn der Token schon in der URL ist, nichts doppelt anhängen
    if "token=" in url.lower():
        return url

    separator = "&" if "?" in url else "?"
    return f"{url}{separator}token={civitai_token}"


def mask_sensitive_command(command: str) -> str:
    # Verhindert, dass Tokens im lokalen App-Log angezeigt werden.
    return re.sub(r"([?&]token=)[^'\"\\s]+", r"\\1***", command)

def resolve_destination_dir(req: ModelInstallRequest) -> str:
    """
    New universal path resolver.

    If target_ui + model_type are provided, the destination is resolved from
    services/model_paths.py. Otherwise the old ComfyUI-compatible behavior is
    preserved for backwards compatibility.
    """
    if req.target_ui:
        base_path = (req.ui_base_path or req.comfy_path or get_default_base_path(req.target_ui)).rstrip("/")
        relative_model_path = get_model_path(req.target_ui, req.model_type or req.target_folder)
        return f"{base_path}/{relative_model_path.strip('/')}"

    folder = req.target_folder.strip().strip("/")
    if folder not in SAFE_FOLDERS:
        raise ValueError(f"Unsafe target folder: {folder}")

    comfy = (req.comfy_path or "/workspace/ComfyUI").rstrip("/")
    return f"{comfy}/models/{folder}"




def is_huggingface_url(url: str) -> bool:
    return bool(url and "huggingface.co" in url.lower())


def huggingface_model_page(url: str) -> str:
    """Returns the HF repo page for both repo URLs and /resolve/ file URLs."""
    raw = (url or "").strip()
    if "huggingface.co/" not in raw:
        return raw
    part = raw.split("huggingface.co/", 1)[1].strip("/")
    pieces = part.split("/")
    if len(pieces) >= 2:
        return "https://huggingface.co/" + "/".join(pieces[:2])
    return raw


def hf_access_help_text(url: str, has_token: bool) -> str:
    page = huggingface_model_page(url)
    token_line = "HF Token wurde verwendet." if has_token else "Kein HF Token wurde mitgegeben oder gespeichert."
    return "\n".join([
        "HUGGINGFACE ACCESS CHECK:",
        "This model/repo appears to require HuggingFace access.",
        token_line,
        "Wenn trotzdem 401/403 kommt, ist der Token meistens NICHT das Problem.",
        "Most likely you need to accept the model terms / request access on HuggingFace first.",
        f"Open the model page and click Agree/Accept: {page}",
        "Danach denselben Download im Hub erneut start.",
    ])


def looks_like_hf_access_error(text: str) -> bool:
    t = (text or "").lower()
    needles = [
        "403 forbidden",
        "http error 403",
        "401 unauthorized",
        "http error 401",
        "gated repo",
        "gated repository",
        "access to model",
        "access is restricted",
        "you are not authorized",
        "repository not found",
        "cannot access gated repo",
        "please enable access",
        "must be authenticated",
    ]
    return any(n in t for n in needles)

def build_download_command(req: ModelInstallRequest) -> str:
    dest_dir = resolve_destination_dir(req)

    # WICHTIG:
    # CivitAI unabhängig von req.source erkennen.
    # So funktioniert es auch, wenn der User "direct" gewählt hat,
    # aber eine CivitAI-URL einfügt.
    input_url = req.url.strip()

    if is_civitai_url(input_url):
        final_url = build_civitai_download_url(input_url, req.civitai_token)

        # Kurzen, sauberen Dateinamen erzwingen.
        # Wichtig gegen Linux-Error: "destination name is too long".
        filename = guess_filename(input_url, req.filename)
        if "." not in filename:
            filename += ".safetensors"
        if not filename.lower().endswith((".safetensors", ".ckpt", ".pt", ".pth", ".bin")):
            filename += ".safetensors"

        dest_file = f"{dest_dir}/{filename}"

        setup = (
            "set -e; "
            f"mkdir -p {shlex.quote(dest_dir)}; "
            "command -v wget >/dev/null 2>&1 || (apt-get update -y && apt-get install -y wget); "
        )

        user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/137.0 Safari/537.36"
        )

        # -U / --user-agent: Cloudflare/CivitAI weniger blockanfällig
        # -O: kurzer Ziel-Dateiname statt riesiger Redirect-URL
        # --max-redirect: Redirect-Kette erlauben
        # -c: Resume
        return (
            setup
            + "echo 'CivitAI Download erkannt.'; "
            + ("echo 'CivitAI Token gefunden und an URL angehaengt.'; " if req.civitai_token else "echo 'WARNUNG: Kein CivitAI Token gefunden.'; ")
            + "echo 'Nutze Browser User-Agent und kurzen Dateinamen.'; "
            + f"wget "
            + "--progress=bar:force:noscroll "
            + f"--user-agent={shlex.quote(user_agent)} "
            + "--max-redirect=20 "
            + "--trust-server-names "
            + "-c "
            + f"-O {shlex.quote(dest_file)} "
            + f"{shlex.quote(final_url)}"
        )

    filename = guess_filename(input_url, req.filename)
    dest_file = f"{dest_dir}/{filename}"

    setup = (
        "set -e; "
        f"mkdir -p {shlex.quote(dest_dir)}; "
        "command -v wget >/dev/null 2>&1 || (apt-get update -y && apt-get install -y wget); "
        "python3 -m pip install -q -U huggingface_hub hf_transfer || true; "
    )

    if req.source == "huggingface":
        if "huggingface.co" in input_url and "/resolve/" not in input_url:
            repo = input_url.split("huggingface.co/", 1)[1].strip("/")
            token_part = f"export HF_TOKEN={shlex.quote(req.hf_token)}; " if req.hf_token else ""
            return (
                setup
                + token_part
                + "python3 - <<'PY'\n"
                + "import os, sys\n"
                + "from huggingface_hub import snapshot_download\n"
                + "try:\n"
                + "    from huggingface_hub.errors import GatedRepoError, HfHubHTTPError, RepositoryNotFoundError\n"
                + "except Exception:\n"
                + "    GatedRepoError = HfHubHTTPError = RepositoryNotFoundError = Exception\n"
                + f"repo_id = {repo!r}\n"
                + f"local_dir = {dest_dir!r}\n"
                + "print(f'Downloade HF Repo {repo_id} nach {local_dir}')\n"
                + "try:\n"
                + "    snapshot_download(repo_id=repo_id, local_dir=local_dir, token=os.environ.get('HF_TOKEN'))\n"
                + "except (GatedRepoError, RepositoryNotFoundError, HfHubHTTPError) as exc:\n"
                + "    print('HF_ACCESS_ERROR: Zugriff verweigert oder Repo gated.')\n"
                + "    print(str(exc))\n"
                + "    sys.exit(33)\n"
                + "print('HF Repo Download done')\n"
                + "PY"
            )
        header = f"--header='Authorization: Bearer {req.hf_token}' " if req.hf_token else ""
        return (
            setup
            + "echo 'HuggingFace Download erkannt.'; "
            + ("echo 'HF Token gefunden.'; " if req.hf_token else "echo 'WARNUNG: Kein HF Token gefunden.'; ")
            + f"wget --server-response --progress=bar:force:noscroll -c {header}{shlex.quote(input_url)} -O {shlex.quote(dest_file)}"
        )

    return setup + f"wget --progress=bar:force:noscroll -c {shlex.quote(input_url)} -O {shlex.quote(dest_file)}"



def normalize_runpod_pod_id(value: str) -> str:
    """
    Accepts a raw Pod-ID or an already copied RunPod proxy URL and returns only the Pod-ID.

    Supported inputs:
    - abc123xyz
    - https://abc123xyz-7860.proxy.runpod.net
    - abc123xyz-8188.proxy.runpod.net
    """
    pod_id = (value or "").strip()
    pod_id = pod_id.replace("https://", "").replace("http://", "")
    pod_id = pod_id.replace(".proxy.runpod.net", "")
    pod_id = pod_id.strip().strip("/").strip()

    # If the user pasted PODID-PORT, remove only the final numeric port part.
    if "-" in pod_id:
        parts = pod_id.split("-")
        if parts[-1].isdigit():
            pod_id = "-".join(parts[:-1])

    if not pod_id:
        raise ValueError("RunPod Pod-ID fehlt.")

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{2,80}", pod_id):
        raise ValueError("RunPod pod ID looks invalid. Please enter only the pod ID or a RunPod proxy URL.")

    return pod_id


def build_runpod_proxy_url(pod_id: str, port: int) -> str:
    normalized = normalize_runpod_pod_id(pod_id)
    return f"https://{normalized}-{int(port)}.proxy.runpod.net"


def check_runpod_proxy_url(url: str, timeout: int = 7) -> dict:
    """Checks from the local machine/backend whether RunPod actually exposes this HTTP proxy URL."""
    started = time.time()
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "RunPod-OneClick-AI-Hub/21.0",
            "Accept": "text/html,application/json,*/*",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            sample = response.read(512).decode("utf-8", errors="replace")
            status = int(getattr(response, "status", 0) or 0)
            content_type = response.headers.get("content-type", "")
            ok = 200 <= status < 400
            return {
                "ok": ok,
                "reachable": ok,
                "status": status,
                "content_type": content_type,
                "url": url,
                "elapsed_ms": int((time.time() - started) * 1000),
                "message": "RunPod proxy is externally reachable." if ok else f"RunPod proxy responded with HTTP {status}.",
                "sample": sample[:300],
            }
    except urllib.error.HTTPError as exc:
        try:
            sample = exc.read(512).decode("utf-8", errors="replace")
        except Exception:
            sample = ""
        status = int(getattr(exc, "code", 0) or 0)
        if status == 404:
            message = "RunPod proxy reports 404 / page not found. This port is very likely not exposed as an HTTP service for the pod."
        elif status in (401, 403):
            message = f"RunPod proxy responded with HTTP {status}. Port ist wahrscheinlich exposed, aber Zugriff/Auth blockiert."
        else:
            message = f"RunPod proxy responded with HTTP {status}."
        return {
            "ok": False,
            "reachable": False,
            "status": status,
            "url": url,
            "elapsed_ms": int((time.time() - started) * 1000),
            "message": message,
            "sample": sample[:300],
        }
    except urllib.error.URLError as exc:
        return {
            "ok": False,
            "reachable": False,
            "status": None,
            "url": url,
            "elapsed_ms": int((time.time() - started) * 1000),
            "message": f"RunPod proxy not reachable: {exc.reason}",
            "sample": "",
        }
    except Exception as exc:
        return {
            "ok": False,
            "reachable": False,
            "status": None,
            "url": url,
            "elapsed_ms": int((time.time() - started) * 1000),
            "message": f"RunPod Proxy Check failed: {exc}",
            "sample": "",
        }


@app.get("/api/health")
def health():
    return {"ok": True, "message": "RunPod One-Click backend is running."}




def get_runpod_api_key() -> str:
    key = (load_settings_full().get("runpod_api_key") or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="RunPod API key is not saved. Open Settings and add your API key first.")
    return key


def runpod_graphql(query: str, variables: Optional[dict] = None) -> dict:
    api_key = get_runpod_api_key()
    url = f"https://api.runpod.io/graphql?api_key={api_key}"
    payload = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"content-type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace") or "{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise HTTPException(status_code=exc.code, detail=f"RunPod GraphQL error: {body[:1000]}")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"RunPod GraphQL request failed: {exc}")
    if data.get("errors"):
        raise HTTPException(status_code=400, detail=data.get("errors"))
    return data


def runpod_serverless_request(endpoint_id: str, operation: str, body: Optional[dict] = None, method: str = "POST") -> dict:
    api_key = get_runpod_api_key()
    endpoint_id = re.sub(r"[^a-zA-Z0-9_-]", "", endpoint_id.strip())
    if not endpoint_id:
        raise HTTPException(status_code=400, detail="Serverless endpoint ID is missing.")
    url = f"https://api.runpod.ai/v2/{endpoint_id}/{operation.lstrip('/')}"
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"authorization": f"Bearer {api_key}", "content-type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return {"ok": 200 <= resp.status < 300, "status_code": resp.status, "data": json.loads(raw or "{}"), "url": url}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = {"raw": raw}
        return {"ok": False, "status_code": exc.code, "data": parsed, "url": url, "message": f"HTTP {exc.code}"}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"RunPod Serverless request failed: {exc}")


@app.get("/api/runpod/account")
def runpod_account_check():
    query = "query { myself { id email } }"
    data = runpod_graphql(query)
    return {"ok": True, "account": data.get("data", {}).get("myself")}


@app.post("/api/runpod/proxy-url")
def create_runpod_proxy_url(payload: RunPodProxyRequest):
    try:
        pod_id = normalize_runpod_pod_id(payload.pod_id)
        url = build_runpod_proxy_url(pod_id, payload.port)
        return {"ok": True, "pod_id": pod_id, "port": payload.port, "url": url}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/runpod/proxy-check")
def check_runpod_proxy(payload: RunPodProxyRequest):
    try:
        pod_id = normalize_runpod_pod_id(payload.pod_id)
        url = build_runpod_proxy_url(pod_id, payload.port)
        return check_runpod_proxy_url(url) | {"pod_id": pod_id, "port": payload.port}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/runpod/pod/start")
def api_start_pod(payload: RunPodPodActionRequest):
    try:
        pod_id = normalize_runpod_pod_id(payload.pod_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    safe_pod_id = pod_id.replace('"', '')
    query = f"""mutation {{ podResume(input: {{ podId: \"{safe_pod_id}\", gpuCount: {payload.gpu_count} }}) {{ id desiredStatus imageName }} }}"""
    data = runpod_graphql(query)
    pod = data.get("data", {}).get("podResume")
    return {
        "ok": True,
        "action": "start",
        "pod_id": pod_id,
        "pod": pod,
        "message": f"RunPod API start requested for {pod_id}.",
    }


@app.post("/api/runpod/pod/stop")
def api_stop_pod(payload: RunPodPodActionRequest):
    try:
        pod_id = normalize_runpod_pod_id(payload.pod_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    safe_pod_id = pod_id.replace('"', '')
    query = f"""mutation {{ podStop(input: {{ podId: \"{safe_pod_id}\" }}) {{ id desiredStatus }} }}"""
    data = runpod_graphql(query)
    pod = data.get("data", {}).get("podStop")
    return {
        "ok": True,
        "action": "stop",
        "pod_id": pod_id,
        "pod": pod,
        "message": f"RunPod API stop requested for {pod_id}.",
    }


@app.post("/api/runpod/serverless/run")
def run_serverless(payload: ServerlessRunRequest):
    try:
        if payload.raw_input_json and payload.raw_input_json.strip():
            raw = json.loads(payload.raw_input_json)
            body = raw if "input" in raw else {"input": raw}
        else:
            body = {"input": {"prompt": payload.prompt}}
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON input: {exc}")

    mode = payload.mode if payload.mode in ("run", "runsync") else "runsync"
    result = runpod_serverless_request(payload.endpoint_id, mode, body, method="POST")
    return result | {"operation": mode}


@app.post("/api/runpod/serverless/status")
def serverless_status(payload: ServerlessStatusRequest):
    return runpod_serverless_request(payload.endpoint_id, f"status/{payload.job_id}", None, method="GET") | {"operation": "status"}


@app.get("/api/settings")
def get_settings():
    return public_settings()


@app.post("/api/settings")
def save_settings(payload: SettingsPayload):
    current = load_settings_full()

    incoming = payload.model_dump()
    for key, value in incoming.items():
        if value is not None:
            current[key] = value

    save_settings_full(current)
    return {"ok": True, "settings": public_settings()}


@app.delete("/api/settings")
def delete_settings():
    if CONFIG_FILE.exists():
        CONFIG_FILE.unlink()
    return {"ok": True, "settings": public_settings()}



@app.post("/api/parse-ssh-command")
def parse_ssh_command(payload: SSHCommandPayload):
    try:
        return {"ok": True, "parsed": parse_runpod_ssh_command(payload.command)}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@app.post("/api/test-connection")
def test_connection(cfg: SSHConfig):
    try:
        client = create_ssh_client(cfg)
        try:
            stdin, stdout, stderr = client.exec_command("echo CONNECTED && whoami && hostname && pwd", timeout=20)
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            if err.strip():
                return {"ok": False, "output": out, "error": err}
            return {"ok": True, "output": out}
        finally:
            client.close()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/pod/scan")
def scan_connected_pod(cfg: SSHConfig):
    try:
        client = create_ssh_client(cfg)
        try:
            return scan_pod(client)
        finally:
            client.close()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/pod/storage")
def pod_storage_status(payload: StorageStatusRequest):
    """Return remote RunPod container / volume storage usage via SSH."""
    paths = payload.paths or ["/", "/workspace", "/runpod-volume"]
    safe_paths = []
    for path in paths:
        path = str(path or "").strip()
        if path and re.match(r"^/[A-Za-z0-9_./-]+$", path):
            safe_paths.append(path)
    if not safe_paths:
        safe_paths = ["/", "/workspace", "/runpod-volume"]

    quoted_paths = " ".join(shlex.quote(path) for path in safe_paths)
    script = f"""
set +e
printf 'DF_BEGIN\\n'
df -B1 -P {quoted_paths} 2>/dev/null | tail -n +2
printf 'DF_END\\n'
printf 'DU_BEGIN\\n'
for p in {quoted_paths}; do
  if [ -e \"$p\" ]; then
    used=$(du -sb \"$p\" 2>/dev/null | awk '{{print $1}}')
    printf '%s|%s\\n' \"$p\" \"$used\"
  fi
done
printf 'DU_END\\n'
"""
    try:
        client = create_ssh_client(payload.ssh)
        try:
            result = ssh_capture(client, bash_login_command(script), timeout=35)
        finally:
            client.close()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    stdout = result.get("stdout", "") or ""
    stderr = result.get("stderr", "") or ""
    entries = []
    in_df = False
    du_map = {}
    in_du = False
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if line == "DF_BEGIN":
            in_df = True
            continue
        if line == "DF_END":
            in_df = False
            continue
        if line == "DU_BEGIN":
            in_du = True
            continue
        if line == "DU_END":
            in_du = False
            continue
        if in_du and "|" in line:
            key, value = line.split("|", 1)
            try:
                du_map[key] = int(value or 0)
            except ValueError:
                du_map[key] = None
            continue
        if in_df and line:
            parts = line.split()
            if len(parts) >= 6:
                fs, total, used, available, pct, mount = parts[0], parts[1], parts[2], parts[3], parts[4], parts[-1]
                try:
                    total_i = int(total)
                    used_i = int(used)
                    free_i = int(available)
                except ValueError:
                    continue
                free_pct = round((free_i / total_i) * 100, 1) if total_i else 0
                used_pct = round((used_i / total_i) * 100, 1) if total_i else 0
                label = "Container Disk" if mount == "/" else ("Persistent Volume" if "runpod" in mount.lower() or "workspace" in mount.lower() else mount)
                entries.append({
                    "filesystem": fs,
                    "mount": mount,
                    "label": label,
                    "total_bytes": total_i,
                    "used_bytes": used_i,
                    "free_bytes": free_i,
                    "used_percent": used_pct,
                    "free_percent": free_pct,
                    "du_bytes": du_map.get(mount),
                    "warning": free_i < 10 * 1024**3,
                })

    return {"ok": True, "entries": entries, "stderr": stderr[-1000:] if stderr else ""}






def default_app_base_path(app_id: str) -> str:
    app_id = (app_id or "").lower()
    if app_id == "comfyui":
        return "/workspace/ComfyUI"
    if app_id == "forge":
        return "/workspace/stable-diffusion-webui-forge"
    if app_id in {"automatic1111", "a1111"}:
        return "/workspace/stable-diffusion-webui"
    return "/workspace"


def bash_login_command(script: str) -> str:
    # shlex.quote keeps paths with spaces safe inside the remote bash -lc call.
    return "bash -lc " + shlex.quote(script)


def build_app_start_command(app_id: str, base_path: str, port: int) -> str:
    app_id = (app_id or "").lower()
    base_path = base_path or default_app_base_path(app_id)
    port = int(port)

    if app_id == "comfyui":
        launch_args = f"main.py --listen 0.0.0.0 --port {port}"
    else:
        launch_args = f"launch.py --listen --port {port}"

    script = f"""
set -e
APP_DIR={shlex.quote(base_path)}
PORT={port}
LOG_FILE="$APP_DIR/runpod_hub_{app_id}_{port}.log"
if [ ! -d "$APP_DIR" ]; then
  echo "APP_DIR_MISSING $APP_DIR"
  exit 7
fi
if (ss -tulpn 2>/dev/null || netstat -tulpn 2>/dev/null || true) | grep -E "(:|\\.)$PORT([[:space:]]|$)" >/dev/null; then
  echo "ALREADY_RUNNING port=$PORT"
  exit 0
fi
cd "$APP_DIR"
PYBIN="python"
if [ -x "venv/bin/python" ]; then PYBIN="venv/bin/python"; elif command -v python3 >/dev/null 2>&1; then PYBIN="python3"; fi
CMD="$PYBIN {launch_args}"
echo "STARTING $CMD" > "$LOG_FILE"
nohup bash -lc "$CMD" >> "$LOG_FILE" 2>&1 < /dev/null &
echo $! > "$APP_DIR/runpod_hub_{app_id}_{port}.pid"
sleep 3
if (ss -tulpn 2>/dev/null || netstat -tulpn 2>/dev/null || true) | grep -E "(:|\\.)$PORT([[:space:]]|$)" >/dev/null; then
  echo "STARTED port=$PORT"
else
  echo "START_SENT port=$PORT"
  echo "LOG_TAIL"
  tail -n 40 "$LOG_FILE" 2>/dev/null || true
fi
"""
    return bash_login_command(script)


def build_app_stop_command(app_id: str, base_path: str, port: int) -> str:
    app_id = (app_id or "").lower()
    base_path = base_path or default_app_base_path(app_id)
    port = int(port)
    if app_id == "comfyui":
        pattern = "main.py.*ComfyUI"
    elif app_id == "forge":
        pattern = "launch.py.*forge|stable-diffusion-webui-forge"
    else:
        pattern = "launch.py.*stable-diffusion-webui|webui.sh|automatic1111"

    script = f"""
APP_DIR={shlex.quote(base_path)}
PORT={port}
if [ -f "$APP_DIR/runpod_hub_{app_id}_{port}.pid" ]; then
  PID=$(cat "$APP_DIR/runpod_hub_{app_id}_{port}.pid" 2>/dev/null || true)
  if [ -n "$PID" ]; then kill "$PID" 2>/dev/null || true; fi
fi
if command -v fuser >/dev/null 2>&1; then fuser -k $PORT/tcp 2>/dev/null || true; fi
pkill -f {shlex.quote(pattern)} 2>/dev/null || true
sleep 2
if (ss -tulpn 2>/dev/null || netstat -tulpn 2>/dev/null || true) | grep -E "(:|\\.)$PORT([[:space:]]|$)" >/dev/null; then
  echo "STOP_SENT_BUT_PORT_STILL_ACTIVE port=$PORT"
else
  echo "STOPPED port=$PORT"
fi
"""
    return bash_login_command(script)


def build_app_log_command(app_id: str, base_path: str, port: int, lines: int = 120) -> str:
    app_id = (app_id or "").lower()
    base_path = base_path or default_app_base_path(app_id)
    port = int(port)
    lines = max(20, min(int(lines), 300))
    script = f"""
APP_DIR={shlex.quote(base_path)}
LOG_FILE="$APP_DIR/runpod_hub_{app_id}_{port}.log"
if [ -f "$LOG_FILE" ]; then
  tail -n {lines} "$LOG_FILE"
else
  echo "No Hub start log found yet: $LOG_FILE"
fi
"""
    return bash_login_command(script)


@app.post("/api/pod/app-health")
def check_app_health(payload: AppHealthRequest):
    """
    Diagnostics for Stage 2 Control Center:
    - SSH connection established?
    - Lauscht die erkannte App auf dem erwaitingen Port?
    - Lauscht sie auf 0.0.0.0 oder nur 127.0.0.1?
    - Antwortet localhost im Pod?
    """
    result = {
        "ok": False,
        "app_id": payload.app_id,
        "app_name": payload.app_name,
        "port": payload.port,
        "checks": [],
        "recommendations": [],
    }
    try:
        client = create_ssh_client(payload.ssh)
        try:
            result["checks"].append({"name": "SSH", "ok": True, "message": "SSH connection to the pod works."})

            port = int(payload.port)
            listen_cmd = (
                f"(ss -tulpn 2>/dev/null || netstat -tulpn 2>/dev/null || true) | "
                f"grep -E '(:|\\.){port}([[:space:]]|$)' || true"
            )
            listen = ssh_capture(client, listen_cmd, timeout=12)
            bind = analyze_bind_output(listen.get("stdout", ""), port)
            result["checks"].append({
                "name": "Port Bind",
                "ok": bind["ok"],
                "state": bind["state"],
                "message": bind["message"],
                "details": bind["lines"][:8],
            })

            curl_cmd = (
                f"python3 - <<'PY'\n"
                f"import urllib.request, sys\n"
                f"url='http://127.0.0.1:{port}/'\n"
                f"try:\n"
                f"    r=urllib.request.urlopen(url, timeout=5)\n"
                f"    print('HTTP', r.status)\n"
                f"    print('CONTENT_TYPE', r.headers.get('content-type',''))\n"
                f"except Exception as e:\n"
                f"    print('ERROR', repr(e))\n"
                f"    sys.exit(1)\n"
                f"PY"
            )
            curl = ssh_capture(client, curl_cmd, timeout=12)
            curl_ok = curl.get("exit_code") == 0 and "HTTP" in curl.get("stdout", "")
            result["checks"].append({
                "name": "Lokaler HTTP-Test",
                "ok": curl_ok,
                "message": "App responds on localhost inside the pod." if curl_ok else "App does not respond on localhost inside the pod.",
                "details": (curl.get("stdout", "") or curl.get("stderr", "")).splitlines()[:8],
            })

            if bind["state"] == "missing":
                result["recommendations"].append(f"{payload.app_name} does not seem to be running on port {port}. Start the app or check the correct port.")
            elif bind["state"] == "local_only":
                result["recommendations"].append("Start the app with --listen / host 0.0.0.0, otherwise the RunPod browser proxy often cannot access it.")
            if not curl_ok:
                result["recommendations"].append("If the process is running: wait 30-60 seconds, check logs, or restart the app.")
            proxy_ok = None
            if payload.pod_id:
                try:
                    result["proxy_url"] = build_runpod_proxy_url(payload.pod_id, port)
                    proxy = check_runpod_proxy_url(result["proxy_url"], timeout=7)
                    proxy_ok = bool(proxy.get("ok"))
                    result["proxy_check"] = proxy
                    result["checks"].append({
                        "name": "RunPod Exposed Port",
                        "ok": proxy_ok,
                        "state": "exposed" if proxy_ok else "not_exposed",
                        "message": proxy.get("message", "RunPod Proxy Check abgeschlossen."),
                        "details": [result["proxy_url"]],
                    })
                    if not proxy_ok:
                        result["recommendations"].append(
                            f"The app is running internally on port {port}, but RunPod apparently does not expose this port. In the RunPod template/pod, expose HTTP service port {port} freigeben oder die App auf einen bereits exposed Port start."
                        )
                except Exception as exc:
                    result["checks"].append({
                        "name": "RunPod Exposed Port",
                        "ok": False,
                        "state": "proxy_check_failed",
                        "message": f"RunPod proxy check could not be executed: {exc}",
                        "details": [],
                    })

            result["internal_ok"] = bool(bind["ok"] and curl_ok)
            result["external_ok"] = proxy_ok
            result["ok"] = bool(result["internal_ok"] and (proxy_ok is not False))
            if result["internal_ok"] and proxy_ok is True:
                result["summary"] = f"{payload.app_name} is internally healthy and externally reachable via RunPod proxy."
            elif result["internal_ok"] and proxy_ok is False:
                result["summary"] = f"{payload.app_name} is running internally on port {port}, but this port is not externally exposed through RunPod."
            elif result["internal_ok"]:
                result["summary"] = f"{payload.app_name} wirkt intern gesund: Port {port} lauscht und HTTP antwortet im Pod."
            else:
                result["summary"] = f"{payload.app_name} needs diagnostics: see checks and recommendations."
            return result
        finally:
            client.close()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))



@app.post("/api/pod/app-start")
def start_detected_app(payload: AppActionRequest):
    """Starting eine erkannte UI im Pod per SSH. V19: Fokus A1111/Forge/ComfyUI."""
    port = int(payload.port)
    base_path = payload.base_path or default_app_base_path(payload.app_id)
    try:
        client = create_ssh_client(payload.ssh)
        try:
            cmd = build_app_start_command(payload.app_id, base_path, port)
            started = ssh_capture(client, cmd, timeout=25)
            health_payload = AppHealthRequest(
                ssh=payload.ssh,
                app_id=payload.app_id,
                app_name=payload.app_name,
                port=port,
                pod_id=payload.pod_id,
            )
            # Health wird hier bewusst leichtgewichtig direkt nochmal geprüft.
            listen_cmd = (
                f"(ss -tulpn 2>/dev/null || netstat -tulpn 2>/dev/null || true) | "
                f"grep -E '(:|\\.){port}([[:space:]]|$)' || true"
            )
            listen = ssh_capture(client, listen_cmd, timeout=10)
            bind = analyze_bind_output(listen.get("stdout", ""), port)
            proxy_url = None
            if payload.pod_id:
                try:
                    proxy_url = build_runpod_proxy_url(payload.pod_id, port)
                except Exception:
                    proxy_url = None
            return {
                "ok": True,
                "action": "start",
                "app_id": payload.app_id,
                "app_name": payload.app_name,
                "port": port,
                "base_path": base_path,
                "stdout": started.get("stdout", ""),
                "stderr": started.get("stderr", ""),
                "exit_code": started.get("exit_code"),
                "bind": bind,
                "proxy_url": proxy_url,
                "message": f"Start command for {payload.app_name} was sent. Use Health Check afterwards if the UI is still loading.",
            }
        finally:
            client.close()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/pod/app-stop")
def stop_detected_app(payload: AppActionRequest):
    port = int(payload.port)
    base_path = payload.base_path or default_app_base_path(payload.app_id)
    try:
        client = create_ssh_client(payload.ssh)
        try:
            cmd = build_app_stop_command(payload.app_id, base_path, port)
            stopped = ssh_capture(client, cmd, timeout=20)
            return {
                "ok": True,
                "action": "stop",
                "app_id": payload.app_id,
                "app_name": payload.app_name,
                "port": port,
                "base_path": base_path,
                "stdout": stopped.get("stdout", ""),
                "stderr": stopped.get("stderr", ""),
                "exit_code": stopped.get("exit_code"),
                "message": f"Stop command for {payload.app_name} was sent.",
            }
        finally:
            client.close()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/pod/app-logs")
def read_detected_app_logs(payload: AppActionRequest):
    port = int(payload.port)
    base_path = payload.base_path or default_app_base_path(payload.app_id)
    try:
        client = create_ssh_client(payload.ssh)
        try:
            cmd = build_app_log_command(payload.app_id, base_path, port)
            logs = ssh_capture(client, cmd, timeout=12)
            return {
                "ok": True,
                "app_id": payload.app_id,
                "app_name": payload.app_name,
                "port": port,
                "base_path": base_path,
                "stdout": logs.get("stdout", ""),
                "stderr": logs.get("stderr", ""),
                "exit_code": logs.get("exit_code"),
            }
        finally:
            client.close()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))




def get_gpu_utilization(client: paramiko.SSHClient) -> Optional[int]:
    cmd = "nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits 2>/dev/null | head -1"
    out = ssh_capture(client, cmd, timeout=8)
    text = (out.get("stdout") or "").strip()
    try:
        return int(text.splitlines()[0].strip())
    except Exception:
        return None


@app.post("/api/autostop/configure")
def configure_autostop(req: AutoStopConfigRequest):
    now = time.time()
    if not req.enabled:
        autostop_state.clear()
        return {"ok": True, "enabled": False, "message": "Auto-Stop disabled."}

    autostop_state.clear()
    autostop_state.update({
        "enabled": True,
        "pod_id": req.pod_id,
        "ssh": req.ssh.model_dump() if req.ssh else None,
        "inactivity_minutes": req.inactivity_minutes,
        "gpu_idle_threshold": req.gpu_idle_threshold,
        "last_activity": now,
        "armed_at": now,
        "last_check": None,
        "last_gpu_util": None,
        "would_stop": False,
        "message": "Auto-Stop active. The safety timer monitors inactivity and GPU utilization.",
    })
    return {"ok": True, **autostop_state}


@app.post("/api/autostop/ping")
def autostop_ping(req: AutoStopPingRequest):
    if autostop_state.get("enabled"):
        autostop_state["last_activity"] = time.time()
        autostop_state["last_reason"] = req.reason
    return {"ok": True, "enabled": bool(autostop_state.get("enabled"))}


@app.get("/api/autostop/status")
def autostop_status():
    if not autostop_state.get("enabled"):
        return {"ok": True, "enabled": False}

    now = time.time()
    last_activity = float(autostop_state.get("last_activity") or now)
    inactivity_seconds = max(0, int(now - last_activity))
    limit_seconds = int(autostop_state.get("inactivity_minutes", 120)) * 60
    remaining_seconds = max(0, limit_seconds - inactivity_seconds)
    gpu_util = None
    idle = None

    # GPU nur alle ~30 Sekunden prüfen, damit SSH nicht unnötig belastet wird.
    if not autostop_state.get("last_check") or now - float(autostop_state.get("last_check")) > 30:
        ssh_cfg = autostop_state.get("ssh")
        if ssh_cfg:
            try:
                client = create_ssh_client(SSHConfig(**ssh_cfg))
                try:
                    gpu_util = get_gpu_utilization(client)
                finally:
                    client.close()
            except Exception:
                gpu_util = None
        autostop_state["last_gpu_util"] = gpu_util
        autostop_state["last_check"] = now
    else:
        gpu_util = autostop_state.get("last_gpu_util")

    if gpu_util is not None:
        idle = gpu_util <= int(autostop_state.get("gpu_idle_threshold", 5))

    should_stop = remaining_seconds <= 0 and idle is True
    if should_stop:
        # Safety-first: noch keine harte RunPod-Mutation ohne explizit getesteten Provider-Adapter.
        # Der Zustand wird klar gemeldet, damit der User/Adapter stop kann.
        autostop_state["would_stop"] = True
        autostop_state["message"] = "Auto-Stop would be due now: timer expired and GPU is idle. RunPod stop mutation will be connected in the provider adapter."
    else:
        autostop_state["would_stop"] = False
        autostop_state["message"] = "Auto-Stop is actively monitoring."

    return {
        "ok": True,
        "enabled": True,
        "inactivity_seconds": inactivity_seconds,
        "remaining_seconds": remaining_seconds,
        "gpu_util": gpu_util,
        "gpu_idle": idle,
        **autostop_state,
    }

@app.post("/api/install-model")
def install_model(req: ModelInstallRequest):
    req = merge_tokens(req)
    job_id = str(uuid.uuid4())
    job = Job(id=job_id, logs=queue.Queue())
    jobs[job_id] = job

    def worker():
        recent_lines = []
        original_put = job.logs.put

        def tracked_put(item):
            if isinstance(item, str) and item != "__JOB_DONE__":
                recent_lines.append(item)
                del recent_lines[:-80]
            original_put(item)

        job.logs.put = tracked_put
        try:
            log(job, f"Starting job for: {req.model_name}")
            if is_huggingface_url(req.url):
                log(job, "HuggingFace URL erkannt.")
                log(job, "HF Token: " + ("gefunden" if req.hf_token else "NICHT gefunden"))
            if req.target_ui:
                log(job, f"Source: {req.source} | Target UI: {req.target_ui} | Model type: {req.model_type or req.target_folder}")
                log(job, f"Target path: {resolve_destination_dir(req)}")
            else:
                log(job, f"Source: {req.source} | Target folder: {req.target_folder}")
            if is_civitai_url(req.url):
                log(job, "CivitAI URL erkannt.")
                log(job, "CivitAI Token: " + ("gefunden" if req.civitai_token else "NICHT gefunden"))
            command = build_download_command(req)
            client = create_ssh_client(req.ssh)
            try:
                code = run_command(client, command, job=job)
                if code != 0:
                    recent_text = "\n".join(recent_lines[-80:])
                    if is_huggingface_url(req.url) and (code == 33 or looks_like_hf_access_error(recent_text)):
                        help_text = hf_access_help_text(req.url, bool(req.hf_token))
                        log(job, help_text)
                        raise RuntimeError("HuggingFace access denied: please accept the model terms / access on HuggingFace and check your token.")
                    raise RuntimeError(f"Download-Befehl failed. Exit Code: {code}")

                if req.restart_comfyui:
                    restart_cmd = "pkill -f 'main.py' 2>/dev/null || true; sleep 2; bash /workspace/start.sh"
                    code = run_command(client, restart_cmd, job=job)
                    if code != 0:
                        raise RuntimeError(f"Restart failed. Exit Code: {code}")

                log(job, "Done. Model was uploaded to the pod.")
                job.ok = True
            finally:
                client.close()
        except Exception as exc:
            if is_huggingface_url(req.url):
                recent_text = "\n".join(recent_lines[-80:])
                if looks_like_hf_access_error(str(exc) + "\n" + recent_text):
                    log(job, hf_access_help_text(req.url, bool(req.hf_token)))
            job.error = str(exc)
            log(job, f"FEHLER: {exc}")
        finally:
            job.done = True
            job.logs.put("__JOB_DONE__")

    threading.Thread(target=worker, daemon=True).start()
    return {"ok": True, "job_id": job_id}


@app.get("/api/logs/{job_id}")
def stream_logs(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")

    def event_stream():
        while True:
            line = job.logs.get()
            if line == "__JOB_DONE__":
                payload = json.dumps({"done": True, "ok": job.ok, "error": job.error})
                yield f"event: done\ndata: {payload}\n\n"
                break
            payload = json.dumps({"line": line})
            yield f"data: {payload}\n\n"
            if job.progress:
                yield f"event: progress\ndata: {json.dumps(job.progress)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
