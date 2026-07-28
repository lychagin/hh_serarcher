import gzip
import json
import logging
from pathlib import Path

import pytest

from hh_search.config.models import QuerySpec
from hh_search.domain.models import DiscoveredVacancy
from hh_search.errors import FetchFailed
from hh_search.sources.listing import build_listing_url, parse_listing

FIXTURES = Path(__file__).parent / "fixtures"
# Живые страницы, скачанные один раз честным User-Agent с паузой 2 с:
# /vacancies/programmist — существующий курируемый листинг, /vacancies/yocto —
# несуществующий slug, который hh.ru молча отдаёт как общий индекс /vacancies.
LIVE_LISTING = "listing_programmist.html.gz"
LIVE_INDEX_INSTEAD_OF_SLUG = "listing_unknown_slug.html.gz"


def load(name: str) -> str:
    with gzip.open(FIXTURES / name, "rt", encoding="utf-8") as handle:
        return handle.read()


def page(slug: str, items: object, canonical: str | None = None) -> str:
    """Синтетическая страница листинга: canonical + один блок ItemList."""
    href = canonical if canonical is not None else f"https://hh.ru/vacancies/{slug}"
    link = f'<link rel="canonical" href="{href}">' if canonical != "" else ""
    payload = json.dumps({"@context": "http://schema.org", "@type": "ItemList",
                          "itemListElement": items}, ensure_ascii=False)
    return f'<html><head>{link}</head><body>' \
           f'<script type="application/ld+json">{payload}</script></body></html>'


def item(vacancy_id: str, name: str = "Инженер") -> dict[str, object]:
    return {
        "@type": "ListItem",
        "position": 1,
        "url": f"https://hh.ru/vacancy/{vacancy_id}",
        "name": name,
    }


# --- построение URL: разрешены ровно две формы ---------------------------


def test_build_listing_url_first_page_carries_no_query_string() -> None:
    """`Disallow: *?*` запрещает любой URL с query-строкой, поэтому первая
    страница обязана быть голым путём, а не `?page=0`."""
    url = build_listing_url(QuerySpec(slug="programmist", cluster="dev"))
    assert url == "https://hh.ru/vacancies/programmist"


def test_build_listing_url_uses_page_parameter_for_later_pages() -> None:
    """Единственная разрешённая query-строка — `?page=N`: её пропускает
    `Allow: /vacancies/*?page=`, который длиннее запрета `*?*` и побеждает."""
    url = build_listing_url(QuerySpec(slug="programmist", cluster="dev"), page=2)
    assert url == "https://hh.ru/vacancies/programmist?page=2"


def test_build_listing_url_rejects_negative_page() -> None:
    with pytest.raises(ValueError, match="страниц"):
        build_listing_url(QuerySpec(slug="programmist", cluster="dev"), page=-1)


# --- разбор живой страницы -----------------------------------------------


def test_parse_listing_reads_every_item_of_the_live_page() -> None:
    """Разбор живой выдачи: 20 элементов, у всех id из цифр и непустой заголовок."""
    found = parse_listing(load(LIVE_LISTING), "programmist")
    assert len(found) == 20
    assert all(isinstance(v, DiscoveredVacancy) for v in found)
    assert all(v.id.isdigit() for v in found)
    assert all(v.title.strip() for v in found)
    assert all(v.url == f"https://hh.ru/vacancy/{v.id}" for v in found)
    assert all(v.found_by_query == "programmist" for v in found)
    # id уникальны: повторов внутри одной страницы быть не должно
    assert len({v.id for v in found}) == 20


def test_parse_listing_leaves_enrichment_fields_empty() -> None:
    """Листинг отдаёт только id, url и заголовок. Всё остальное — company,
    area, salary, published_at — приходит на шаге обогащения, и discovery
    не имеет права выдумывать для них значения."""
    first = parse_listing(load(LIVE_LISTING), "programmist")[0]
    assert first.published_at is None
    assert first.company is None
    assert first.area is None
    assert first.salary.raw is None


# --- несуществующий slug: молчаливый редирект обязан быть громким ---------


def test_parse_listing_rejects_index_returned_instead_of_requested_slug() -> None:
    """Главный случай переезда: `/vacancies/yocto` не существует, и hh.ru
    отдаёт на него общий индекс `/vacancies` со статусом 200 и полным
    ItemList из 20 посторонних вакансий (кладовщик, сборщик заказов).
    Без проверки canonical опечатка в конфиге тихо приносила бы мусор."""
    with pytest.raises(FetchFailed, match="yocto"):
        parse_listing(load(LIVE_INDEX_INSTEAD_OF_SLUG), "yocto")


def test_only_the_canonical_stands_between_us_and_twenty_wrong_vacancies() -> None:
    """Контроль к предыдущему тесту: та же самая страница-подмена, у которой
    подправлен ТОЛЬКО canonical, разбирается в 20 совершенно посторонних
    вакансий. То есть отказ выше вызван именно подменой slug, а не тем, что
    разбирать оказалось нечего, — и без проверки canonical этот мусор
    попал бы в базу, был бы обогащён и отчитан."""
    html = load(LIVE_INDEX_INSTEAD_OF_SLUG).replace(
        'href="https://hh.ru/vacancies"', 'href="https://hh.ru/vacancies/yocto"'
    )
    found = parse_listing(html, "yocto")
    assert len(found) == 20
    assert not any("yocto" in v.title.lower() for v in found), "это и есть посторонние вакансии"


def test_parse_listing_accepts_paged_canonical() -> None:
    """У второй страницы canonical сохраняет `?page=2`; сравнивается путь."""
    html = page("programmist", [item("1")], canonical="https://hh.ru/vacancies/programmist?page=2")
    assert [v.id for v in parse_listing(html, "programmist")] == ["1"]


def test_parse_listing_raises_when_canonical_is_absent() -> None:
    """Без canonical подмену slug проверить нечем, а тихо доверять странице
    после этого раунда нельзя."""
    with pytest.raises(FetchFailed, match="canonical"):
        parse_listing(page("programmist", [item("1")], canonical=""), "programmist")


# --- дрейф формата: громкий отказ вместо тихой пустоты --------------------


def test_parse_listing_raises_when_item_list_is_missing() -> None:
    html = (
        '<link rel="canonical" href="https://hh.ru/vacancies/programmist">'
        '<script type="application/ld+json">{"@type": "BreadcrumbList"}</script>'
    )
    with pytest.raises(FetchFailed, match="ItemList"):
        parse_listing(html, "programmist")


def test_parse_listing_raises_when_not_a_single_item_parsed() -> None:
    """Элементы есть, но ни один не разобрался — это смена формата, а не
    «вакансий нет». Тихая пустота здесь означала бы месяцы молчания при
    зелёном healthcheck."""
    broken = [{"@type": "ListItem", "url": "https://hh.ru/employer/1", "name": "Не вакансия"}]
    with pytest.raises(FetchFailed, match="формат"):
        parse_listing(page("programmist", broken), "programmist")


def test_parse_listing_accepts_honestly_empty_list() -> None:
    """Ноль элементов — законный результат узкого листинга, не отказ."""
    assert parse_listing(page("programmist", []), "programmist") == []


def test_parse_listing_skips_one_broken_item_and_logs_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Одна битая запись не уносит остальные, но молча не пропадает."""
    items = [item("1"), {"@type": "ListItem", "url": "https://hh.ru/vacancy/нет"}, item("2")]
    with caplog.at_level(logging.WARNING):
        found = parse_listing(page("programmist", items), "programmist")
    assert [v.id for v in found] == ["1", "2"]
    assert "пропущен" in caplog.text


def test_parse_listing_skips_item_without_title() -> None:
    """Пустой заголовок — то же, что отсутствующий: заголовок идёт в скоринг,
    и пустая строка тихо обнулила бы его вклад."""
    items = [item("1"), {"@type": "ListItem", "url": "https://hh.ru/vacancy/2", "name": "  "}]
    assert [v.id for v in parse_listing(page("programmist", items), "programmist")] == ["1"]


def test_parse_listing_raises_when_item_list_element_is_not_a_list() -> None:
    html = page("programmist", {"@type": "ListItem"})
    with pytest.raises(FetchFailed, match="itemListElement"):
        parse_listing(html, "programmist")


# --- Раунд исправлений 6 --------------------------------------------------


def test_url_is_always_rebuilt_and_never_taken_from_the_feed() -> None:
    """Решение «url собираем канонический, из ленты не берём» на живой
    фикстуре не сторожится: там url и так канонические. А utm-хвост из ленты
    живые правила ПРЯМО запрещают (`Disallow: *?*`), то есть сохранённая
    ссылка стала бы вакансией, которую нельзя скачать."""
    items = [
        {
            "@type": "ListItem",
            "url": "https://hh.ru/vacancy/135437299?from=vacancy_search_list&query=x",
            "name": "Инженер",
        }
    ]
    found = parse_listing(page("programmist", items), "programmist")
    assert [v.url for v in found] == ["https://hh.ru/vacancy/135437299"]


def test_canonical_on_a_foreign_host_does_not_confirm_the_listing() -> None:
    """Сравнивался только путь, поэтому canonical чужого хоста подтверждал
    наш листинг: страница с evil.example.com проходила как своя."""
    html = page("programmist", [item("1")], canonical="https://evil.example.com/vacancies/programmist")
    with pytest.raises(FetchFailed, match="evil.example.com"):
        parse_listing(html, "programmist")


def test_fake_canonical_planted_before_the_real_one_does_not_win() -> None:
    """Сторож брал ПЕРВОЕ совпадение где угодно в документе, поэтому
    фальшивый canonical в HTML-комментарии побеждал настоящий — и двадцать
    посторонних вакансий проходили тихо. hh.ru отдаёт голову через
    react-helmet и уже кладёт сериализованное состояние в тело страницы."""
    html = (
        "<html><body>"
        '<!-- <link rel="canonical" href="https://hh.ru/vacancies/programmist"> -->'
        "</body>"
        '<head><link rel="canonical" href="https://hh.ru/vacancies"></head></html>'
        '<script type="application/ld+json">'
        '{"@type": "ItemList", "itemListElement": []}</script>'
    )
    with pytest.raises(FetchFailed, match="programmist"):
        parse_listing(html, "programmist")


def test_two_disagreeing_canonicals_are_a_refusal() -> None:
    """Если canonical'ов несколько и они расходятся, верить нечему."""
    html = page("programmist", [item("1")]).replace(
        "</head>", '<link rel="canonical" href="https://hh.ru/vacancies"></head>'
    )
    with pytest.raises(FetchFailed):
        parse_listing(html, "programmist")


def test_item_from_a_foreign_host_is_not_laundered_into_an_hh_url() -> None:
    """Хост элемента не проверялся, а url собирается канонический — поэтому
    ссылка чужого хоста «отмывалась» в https://hh.ru/vacancy/{id} и уходила
    в базу как настоящая вакансия hh.ru."""
    items = [item("1"), {"@type": "ListItem", "url": "https://evil.example.com/vacancy/2",
                         "name": "Чужая"}]
    assert [v.id for v in parse_listing(page("programmist", items), "programmist")] == ["1"]


def test_duplicate_ids_within_one_page_collapse() -> None:
    """Дубликат — это не вторая вакансия. Он врёт счётчику `discovered`,
    который вычисляется по длине списка."""
    items = [item("1"), item("1", "Тот же id, другой заголовок"), item("2")]
    assert [v.id for v in parse_listing(page("programmist", items), "programmist")] == ["1", "2"]


@pytest.mark.parametrize(
    ("bad_id", "case"),
    [
        ("000123", "ведущие нули: отдельная строка для той же вакансии"),
        ("0", "нулевой id"),
        ("1" * 400, "четырёхсотзначный id"),
    ],
)
def test_implausible_id_is_skipped(bad_id: str, case: str) -> None:
    items = [item("135437299"), item(bad_id)]
    found = parse_listing(page("programmist", items), "programmist")
    assert [v.id for v in found] == ["135437299"], case


def test_canonical_outside_the_head_does_not_get_a_vote() -> None:
    """Обратная сторона предыдущего теста, и она же — причина искать в
    `<head>`, а не «требовать согласия всех найденных». Строка, похожая на
    canonical, в сериализованном состоянии страницы (hh.ru кладёт его в
    тело) не имеет права ни подтвердить листинг, ни отвергнуть его: иначе
    правильная страница отказывала бы себе сама."""
    html = page("programmist", [item("1")]).replace(
        "<body>", '<body><!-- <link rel="canonical" href="https://hh.ru/vacancies"> -->'
    )
    assert [v.id for v in parse_listing(html, "programmist")] == ["1"]
