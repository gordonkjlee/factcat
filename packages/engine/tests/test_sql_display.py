from factcat_app.sql_display import apply_sql_keyword_case, sql_chrome, sql_plain


def test_keyword_case_lower_keeps_literals_and_names():
    sql = (
        "SELECT COUNT(DISTINCT fc_entity) AS value\n"
        "FROM `acme.analytics.events`\n"
        "WHERE event_name IN ('started', 'UK')\n"
        "GROUP BY 1\n"
        "ORDER BY 1\n"
    )
    out = apply_sql_keyword_case(sql, "bigquery", "lower")
    assert "select" in out
    assert "count(distinct fc_entity)" in out
    assert "from" in out
    assert "`acme.analytics.events`" in out
    assert "where" in out
    assert "event_name" in out
    assert "'started'" in out
    assert "'UK'" in out
    assert "group by" in out
    assert "SELECT" not in out
    assert "COUNT" not in out


def test_keyword_case_upper_is_noop():
    sql = "SELECT COUNT(*) FROM t WHERE x = 'UK'"
    assert apply_sql_keyword_case(sql, "bigquery", "upper") == sql


def test_sql_chrome_marks_only_ticks():
    html = str(sql_chrome("`GROUP BY` each series"))
    assert '<code class="fc-sql">GROUP BY</code> each series' == html
    assert sql_plain("`GROUP BY` each series") == "GROUP BY each series"
    assert sql_plain("Add event") == "Add event"
