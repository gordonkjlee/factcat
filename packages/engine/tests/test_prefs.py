"""User preferences. Isolated from the project file."""

from __future__ import annotations

import json

import pytest

from factcat_app.filters import filter_ui
from factcat_app.prefs import (
    canonical_number,
    format_hour,
    load,
    parse_hour,
    save,
)


def test_defaults_when_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTCAT_PREFS", str(tmp_path / "preferences.json"))
    monkeypatch.setenv("FACTCAT_CONFIG", str(tmp_path / "cfg.json"))
    data = load()
    assert data["vocab"] == "plain"
    assert data["sql_case"] == "upper"
    assert data["sql_neq"] == "<>"
    assert data["thousand_sep"] == "comma"
    assert data["decimal_sep"] == "period"
    assert data["theme"] == "light"
    assert data["pad_day"] is False
    assert data["hour_style"] == "3"
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
    assert ui["ops"]["contains"]["label"] == "LIKE '%s%'"
    assert ui["ops"]["not_contains"]["label"] == "NOT LIKE '%s%'"
    assert ui["ops"]["starts_with"]["label"] == "LIKE 's%'"
    assert ui["ops"]["not_starts_with"]["label"] == "NOT LIKE 's%'"
    assert ui["ops"]["ends_with"]["label"] == "LIKE '%s'"
    assert ui["ops"]["not_ends_with"]["label"] == "NOT LIKE '%s'"
    assert ui["ops"]["is_empty"]["label"] == "= (empty)"
    assert ui["ops"]["is_not"]["label"] == "<>"
    assert ui["ops"]["is_not_empty"]["label"] == "<> (empty)"
    assert ui["numeric_op_labels"]["is_not"] == "<>"
    assert ui["sql_neq"] == "<>"
    assert ui["chrome"]["breakdown"] == "`GROUP BY`"
    assert ui["chrome_plain"]["breakdown"] == "GROUP BY"
    assert ui["chrome"]["add_breakdown"] == "`GROUP BY`"
    assert ui["chrome"]["breakdown_each"] == "`GROUP BY` each series"
    assert ui["chrome_plain"]["breakdown_each"] == "GROUP BY each series"
    assert ui["chrome"]["any_of"] == "`OR`"
    assert ui["chrome"]["event_or"] == "`OR`"
    assert ui["chrome"]["add_group_event"] == "Add event"
    assert ui["chrome"]["add_filter"] == "`WHERE`"
    assert ui["chrome"]["combine"] == "`OR`"
    assert ui["chrome"]["of"] == "of"
    assert ui["chrome"]["volume"] == "`COUNT(*)`"
    assert ui["chrome_plain"]["volume"] == "COUNT(*)"
    assert ui["chrome"]["unique"] == "`COUNT(DISTINCT id)`"
    assert ui["chrome"]["average_per"] == "`COUNT(*)`/`COUNT(DISTINCT id)`"
    assert ui["chrome"]["property_sum"] == "`SUM(x)`"
    assert ui["chrome"]["property_average"] == "`AVERAGE(x)`"
    assert ui["chrome"]["property_median"] == "`MEDIAN(x)`"
    assert ui["chrome"]["property_distinct"] == "`AVG(COUNT(DISTINCT x))`"
    assert "fc-sql" in ui["chrome_html"]["breakdown_each"]
    assert "each series" in ui["chrome_html"]["breakdown_each"]
    assert ui["sql_case"] == "upper"
    assert ui["weekdays"][0] == "Mon"
    assert ui["months"][0] == "Jan"
    hour = next(p for p in ui["date_parts"] if p["id"] == "hour_of_day")
    assert "00" in hour["label"]
    assert hour["group"] == "EXTRACT"
    trunc = next(p for p in ui["date_parts"] if p["id"] == "day")
    assert trunc["group"] == "DATE_TRUNC"


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
    assert ui["chrome"]["of"] == "Of"
    assert ui["chrome"]["property_sum"] == "Sum"
    assert ui["chrome"]["add_breakdown"] == "Add breakdown"
    assert ui["chrome"]["any_of"] == "Any of"
    assert ui["chrome"]["event_or"] == "or"
    assert ui["weekdays"][0] == "Monday"
    hour = next(p for p in ui["date_parts"] if p["id"] == "hour_of_day")
    assert hour["group"] == "Extract"


def test_filter_ui_sql_case_lower():
    ui = filter_ui(
        {
            "vocab": "sql",
            "sql_case": "lower",
            "weekday_style": "short",
            "month_style": "short",
        }
    )
    assert ui["chrome"]["add_filter"] == "`where`"
    assert ui["chrome"]["combine"] == "`or`"
    assert ui["chrome"]["event_or"] == "`or`"
    assert ui["chrome"]["breakdown"] == "`group by`"
    assert ui["chrome"]["add_breakdown"] == "`group by`"
    assert ui["chrome"]["breakdown_each"] == "`group by` each series"
    assert ui["chrome"]["add_group_event"] == "Add event"
    assert ui["chrome_plain"]["volume"] == "count(*)"
    assert ui["chrome_plain"]["unique"] == "count(distinct id)"
    assert ui["chrome"]["property_sum"] == "`sum(x)`"
    assert ui["chrome"]["property_distinct"] == "`avg(count(distinct x))`"
    assert ui["chrome"]["of"] == "of"
    assert ui["ops"]["contains"]["label"] == "like '%s%'"
    assert ui["ops"]["is_null"]["label"] == "is null"
    assert ui["ops"]["is_empty"]["label"] == "= (empty)"
    assert ui["ops"]["is_not"]["label"] == "<>"
    assert ui["sql_case"] == "lower"
    hour = next(p for p in ui["date_parts"] if p["id"] == "hour_of_day")
    assert hour["group"] == "extract"


def test_filter_ui_sql_neq_bang():
    ui = filter_ui(
        {
            "vocab": "sql",
            "sql_neq": "!=",
            "sql_case": "upper",
        }
    )
    assert ui["ops"]["is_not"]["label"] == "!="
    assert ui["ops"]["is_not_empty"]["label"] == "!= (empty)"
    assert ui["numeric_op_labels"]["is_not"] == "!="
    assert ui["ops"]["lt"]["label"] == "<"
    assert ui["ops"]["lte"]["label"] == "<="
    assert ui["sql_neq"] == "!="


def test_pad_calendar_migrates_to_pad_day_and_hour_format(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTCAT_PREFS", str(tmp_path / "preferences.json"))
    (tmp_path / "preferences.json").write_text(
        json.dumps({"pad_calendar": True, "thousand_sep": "comma", "decimal_sep": "period"}),
        encoding="utf-8",
    )
    data = load()
    assert data["pad_day"] is True
    assert data["hour_style"] == "03:00"
    assert "pad_calendar" not in data
    assert "hour_clock" not in data


def test_legacy_clock_and_shape_become_one_style(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTCAT_PREFS", str(tmp_path / "preferences.json"))
    save({"hour_format": "03h", "hour_clock": "12"})
    data = load()
    assert data["hour_style"] == "3 pm"
    assert format_hour(15, data=data) == "3 pm"
    assert "h" not in format_hour(15, data=data)


def test_format_hour_complete_styles():
    assert format_hour(3, "3") == "3"
    assert format_hour(15, "3:00") == "15:00"
    assert format_hour(3, "03:00") == "03:00"
    assert format_hour(0, "03:00") == "00:00"
    assert format_hour(15, "3h") == "15h"
    assert format_hour(0, "3h") == "0h"
    assert format_hour(15, "3pm") == "3pm"
    assert format_hour(15, "3 pm") == "3 pm"
    assert format_hour(15, "3 p.m.") == "3 p.m."
    assert format_hour(15, "3PM") == "3PM"
    assert format_hour(15, "3 PM") == "3 PM"
    assert format_hour(15, "3 P.M.") == "3 P.M."
    assert format_hour(0, "3pm") == "12am"
    assert format_hour(12, "3 pm") == "12 pm"


def test_parse_hour_display_tokens():
    assert parse_hour("15") == 15
    assert parse_hour("3pm") == 15
    assert parse_hour("3 pm") == 15
    assert parse_hour("3 p.m.") == 15
    assert parse_hour("3PM") == 15
    assert parse_hour("3:00 am") == 3
    assert parse_hour("12am") == 0
    assert parse_hour("12 pm") == 12
    assert parse_hour("15h") == 15
    with pytest.raises(ValueError):
        parse_hour("25")

