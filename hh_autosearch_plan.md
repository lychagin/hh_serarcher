# HH.ru Autosearch Plan for Sergey

## Search map

| Query | Filters | What to exclude |
|---|---|---|
| Backend Team Lead | Location: Nizhny Novgorod, remote, hybrid; Experience: 3–6, 6+; Employment: full-time | junior, intern, support-only, sales, 1C |
| Backend Lead | Location: Nizhny Novgorod, remote, hybrid; Experience: 3–6, 6+; Employment: full-time | junior, intern, support-only, sales, 1C |
| Tech Lead backend | Location: Nizhny Novgorod, remote, hybrid; Experience: 3–6, 6+; Employment: full-time | junior, intern, support-only, sales, 1C |
| Team Lead разработки | Location: Nizhny Novgorod, remote, hybrid; Experience: 3–6, 6+; Employment: full-time | junior, intern, support-only, sales, 1C |
| Senior Backend Developer | Location: Nizhny Novgorod, remote, hybrid; Experience: 3–6, 6+; Employment: full-time | junior, intern, support-only, sales, 1C |
| Node.js backend | Location: Nizhny Novgorod, remote, hybrid; Experience: 3–6, 6+; Employment: full-time | junior, intern, support-only, sales, 1C |
| TypeScript backend | Location: Nizhny Novgorod, remote, hybrid; Experience: 3–6, 6+; Employment: full-time | junior, intern, support-only, sales, 1C |
| Микросервисы backend | Location: Nizhny Novgorod, remote, hybrid; Experience: 3–6, 6+; Employment: full-time | junior, intern, support-only, sales, 1C |
| Distributed systems backend | Location: Nizhny Novgorod, remote, hybrid; Experience: 3–6, 6+; Employment: full-time | junior, intern, support-only, sales, 1C |
| AI engineer | Location: remote, hybrid; Experience: 3–6, 6+; Employment: full-time | prompt-only, no-code, training-only, intern |
| LLM engineer | Location: remote, hybrid; Experience: 3–6, 6+; Employment: full-time | prompt-only, no-code, training-only, intern |
| MCP | Location: remote, hybrid; Experience: 3–6, 6+; Employment: full-time | prompt-only, no-code, training-only, intern |
| RAG | Location: remote, hybrid; Experience: 3–6, 6+; Employment: full-time | prompt-only, no-code, training-only, intern |
| Agentic AI | Location: remote, hybrid; Experience: 3–6, 6+; Employment: full-time | prompt-only, no-code, training-only, intern |
| AI Native | Location: remote, hybrid; Experience: 3–6, 6+; Employment: full-time | prompt-only, no-code, training-only, intern |
| C++ Embedded Linux | Location: Nizhny Novgorod, remote, hybrid; Experience: 3–6, 6+; Employment: full-time | desktop-app only, game-dev, junior, intern |
| Embedded Linux BSP | Location: Nizhny Novgorod, remote, hybrid; Experience: 3–6, 6+; Employment: full-time | desktop-app only, game-dev, junior, intern |
| BSP engineer | Location: Nizhny Novgorod, remote, hybrid; Experience: 3–6, 6+; Employment: full-time | desktop-app only, game-dev, junior, intern |
| Firmware developer | Location: Nizhny Novgorod, remote, hybrid; Experience: 3–6, 6+; Employment: full-time | desktop-app only, game-dev, junior, intern |
| Yocto | Location: Nizhny Novgorod, remote, hybrid; Experience: 3–6, 6+; Employment: full-time | desktop-app only, game-dev, junior, intern |
| Buildroot | Location: Nizhny Novgorod, remote, hybrid; Experience: 3–6, 6+; Employment: full-time | desktop-app only, game-dev, junior, intern |
| OpenWRT | Location: Nizhny Novgorod, remote, hybrid; Experience: 3–6, 6+; Employment: full-time | desktop-app only, game-dev, junior, intern |
| Linux kernel module | Location: Nizhny Novgorod, remote, hybrid; Experience: 3–6, 6+; Employment: full-time | desktop-app only, game-dev, junior, intern |
| Telecom C++ | Location: Nizhny Novgorod, remote, hybrid; Experience: 3–6, 6+; Employment: full-time | desktop-app only, game-dev, junior, intern |
| Linux system engineer | Location: Nizhny Novgorod, remote, hybrid; Experience: 3–6, 6+; Employment: full-time | desktop-app only, game-dev, junior, intern |
| Python backend | Location: Nizhny Novgorod, remote, hybrid; Experience: 3–6, 6+; Employment: full-time | data-entry, support-only, junior, intern |
| Data platform engineer | Location: remote, hybrid; Experience: 3–6, 6+; Employment: full-time | data-entry, support-only, junior, intern |
| Kafka backend | Location: remote, hybrid; Experience: 3–6, 6+; Employment: full-time | data-entry, support-only, junior, intern |
| ClickHouse backend | Location: remote, hybrid; Experience: 3–6, 6+; Employment: full-time | data-entry, support-only, junior, intern |
| PostgreSQL backend | Location: remote, hybrid; Experience: 3–6, 6+; Employment: full-time | data-entry, support-only, junior, intern |

## Autosearch structure

### 1. Inputs
- Keywords from the query set.
- Filters: location, remote/hybrid, experience, employment, salary optional.
- Exclusion keywords.

### 2. Vacancy scoring
Use a weighted score:
- Title match: 40%.
- Stack match: 30%.
- Responsibilities match: 20%.
- Company/domain fit: 10%.

### 3. Match signals
Positive signals:
- Node.js, Python, C++, Embedded Linux, BSP, Yocto, Kafka, Docker, Kubernetes, PostgreSQL, ClickHouse, Team Lead, Tech Lead, distributed systems, LLM, RAG, MCP.

Negative signals:
- internship, junior, support-only, sales, recruiter, manual QA only, no coding, 1C-only.

### 4. Workflow
1. Run query by keyword.
2. Apply filters.
3. Parse title, company, salary, experience, area, snippet.
4. Score against profile.
5. Save top results.
6. Generate tailored cover letter for top matches.

### 5. Data model
Store each vacancy with:
- id
- title
- company
- area
- salary
- experience
- url
- snippet
- score
- tags
- status

### 6. Output
- Shortlist sorted by score.
- Separate buckets: backend, embedded, AI, telecom.
- Cover letter draft for the top 3 matches.
