from dataclasses import asdict, dataclass, field
from typing import Optional


@dataclass
class DetectedUI:
    id: str
    name: str
    confidence: int
    reason: str
    base_path: Optional[str] = None
    port: Optional[int] = None
    ports: list[int] = field(default_factory=list)
    process_match: Optional[str] = None
    running: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


UI_SIGNATURES = {
    "comfyui": {
        "name": "ComfyUI",
        "ports": [8188],
        "folders": ["ComfyUI", "comfyui"],
        "strong_folders": ["/ComfyUI", "/comfyui"],
        "process_keywords": ["ComfyUI", "comfyui", "main.py"],
        "strong_process_keywords": ["ComfyUI/main.py", "comfyui/main.py"],
    },
    "automatic1111": {
        "name": "Automatic1111",
        "ports": [7860],
        "folders": ["stable-diffusion-webui"],
        "strong_folders": ["/stable-diffusion-webui"],
        "process_keywords": ["stable-diffusion-webui", "webui.py", "launch.py"],
        "strong_process_keywords": ["stable-diffusion-webui/webui.py", "stable-diffusion-webui/launch.py"],
    },
    "forge": {
        "name": "Forge",
        "ports": [7860],
        "folders": ["stable-diffusion-webui-forge", "webui-forge", "forge"],
        "strong_folders": ["/stable-diffusion-webui-forge", "/webui-forge"],
        "process_keywords": ["stable-diffusion-webui-forge", "webui-forge", "forge"],
        "strong_process_keywords": ["stable-diffusion-webui-forge", "webui-forge", "--forge"],
    },
    "sdnext": {
        "name": "SD.Next",
        "ports": [7860],
        "folders": ["sdnext", "SD.Next", "vladmandic", "automatic"],
        "strong_folders": ["/sdnext", "/SD.Next", "/automatic"],
        "process_keywords": ["sdnext", "SD.Next", "vladmandic", "automatic"],
        "strong_process_keywords": ["vladmandic", "sdnext", "SD.Next"],
    },
    "swarmui": {
        "name": "SwarmUI",
        "ports": [7801, 7860],
        "folders": ["SwarmUI", "swarmui"],
        "strong_folders": ["/SwarmUI", "/swarmui"],
        "process_keywords": ["SwarmUI", "swarmui", "dotnet"],
        "strong_process_keywords": ["SwarmUI", "swarmui"],
    },
}


def _contains_any(text: str, needles: list[str]) -> bool:
    t = (text or "").lower().replace("\\", "/")
    return any(n.lower().replace("\\", "/") in t for n in needles)


def _best_folder_match(folders: list[dict], signature: dict) -> tuple[Optional[str], int, Optional[str]]:
    # Strong exact-ish path hits first, then broad name hits.
    for folder in folders:
        path = str(folder.get("path", ""))
        if _contains_any(path, signature.get("strong_folders", [])):
            return path, 50, f"folder strong: {path}"
    for folder in folders:
        path = str(folder.get("path", ""))
        name = str(folder.get("name", ""))
        if _contains_any(path, signature["folders"]) or _contains_any(name, signature["folders"]):
            return path, 35, f"folder: {path}"
    return None, 0, None


def _best_process_match(processes: list[str], signature: dict) -> tuple[Optional[str], int, Optional[str]]:
    for process in processes:
        if _contains_any(process, signature.get("strong_process_keywords", [])):
            return process[:240], 35, f"process strong: {process[:140]}"
    for process in processes:
        if _contains_any(process, signature["process_keywords"]):
            return process[:240], 25, f"process: {process[:140]}"
    return None, 0, None


def _matching_ports(ports: list[int], signature: dict) -> list[int]:
    wanted = set(int(p) for p in signature["ports"])
    return [int(p) for p in ports if int(p) in wanted]


def _apply_shared_port_rules(ui_id: str, confidence: int, folders: list[dict], processes: list[str], reasons: list[str]) -> int:
    all_text = "\n".join([*(str(f.get("path", "")) for f in folders), *processes])

    forge_hit = _contains_any(all_text, ["stable-diffusion-webui-forge", "webui-forge", "forge"])
    sdnext_hit = _contains_any(all_text, ["sdnext", "vladmandic", "/automatic"])
    a1111_hit = _contains_any(all_text, ["stable-diffusion-webui"])

    # 7860 is shared by A1111, Forge and SD.Next. Avoid showing A1111 at high confidence
    # just because 7860 is open when Forge/SD.Next signatures are present.
    if ui_id == "automatic1111" and (forge_hit or sdnext_hit):
        confidence = max(0, confidence - 30)
        reasons.append("lowered: shared 7860 but Forge/SD.Next indicators found")

    if ui_id == "forge" and a1111_hit and forge_hit:
        confidence += 10
        reasons.append("boosted: Forge-specific webui folder/process found")

    if ui_id == "sdnext" and sdnext_hit:
        confidence += 10
        reasons.append("boosted: SD.Next/vladmandic indicators found")

    return confidence


def detect_uis(scan_result: dict) -> list[DetectedUI]:
    folders = scan_result.get("folders", []) or []
    processes = scan_result.get("processes", []) or []
    ports = [int(p) for p in (scan_result.get("ports", []) or []) if str(p).isdigit()]
    detected: list[DetectedUI] = []

    for ui_id, signature in UI_SIGNATURES.items():
        confidence = 0
        reasons: list[str] = []

        base_path, folder_score, folder_reason = _best_folder_match(folders, signature)
        if folder_score:
            confidence += folder_score
            reasons.append(folder_reason or "folder match")

        process_match, process_score, process_reason = _best_process_match(processes, signature)
        if process_score:
            confidence += process_score
            reasons.append(process_reason or "process match")

        matched_ports = _matching_ports(ports, signature)
        if matched_ports:
            confidence += 20
            reasons.append("port: " + ", ".join(str(p) for p in matched_ports))

        confidence = _apply_shared_port_rules(ui_id, confidence, folders, processes, reasons)
        confidence = max(0, min(confidence, 100))

        if confidence > 0:
            detected.append(
                DetectedUI(
                    id=ui_id,
                    name=signature["name"],
                    confidence=confidence,
                    reason="; ".join(r for r in reasons if r),
                    base_path=base_path,
                    port=matched_ports[0] if matched_ports else None,
                    ports=matched_ports,
                    process_match=process_match,
                    running=bool(process_match or matched_ports),
                )
            )

    detected.sort(key=lambda item: (item.confidence, item.running), reverse=True)
    return detected
