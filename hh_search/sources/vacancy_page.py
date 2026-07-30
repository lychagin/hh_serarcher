import json
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from html import unescape
from typing import Any

from hh_search.domain.models import Salary, VacancyDetails
from hh_search.errors import FetchFailed
from hh_search.sources.salary import parse_salary
from hh_search.sources.work_format import WorkFormatBlockStats, extract_work_formats

logger = logging.getLogger(__name__)

# Кавычки вокруг значения атрибута — любые: HTML разрешает и одинарные, и
# двойные, а цена расхождения несимметрична. hh.ru сегодня пишет двойные,
# но смена шаблонизатора на одинарные означала бы «блока JSON-LD нет» —
# то есть громкий FetchFailed на КАЖДОЙ валидной странице, сжигающий
# попытки обогащения всему бэклогу.
_LD_JSON_RE = re.compile(
    r"""<script[^>]*type=["']application/ld\+json["'][^>]*>(.*?)</script>""",
    re.DOTALL | re.IGNORECASE,
)
# Единственный элемент страницы, который разбирается вручную: зарплата есть
# только в разметке. Содержимое берётся до первого закрывающего </div> —
# ровно граница блока hh.ru: внутри лежат только <span> с суммой и типом
# выплаты (проверено 2026-07-28).
_SALARY_BLOCK_RE = re.compile(
    r'data-qa="vacancy-salary"[^>]*>(.*?)</div>', re.DOTALL | re.IGNORECASE
)
_BLOCK_END_RE = re.compile(r"</(p|div|li|ul|ol|h[1-6])>|<br\s*/?>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_SPACES_RE = re.compile(r"[ \t\xa0]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


# Ведущий нуль запрещён, длина ограничена: `000123` — это отдельная строка
# в базе для той же самой вакансии (первичный ключ у нас текстовый), а
# `\d+` принимал и четырёхсотзначное число. Верхняя граница взята с
# запасом к живым id (9 знаков) и всё ещё влезает в INTEGER SQLite.
#
# Живёт здесь, рядом с `vacancy_url`, а не в модуле-читателе: форма
# `https://hh.ru/vacancy/{id}` одна на весь проект, и разбор обязан быть
# обратной стороной сборки. Разъехавшись, они и разъехались: листинг
# ужесточил свою копию регулярки, а `rss.py` остался с `/vacancy/(\d+)` —
# то есть модуль, объявленный готовым к восстановлению, вернул бы вместе
# с собой ровно тот баг, ради которого правилась вторая копия.
_VACANCY_ID_RE = re.compile(r"^/vacancy/([1-9][0-9]{0,14})$")


def vacancy_url(vacancy_id: str) -> str:
    return f"https://hh.ru/vacancy/{vacancy_id}"


def vacancy_id_from_path(path: str) -> str | None:
    """id вакансии из ПУТИ ссылки или `None`, если это не она.

    На вход идёт именно путь, а не URL целиком: `search()` по всей строке
    находил бы `/vacancy/123` в query-строке и в фрагменте, а якоря
    `^...$` на пути отсекают и хвост, и подстроку.
    """
    match = _VACANCY_ID_RE.match(path)
    return match.group(1) if match else None


def iter_ld_json(html: str) -> Iterator[Any]:
    """Разобранные блоки JSON-LD страницы, в порядке появления.

    Битые блоки пропускаются: hh.ru кладёт на страницу несколько блоков
    (BreadcrumbList, ItemList/JobPosting, служебный @graph), и поломка
    одного не должна лишать нас остальных. Единственное место в проекте,
    знающее, как выглядит `<script type="application/ld+json">`, —
    страница вакансии и страница листинга разбирают его одним кодом.
    """
    for block in _LD_JSON_RE.findall(html):
        try:
            yield json.loads(block)
        except json.JSONDecodeError:
            continue


def _has_type(data: dict[str, Any], ld_type: str) -> bool:
    """`@type` в JSON-LD законно бывает и строкой, и СПИСКОМ типов.

    Форма `"@type": ["JobPosting", "Thing"]` разрешена спецификацией
    JSON-LD, и сравнение на равенство строке её не узнаёт. Цена ошибки
    односторонняя: страница валидна, а мы отвечаем `FetchFailed`, жжём
    попытку обогащения и в конце концов отправляем вакансию в
    `enrich_failed` — терминально. У hh.ru поле сегодня плоское
    (проверено на двух живых фикстурах), поэтому это защита от дрейфа, а
    не обход текущего формата.
    """
    raw = data.get("@type")
    if isinstance(raw, list):
        return ld_type in raw
    return raw == ld_type


def find_ld_json(html: str, ld_type: str) -> dict[str, Any] | None:
    """Первый блок JSON-LD с указанным `@type`, если он есть."""
    for data in iter_ld_json(html):
        if isinstance(data, dict) and _has_type(data, ld_type):
            return data
    return None


def extract_job_posting(html: str) -> dict[str, Any] | None:
    """Находит блок JSON-LD с типом JobPosting. Битые блоки пропускает."""
    return find_ld_json(html, "JobPosting")


def html_to_text(html: str) -> str:
    text = _BLOCK_END_RE.sub("\n", html)
    text = _TAG_RE.sub("", text)
    text = unescape(text)
    text = _SPACES_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return _BLANK_LINES_RE.sub("\n\n", text).strip()


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _extract_locality(posting: dict[str, Any]) -> str | None:
    location = posting.get("jobLocation")
    if not isinstance(location, dict):
        return None
    address = location.get("address")
    if not isinstance(address, dict):
        return None
    locality = address.get("addressLocality")
    return locality if isinstance(locality, str) else None


def _extract_description(posting: dict[str, Any]) -> str:
    """Описание из JSON-LD. Пустоты не бывает: её нельзя ни записать, ни повторить.

    Обогащение выбирается по `description IS NULL`, поэтому записанная пустая
    строка — необратимый приговор: вакансия навсегда числится обогащённой, с
    баллом 0 и без единой попытки перекачать страницу. Структурная поломка
    JSON-LD накрывает при этом весь бэклог сразу, поэтому любое неожиданное
    значение — отказ, включающий штатный счётчик enrich_attempts.
    """
    if "description" not in posting:
        raise FetchFailed("в JSON-LD JobPosting нет поля description")
    raw_description = posting["description"]
    if not isinstance(raw_description, str):
        raise FetchFailed(
            f"поле description в JSON-LD JobPosting не строка, а {type(raw_description).__name__}"
        )
    text = html_to_text(raw_description)
    if not text:
        raise FetchFailed("поле description в JSON-LD JobPosting пусто после снятия разметки")
    return text


def _extract_organization(posting: dict[str, Any]) -> str | None:
    organization = posting.get("hiringOrganization")
    if not isinstance(organization, dict):
        return None
    name = organization.get("name")
    return name if isinstance(name, str) else None


def extract_salary(html: str) -> Salary | None:
    """Зарплата из разметки. `None` — блок ничего не дал.

    В JSON-LD её нет: поля `baseSalary` у hh.ru не существует вовсе — эта
    подстрока не встречается на странице ни разу (проверено 2026-07-28 на
    вакансии с «от 100 000 до 150 000 ₽», фикстура
    `tests/fixtures/vacancy_salary.html.gz`). Единственный источник —
    атрибут `data-qa="vacancy-salary"`, поэтому здесь одна точечная
    регулярка, а не HTML-парсер: разбирается ровно один известный элемент,
    всё остальное дерево страницы не трогается.

    `None` возвращается в ДВУХ случаях, и объединены они не по лени, а по
    наблюдаемости: блока нет вовсе — и блок нашёлся, но текста не дал.
    Второе означает дрейф разметки: `_SALARY_BLOCK_RE` берёт содержимое до
    первого `</div>`, поэтому лишний вложенный `<div>` (обычный
    React-рефакторинг — вложенный `<span>` там уже есть) оставляет группу
    пустой. Возвращать здесь пустой `Salary` значило бы сказать
    `SalaryBlockStats`, что блок НАЙДЕН, — и зарплата терялась бы у всех
    вакансий при молчащем стороже. Для наблюдателя оба случая
    неотличимы и лечатся одинаково, поэтому и сигнал один.

    «Зарплата не указана» под это не попадает: там `raw` непуст, значение
    возвращается, и сторож справедливо молчит.
    """
    match = _SALARY_BLOCK_RE.search(html)
    if match is None:
        return None
    text = html_to_text(match.group(1))
    if not text:
        return None
    return parse_salary(text)


@dataclass
class SalaryBlockStats:
    """Счётчик страниц без зарплаты — сторож дрейфа блока `data-qa`.

    По одной странице сказать нельзя ничего: «зарплата не указана» —
    самый обычный случай на hh.ru. А вот прогон, в котором зарплата не
    добылась НИ РАЗУ, почти наверняка означает не рынок без зарплат, а
    переименованный атрибут или перестроенный блок. Разница видна только
    в агрегате, поэтому счётчик живёт здесь и пишет итог сам.

    Считается именно «зарплаты нет», а не «атрибута нет»: блок, найденный
    по атрибуту и не отдавший текста, — такая же потеря, и `extract_salary`
    сводит оба случая к `None` ровно затем, чтобы этот счётчик их не
    различал.
    """

    pages: int = 0
    without_salary: int = 0

    def record(self, salary: Salary | None) -> None:
        self.pages += 1
        if salary is None:
            self.without_salary += 1

    def log_summary(self) -> None:
        if not self.pages:
            return
        if self.without_salary == self.pages:
            logger.warning(
                "ни с одной из %d страниц вакансий не удалось прочитать блок "
                'data-qa="vacancy-salary". Либо ни у одной вакансии не указана '
                "зарплата, либо атрибут переименован (или блок перестроен) и "
                "зарплата потеряна для всех",
                self.pages,
            )
        elif self.without_salary:
            logger.info(
                "зарплата не указана у %d из %d страниц вакансий",
                self.without_salary,
                self.pages,
            )


def parse_vacancy_page(
    html: str,
    stats: SalaryBlockStats | None = None,
    work_format_stats: WorkFormatBlockStats | None = None,
) -> VacancyDetails:
    """Всё, что даёт страница вакансии, — за один разбор.

    После переезда discovery на листинг это единственный источник
    компании, региона, зарплаты, формата работы и даты публикации:
    листинг отдаёт только id, url и заголовок.
    """
    posting = extract_job_posting(html)
    if posting is None:
        raise FetchFailed("на странице нет блока JSON-LD с JobPosting")
    salary = extract_salary(html)
    if stats is not None:
        stats.record(salary)
    work_formats = extract_work_formats(html)
    if work_format_stats is not None:
        work_format_stats.record(work_formats)
    return VacancyDetails(
        description=_extract_description(posting),
        valid_through=_parse_datetime(posting.get("validThrough")),
        published_at=_parse_datetime(posting.get("datePosted")),
        company=_extract_organization(posting),
        area=_extract_locality(posting),
        salary=salary or Salary(),
        work_formats=work_formats,
    )
