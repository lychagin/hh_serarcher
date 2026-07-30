"""Сторожа CLAUDE.md и единой команды ворот.

Правило проекта «документ без сторожащего теста протухает» применяется и к
самому CLAUDE.md. Здесь живут сторожа его утверждений — тех, которые дёшево
сверить исполнением: пути существуют, состав ворот совпадает со скриптом, CI
зовёт скрипт и только его, корневой документ знает все вложенные, а сам
скрипт действительно останавливается на первой красной проверке.

Каждый сторож проверен мутацией: порча утверждения красит ровно один тест.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
GATE = ROOT / "gate"
CI = ROOT / ".github/workflows/ci.yml"


def _ci_run_steps() -> list[str]:
    """Тело каждого `run`-шага КАЖДОЙ джобы CI.

    Обход по всем джобам, а не по одной `check`: вторая джоба с прямым
    вызовом `pytest` вернула бы состав ворот в два места, а сторож,
    смотрящий в одну джобу, этого не увидел бы.
    """
    workflow = yaml.safe_load(CI.read_text(encoding="utf-8"))
    return [
        step["run"]
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if "run" in step
    ]


def test_ci_runs_the_gate_and_no_check_beside_it() -> None:
    """CI обязан звать `./gate` и не звать проверки напрямую.

    Второе условие — то, что делает этот тест сторожем, а не украшением:
    без него четвёртую проверку можно вернуть в CI мимо `gate`, и состав
    ворот снова начнёт существовать в двух местах. Шаги подготовки
    (`uv python install`, `uv sync`) проверками не являются и под запрет не
    попадают.
    """
    runs = _ci_run_steps()
    assert "./gate" in runs, f"CI не зовёт ./gate: {runs}"
    direct = [run for run in runs if re.search(r"\b(ruff|mypy|pytest)\b", run)]
    assert direct == [], f"проверка в CI идёт мимо ./gate: {direct}"


# Расширения, по которым токен без слэша всё равно опознаётся как путь.
# `.txt` здесь нет намеренно: `robots.txt` упоминается как имя файла на
# стороне hh.ru, а не как файл репозитория.
PATH_SUFFIXES = (".md", ".py", ".yaml", ".yml", ".sql", ".toml")

# Пути, которых нет в свежем клоне: рабочий том и локальный .env оба под
# .gitignore. Упомянуть их в CLAUDE.md нужно, а проверить существованием
# нельзя — в CI проверка бы краснела на чистом checkout.
RUNTIME_ONLY = frozenset({"data", ".env"})


def _uv_commands(text: str) -> list[str]:
    """Строки, начинающиеся с `uv run`, в порядке появления."""
    return [line.strip() for line in text.splitlines() if line.strip().startswith("uv run ")]


def _section(path: Path, heading: str) -> str:
    """Текст раздела от заголовка до следующего заголовка того же уровня."""
    text = path.read_text(encoding="utf-8")
    rest = text[text.index(heading) + len(heading) :]
    level = heading.split(" ")[0]
    end = rest.find(f"\n{level} ")
    return rest if end == -1 else rest[:end]


def _claude_md_files() -> list[Path]:
    """Корневой CLAUDE.md и все вложенные внутри пакета."""
    return [ROOT / "CLAUDE.md", *sorted((ROOT / "hh_search").rglob("CLAUDE.md"))]


def _looks_like_a_path(token: str) -> bool:
    """Токен в обратных кавычках, который обязан существовать на диске.

    Отброшено всё, что путём быть не может: URL-пути hh.ru (начинаются с
    `/`), шаблоны и команды (содержат `?`, `{`, `*` или пробел). Из остатка
    путём считается токен со слэшем или с известным расширением.

    Токены без слэша и без расширения (`time_utils`, `gate`,
    `WorkFormatBlockStats`) проверка пропускает сознательно: отличить имя
    модуля от имени класса регуляркой нечем, а требовать расширения у
    каждого упоминания значило бы портить текст ради сторожа. Дыра названа
    в спеке §6.1 — опечатка в `time_utils` поймана не будет.

    Голое расширение (`.py`, `.md`) — не путь, а упоминание расширения:
    правила проекта говорят про файлы `.py` и `.sql`, и без этой отсечки
    сторож требовал бы, чтобы в корне лежал файл с именем `.py`.
    """
    if token.startswith("/") or any(char in token for char in "?{* "):
        return False
    if token.rstrip("/") in RUNTIME_ONLY or token in PATH_SUFFIXES:
        return False
    return "/" in token or token.endswith(PATH_SUFFIXES)


def test_every_path_in_claude_md_exists() -> None:
    """Путь из CLAUDE.md обязан существовать — от корня или от своего каталога.

    Два способа разрешения нужны оба: во вложенном документе естественно
    писать `base.py` про сосед по каталогу и `hh_search/pipeline/enrichment.py`
    про чужой пакет. Ловит переезды файлов и опечатки в ссылках.
    """
    missing: list[str] = []
    for doc in _claude_md_files():
        for token in re.findall(r"`([^`\n]+)`", doc.read_text(encoding="utf-8")):
            if not _looks_like_a_path(token):
                continue
            candidate = token.rstrip("/")
            if (ROOT / candidate).exists() or (doc.parent / candidate).exists():
                continue
            missing.append(f"{doc.relative_to(ROOT)}: {token}")
    assert missing == [], f"путь из CLAUDE.md не найден: {missing}"


def test_claude_md_gate_section_matches_the_gate_script() -> None:
    """Перечень проверок в §Ворота — копия скрипта, а копии обязаны сверяться.

    Перечень в документе нужен: в fix-loop гоняют одну проверку отдельно, и
    её надо знать. Но именно копия и протухает первой, поэтому сверяется
    буквально, включая порядок.
    """
    documented = _uv_commands(_section(ROOT / "CLAUDE.md", "## Ворота"))
    assert documented == _uv_commands(GATE.read_text(encoding="utf-8"))


def test_gate_stops_on_the_first_red_check(tmp_path: Path) -> None:
    """Ворота обязаны упасть на первой красной проверке, а не досчитать до конца.

    Сторожа выше сверяют состав ворот по тексту и останутся зелёными, если из
    `gate` пропадёт строка `set -eu`. Без неё красная первая проверка не
    останавливает скрипт: он досчитывает до конца, печатает «Ворота зелёные» и
    возвращает 0 — ровно тот класс отказа «прогнал не всё и сказал зелено»,
    ради закрытия которого `gate` и заведён. Проверяется он только исполнением.

    Настоящий `uv` не зовётся и сеть не задействуется: в `PATH` подставлен
    фальшивый `uv`, который записывает свой вызов и падает ненулевым кодом.
    """
    script = tmp_path / "gate"
    shutil.copy(GATE, script)
    script.chmod(0o755)
    calls = tmp_path / "uv-calls.log"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(f'#!/bin/sh\necho "$@" >> "{calls}"\nexit 1\n', encoding="utf-8")
    fake_uv.chmod(0o755)
    env = {**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"}

    result = subprocess.run([str(script)], env=env, capture_output=True, text=True, check=False)

    assert result.returncode != 0, f"ворота вернули 0 при красной проверке:\n{result.stdout}"
    assert "Ворота зелёные" not in result.stdout + result.stderr
    made = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
    assert made == ["run ruff check ."], f"ворота не встали на первой красной проверке: {made}"


def test_root_claude_md_names_every_nested_file() -> None:
    """Корневой документ обязан называть ровно те вложенные, что существуют
    внутри `hh_search/`.

    Приём тот же, что в §4.3 спеки: сторожится не число, а список. Третий
    вложенный файл, появившийся без строки в корневом, останется
    ненайденным для читателя, который в тот каталог не заходил.

    Граница названа, как и дыра в `_looks_like_a_path`: обход идёт по
    `_claude_md_files`, то есть только по пакету. `tests/CLAUDE.md` или
    `docs/CLAUDE.md` не заметит ни этот сторож, ни сторож путей — вложенные
    документы заведены как инварианты слоёв кода. Документ, заведённый вне
    пакета и не упомянутый в корневом, пройдёт молча (спека §6.4).
    """
    text = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    documented = set(re.findall(r"`(hh_search/[^`\n]*CLAUDE\.md)`", text))
    nested = {str(path.relative_to(ROOT)) for path in _claude_md_files()[1:]}
    assert documented == nested
