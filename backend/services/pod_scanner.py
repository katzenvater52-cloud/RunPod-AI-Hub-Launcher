from typing import Any
import json

from services.model_paths import get_default_base_path, get_model_paths
from services.ui_detector import detect_uis

COMMON_APP_PATHS = [
    "/workspace",
    "/workspace/ComfyUI",
    "/workspace/comfyui",
    "/workspace/stable-diffusion-webui",
    "/workspace/stable-diffusion-webui-forge",
    "/workspace/webui-forge",
    "/workspace/sdnext",
    "/workspace/SD.Next",
    "/workspace/automatic",
    "/workspace/SwarmUI",
    "/root",
    "/root/ComfyUI",
    "/root/comfyui",
    "/root/stable-diffusion-webui",
    "/root/stable-diffusion-webui-forge",
    "/root/webui-forge",
    "/root/sdnext",
    "/root/SD.Next",
    "/root/automatic",
    "/root/SwarmUI",
]

PORT_HINTS = {
    3000: "Template/WebUI",
    3001: "Template/WebUI",
    5000: "WebUI/API",
    7000: "WebUI",
    7860: "A1111/Forge/SD.Next",
    7861: "A1111/Forge Alt",
    7801: "SwarmUI",
    8188: "ComfyUI",
    8080: "WebUI/Proxy",
    8888: "Jupyter/WebUI",
}

SKIP_HTTP_PORTS = {22, 53, 111, 123, 443, 445, 873, 2049, 3306, 5432, 6379, 11211}


def run_ssh_command(ssh_client: Any, command: str, timeout: int = 25) -> str:
    stdin, stdout, stderr = ssh_client.exec_command(command, timeout=timeout)
    output = stdout.read().decode("utf-8", errors="replace").strip()
    error = stderr.read().decode("utf-8", errors="replace").strip()
    return output or error


def scan_folders(ssh_client: Any) -> list[dict]:
    quoted_paths = " ".join(f'\"{path}\"' for path in COMMON_APP_PATHS)
    command = rf"""
    for p in {quoted_paths}; do
        if [ -d "$p" ]; then
            echo "$p"
        fi
    done
    find /workspace /root -maxdepth 3 -type d \( \
        -iname '*comfy*' -o \
        -iname '*stable-diffusion-webui*' -o \
        -iname '*webui-forge*' -o \
        -iname '*forge*' -o \
        -iname '*sdnext*' -o \
        -iname '*vladmandic*' -o \
        -iname '*automatic*' -o \
        -iname '*swarm*' \
    \) 2>/dev/null | head -120
    """
    output = run_ssh_command(ssh_client, command)
    seen = set()
    folders = []
    for line in output.splitlines():
        path = line.strip()
        if not path or path in seen:
            continue
        seen.add(path)
        folders.append({"path": path, "name": path.rstrip("/").split("/")[-1]})
    return folders


def scan_processes(ssh_client: Any) -> list[str]:
    command = "ps auxww | grep -Ei 'comfy|webui|forge|sdnext|vladmandic|swarm|gradio|python|node|uvicorn|dotnet' | grep -v grep | head -160"
    output = run_ssh_command(ssh_client, command)
    return [line.strip() for line in output.splitlines() if line.strip()]


def scan_ports(ssh_client: Any) -> list[int]:
    command = """
    if command -v ss >/dev/null 2>&1; then
        ss -tulpen | awk '{print $5}' | grep -Eo ':[0-9]+' | tr -d ':' | sort -n | uniq
    elif command -v netstat >/dev/null 2>&1; then
        netstat -tulpen | awk '{print $4}' | grep -Eo ':[0-9]+' | tr -d ':' | sort -n | uniq
    else
        echo ""
    fi
    """
    output = run_ssh_command(ssh_client, command)
    ports = []
    for line in output.splitlines():
        line = line.strip()
        if line.isdigit():
            ports.append(int(line))
    return sorted(set(ports))


def scan_port_details(ssh_client: Any) -> list[dict]:
    command = """
    if command -v ss >/dev/null 2>&1; then
        ss -tulpen || true
    elif command -v netstat >/dev/null 2>&1; then
        netstat -tulpen || true
    else
        echo ""
    fi
    """
    output = run_ssh_command(ssh_client, command)
    rows = []
    for line in output.splitlines():
        line = line.strip()
        if not line or "LISTEN" not in line.upper():
            continue
        port = None
        import re
        matches = re.findall(r"[:.]([0-9]{2,5})(?:\s|$)", line)
        if matches:
            try:
                port = int(matches[-1])
            except Exception:
                port = None
        if port:
            rows.append({"port": port, "hint": PORT_HINTS.get(port, "unknown"), "raw": line})
    return rows


def scan_http_ports(ssh_client: Any, ports: list[int]) -> list[dict]:
    """Probe every listening port locally inside the pod and keep only HTTP-ish responders."""
    candidate_ports = [p for p in sorted(set(int(p) for p in ports)) if 1 <= p <= 65535 and p not in SKIP_HTTP_PORTS]
    if not candidate_ports:
        return []

    # Keep this broad enough for random RunPod templates, but avoid wasting time on huge port lists.
    candidate_ports = candidate_ports[:40]
    ports_json = json.dumps(candidate_ports)
    command = f"""python3 - <<'PY'
import json, urllib.request, urllib.error, socket, re
ports = {ports_json}
for port in ports:
    row = {{"port": port, "ok": False}}
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{{port}}/", headers={{"User-Agent":"RunPodAIHub-PortDiscovery/1.0"}})
        with urllib.request.urlopen(req, timeout=2.5) as r:
            body = r.read(4096).decode("utf-8", errors="replace")
            title = ""
            m = re.search(r"<title[^>]*>(.*?)</title>", body, re.I|re.S)
            if m:
                title = re.sub(r"\\s+", " ", m.group(1)).strip()[:160]
            row.update({{
                "ok": True,
                "status": getattr(r, "status", 0),
                "content_type": r.headers.get("content-type", ""),
                "title": title,
                "sample": re.sub(r"\\s+", " ", body[:1200]).strip()[:1200],
            }})
    except Exception as e:
        row["error"] = repr(e)[:240]
    print(json.dumps(row, ensure_ascii=False))
PY"""
    output = run_ssh_command(ssh_client, command, timeout=130)
    results = []
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            item = json.loads(line)
            if item.get("ok"):
                results.append(item)
        except Exception:
            continue
    return results


def _text_blob(*parts: object) -> str:
    return "\n".join(str(p or "") for p in parts).lower()


def _score_http_probe_for_ui(ui_id: str, probe: dict) -> int:
    text = _text_blob(probe.get("title"), probe.get("content_type"), probe.get("sample"))
    port = int(probe.get("port") or 0)
    score = 0

    # Generic web UIs on RunPod often live on 3000; do not make this decisive alone.
    if port in (3000, 3001, 5000, 7000, 8080):
        score += 8

    if ui_id == "comfyui":
        if "comfyui" in text or "comfy" in text:
            score += 95
        if port == 8188:
            score += 35
        if "queue" in text and "prompt" in text:
            score += 20

    elif ui_id == "automatic1111":
        if "stable diffusion" in text:
            score += 80
        if "txt2img" in text or "img2img" in text or "extras" in text:
            score += 65
        if "gradio" in text:
            score += 25
        if "forge" in text or "sd.next" in text or "vladmandic" in text:
            score -= 45
        if port == 7860:
            score += 30

    elif ui_id == "forge":
        if "forge" in text:
            score += 95
        if "stable diffusion" in text or "txt2img" in text or "img2img" in text:
            score += 50
        if "gradio" in text:
            score += 25
        if port in (7860, 7861, 3000):
            score += 18

    elif ui_id == "sdnext":
        if "sd.next" in text or "vladmandic" in text:
            score += 95
        if "stable diffusion" in text:
            score += 45
        if port == 7860:
            score += 20

    elif ui_id == "swarmui":
        if "swarmui" in text or "swarm ui" in text:
            score += 95
        if port == 7801:
            score += 35

    return score


def _choose_dynamic_port(ui_id: str, ui: dict, raw_scan: dict) -> tuple[int | None, str, dict | None]:
    http_ports = raw_scan.get("http_ports", []) or []
    all_ports = [int(p) for p in (raw_scan.get("ports", []) or []) if str(p).isdigit()]
    existing_ports = [int(p) for p in (ui.get("ports") or []) if str(p).isdigit()]

    scored: list[tuple[int, int, dict | None, str]] = []
    for probe in http_ports:
        port = int(probe.get("port") or 0)
        if not port:
            continue
        score = _score_http_probe_for_ui(ui_id, probe)
        if port in existing_ports:
            score += 25
        if score > 0:
            scored.append((score, port, probe, "http_fingerprint"))

    if scored:
        scored.sort(key=lambda x: (x[0], -abs(x[1] - 3000)), reverse=True)
        score, port, probe, source = scored[0]
        if score >= 25 or len(http_ports) == 1:
            return port, source, probe

    # If the detector already found a known port and HTTP responded on it, keep it.
    for port in existing_ports:
        for probe in http_ports:
            if int(probe.get("port") or 0) == port:
                return port, "known_port_http", probe

    # If exactly one HTTP port is open and exactly one UI was strongly detected by folders/processes,
    # use it. This is common in RunPod templates that map A1111 to 3000.
    if len(http_ports) == 1:
        probe = http_ports[0]
        return int(probe.get("port")), "single_http_port", probe

    # Last fallback: use known detector port, even without HTTP success.
    if existing_ports:
        return existing_ports[0], "known_port", None

    return None, "not_found", None


def enrich_detected_ui(ui: dict, raw_scan: dict) -> dict:
    ui_id = ui.get("id")
    model_paths = get_model_paths(ui_id)
    base_path = ui.get("base_path") or get_default_base_path(ui_id)
    ui["base_path"] = base_path
    ui["model_paths"] = model_paths
    ui["resolved_paths"] = {
        key: f"{base_path.rstrip('/')}/{value.strip('/')}"
        for key, value in model_paths.items()
    }

    selected_port, source, probe = _choose_dynamic_port(ui_id, ui, raw_scan)
    if selected_port:
        ui["port"] = selected_port
        ports = [selected_port] + [int(p) for p in (ui.get("ports") or []) if int(p) != selected_port]
        ui["ports"] = ports
        ui["running"] = True
        ui["detected_port_source"] = source
        if probe:
            ui["http_probe"] = {
                "port": probe.get("port"),
                "status": probe.get("status"),
                "content_type": probe.get("content_type"),
                "title": probe.get("title"),
            }
    else:
        ui["detected_port_source"] = source

    return ui


def scan_pod(ssh_client: Any) -> dict:
    ports = scan_ports(ssh_client)
    raw_scan = {
        "folders": scan_folders(ssh_client),
        "processes": scan_processes(ssh_client),
        "ports": ports,
        "port_details": scan_port_details(ssh_client),
        "http_ports": scan_http_ports(ssh_client, ports),
    }

    detected = [enrich_detected_ui(ui.to_dict(), raw_scan) for ui in detect_uis(raw_scan)]

    return {
        "connected": True,
        "status": "Connected Pod",
        "scan": raw_scan,
        "detected_uis": detected,
        "primary_ui": detected[0] if detected else None,
    }
