"""Config loader: reads config.yaml, applies proxy env, resolves paths."""
from __future__ import annotations
import os
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"


def load(path: str | Path | None = None) -> dict:
    p = Path(path) if path else CONFIG_PATH
    cfg = yaml.safe_load(p.read_text(encoding="utf-8"))
    # resolve relative paths against project root
    for key in ("work_dir", "db_path", "voices_dir"):
        v = cfg.get(key)
        if v and not Path(v).is_absolute():
            cfg[key] = str(ROOT / v)
    apply_proxy(cfg)
    return cfg


def apply_proxy(cfg: dict):
    px = cfg.get("proxy") or {}
    # 国内站点永远直连（requests 在 Windows 会读注册表系统代理，必须显式 NO_PROXY）
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = ",".join(
        ["localhost", "127.0.0.1", "*.modelscope.cn", "*.aliyuncs.com", "*.tuna.tsinghua.edu.cn"]
    )
    if px.get("enabled"):
        os.environ["HTTP_PROXY"] = px.get("http", "")
        os.environ["HTTPS_PROXY"] = px.get("https", "")
    else:
        os.environ.pop("HTTP_PROXY", None)
        os.environ.pop("HTTPS_PROXY", None)
    if px.get("model_hub", "modelscope") == "modelscope":
        os.environ.setdefault("HF_HUB_OFFLINE", "0")


if __name__ == "__main__":
    import json
    c = load()
    c.pop("filename_patterns", None)
    print(json.dumps(c, ensure_ascii=False, indent=2))
