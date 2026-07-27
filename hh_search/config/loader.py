from pathlib import Path
from typing import Any

import yaml
from yaml.nodes import MappingNode

from hh_search.config.models import Config


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader, для которого повторный ключ в одном отображении — ошибка.

    Штатный YAML молча оставляет последнее значение, а это ровно тот класс
    опечатки, который `extra="forbid"` поймать не может: имя ключа правильное.
    Случайно продублированный `stack:` внутри `signals` бесшумно выбрасывает
    половину сигналов, и наружу это выйдет через недели необъяснимым падением
    качества оценок. profile.yaml по спеке правится регулярно, так что случай
    не гипотетический.
    """

    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: set[Any] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in seen
            except TypeError:  # pragma: no cover - нехешируемый ключ отвергнет сам SafeLoader
                continue
            if duplicate:
                raise yaml.constructor.ConstructorError(
                    "при разборе отображения",
                    node.start_mark,
                    f"ключ {key!r} задан повторно; YAML оставил бы последнее "
                    "значение, молча выбросив предыдущее",
                    key_node.start_mark,
                )
            seen.add(key)
        return super().construct_mapping(node, deep)


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"конфиг не найден: {path}")
    with path.open(encoding="utf-8") as handle:
        try:
            # yaml.load здесь безопасен: лоадер — подкласс SafeLoader,
            # добавляющий только запрет на повторный ключ.
            data = yaml.load(handle, Loader=_UniqueKeyLoader)
        except yaml.YAMLError as error:
            raise ValueError(f"не удалось разобрать {path}: {error}") from error
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
