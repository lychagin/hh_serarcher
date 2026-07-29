# План разработки Python-скрипта для поиска вакансий через hh.ru API

> **ИСХОДНЫЙ МАТЕРИАЛ, НЕ ДЕЙСТВУЮЩИЙ ПЛАН.** Документ описывает архитектуру,
> которая не была реализована: он ходит в `api.hh.ru`, а публичный API для
> соискателей закрыт (403; поддержка прекращена 15.12.2025) и токен получить
> нельзя. Ни одна ветка кода к `api.hh.ru` не обращается. Действующее описание
> системы — `docs/superpowers/specs/2026-07-27-hh-autosearch-design.md`; здесь
> сохранены только исходные соображения (набросок модулей), из которых спека
> выросла (§1 спеки).

## Цель
Создать Python-скрипт, который автоматически ищет вакансии на hh.ru, фильтрует их по профилю, присваивает score релевантности и сохраняет shortlist в CSV или JSON.

## Полезные ссылки на API hh.ru
- HeadHunter API: документация и библиотеки — <https://github.com/hhru/api/blob/master/docs/vacancies.md> [web:21]
- HeadHunter API — общая документация — <https://github.com/hhru/api> [web:39]
- Документация api.hh.ru — <https://api.hh.ru/> [web:23][web:24]
- Быстрый старт по API hh.ru — <https://habr.com/ru/articles/???>

## Что будет делать скрипт
1. Принимать список поисковых запросов.
2. Ходить в API hh.ru и получать вакансии по каждому запросу.
3. Фильтровать вакансии по локации, формату работы, опыту и занятости.
4. Исключать нерелевантные вакансии по стоп-словам.
5. Считать score по признакам совпадения.
6. Сохранять результаты в CSV/JSON.
7. Формировать shortlist для ручного просмотра.

## Исходные параметры
### Входные запросы
- Backend Team Lead
- Tech Lead backend
- Node.js backend
- Python backend
- C++ Embedded Linux
- Embedded Linux BSP
- Yocto
- Buildroot
- MCP
- RAG
- Agentic AI
- Telecom C++

### Базовые фильтры
- Регион: Нижний Новгород, плюс remote/hybrid.
- Опыт: 3–6 лет и 6+ лет.
- Формат: full-time.
- Исключать: junior, intern, support-only, sales, 1C-only.

## Архитектура скрипта
### 1. Config layer
Хранит:
- поисковые запросы,
- фильтры,
- стоп-слова,
- веса скоринга,
- настройки API.

### 2. API client
Отвечает за:
- вызовы `GET /vacancies`,
- пагинацию,
- обработку ошибок,
- rate limit / retry,
- нормализацию ответа.

### 3. Normalizer
Преобразует ответ API в единый формат:
- id,
- title,
- company,
- area,
- salary,
- experience,
- url,
- snippet,
- tags.

### 4. Matcher / Scorer
Считает релевантность по правилам:
- title match,
- stack match,
- responsibility match,
- domain fit,
- penalty за стоп-слова.

### 5. Storage layer
Сохраняет результаты в:
- CSV,
- JSON,
- SQLite при необходимости.

### 6. Report layer
Генерирует:
- shortlist,
- grouped results by cluster,
- top matches for cover letters.

## Логика скоринга
### Позитивные сигналы
- Node.js, Python, C++, Embedded Linux, BSP, Yocto, Buildroot, Docker, Kubernetes, Kafka, PostgreSQL, ClickHouse, Team Lead, Tech Lead, distributed systems, LLM, RAG, MCP.

### Негативные сигналы
- junior, intern, support-only, sales, recruiter, no coding, 1C-only, manual QA.

### Пример весов
- Название вакансии — 40%
- Стек — 30%
- Обязанности — 20%
- Доменная близость — 10%

## Этапы реализации
### Этап 1. MVP
- Скрипт с одним поисковым запросом.
- Получение вакансий из API.
- Фильтрация по стоп-словам.
- Сохранение CSV.

### Этап 2. Multi-query search
- Несколько запросов.
- Слияние результатов.
- Удаление дублей.

### Этап 3. Scoring
- Добавить правила оценки релевантности.
- Сортировать shortlist.

### Этап 4. Export and reporting
- CSV/JSON export.
- Группировка по кластерам.
- Отдельный файл для вакансий с высоким score.

### Этап 5. Automation
- Запуск по расписанию.
- Сохранение истории откликов.
- Подготовка данных для сопроводительных писем.

## Технические детали
### Предлагаемый стек
- Python 3.11+
- requests or httpx
- pandas
- pydantic
- sqlite3 or CSV/JSON
- logging
- argparse or typer

### Структура проекта
```text
hh_search/
  config.py
  client.py
  normalize.py
  scorer.py
  storage.py
  report.py
  main.py
  requirements.txt
```

## Что проверить перед разработкой
- Нужен ли access token для конкретных endpoint'ов.
- Какие лимиты и требования к авторизации действуют для поиска вакансий.
- Как лучше использовать paging и filtering.
- Какие поля API доступны стабильно в поисковой выдаче.

## Результат
После реализации скрипт должен:
- быстро собирать вакансии по нужным запросам,
- отсекать мусор,
- выдавать shortlist по профилю,
- ускорять подготовку откликов и сопроводительных писем.
