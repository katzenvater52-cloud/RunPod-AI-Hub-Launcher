UI_MODEL_PATHS = {
    "comfyui": {"checkpoints": "models/checkpoints", "loras": "models/loras", "vae": "models/vae"},
    "automatic1111": {"checkpoints": "models/Stable-diffusion", "loras": "models/Lora", "vae": "models/VAE"},
    "forge": {"checkpoints": "models/Stable-diffusion", "loras": "models/Lora", "vae": "models/VAE"},
    "sdnext": {"checkpoints": "models/Stable-diffusion", "loras": "models/Lora", "vae": "models/VAE"},
    "swarmui": {"checkpoints": "Models/Stable-Diffusion", "loras": "Models/Lora", "vae": "Models/VAE"},
}

DEFAULT_UI_BASE_PATHS = {
    "comfyui": "/workspace/ComfyUI",
    "automatic1111": "/workspace/stable-diffusion-webui",
    "forge": "/workspace/stable-diffusion-webui-forge",
    "sdnext": "/workspace/sdnext",
    "swarmui": "/workspace/SwarmUI",
}

MODEL_TYPE_ALIASES = {
    "checkpoint": "checkpoints",
    "checkpoints": "checkpoints",
    "ckpt": "checkpoints",
    "lora": "loras",
    "loras": "loras",
    "vae": "vae",
}


def normalize_model_type(model_type: str) -> str:
    value = (model_type or "checkpoints").strip().lower()
    return MODEL_TYPE_ALIASES.get(value, value)


def get_model_paths(ui_id: str) -> dict:
    ui_id = (ui_id or "").lower().strip()
    if ui_id not in UI_MODEL_PATHS:
        raise ValueError(f"Unsupported UI type: {ui_id}")
    return UI_MODEL_PATHS[ui_id]


def get_model_path(ui_id: str, model_type: str) -> str:
    paths = get_model_paths(ui_id)
    normalized = normalize_model_type(model_type)
    if normalized not in paths:
        raise ValueError(f"Unsupported model type '{model_type}' for UI '{ui_id}'")
    return paths[normalized]


def get_default_base_path(ui_id: str) -> str:
    return DEFAULT_UI_BASE_PATHS.get((ui_id or "").lower().strip(), "/workspace/ComfyUI")
