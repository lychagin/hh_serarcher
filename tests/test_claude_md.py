"""Сторожа CLAUDE.md и единой команды ворот.

Правило проекта «документ без сторожащего теста протухает» применяется и к
самому CLAUDE.md. Здесь живут сторожа его утверждений — тех, которые дёшево
сверить исполнением: пути существуют, состав ворот совпадает со скриптом, CI
зовёт скрипт и только его, корневой документ знает все вложенные.

Каждый сторож проверен мутацией: порча утверждения красит ровно один тест.
"""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
GATE = ROOT / "gate"
CI = ROOT / ".github/workflows/ci.yml"


def _ci_run_steps() -> list[str]:
    """Тело каждого `run`-шага джобы CI."""
    workflow = yaml.safe_load(CI.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["check"]["steps"]
    return [step["run"] for step in steps if "run" in step]


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
