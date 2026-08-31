"""User preferences. Isolated from the project file."""

from __future__ import annotations

import json

import pytest

from factcat_app.filters import filter_ui
from factcat_app.prefs import (
    canonical_number,
    load,
    save,
)


def test_defaults_when_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTCAT_PREFS", str(tmp_path / "preferences.json"))
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    data = load()
    assert data["vocab"] == "plain"
    assert data["thousand_sep"] == "comma"
    assert data["decimal_sep"] == "period"
    assert data["theme"] == "light"
    assert data["pad_calendar"] is False
    assert not (tmp_path / "preferences.json").is_file()


def test_save_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTCAT_PREFS", str(tmp_path / "preferences.json"))
    save({"vocab": "sql", "weekday_style": "short"})
    data = load()
    assert data["vocab"] == "sql"
    assert data["weekday_style"] == "short"
    assert data["thousand_sep"] == "comma"


def test_rejects_same_separators(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTCAT_PREFS", str(tmp_path / "preferences.json"))
    with pytest.raises(ValueError, match="differ"):
        save({"thousand_sep": "period", "decimal_sep": "period"})


def test_migrates_seps_from_project_once(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTCAT_PREFS", str(tmp_path / "preferences.json"))
    cfg = tmp_path / "cfg.json"
    monkeypatch.setenv("FACTCAT_CONFIG", str(cfg))
    cfg.write_text(
        json.dumps(
            {
                "project": "acme",
                "thousand_sep": "space",
                "decimal_sep": "comma",
                "entity": "account_id",
            }
        ),
        encoding="utf-8",
    )
    data = load()
    assert data["thousand_sep"] == "space"
    assert data["decimal_sep"] == "comma"
    stored = json.loads((tmp_path / "preferences.json").read_text(encoding="utf-8"))
    assert stored["thousand_sep"] == "space"
    project = json.loads(cfg.read_text(encoding="utf-8"))
    assert "thousand_sep" not in project
    assert "decimal_sep" not in project
    assert project["project"] == "acme"
    assert project["entity"] == "account_id"


def test_canonical_number_period_decimal():
    prefs = {"thousand_sep": "comma", "decimal_sep": "period"}
    assert canonical_number("1,234.56", prefs) == "1234.56"
    assert canonical_number("1234.56", prefs) == "1234.56"
    assert canonical_number("10", prefs) == "10"


def test_canonical_number_comma_decimal():
    prefs = {"thousand_sep": "period", "decimal_sep": "comma"}
    assert canonical_number("1.234,56", prefs) == "1234.56"
    assert canonical_number("1234,56", prefs) == "1234.56"
    assert canonical_number("1234.56", prefs) == "1234.56"


def test_canonical_number_rejects_empty():
    with pytest.raises(ValueError, match="number"):
        canonical_number(" ", {"thousand_sep": "comma", "decimal_sep": "period"})


def test_filter_ui_sql_vocab():
    ui = filter_ui(
        {
            "vocab": "sql",
            "weekday_style": "short",
            "month_style": "short",
            "pad_calendar": True,
            "thousand_sep": "comma",
            "decimal_sep": "period",
        }
    )
    assert ui["ops"]["is_any_of"]["label"] == "IN"
    assert ui["ops"]["contains"]["label"] == "LIKE"
    assert ui["chrome"]["breakdown"] == "GROUP BY"
    assert ui["chrome"]["add_filter"] == "WHERE"
    assert ui["weekdays"][0] == "Mon"
    assert ui["months"][0] == "Jan"
    hour = next(p for p in ui["date_parts"] if p["id"] == "hour_of_day")
    assert "00-23" in hour["label"]


def test_filter_ui_plain_default():
    ui = filter_ui(
        {
            "vocab": "plain",
            "weekday_style": "long",
            "month_style": "long",
            "pad_calendar": False,
        }
    )
    assert ui["ops"]["is_any_of"]["label"] == "is any of"
    assert ui["chrome"]["breakdown"] == "Break down by"
    assert ui["weekdays"][0] == "Monday"
