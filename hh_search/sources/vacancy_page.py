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

logger = logging.getLogger(__name__)

_LD_JSON_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.DOTALL | re.IGNORECASE
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


def vacancy_url(vacancy_id: str) -> str:
    return f"https://hh.ru/vacancy/{vacancy_id}"


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


def find_ld_json(html: str, ld_type: str) -> dict[str, Any] | None:
    """Первый блок JSON-LD с указанным `@type`, если он есть."""
    for data in iter_ld_json(html):
        if isinstance(data, dict) and data.get("@type") == ld_type:
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
            "поле description в JSON-LD JobPosting не строка, "
            f"а {type(raw_description).__name__}"
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
    """Зарплата из разметки. `None` — блока на странице нет.

    В JSON-LD её нет: `baseSalary` у hh.ru всегда `null`, даже когда
    зарплата указана (проверено 2026-07-28 на вакансии с «от 40 000 ₽»).
    Единственный источник — атрибут `data-qa="vacancy-salary"`, поэтому
    здесь одна точечная регулярка, а не HTML-парсер: разбирается ровно
    один известный элемент, всё остальное дерево страницы не трогается.

    Отсутствие блока — законный случай «зарплата не указана», а не
    ошибка; сторож дрейфа самого атрибута — `SalaryBlockStats` ниже.
    """
    match = _SALARY_BLOCK_RE.search(html)
    if match is None:
        return None
    return parse_salary(html_to_text(match.group(1)))


@dataclass
class SalaryBlockStats:
    """Счётчик страниц без блока зарплаты — сторож дрейфа `data-qa`.

    По одной странице сказать нельзя ничего: «зарплата не указана» —
    самый обычный случай на hh.ru. А вот прогон, в котором блок не нашёлся
    НИ РАЗУ, почти наверняка означает не рынок без зарплат, а
    переименованный атрибут. Разница видна только в агрегате, поэтому
    счётчик живёт здесь и пишет итог сам.
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
                "ни на одной из %d страниц вакансий не найден блок "
                'data-qa="vacancy-salary". Либо ни у одной вакансии не указана '
                "зарплата, либо атрибут переименован и зарплата потеряна для всех",
                self.pages,
            )
        elif self.without_salary:
            logger.info(
                "зарплата не указана у %d из %d страниц вакансий",
                self.without_salary,
                self.pages,
            )


def parse_vacancy_page(html: str, stats: SalaryBlockStats | None = None) -> VacancyDetails:
    """Всё, что даёт страница вакансии, — за один разбор.

    После переезда discovery на листинг это единственный источник
    компании, региона, зарплаты и даты публикации: листинг отдаёт только
    id, url и заголовок.
    """
    posting = extract_job_posting(html)
    if posting is None:
        raise FetchFailed("на странице нет блока JSON-LD с JobPosting")
    salary = extract_salary(html)
    if stats is not None:
        stats.record(salary)
    return VacancyDetails(
        description=_extract_description(posting),
        valid_through=_parse_datetime(posting.get("validThrough")),
        published_at=_parse_datetime(posting.get("datePosted")),
        company=_extract_organization(posting),
        area=_extract_locality(posting),
        salary=salary or Salary(),
    )
