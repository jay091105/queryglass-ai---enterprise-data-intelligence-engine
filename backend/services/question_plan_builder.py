import re
from backend.schemas.question_plan import QuestionPlan, RequestType, UnsupportedRequest


class QuestionPlanBuilder:
    def __init__(self, schema_ctx, available_tables):
        self.schema_ctx = schema_ctx
        self.available_tables = [str(t) for t in available_tables]
        self.schema = getattr(schema_ctx, "to_schema_text", lambda: "")()

    def parse(self, question: str) -> QuestionPlan:
        types = self._identify_request_types(question)
        tables = self._extract_table_mentions(question)
        columns = self._extract_column_mentions(question)
        unsupported = self._detect_unsupported_requests(question, tables, columns)
        is_valid = not unsupported
        return QuestionPlan(
            original_question=question,
            is_valid=is_valid,
            blocking_reason=f"Found {len(unsupported)} unsupported request(s)" if unsupported else None,
            request_types=types,
            mentioned_tables=tables,
            mentioned_columns=columns,
            unsupported_requests=unsupported,
        )

    def _identify_request_types(self, question: str) -> list[RequestType]:
        text = question.lower()
        found = []
        patterns = [
            ("aggregation", r"\b(sum|count|average|avg|mean|max|min|total)\b"),
            ("filtering", r"\b(where|only|greater|less|below|above|between|after|before)\b"),
            ("sorting", r"\b(order|sort|top|bottom|highest|lowest|recent|latest)\b"),
            ("grouping", r"\b(group|by gender|by category|by year|per)\b"),
            ("joining", r"\b(join|combine|with records from|from .* (and|join) )\b"),
        ]
        for kind, pattern in patterns:
            if re.search(pattern, text):
                found.append(RequestType(type=kind, confidence=0.9))
        return found

    def _extract_table_mentions(self, question: str) -> list[str]:
        text = question.lower()
        return [table for table in self.available_tables if re.search(r"\b" + re.escape(table.lower()) + r"\b", text)]

    def _extract_column_mentions(self, question: str) -> list[str]:
        known = {
            str(name).lower()
            for name in (getattr(self.schema_ctx, "get_column_names", lambda: [])())
        }
        text = question.lower()
        return [
            name
            for name in sorted(known)
            if re.search(r"\b" + re.escape(name) + r"\b", text)
        ]

    def _detect_unsupported_requests(self, question: str, tables: list[str], columns: list[str]) -> list[UnsupportedRequest]:
        unsupported = []
        text = question.lower()
        known_tables = {table.lower() for table in self.available_tables}
        mentioned = re.findall(
            r"\b(?:from|records?\s+from|compare\s+with|dataset\s+)([a-z][a-z0-9_]*)",
            text,
        )
        for table in mentioned:
            if table not in known_tables and table not in {"the", "a", "an", "records"}:
                unsupported.append(UnsupportedRequest(
                    type="missing_table",
                    description=f'Missing table "{table}".',
                    mentioned_object=table,
                    suggestion="Ask for data from an available table.",
                ))
        for table in re.findall(r"\b([a-z][a-z0-9_]*(?:experience|database|records|table))\b", text):
            if table not in known_tables:
                unsupported.append(UnsupportedRequest(
                    type="missing_table",
                    description=f'Missing table "{table}".',
                    mentioned_object=table,
                    suggestion="Ask for data from an available table.",
                ))
        if re.search(r"\b(join|combine|with records from)\b", text):
            unsupported.append(UnsupportedRequest(type="unsupported_join", description="Cross-table joining is not supported.", suggestion="Use the active table or ask for a single-table query."))
        known_columns = {
            str(name).lower()
            for name in (getattr(self.schema_ctx, "get_column_names", lambda: [])())
        }
        unknown = re.findall(
            r"\b(?:whose|with|where|for|by|column|field)\s+([a-z][a-z0-9_]*)",
            text,
        )
        unknown.extend(re.findall(r"\b([a-z][a-z0-9]+_[a-z0-9_]+)\b", text))
        for name in unknown:
            if name.endswith(("income", "salary", "department", "parent")) and name not in known_columns:
                unsupported.append(UnsupportedRequest(type="missing_column", description=f'Missing column "{name}".', mentioned_object=name, suggestion="Choose a column shown in the schema."))
        return unsupported
