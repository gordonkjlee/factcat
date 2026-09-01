"""Display-only SQL casing and chrome markup. Does not change warehouse SQL."""

from __future__ import annotations

import html
import re

from markupsafe import Markup
from sqlglot.dialects.dialect import Dialect
from sqlglot.tokens import TokenType

_SQL_MARK = re.compile(r"`([^`]*)`")

_KEEP_CASE = {
    TokenType.STRING,
    TokenType.IDENTIFIER,
    TokenType.NUMBER,
    TokenType.STAR,
    TokenType.PLACEHOLDER,
    TokenType.UNKNOWN,
}


def apply_sql_keyword_case(sql: str, dialect: str, case: str) -> str:
    """Lowercase keywords and all-caps functions; leave literals and names."""
    if case != "lower" or not (sql or "").strip():
        return sql
    try:
        tokens = Dialect.get_or_raise(dialect).tokenizer().tokenize(sql)
    except Exception:
        return sql
    pieces: list[str] = []
    pos = 0
    for tok in tokens:
        if tok.start > pos:
            pieces.append(sql[pos : tok.start])
        chunk = sql[tok.start : tok.end + 1]
        if tok.token_type not in _KEEP_CASE and _foldable_keyword(chunk):
            chunk = chunk.lower()
        pieces.append(chunk)
        pos = tok.end + 1
    pieces.append(sql[pos:])
    return "".join(pieces)


def _foldable_keyword(chunk: str) -> bool:
    if not chunk or not any(c.isalpha() for c in chunk):
        return False
    return chunk == chunk.upper()


def sql_plain(text: str) -> str:
    return _SQL_MARK.sub(r"\1", text or "")


def sql_chrome(text: str) -> Markup:
    """Render ``GROUP BY`` marks as inline code. English around them stays UI font."""
    raw = text or ""
    out: list[str] = []
    last = 0
    for match in _SQL_MARK.finditer(raw):
        out.append(html.escape(raw[last : match.start()]))
        out.append(f'<code class="fc-sql">{html.escape(match.group(1))}</code>')
        last = match.end()
    out.append(html.escape(raw[last:]))
    return Markup("".join(out))
