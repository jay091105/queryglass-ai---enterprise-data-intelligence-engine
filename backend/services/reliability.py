"""Deterministic semantic checks and reliability scoring for SQL requests."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from backend.services.sql_engine import SchemaContext


_COMPARATORS = (
    (re.compile(r"(?<![a-z0-9])(?:at[_\s]+least|no[_\s]+less[_\s]+than|greater[_\s]+than[_\s]+or[_\s]+equal[_\s]+to)(?![a-z0-9])"), "gte"),
    (re.compile(r"(?<![a-z0-9])(?:at[_\s]+most|no[_\s]+more[_\s]+than|less[_\s]+than[_\s]+or[_\s]+equal[_\s]+to)(?![a-z0-9])"), "lte"),
    (re.compile(r"(?<![a-z0-9])(?:greater[_\s]+than|more[_\s]+than|above|over|exceeds?)(?![a-z0-9])"), "gt"),
    (re.compile(r"(?<![a-z0-9])(?:less[_\s]+than|below|under|fewer[_\s]+than)(?![a-z0-9])"), "lt"),
    (re.compile(r"(?<![a-z0-9])(?:equal[_\s]+to|equals?)(?![a-z0-9])"), "eq"),
)
_BETWEEN = re.compile(
    r"(?<![a-z0-9])between[_\s]+(-?\d[\d,]*(?:\.\d+)?)"
    r"[_\s]+and[_\s]+(-?\d[\d,]*(?:\.\d+)?)(?![a-z0-9])"
)
_SQL_COMPARATORS = {
    exp.GT: "gt",
    exp.GTE: "gte",
    exp.LT: "lt",
    exp.LTE: "lte",
    exp.EQ: "eq",
}
_AGGREGATE_TERMS = {
    "average": "avg",
    "avg": "avg",
    "mean": "avg",
    "sum": "sum",
    "total": "sum",
    "count": "count",
    "maximum": "max",
    "max": "max",
    "minimum": "min",
    "min": "min",
}
_CRITICAL_TYPES = {
    "missing_column",
    "missing_table",
    "unsupported_relationship",
    "missing_required_filter",
    "unsupported_diagnosis",
    "fabricated_denominator",
    "unrelated_sql",
    "unsafe_sql",
    "invalid_schema",
    "incorrect_aggregation",
    "incorrect_grouping",
    "incorrect_ranking",
    "missing_output",
    "excess_output",
    "prompt_injection",
    "unsupported_assumption",
    "contradictory_filter",
}


def _normalized(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")


def _parse_query(sql: str) -> exp.Query | None:
    try:
        statements = [statement for statement in sqlglot.parse(sql, read="sqlite") if statement]
    except ParseError:
        return None
    if len(statements) != 1 or not isinstance(statements[0], exp.Query):
        return None
    return statements[0]


def _database_tables(db_path: str) -> set[str]:
    path = Path(db_path)
    if not path.is_file():
        return set()
    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=1) as connection:
            return {
                str(row[0]).casefold()
                for row in connection.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type='table'
                      AND name NOT LIKE 'sqlite_%'
                      AND name NOT LIKE '_queryglass_%'
                    """
                )
            }
    except sqlite3.Error as error:
        raise RuntimeError("Could not inspect tables in the configured database.") from error


def _issue(
    issue_type: str,
    message: str,
    requirement: str,
    recommendation: str,
    *,
    sql_section: str = "query",
    missing_object: str | None = None,
    critical: bool | None = None,
) -> dict[str, Any]:
    return {
        "type": issue_type,
        "message": message,
        "missing_object": missing_object,
        "requirement": requirement,
        "sql_section": sql_section,
        "recommendation": recommendation,
        "critical": issue_type in _CRITICAL_TYPES if critical is None else critical,
    }


def extract_requirements(
    question: str,
    schema_ctx: SchemaContext,
    db_path: str,
) -> dict[str, Any]:
    """Extract requirements that can be checked against the selected schema."""
    normalized_question = _normalized(question)
    columns = {
        name: _normalized(info["name"])
        for name, info in schema_ctx.columns_info.items()
    }
    mentions: list[tuple[str, int, int]] = []
    for column, normalized_column in columns.items():
        match = re.search(
            rf"(?<![a-z0-9]){re.escape(normalized_column)}(?![a-z0-9])",
            normalized_question,
        )
        if match:
            mentions.append((column, match.start(), match.end()))
    mentioned_names = {column for column, _, _ in mentions}
    question_tokens = set(normalized_question.split("_"))
    if "duration" in question_tokens:
        duration_matches = []
        for column, normalized_column in columns.items():
            tokens = set(normalized_column.split("_"))
            if column not in mentioned_names and tokens.intersection({"hour", "hours"}):
                overlap = len(tokens.intersection(question_tokens - {"duration", "hour", "hours"}))
                if overlap:
                    score = overlap + int("avg" in tokens or "average" in tokens)
                    score -= int("delta" in tokens)
                    duration_matches.append((score, column))
        best_score = max((score for score, _ in duration_matches), default=0)
        best_columns = [column for score, column in duration_matches if score == best_score]
        if len(best_columns) == 1:
            match = re.search(r"(?<![a-z0-9])duration(?![a-z0-9])", normalized_question)
            if match:
                mentions.append((best_columns[0], match.start(), match.end()))
    if "year" in question_tokens:
        year_columns = [
            column for column, normalized_column in columns.items()
            if "year" in normalized_column.split("_")
        ]
        if len(year_columns) == 1 and year_columns[0] not in mentioned_names:
            match = re.search(r"(?<![a-z0-9])year(?![a-z0-9])", normalized_question)
            if match:
                mentions.append((year_columns[0], match.start(), match.end()))

    identifier_candidates = set(
        re.findall(r"\b[a-zA-Z][a-zA-Z0-9]*(?:_[a-zA-Z0-9]+)+\b", question)
    )
    existing_columns = set(columns)
    existing_tables = _database_tables(db_path)
    existing_tables.add(schema_ctx.table_name.casefold())
    missing_columns: list[str] = []
    missing_tables: list[str] = []
    unsupported_tables: list[str] = []
    for candidate in sorted(identifier_candidates, key=str.casefold):
        normalized_candidate = candidate.casefold()
        if normalized_candidate in existing_columns or normalized_candidate in existing_tables:
            continue
        if re.fullmatch(r"(?:top|bottom)_\d+", normalized_candidate):
            continue
        preceding = question[max(0, question.casefold().find(candidate.casefold()) - 45):].casefold()
        following_start = question.casefold().find(candidate.casefold()) + len(candidate)
        following = question[following_start:following_start + 30].casefold()
        table_context = (
            re.search(r"\b(?:table|database|records?\s+from|dataset|join|compare\s+with)\b", preceding)
            or re.match(r"\s+(?:table|database)\b", following)
        )
        if table_context:
            if normalized_candidate in existing_tables:
                unsupported_tables.append(candidate)
            else:
                missing_tables.append(candidate)
        else:
            missing_columns.append(candidate)

    requested_filters: list[dict[str, Any]] = []
    for column, start, end in mentions:
        next_start = min(
            (item_start for _, item_start, _ in mentions if item_start > start),
            default=len(normalized_question),
        )
        remainder = normalized_question[end:next_start]
        between_match = _BETWEEN.search(remainder)
        if between_match:
            requested_filters.append({
                "column": column,
                "operator": "between",
                "lower": float(between_match.group(1).replace(",", "")),
                "upper": float(between_match.group(2).replace(",", "")),
                "source": question[start:end],
            })
            continue
        for comparator_pattern, comparator in _COMPARATORS:
            comparator_match = comparator_pattern.search(remainder)
            if not comparator_match:
                continue
            number_match = re.search(
                r"(?:₹|rs\.?\s*)?[_\s]*(-?\d[\d,]*(?:\.\d+)?)",
                remainder[comparator_match.end():],
                re.IGNORECASE,
            )
            if number_match:
                number = number_match.group(1).replace(",", "")
                requested_filters.append({
                    "column": column,
                    "operator": comparator,
                    "value": float(number),
                    "source": question[start:end],
                })
            break

    requested_outputs = []
    output_words = ("show", "display", "list", "return", "include", "provide")
    for column, start, _ in mentions:
        prefix = normalized_question[max(0, start - 60):start]
        if any(
            re.search(
                rf"(?<![a-z0-9]){word}(?![a-z0-9])[^.?!;]*$",
                prefix,
            )
            for word in output_words
        ):
            requested_outputs.append(column)

    group_by_columns: list[str] = []
    for column, start, _ in mentions:
        prefix = normalized_question[max(0, start - 35):start]
        if re.search(
            r"(?:^|_)(?:by|each|every|per|respective)(?:_the)?(?:_year)?_?$", prefix
        ) or re.search(
            r"(?:^|_)(?:each|every|per|respective)_?$", prefix
        ):
            group_by_columns.append(column)

    relative_average = None
    relative_comparison = re.search(
        r"(?<![a-z0-9])(?:above|exceed(?:s)?|greater[_\s]+than)(?![a-z0-9])",
        normalized_question,
    )
    if relative_comparison and re.search(
        r"(?<![a-z0-9])average(?![a-z0-9])",
        normalized_question[relative_comparison.end():],
    ):
        prior_mentions = [
            (end, column)
            for column, _, end in mentions
            if end <= relative_comparison.start()
        ]
        if prior_mentions:
            relative_average = {"column": max(prior_mentions)[1]}

    aggregation = None
    for term, function in _AGGREGATE_TERMS.items():
        if re.search(
            rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])",
            normalized_question,
        ):
            metrics = [
                (start, column)
                for column, start, _ in mentions
                if start >= normalized_question.find(term)
            ]
            metric = min(metrics)[1] if metrics else None
            aggregation = {"function": function, "column": metric}
            break
    if aggregation and aggregation["function"] == "avg" and aggregation["column"] is None:
        aggregation["column"] = relative_average["column"] if relative_average else None

    extreme_mentions = []
    for match in re.finditer(
        r"(?<![a-z0-9])(?:highest|maximum|max|lowest|minimum|min)(?![a-z0-9])",
        normalized_question,
    ):
        following_column = next(
            (
                (column, start, end)
                for column, start, end in sorted(mentions, key=lambda item: item[1])
                if match.end() <= start < match.end() + 60
            ),
            None,
        )
        if following_column:
            extreme_mentions.append((match.group(), match.start(), *following_column))

    extreme_pair = None
    if len(extreme_mentions) >= 2:
        first, second = extreme_mentions[0], extreme_mentions[1]
        relation = normalized_question[first[4]:second[1]]
        first_function = "max" if first[0] in {"highest", "maximum", "max"} else "min"
        second_function = "max" if second[0] in {"highest", "maximum", "max"} else "min"
        if (
            first[2] != second[2]
            and re.search(r"(?<![a-z0-9])(?:that|which|who|with|among|where|whose|for)(?![a-z0-9])", relation)
        ):
            extreme_pair = {
                "primary_column": first[2],
                "primary_function": first_function,
                "condition_column": second[2],
                "condition_function": second_function,
            }
            if aggregation is None:
                aggregation = {
                    "function": first_function,
                    "column": first[2],
                }

    top_match = re.search(
        r"(?<![a-z0-9])top[_\s]+(\d+)(?![a-z0-9])",
        normalized_question,
    )
    rank_column = None
    if top_match:
        ranked_phrase = re.search(
            r"(?<![a-z0-9])(?:based_on|according_to|highest|largest|greatest|ordered_by)(?![a-z0-9])",
            normalized_question[top_match.end():],
        )
        rank_column = None
        if ranked_phrase:
            rank_start = top_match.end() + ranked_phrase.end()
            rank_column = next(
                (column for column, start, _ in mentions if start >= rank_start),
                None,
            )
        if rank_column is None:
            rank_column = next(
                (column for column, start, _ in mentions if start >= top_match.end()),
                None,
            )
        if rank_column is None:
            rank_column = next((column for column, _, _ in mentions), None)

    hypothetical_counts = []
    if re.search(
        r"(?<![a-z0-9])(?:pretend|assume|hypothetical|imagine)(?![a-z0-9])",
        normalized_question,
    ):
        hypothetical_counts = list({
            int(value.replace(",", ""))
            for value in (
                re.findall(
                    r"\b(?:dataset|data|table|database)\D{0,24}(\d[\d,]*)\b",
                    question,
                    re.IGNORECASE,
                )
                + re.findall(
                    r"\b(\d[\d,]*)\s+(?:rows?|records?|students?)\b",
                    question,
                    re.IGNORECASE,
                )
            )
        })

    requires_percentage = bool(
        re.search(r"(?<![a-z0-9])(?:percentage|percent)(?![a-z0-9])", normalized_question)
    )
    diagnosis_requested = bool(re.search(
        r"(?<![a-z0-9])(?:depression|diagnos(?:e|is|ed)|medical[_\s]+condition|disease)(?![a-z0-9])",
        normalized_question,
    ))
    causal_inference_requested = bool(
        re.search(
            r"(?<![a-z0-9])(?:due[_\s]+to|caused[_\s]+by|causes?|because[_\s]+of)(?![a-z0-9])",
            normalized_question,
        )
        and re.search(
            r"(?<![a-z0-9])(?:sleep|screen|study|concentration|energy)(?![a-z0-9])",
            normalized_question,
        )
    )
    diagnosis_column_exists = any(
        re.search(r"(?:depress|diagnos|medical_condition|disease)", column)
        for column in columns
    )
    unsupported_diagnosis = causal_inference_requested or (
        diagnosis_requested and not diagnosis_column_exists
    )

    return {
        "mentioned_columns": sorted({column for column, _, _ in mentions}),
        "missing_columns": sorted(set(missing_columns)),
        "missing_tables": sorted(set(missing_tables)),
        "unsupported_tables": sorted(set(unsupported_tables)),
        "filters": requested_filters,
        "requested_outputs": sorted(set(requested_outputs)),
        "group_by_columns": sorted(set(group_by_columns)),
        "aggregation": aggregation,
        "extreme_pair": extreme_pair,
        "relative_average_filter": relative_average,
        "top_n": int(top_match.group(1)) if top_match else None,
        "rank_column": rank_column,
        "requires_percentage": requires_percentage,
        "hypothetical_counts": hypothetical_counts,
        "unsupported_diagnosis": unsupported_diagnosis,
    }


def _column_name(expression: exp.Expression) -> str | None:
    if isinstance(expression, exp.Column):
        return expression.name.casefold()
    return None


def _numeric_value(expression: exp.Expression) -> float | None:
    if isinstance(expression, (exp.Paren, exp.Cast, exp.TryCast)):
        return _numeric_value(expression.this)
    if isinstance(expression, exp.Neg):
        value = _numeric_value(expression.this)
        return -value if value is not None else None
    if isinstance(expression, exp.Literal) and not expression.is_string:
        try:
            return float(expression.this)
        except (TypeError, ValueError):
            return None
    return None


def _matches_required_filter(query: exp.Query, requirement: dict[str, Any]) -> bool:
    if requirement["operator"] == "between":
        return any(
            _column_name(node.this) == requirement["column"]
            and _numeric_value(node.args.get("low")) == requirement["lower"]
            and _numeric_value(node.args.get("high")) == requirement["upper"]
            for node in query.find_all(exp.Between)
        )
    for node in query.walk():
        comparator = _SQL_COMPARATORS.get(type(node))
        if comparator != requirement["operator"]:
            continue
        left, right = node.args.get("this"), node.args.get("expression")
        for column_expr, value_expr in ((left, right), (right, left)):
            if _column_name(column_expr) != requirement["column"]:
                continue
            value = _numeric_value(value_expr)
            if value is not None and value == requirement["value"]:
                return True
    return False


def _literal_filter_values(query: exp.Query) -> list[exp.Expression]:
    values: list[exp.Expression] = []
    comparison_types = (exp.GT, exp.GTE, exp.LT, exp.LTE, exp.EQ, exp.NEQ)
    for where in (*query.find_all(exp.Where), *query.find_all(exp.Having)):
        condition = where.this
        for node in condition.walk():
            if isinstance(node, comparison_types):
                left, right = node.this, node.expression
                if isinstance(left, exp.Case):
                    continue
                if isinstance(left, exp.Column) and isinstance(right, exp.Literal):
                    values.append(right)
                elif isinstance(right, exp.Column) and isinstance(left, exp.Literal):
                    values.append(left)
            elif isinstance(node, exp.In) and isinstance(node.this, exp.Column):
                values.extend(
                    value for value in node.expressions if isinstance(value, exp.Literal)
                )
            elif isinstance(node, exp.Between) and isinstance(node.this, exp.Column):
                values.extend(
                    value for value in (node.args.get("low"), node.args.get("high"))
                    if isinstance(value, exp.Literal)
                )
    return values


def _conjunctive_terms(expression: exp.Expression) -> list[exp.Expression]:
    if isinstance(expression, exp.Paren):
        return _conjunctive_terms(expression.this)
    if isinstance(expression, exp.And):
        return _conjunctive_terms(expression.this) + _conjunctive_terms(expression.expression)
    return [expression]


def _has_contradictory_filters(query: exp.Query) -> bool:
    inverses = {
        exp.GT: exp.LT,
        exp.GTE: exp.LTE,
        exp.LT: exp.GT,
        exp.LTE: exp.GTE,
        exp.EQ: exp.EQ,
    }
    comparison_types = (exp.GT, exp.GTE, exp.LT, exp.LTE, exp.EQ)
    for where in query.find_all(exp.Where):
        bounds: dict[str, dict[str, Any]] = {}
        for term in _conjunctive_terms(where.this):
            if not isinstance(term, comparison_types):
                continue
            left, right = term.this, term.expression
            column = left if isinstance(left, exp.Column) else right if isinstance(right, exp.Column) else None
            literal = right if column is left else left if column is right else None
            if column is None or not isinstance(literal, exp.Literal) or literal.is_string:
                continue
            value = _numeric_value(literal)
            if value is None:
                continue
            operator = type(term) if column is left else inverses.get(type(term))
            if operator is None:
                continue
            entry = bounds.setdefault(
                column.name.casefold(),
                {"lower": None, "upper": None, "equal": set()},
            )
            if operator is exp.EQ:
                entry["equal"].add(value)
            elif operator in (exp.GT, exp.GTE):
                candidate = (value, operator is exp.GTE)
                current = entry["lower"]
                if current is None or candidate[0] > current[0] or (
                    candidate[0] == current[0] and not candidate[1]
                ):
                    entry["lower"] = candidate
            else:
                candidate = (value, operator is exp.LTE)
                current = entry["upper"]
                if current is None or candidate[0] < current[0] or (
                    candidate[0] == current[0] and not candidate[1]
                ):
                    entry["upper"] = candidate
        for entry in bounds.values():
            if len(entry["equal"]) > 1:
                return True
            lower, upper = entry["lower"], entry["upper"]
            if lower and upper and (
                lower[0] > upper[0]
                or (
                    lower[0] == upper[0]
                    and not (lower[1] and upper[1])
                )
            ):
                return True
            if entry["equal"]:
                value = next(iter(entry["equal"]))
                if lower and (value < lower[0] or (value == lower[0] and not lower[1])):
                    return True
                if upper and (value > upper[0] or (value == upper[0] and not upper[1])):
                    return True
    return False


def _question_literals(question: str) -> tuple[set[float], str]:
    numbers = {
        float(value.replace(",", ""))
        for value in re.findall(
            r"(?<![a-zA-Z0-9])[-+]?\d[\d,]*(?:\.\d+)?", question
        )
    }
    return numbers, _normalized(question)


def _selected_columns(query: exp.Query) -> tuple[set[str], bool]:
    outer = query.find(exp.Select)
    if outer is None:
        return set(), False
    selected: set[str] = set()
    has_star = False
    for projection in outer.expressions:
        if isinstance(projection, exp.Star) or projection.find(exp.Star):
            has_star = True
        selected.update(column.name.casefold() for column in projection.find_all(exp.Column))
    return selected, has_star


def _has_percentage_denominator(query: exp.Query) -> bool:
    for division in query.find_all(exp.Div):
        if any(
            isinstance(node, exp.Count)
            for node in division.expression.walk()
        ):
            return True
    for average in query.find_all(exp.Avg):
        if average.find(exp.Case):
            return True
    return False


def semantic_validate(
    question: str,
    sql: str,
    schema_ctx: SchemaContext,
    db_path: str,
    *,
    schema_validation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compare parsed SQL against explicit, schema-grounded question requirements."""
    requirements = extract_requirements(question, schema_ctx, db_path)
    understood = bool(question.strip())
    issues: list[dict[str, Any]] = []
    for column in requirements["missing_columns"]:
        issues.append(_issue(
            "missing_column",
            f"Requested column '{column}' does not exist in the selected dataset.",
            f"Use the requested field '{column}'.",
            "Choose a field present in the selected dataset or change the request.",
            missing_object=column,
            sql_section="schema",
        ))
    for table in requirements["missing_tables"]:
        issues.append(_issue(
            "missing_table",
            f"Requested table or dataset '{table}' is not present in the database.",
            f"Use records from '{table}'.",
            "Add or select the requested dataset before comparing it.",
            missing_object=table,
            sql_section="FROM/JOIN",
        ))
    for table in requirements["unsupported_tables"]:
        issues.append(_issue(
            "unsupported_relationship",
            f"'{table}' exists but is not the selected dataset; cross-table relationships are not available in this query.",
            f"Compare the selected dataset with '{table}'.",
            "Select the requested dataset or provide a supported relationship before joining.",
            missing_object=table,
            sql_section="FROM/JOIN",
        ))

    invalid_schema = schema_validation or {}
    for table in invalid_schema.get("invalid_tables", []):
        if not any(issue["missing_object"] == table for issue in issues):
            issues.append(_issue(
                "invalid_schema",
                f"SQL references unavailable table '{table}'.",
                f"Use only the selected dataset '{schema_ctx.table_name}'.",
                "Remove the unsupported table reference.",
                missing_object=table,
                sql_section="FROM/JOIN",
            ))
    for column in invalid_schema.get("invalid_columns", []):
        if not any(issue["missing_object"] == column for issue in issues):
            issues.append(_issue(
                "invalid_schema",
                f"SQL references unavailable column '{column}'.",
                "Use columns from the selected dataset schema.",
                "Replace the field with an existing column or clarify the request.",
                missing_object=column,
                sql_section="projection/filter",
            ))

    if requirements["unsupported_diagnosis"]:
        issues.append(_issue(
            "unsupported_diagnosis",
            "The selected data cannot establish a medical diagnosis or causal claim.",
            question,
            "Request descriptive analysis of measured fields without inferring a diagnosis or cause.",
            sql_section="interpretation",
        ))

    if re.search(
        r"\b(?:ignore[_\s]+(?:(?:all|the)[_\s]+)?(?:previous[_\s]+)?(?:instructions|rules|schema)|"
        r"bypass (?:the )?(?:guardrails|safety)|show (?:me )?(?:secrets|environment variables|api keys))\b",
        question,
        re.IGNORECASE,
    ):
        issues.append(_issue(
            "prompt_injection",
            "The request attempts to bypass the SQL policy or access application secrets.",
            question,
            "Ask a question answerable from the selected dataset.",
            sql_section="request",
        ))

    query = _parse_query(sql) if sql else None
    if not sql:
        return {
            "status": "invalid" if issues else "not_fully_verified",
            "passed": not any(issue["critical"] for issue in issues),
            "question_understanding_status": "verified" if understood else "not_fully_verified",
            "sql_coverage_status": "not_fully_verified",
            "requirements": requirements,
            "issues": issues,
        }
    if query is None:
        return {
            "status": "not_fully_verified",
            "passed": not any(issue["critical"] for issue in issues),
            "question_understanding_status": "verified" if understood else "not_fully_verified",
            "sql_coverage_status": "not_fully_verified",
            "requirements": requirements,
            "issues": issues,
        }

    if question.strip():
        question_numbers, normalized_question = _question_literals(question)
        question_words = set(normalized_question.split("_"))
        for literal in _literal_filter_values(query):
            if literal.is_string:
                literal_words = set(_normalized(literal.this).split("_"))
                is_grounded = bool(literal_words) and literal_words.issubset(question_words)
            else:
                value = _numeric_value(literal)
                is_grounded = value is not None and value in question_numbers
            if not is_grounded:
                value = literal.this
                issues.append(_issue(
                    "unsupported_assumption",
                    f"The SQL filters on the unrequested value '{value}'.",
                    question,
                    "Remove the unsupported filter or ask the user to confirm that value.",
                    sql_section="WHERE/HAVING",
                ))
                break

    if _has_contradictory_filters(query):
        issues.append(_issue(
            "contradictory_filter",
            "The SQL combines mutually exclusive conditions on the same field and can never match a row.",
            question,
            "Resolve the conflicting predicates and preserve the conditions intended by the request.",
            sql_section="WHERE",
        ))

    cte_names = {
        cte.alias_or_name.casefold()
        for cte in query.find_all(exp.CTE)
    }
    for select in query.find_all(exp.Select):
        source = select.args.get("from_")
        left_source = source.this if source is not None else None
        if not isinstance(left_source, exp.Table) or left_source.name.casefold() in cte_names:
            continue
        for join in select.args.get("joins") or []:
            right_source = join.this
            if (
                isinstance(right_source, exp.Table)
                and right_source.name.casefold() == left_source.name.casefold()
                and right_source.name.casefold() == schema_ctx.table_name.casefold()
            ):
                issues.append(_issue(
                    "unsupported_relationship",
                    "A direct self-join uses a relationship that is not defined by the selected dataset schema.",
                    question,
                    "Use a schema-defined relationship or derive the comparison from the selected table with a CTE.",
                    sql_section="JOIN",
                    missing_object=right_source.name,
                ))
                break

    for where in query.find_all(exp.Where):
        condition = where.this
        is_constant_false = (
            isinstance(condition, exp.Boolean) and not condition.this
        ) or (
            isinstance(condition, exp.Literal)
            and not condition.is_string
            and _numeric_value(condition) == 0
        )
        if is_constant_false and not re.search(
            r"\b(?:no rows|no results|return nothing|empty result|where\s+0)\b",
            question,
            re.IGNORECASE,
        ):
            issues.append(_issue(
                "unrelated_sql",
                "The query uses an always-false condition not requested by the user.",
                question,
                "Generate a query that applies the requested conditions instead of forcing an empty result.",
                sql_section="WHERE",
            ))
            break

    for requirement in requirements["filters"]:
        if not _matches_required_filter(query, requirement):
            requested_condition = (
                f"{requirement['column']} BETWEEN {requirement['lower']:g} AND {requirement['upper']:g}"
                if requirement["operator"] == "between"
                else f"{requirement['column']} {requirement['operator']} {requirement['value']:g}"
            )
            issues.append(_issue(
                "missing_required_filter",
                f"SQL does not preserve the requested {requested_condition} condition.",
                requested_condition,
                "Add the requested predicate in the WHERE/HAVING clause or a semantically equivalent CTE/subquery.",
                sql_section="WHERE/HAVING",
                missing_object=requirement["column"],
            ))

    selected_columns, has_star = _selected_columns(query)
    for output in requirements["requested_outputs"]:
        if output not in selected_columns and not has_star:
            issues.append(_issue(
                "missing_output",
                f"SQL does not return the requested '{output}' field.",
                f"Display '{output}'.",
                f"Include '{output}' in the final SELECT projection.",
                sql_section="SELECT",
                missing_object=output,
            ))
        elif output in requirements["requested_outputs"] and has_star:
            issues.append(_issue(
                "excess_output",
                "SQL selects every column although the request names a specific output.",
                f"Return only the requested field '{output}'.",
                "Replace SELECT * with the requested output columns.",
                sql_section="SELECT",
            ))

    aggregation = requirements["aggregation"]
    if aggregation:
        expected = aggregation["function"]
        found = any(
            node.key == expected
            and (
                not aggregation["column"]
                or any(
                    column.name.casefold() == aggregation["column"]
                    for column in node.find_all(exp.Column)
                )
            )
            for node in query.walk()
            if isinstance(node, exp.AggFunc)
        )
        if not found:
            issues.append(_issue(
                "incorrect_aggregation",
                f"SQL does not apply the requested {expected.upper()} aggregation.",
                f"Calculate {expected} as requested.",
                f"Use the requested {expected.upper()} aggregation.",
                sql_section="SELECT",
            ))

    extreme_pair = requirements["extreme_pair"]
    if extreme_pair:
        condition_column = extreme_pair["condition_column"]
        condition_function = exp.Max if extreme_pair["condition_function"] == "max" else exp.Min
        comparison_types = (
            (exp.EQ, exp.GTE, exp.GT)
            if extreme_pair["condition_function"] == "max"
            else (exp.EQ, exp.LTE, exp.LT)
        )
        has_condition_aggregate = any(
            isinstance(node, condition_function)
            and any(
                column.name.casefold() == condition_column
                for column in node.find_all(exp.Column)
            )
            for node in query.walk()
        )
        has_extreme_filter = any(
            _column_name(column_expression) == condition_column
            and any(
                isinstance(node, condition_function)
                and any(
                    column.name.casefold() == condition_column
                    for column in node.find_all(exp.Column)
                )
                for node in other_expression.walk()
            )
            for where in query.find_all(exp.Where)
            for comparison in where.this.walk()
            if isinstance(comparison, comparison_types)
            for column_expression, other_expression in (
                (comparison.this, comparison.expression),
                (comparison.expression, comparison.this),
            )
        )
        if not has_condition_aggregate or not has_extreme_filter:
            issues.append(_issue(
                "missing_required_filter",
                f"SQL does not restrict results to rows with the {extreme_pair['condition_function']}imum '{condition_column}'.",
                f"Use only rows with the {extreme_pair['condition_function']} value of '{condition_column}'.",
                f"Compare '{condition_column}' with its {extreme_pair['condition_function']} value in a subquery or equivalent CTE.",
                sql_section="WHERE/CTE",
                missing_object=condition_column,
            ))

    grouped = {
        column.name.casefold()
        for group in query.find_all(exp.Group)
        for column in group.find_all(exp.Column)
    }
    grouped.update(
        column.name.casefold()
        for window in query.find_all(exp.Window)
        for partition in (window.args.get("partition_by") or [])
        for column in partition.find_all(exp.Column)
    )
    for column in requirements["group_by_columns"]:
        if column not in grouped:
            issues.append(_issue(
                "incorrect_grouping",
                f"SQL does not group the result by '{column}'.",
                f"Group results by '{column}'.",
                f"Add '{column}' to GROUP BY.",
                sql_section="GROUP BY",
                missing_object=column,
            ))

    relative_average = requirements["relative_average_filter"]
    if relative_average:
        metric = relative_average["column"]
        has_average = any(
            column.name.casefold() == metric
            for average in query.find_all(exp.Avg)
            for column in average.find_all(exp.Column)
        )
        has_relative_comparison = any(
            isinstance(node, (exp.GT, exp.GTE))
            and _column_name(node.this) == metric
            and not isinstance(node.expression, exp.Literal)
            and _column_name(node.expression) != metric
            for node in query.walk()
        )
        if not has_average or not has_relative_comparison:
            issues.append(_issue(
                "missing_required_filter",
                f"SQL does not compare '{metric}' with its requested average.",
                f"{metric} must exceed the average for its respective group.",
                "Calculate the group average in a CTE and retain the comparison in the final query.",
                sql_section="CTE/WHERE",
                missing_object=metric,
            ))

    if requirements["top_n"] is not None:
        order_columns = [
            (ordered.this.name.casefold(), ordered.args.get("desc") is True)
            for order in query.find_all(exp.Order)
            for ordered in order.expressions
            if isinstance(ordered.this, exp.Column)
        ]
        has_limit = any(
            _numeric_value(limit.expression) is not None
            and _numeric_value(limit.expression) <= requirements["top_n"]
            for limit in query.find_all(exp.Limit)
        )
        has_rank_window = any(
            isinstance(function, (exp.RowNumber, exp.Rank, exp.DenseRank))
            for window in query.find_all(exp.Window)
            for function in window.walk()
        )
        rank_filter = has_rank_window and any(
            isinstance(node, (exp.LTE, exp.LT))
            and _numeric_value(node.expression) is not None
            and _numeric_value(node.expression) <= requirements["top_n"]
            for node in query.walk()
        )
        ranking_column = requirements["rank_column"]
        correctly_ordered = any(
            column == ranking_column and descending
            for column, descending in order_columns
        )
        if not (has_limit or rank_filter) or (ranking_column and not correctly_ordered):
            issues.append(_issue(
                "incorrect_ranking",
                "SQL does not implement the requested top-N ranking and ordering.",
                f"Return the top {requirements['top_n']} records in descending order.",
                "Order by the requested ranking metric descending and apply LIMIT or a ranking window.",
                sql_section="ORDER BY/LIMIT",
            ))

    if requirements["requires_percentage"]:
        percentage_requested = any(
            isinstance(node, exp.Div) and any(
                isinstance(child, exp.Count) for child in node.expression.walk()
            )
            for node in query.walk()
        ) or _has_percentage_denominator(query)
        if not percentage_requested:
            issues.append(_issue(
                "fabricated_denominator",
                "Percentage SQL does not use a database-derived denominator.",
                question,
                "Divide by COUNT(*) or use an equivalent average over actual dataset rows.",
                sql_section="SELECT",
            ))
    for count in requirements["hypothetical_counts"]:
        if any(
            (value := _numeric_value(literal)) is not None and value == count
            for literal in query.find_all(exp.Literal)
        ):
            issues.append(_issue(
                "fabricated_denominator",
                f"The query uses the hypothetical value {count} as if it were an actual dataset fact.",
                question,
                "Use actual database counts such as COUNT(*) for dataset statistics.",
                sql_section="SELECT",
            ))
            break

    critical_issues = any(issue["critical"] for issue in issues)
    status = "invalid" if critical_issues else (
        "valid" if not issues else "not_fully_verified"
    )
    coverage_issue_types = {
        "missing_required_filter",
        "contradictory_filter",
        "incorrect_aggregation",
        "incorrect_grouping",
        "incorrect_ranking",
        "missing_output",
        "excess_output",
        "unrelated_sql",
    }
    coverage_issues = [
        issue for issue in issues if issue["type"] in coverage_issue_types
    ]
    coverage_status = "invalid" if coverage_issues else "verified"
    return {
        "status": status,
        "passed": not critical_issues,
        "question_understanding_status": "verified" if understood else "not_fully_verified",
        "sql_coverage_status": coverage_status,
        "requirements": requirements,
        "issues": issues,
    }


def score_reliability(
    validation: dict[str, Any],
    semantic: dict[str, Any],
    *,
    execution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute transparent engineering scores; unmeasured dimensions stay unevaluated."""
    if validation.get("sql_present") is False:
        dimensions = {
            name: {"weight": weight, "score": None, "status": "not_evaluated"}
            for name, weight in {
                "schema_grounding": 25,
                "question_understanding": 20,
                "sql_requirement_coverage": 25,
                "result_grounding": 20,
                "unsupported_assumptions": 10,
            }.items()
        }
        return {
            "answer_accuracy_score": None,
            "hallucination_risk_score": None,
            "hallucination_risk_level": "Not evaluated",
            "critical_failure": False,
            "evaluation_stage": "awaiting_sql_generation",
            "dimensions": dimensions,
            "validation_status": "Awaiting SQL generation",
            "execution_status": "Not executed",
            "result_verification": "not_evaluated",
            "evaluated_weight": 0,
            "issues": list(semantic.get("issues", [])),
        }

    schema_valid = validation.get("schema_valid")
    syntax_valid = validation.get("syntax_valid")
    read_only = validation.get("safety") == "read_only"
    semantic_status = semantic.get("status", "not_fully_verified")
    issues = list(semantic.get("issues", []))
    understanding_status = semantic.get(
        "question_understanding_status",
        "not_fully_verified",
    )
    coverage_status = semantic.get("sql_coverage_status", "not_fully_verified")
    critical = (
        not syntax_valid
        or not read_only
        or schema_valid is False
        or semantic_status == "invalid"
        or any(issue.get("critical") for issue in issues)
    )
    requirements = semantic.get("requirements", {})

    dimensions: dict[str, dict[str, Any]] = {
        "schema_grounding": {
            "weight": 25,
            "score": 25 if schema_valid is True else (0 if schema_valid is False else None),
            "status": "verified" if schema_valid is True else (
                "invalid" if schema_valid is False else "not_evaluated"
            ),
        },
        "question_understanding": {
            "weight": 20,
            "score": 20 if understanding_status == "verified" else (
                0 if understanding_status == "invalid" else None
            ),
            "status": understanding_status,
        },
        "sql_requirement_coverage": {
            "weight": 25,
            "score": 25 if coverage_status == "verified" else (
                0 if coverage_status == "invalid" else None
            ),
            "status": coverage_status,
        },
        "result_grounding": {
            "weight": 20,
            "score": None,
            "status": "not_evaluated",
        },
        "unsupported_assumptions": {
            "weight": 10,
            "score": 0 if any(
                issue["type"] in {
                    "unsupported_diagnosis",
                    "fabricated_denominator",
                    "unsupported_relationship",
                    "unsupported_assumption",
                }
                for issue in issues
            ) else (10 if semantic_status == "valid" else None),
            "status": "detected" if any(
                issue["type"] in {
                    "unsupported_diagnosis",
                    "fabricated_denominator",
                    "unsupported_relationship",
                    "unsupported_assumption",
                }
                for issue in issues
            ) else ("verified" if semantic_status == "valid" else "not_evaluated"),
        },
    }
    if execution is not None:
        executed = execution.get("status") == "SUCCESS"
        semantic_passed = (
            semantic_status == "valid"
            and coverage_status in {"verified", "not_fully_verified", None}
            and understanding_status in {"verified", "not_fully_verified", None}
            and not critical
        )
        if not executed:
            dimensions["result_grounding"] = {
                "weight": 20,
                "score": 0,
                "status": "invalid",
            }
        elif semantic_status == "valid" and coverage_status == "verified" and understanding_status == "verified" and not critical:
            dimensions["result_grounding"] = {
                "weight": 20,
                "score": 20,
                "status": "verified",
            }
        elif semantic_status in {"invalid"} or critical:
            dimensions["result_grounding"] = {
                "weight": 20,
                "score": 0,
                "status": "invalid",
            }
        else:
            dimensions["result_grounding"] = {
                "weight": 20,
                "score": None,
                "status": "not_evaluated",
            }

    accuracy_score = sum(
        dimension["score"] or 0
        for dimension in dimensions.values()
        if dimension["score"] is not None
    )
    if critical:
        accuracy_score = 0
    if critical:
        risk_score = 100
    else:
        risk_score = 5
        risk_score += 25 if semantic_status == "not_fully_verified" else 0
        risk_score += min(70, 18 * len(issues))
        if schema_valid is False:
            risk_score = max(risk_score, 85)
        risk_score = min(risk_score, 100)

    risk_label = (
        "Very Low Risk" if risk_score <= 10 else
        "Low Risk" if risk_score <= 30 else
        "Moderate Risk" if risk_score <= 60 else
        "High Risk" if risk_score <= 80 else
        "Critical Risk"
    )
    return {
        "answer_accuracy_score": int(accuracy_score),
        "hallucination_risk_score": int(risk_score),
        "hallucination_risk_level": risk_label,
        "critical_failure": critical,
        "evaluation_stage": "results_verified" if execution is not None else "sql_validated",
        "dimensions": dimensions,
        "validation_status": "Rejected" if critical else (
            "Validated" if semantic_status == "valid" else "Not fully verified"
        ),
        "execution_status": (
            "Not executed" if execution is None else execution.get("status", "Unknown")
        ),
        "result_verification": (
            "not_evaluated" if execution is None else (
                "verified" if execution.get("status") == "SUCCESS" else "failed"
            )
        ),
        "evaluated_weight": sum(
            dimension["weight"]
            for dimension in dimensions.values()
            if dimension["score"] is not None
        ),
        "issues": issues,
    }
