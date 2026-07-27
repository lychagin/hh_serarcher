from pathlib import Path
from typing import Any

import yaml

from hh_search.config.models import Config


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"конфиг не найден: {path}")
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"ожидался словарь в {path}, получено {type(data).__name__}")
    return data


def load_config(config_dir: Path) -> Config:
    """Читает три YAML из каталога и валидирует их. Бросает на первой же ошибке."""
    return Config.model_validate(
        {
            "app": _read_yaml(config_dir / "app.yaml"),
            "profile": _read_yaml(config_dir / "profile.yaml"),
            "queries": _read_yaml(config_dir / "queries.yaml"),
        }
    )
