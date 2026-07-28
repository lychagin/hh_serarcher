# Стадия сборки: uv, кэш колёс и инструменты сборки не должны доехать до рантайма.
FROM python:3.12-slim AS builder

# Версия uv пинуется. `latest` — плавающий тег, при котором «собиралось вчера»
# и «собирается сегодня» — разные сборки одного коммита.
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/install

WORKDIR /src

# Слой зависимостей отдельно от кода — но именно --no-install-project, а НЕ
# сборка колеса без исходников: hatchling обязан найти force-include
# hh_search/storage/schema.sql, которого в этом слое ещё нет, и падает
# FileNotFoundError. --frozen требует, чтобы ставилось ровно то, что в uv.lock:
# без него lock игнорируется и версии разъезжаются между сборками.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Код — вторым слоем: его правки не пересобирают зависимости.
COPY hh_search ./hh_search
RUN uv sync --frozen --no-dev --no-editable

# Стадия рантайма.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PATH="/install/bin:$PATH" \
    HH_CONFIG_DIR=/data/config

COPY --from=builder /install /install

RUN useradd --create-home --uid 10001 hh
USER 10001:10001

# Корень, а не /app с копией исходников рядом: текущий каталог опережает
# site-packages, и `python -m hh_search` исполнял бы исходник вместо
# установленного пакета. Тогда force-include схемы в колесе не проверяется
# исполнением НИКОГДА, и регрессия упаковки вылезает не в контейнере.
WORKDIR /

# Явно, хотя и совпадает с умолчанием: сигнал остановки — часть контракта
# с обработчиком из Task 11, и менять его молча нельзя.
STOPSIGNAL SIGTERM

ENTRYPOINT ["python", "-m", "hh_search"]
CMD ["serve"]
