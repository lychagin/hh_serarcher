from pathlib import Path

import pytest
from pydantic import ValidationError

from hh_search.config.loader import load_config

APP_YAML = """
contact_email: "me@example.com"
user_agent: "hh-search/0.1 (personal job search; {contact_email})"
schedule:
  interval_hours: 4
http:
  delay_between_requests_sec: 1.0
  timeout_sec: 20
  max_retries: 3
  respect_robots: true
enrich:
  max_attempts: 3
sinks: [csv, markdown]
paths:
  state: /data/state/hh.db
  reports: /data/reports
  logs: /data/logs
"""

PROFILE_YAML = """
weights: {title: 0.40, stack: 0.30, responsibilities: 0.20, domain: 0.10}
saturation: {stack: 5, responsibilities: 3}
penalty_per_signal: 15
signals:
  title_roles: [team lead]
  title_tech: [backend]
  stack: [yocto]
  responsibilities: [архитектур]
  domain: [телеком]
negative: [junior]
report_threshold: 60
"""

QUERIES_YAML = """
defaults:
  experience: [between3And6, moreThan6]
  employment: full
queries:
  - text: "Yocto"
    cluster: embedded
    weight: 9
    area: [66]
"""


def write_config(tmp_path: Path, **overrides: str) -> Path:
    files = {"app.yaml": APP_YAML, "profile.yaml": PROFILE_YAML, "queries.yaml": QUERIES_YAML}
    files.update(overrides)
    for name, body in files.items():
        (tmp_path / name).write_text(body, encoding="utf-8")
    return tmp_path


def test_loads_all_three_files(tmp_path: Path) -> None:
    cfg = load_config(write_config(tmp_path))
    assert cfg.app.schedule.interval_hours == 4
    assert cfg.profile.weights.stack == 0.30
    assert cfg.queries.queries[0].text == "Yocto"
    assert cfg.queries.queries[0].area == [66]


def test_user_agent_gets_contact_email_substituted(tmp_path: Path) -> None:
    cfg = load_config(write_config(tmp_path))
    assert cfg.app.user_agent == "hh-search/0.1 (personal job search; me@example.com)"


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    broken = PROFILE_YAML + "\nreport_treshold: 70\n"  # опечатка в слове threshold
    with pytest.raises(ValidationError):
        load_config(write_config(tmp_path, **{"profile.yaml": broken}))


def test_weights_must_sum_to_one(tmp_path: Path) -> None:
    broken = PROFILE_YAML.replace("title: 0.40", "title: 0.90")
    with pytest.raises(ValidationError, match="sum to 1.0"):
        load_config(write_config(tmp_path, **{"profile.yaml": broken}))


def test_query_inherits_defaults(tmp_path: Path) -> None:
    cfg = load_config(write_config(tmp_path))
    query = cfg.queries.queries[0]
    assert query.experience == ["between3And6", "moreThan6"]
    assert query.employment == "full"
