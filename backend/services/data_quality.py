"""
Data quality profiling and preprocessing for uploaded datasets.
"""

# ==============================================================================
# Source section: data_profiler.py
# ==============================================================================

import re
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


def detect_duplicates(df: pd.DataFrame, subset: List[str] = None) -> Dict[str, Any]:
    """
    Detects exact duplicate rows.
    
    Args:
        df: DataFrame to analyze
        subset: Column names to check for duplicates. If None, checks all columns.
    
    Returns:
        Dict with duplicate_count and duplicate_indices.
    """
    if df.empty:
        return {"duplicate_count": 0, "duplicate_indices": []}
    
    if subset:
        subset = [c for c in subset if c in df.columns]
    
    duplicated_mask = df.duplicated(subset=subset or None, keep=False)
    duplicate_indices = df[duplicated_mask].index.tolist()
    
    return {
        "duplicate_count": duplicated_mask.sum(),
        "duplicate_indices": duplicate_indices,
    }


def detect_missing_values(df: pd.DataFrame) -> Dict[str, Any]:
    """
    Detects missing values including NaN, None, empty strings, and whitespace-only strings.
    
    Returns:
        Dict with per-column missing value statistics and total missing count.
    """
    if df.empty:
        return {
            "total_missing": 0,
            "columns": {},
        }
    
    missing_by_col = {}
    total_missing = 0
    
    for col in df.columns:
        # Count explicit NaN/None
        explicit_nulls = df[col].isna().sum()
        
        # Count empty strings and whitespace-only strings without counting nulls twice.
        if pd.api.types.is_string_dtype(df[col]):
            string_nulls = df[col].map(
                lambda value: isinstance(value, str) and not value.strip()
            ).sum()
        else:
            string_nulls = 0
        
        col_missing = int(explicit_nulls + string_nulls)
        total_missing += col_missing
        
        missing_by_col[col] = {
            "missing_count": col_missing,
            "missing_pct": round((col_missing / len(df)) * 100, 2) if len(df) > 0 else 0,
            "missing_type": "mixed" if (explicit_nulls > 0 and string_nulls > 0) else
                             "empty_strings" if string_nulls > 0 else "explicit_null",
        }
    
    return {
        "total_missing": total_missing,
        "columns": missing_by_col,
    }


def detect_data_types(df: pd.DataFrame) -> Dict[str, Any]:
    """
    Analyzes current and inferred data types for each column.
    
    Returns:
        Dict with per-column type information.
    """
    if df.empty:
        return {"columns": {}}
    
    type_info = {}
    
    for col in df.columns:
        series = df[col]
        current_type = str(series.dtype)
        inferred_type = "unknown"
        
        # Try to infer type from non-null values
        non_null = series.dropna()
        
        if len(non_null) == 0:
            inferred_type = "empty_column"
        elif pd.api.types.is_numeric_dtype(series):
            if pd.api.types.is_integer_dtype(series):
                inferred_type = "integer"
            else:
                inferred_type = "float"
        elif pd.api.types.is_bool_dtype(series):
            inferred_type = "boolean"
        elif pd.api.types.is_datetime64_any_dtype(series):
            inferred_type = "datetime"
        else:
            # Object column; check if it looks numeric, boolean, or date
            if len(non_null) > 0:
                sample = non_null.iloc[0]
                if isinstance(sample, str):
                    # Try numeric
                    try:
                        pd.to_numeric(series, errors="coerce").dropna()
                        inferred_type = "numeric_string"
                    except:
                        # Try boolean
                        if set(non_null.str.lower().unique()).issubset({"true", "false", "yes", "no", "1", "0"}):
                            inferred_type = "boolean_string"
                        else:
                            inferred_type = "string"
                else:
                    inferred_type = "mixed_object"
            else:
                inferred_type = "string"
        
        type_info[col] = {
            "current_dtype": current_type,
            "inferred_type": inferred_type,
            "non_null_count": len(non_null),
        }
    
    return {"columns": type_info}


def detect_outliers(df: pd.DataFrame, numeric_columns: List[str] = None, method: str = "iqr") -> Dict[str, Any]:
    """
    Detects potential outliers in numeric columns using IQR or Z-score methods.
    
    Args:
        df: DataFrame to analyze
        numeric_columns: List of numeric column names. If None, auto-detects.
        method: "iqr" (default) or "zscore"
    
    Returns:
        Dict with outlier information per numeric column.
    """
    if df.empty:
        return {"columns": {}}
    
    if numeric_columns is None:
        numeric_columns = df.select_dtypes(include=[np.number]).columns.tolist()
    
    outliers_info = {}
    
    for col in numeric_columns:
        if col not in df.columns:
            continue
        
        series = df[col].dropna()
        if len(series) < 4:
            outliers_info[col] = {
                "outlier_count": 0,
                "outlier_indices": [],
                "note": "insufficient data for outlier detection",
            }
            continue
        
        if method == "iqr":
            Q1 = series.quantile(0.25)
            Q3 = series.quantile(0.75)
            IQR = Q3 - Q1
            lower_bound = Q1 - 1.5 * IQR
            upper_bound = Q3 + 1.5 * IQR
            outlier_mask = (df[col] < lower_bound) | (df[col] > upper_bound)
        else:  # zscore
            mean = series.mean()
            std = series.std()
            if std == 0:
                outlier_mask = pd.Series([False] * len(df), index=df.index)
            else:
                z_scores = np.abs((df[col] - mean) / std)
                outlier_mask = z_scores > 3
        
        outlier_indices = df[outlier_mask].index.tolist()
        
        outliers_info[col] = {
            "outlier_count": outlier_mask.sum(),
            "outlier_indices": outlier_indices,
            "method": method,
        }
    
    return {"columns": outliers_info}


def detect_constant_columns(df: pd.DataFrame) -> Dict[str, Any]:
    """
    Detects columns with only constant values (same value in all rows).
    
    Returns:
        List of constant column names and their values.
    """
    if df.empty:
        return {"constant_columns": []}
    
    constant_cols = []
    
    for col in df.columns:
        series = df[col].dropna()
        if len(series) > 0 and series.nunique() == 1:
            constant_cols.append({
                "column_name": col,
                "constant_value": series.iloc[0],
            })
    
    return {"constant_columns": constant_cols}


def detect_categorical_inconsistencies(df: pd.DataFrame, string_columns: List[str] = None) -> Dict[str, Any]:
    """
    Detects categorical inconsistencies like leading/trailing spaces, case variations, typos.
    
    Args:
        df: DataFrame to analyze
        string_columns: List of string column names. If None, auto-detects object columns.
    
    Returns:
        Dict with potential inconsistencies per column.
    """
    if df.empty:
        return {"columns": {}}
    
    if string_columns is None:
        string_columns = df.select_dtypes(include=["object", "string"]).columns.tolist()
    
    inconsistencies = {}
    
    for col in string_columns:
        if col not in df.columns:
            continue
        
        series = df[col].dropna()
        if len(series) == 0:
            continue
        
        # Check for whitespace issues
        with_leading = series.str.startswith(" ").sum() if series.dtype == "object" else 0
        with_trailing = series.str.endswith(" ").sum() if series.dtype == "object" else 0
        
        # Check case variations
        unique_vals = series.dropna().unique()
        case_insensitive_unique = series.str.lower().dropna().unique() if series.dtype == "object" else unique_vals
        case_variations = len(unique_vals) - len(case_insensitive_unique)
        
        inconsistencies[col] = {
            "unique_count": len(unique_vals),
            "leading_spaces": int(with_leading),
            "trailing_spaces": int(with_trailing),
            "case_variations": max(0, case_variations),
            "top_values": series.value_counts().head(5).to_dict(),
        }
    
    return {"columns": inconsistencies}


def profile_dataframe(df: pd.DataFrame, numeric_cols: List[str] = None) -> Dict[str, Any]:
    """
    Comprehensive data quality profile.
    
    Returns:
        Structured profile with all detected issues and patterns.
    """
    if df is None:
        return {
            "status": "error",
            "message": "DataFrame is None",
            "profile": {}
        }
    
    if df.empty:
        return {
            "status": "empty",
            "message": "DataFrame is empty",
            "rows": 0,
            "columns": 0,
            "profile": {}
        }
    
    profile = {
        "rows": len(df),
        "columns": len(df.columns),
        "memory_mb": round(df.memory_usage(deep=True).sum() / (1024 * 1024), 2),
        "duplicates": detect_duplicates(df),
        "missing_values": detect_missing_values(df),
        "data_types": detect_data_types(df),
        "constant_columns": detect_constant_columns(df),
        "categorical_inconsistencies": detect_categorical_inconsistencies(df),
        "outliers": detect_outliers(df, numeric_columns=numeric_cols),
    }

    return {
        "status": "ok",
        "profile": profile,
    }


# ==============================================================================
# Workflow quality analysis
# ==============================================================================

BOOLEAN_TOKENS = {"true", "false", "yes", "no", "t", "f", "y", "n", "1", "0"}
# A column is considered to have a dominant type when at least this share of
# its non-missing values coerces to the type; the remainder are mismatches.
DOMINANT_TYPE_RATIO = 0.6


def _missing_mask(series: pd.Series) -> pd.Series:
    """Mask of genuinely missing values: NaN/None plus empty/whitespace-only strings.

    Valid values such as 0, False, or the literal string "NA" are NOT missing.
    """
    mask = series.isna()
    if series.dtype == object or pd.api.types.is_string_dtype(series):
        if series.dtype == object:
            # .str yields NaN for non-string entries; they are never blank.
            stripped = series.str.strip()
        else:
            stripped = series.astype("string").str.strip()
        mask = mask | stripped.eq("").fillna(False)
    return mask


def _infer_column_type(series: pd.Series) -> Dict[str, Any]:
    """Infer the dominant type of a column and count values violating it.

    Precedence: numeric > datetime > boolean > text. A type is dominant when at
    least DOMINANT_TYPE_RATIO of non-missing values coerce to it; the remaining
    values are counted once as type mismatches. The returned mismatch mask is
    indexed like the non-missing slice of the input series.
    """
    non_missing = series[~_missing_mask(series)]
    total = len(non_missing)
    if total == 0:
        return {"type": "empty", "non_missing": 0, "mismatch_count": 0, "mismatch_mask": None}

    if pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_bool_dtype(series):
        return {
            "type": "numeric",
            "non_missing": total,
            "mismatch_count": 0,
            "mismatch_mask": None,
            "numeric_values": series.dropna(),
        }
    if pd.api.types.is_datetime64_any_dtype(series):
        return {"type": "datetime", "non_missing": total, "mismatch_count": 0, "mismatch_mask": None}
    if pd.api.types.is_bool_dtype(series):
        return {"type": "boolean", "non_missing": total, "mismatch_count": 0, "mismatch_mask": None}

    def _dominant(bad: pd.Series, type_name: str) -> Optional[Dict[str, Any]]:
        good_ratio = float((~bad).mean())
        if good_ratio >= DOMINANT_TYPE_RATIO:
            return {"type": type_name, "non_missing": total, "mismatch_count": int(bad.sum()), "mismatch_mask": bad}
        return None

    try:
        numeric_attempt = pd.to_numeric(non_missing, errors="coerce")
        result = _dominant(numeric_attempt.isna(), "numeric")
        if result:
            result["numeric_values"] = numeric_attempt
            return result
    except (TypeError, ValueError):
        pass

    date_sample = non_missing.head(32).astype(str).str.strip()
    date_candidate = date_sample.str.contains(
        r"(?:\b\d{4}[-/]\d{1,2}\b|\b\d{1,2}[-/]\d{1,2}[-/]\d{2,4}\b|"
        r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}\b)",
        case=False,
        regex=True,
    ).any()
    if date_candidate:
        try:
            datetime_attempt = pd.to_datetime(non_missing, errors="coerce", format="mixed")
            bad = pd.Series(np.asarray(datetime_attempt.isna()), index=non_missing.index)
            result = _dominant(bad, "datetime")
            if result:
                return result
        except (TypeError, ValueError):
            pass

    tokens = non_missing.astype(str).str.strip().str.lower()
    result = _dominant(~tokens.isin(BOOLEAN_TOKENS), "boolean")
    if result:
        return result

    return {"type": "text", "non_missing": total, "mismatch_count": 0, "mismatch_mask": None}


def _column_type_profiles(df: pd.DataFrame) -> Dict[str, Dict[str, Any]]:
    return {col: _infer_column_type(df[col]) for col in df.columns}


def _detect_type_mismatches(
    df: pd.DataFrame,
    profiles: Dict[str, Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    columns = {}
    total = 0
    profiles = profiles or _column_type_profiles(df)
    for col in df.columns:
        info = profiles[col]
        if info["mismatch_count"] > 0:
            columns[col] = {
                "dominant_type": info["type"],
                "mismatch_count": info["mismatch_count"],
            }
            total += info["mismatch_count"]
    return {"total": total, "by_column": columns}


def _detect_outliers_consistent(
    df: pd.DataFrame,
    profiles: Dict[str, Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    """IQR outliers on numeric-dominant columns, using coerced values.

    The same detection settings are used for raw and processed reports so the
    before/after comparison is consistent.
    """
    columns = {}
    total = 0
    profiles = profiles or _column_type_profiles(df)
    for col in df.columns:
        info = profiles[col]
        if info["type"] != "numeric":
            continue
        numeric = info.get("numeric_values")
        if numeric is None:
            numeric = pd.to_numeric(df[col], errors="coerce").dropna()
        else:
            numeric = numeric.dropna()
        if len(numeric) < 4:
            continue
        q1 = numeric.quantile(0.25)
        q3 = numeric.quantile(0.75)
        iqr = q3 - q1
        lower, upper = q1 - 1.5 * iqr, q3 + 1.5 * iqr
        count = int(((numeric < lower) | (numeric > upper)).sum())
        columns[col] = {"outlier_count": count, "lower_bound": float(lower), "upper_bound": float(upper)}
        total += count
    return {"total": total, "method": "iqr_1_5", "by_column": columns}


def analyze_quality(df: pd.DataFrame, key_columns: Optional[List[str]] = None) -> Dict[str, Any]:
    """Comprehensive, JSON-safe quality metrics used by the ingest workflow.

    Counting rules (each problem instance counted exactly once):
    - missing cells: NaN/None plus empty or whitespace-only strings.
    - invalid / type-mismatched values: values failing their column's dominant
      type check (numeric/datetime/boolean). This is a single check reported
      under both names; it is added to total_issues once.
    - duplicate rows: extra occurrences of an exact full-row duplicate.
    - empty rows and empty columns are shown separately but their cells are
      already counted inside missing cells, so they are not added again.
    - outliers and constant columns are advisory only.
    """
    rows, cols = len(df), len(df.columns)
    total_cells = rows * cols

    missing_by_col: Dict[str, Dict[str, Any]] = {}
    missing_cells = 0
    empty_columns = []
    any_missing = pd.Series(False, index=df.index)
    all_missing = pd.Series(True, index=df.index)
    for col in df.columns:
        mask = _missing_mask(df[col])
        count = int(mask.sum())
        missing_by_col[col] = {
            "missing_count": count,
            "missing_pct": round(count / rows * 100, 2) if rows else 0.0,
        }
        missing_cells += count
        if rows and count == rows:
            empty_columns.append(col)
        any_missing |= mask
        all_missing &= mask
    rows_with_missing = int(any_missing.sum()) if rows else 0
    empty_rows = int(all_missing.sum())

    duplicate_mask = df.duplicated(keep=False) if rows else pd.Series(False, index=df.index)
    duplicate_rows_involved = int(duplicate_mask.sum())
    duplicate_groups = (
        int(df.loc[duplicate_mask].drop_duplicates().shape[0])
        if duplicate_rows_involved else 0
    )
    duplicated_extra = duplicate_rows_involved - duplicate_groups

    key_duplicates = None
    if key_columns:
        subset = [c for c in key_columns if c in df.columns]
        if subset and rows:
            key_duplicates = {
                "columns": subset,
                "extra_rows": int(df.duplicated(subset=subset, keep="first").sum()),
                "criteria": f"Duplicate business keys on: {', '.join(subset)}",
            }

    type_profiles = _column_type_profiles(df)
    mismatches = _detect_type_mismatches(df, type_profiles)
    outliers = _detect_outliers_consistent(df, type_profiles)
    constants = [
        str(column)
        for column, count in df.nunique(dropna=True).items()
        if count == 1
    ]

    invalid_total = mismatches["total"]
    total_issues = missing_cells + invalid_total + duplicated_extra

    return {
        "rows": rows,
        "columns": cols,
        "total_cells": total_cells,
        "missing_cells": missing_cells,
        "missing_pct": round(missing_cells / total_cells * 100, 2) if total_cells else 0.0,
        "rows_with_missing": rows_with_missing,
        "missing_by_column": missing_by_col,
        "duplicate_rows": duplicated_extra,
        "duplicate_pct": round(duplicated_extra / rows * 100, 2) if rows else 0.0,
        "duplicate_groups": duplicate_groups,
        "duplicate_rows_involved": duplicate_rows_involved,
        "duplicate_criteria": "Exact duplicate rows across all columns (first occurrence kept)",
        "key_duplicates": key_duplicates,
        "invalid_values": invalid_total,
        "type_mismatches": {"total": invalid_total, "by_column": mismatches["by_column"]},
        "invalid_definition": "Values that fail their column's dominant type check (numeric, datetime, or boolean). Invalid values and type mismatches are the same check and are counted once.",
        "outliers": outliers,
        "constant_columns": constants,
        "empty_rows": empty_rows,
        "empty_columns": empty_columns,
        "parsing_errors": 0,
        "parsing_errors_note": "Upload parsing is all-or-nothing: malformed files are rejected with an error instead of partially imported.",
        "total_issues": total_issues,
        "issue_counting_rules": "total_issues = missing cells + invalid/type-mismatched values + exact duplicate rows. Empty rows/columns are included in missing cells and not double counted; outliers and constant columns are advisory.",
    }

class PreprocessingReport:
    """Structured report of all transformations applied."""
    
    def __init__(self):
        self.original_rows = 0
        self.original_cols = 0
        self.final_rows = 0
        self.final_cols = 0
        self.transformations = []
        self.column_name_mapping = {}
        self.issues_detected = []
        self.issues_resolved = []
        self.issues_unresolved = []
    
    def add_transformation(self, category: str, description: str, count: int = None):
        """Log a transformation."""
        entry = {
            "category": category,
            "description": description,
        }
        if count is not None:
            entry["count"] = count
        self.transformations.append(entry)
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize report to dictionary."""
        return {
            "original_rows": self.original_rows,
            "original_cols": self.original_cols,
            "final_rows": self.final_rows,
            "final_cols": self.final_cols,
            "rows_removed": self.original_rows - self.final_rows,
            "column_name_mapping": self.column_name_mapping,
            "transformations": self.transformations,
            "issues_detected": self.issues_detected,
            "issues_resolved": self.issues_resolved,
            "issues_unresolved": self.issues_unresolved,
        }


def _standardize_column_names(df: pd.DataFrame, report: PreprocessingReport) -> Tuple[pd.DataFrame, Dict[str, str]]:
    """
    Standardizes column names to SQL-friendly identifiers.
    Returns cleaned DataFrame and mapping of original -> cleaned names.
    """
    mapping = {}
    new_columns = []
    seen_names = set()
    
    for i, col in enumerate(df.columns):
        # Convert to string and strip
        col_str = str(col).strip()
        
        if not col_str or col_str.isspace():
            col_str = f"column_{i}"
        
        # Replace spaces and special chars with underscores
        cleaned = re.sub(r"[^\w]", "_", col_str)
        cleaned = re.sub(r"_+", "_", cleaned)
        cleaned = cleaned.strip("_").lower()
        
        # Handle leading digits
        if cleaned and cleaned[0].isdigit():
            cleaned = f"col_{cleaned}"
        
        # Handle duplicates
        if cleaned in seen_names:
            counter = 1
            base = cleaned
            while f"{base}_{counter}" in seen_names:
                counter += 1
            cleaned = f"{base}_{counter}"
        
        seen_names.add(cleaned)
        new_columns.append(cleaned)
        
        if col_str != cleaned:
            mapping[col_str] = cleaned
    
    df_renamed = df.copy()
    df_renamed.columns = new_columns
    
    if mapping:
        report.add_transformation(
            "column_names",
            f"Standardized {len(mapping)} column names to SQL-friendly identifiers",
            len(mapping)
        )
        report.column_name_mapping = mapping
    
    return df_renamed, mapping


def _handle_duplicates(df: pd.DataFrame, report: PreprocessingReport, subset: List[str] = None) -> pd.DataFrame:
    """
    Detects and removes exact duplicate rows.
    """
    if df.empty:
        return df
    
    before_count = len(df)
    
    # If subset columns specified, validate they exist
    if subset:
        subset = [c for c in subset if c in df.columns]
    
    df_no_dupes = df.drop_duplicates(subset=subset or None, keep="first")
    rows_removed = before_count - len(df_no_dupes)
    
    if rows_removed > 0:
        report.add_transformation(
            "duplicates",
            f"Removed exact duplicate rows",
            rows_removed
        )
        report.issues_resolved.append({
            "issue": "duplicate_rows",
            "count": rows_removed,
            "action": "removed",
        })
    
    return df_no_dupes


def _normalize_missing_values(df: pd.DataFrame, report: PreprocessingReport) -> pd.DataFrame:
    """
    Converts empty strings and whitespace-only cells to NaN.
    """
    df_normalized = df.copy()
    missing_converted = 0
    
    for col in df_normalized.columns:
        if df_normalized[col].dtype == "object":
            # Find whitespace-only or empty strings
            mask = df_normalized[col].fillna("").str.strip() == ""
            if mask.any():
                missing_converted += mask.sum()
                df_normalized.loc[mask, col] = np.nan
    
    if missing_converted > 0:
        report.add_transformation(
            "missing_values",
            "Converted empty strings and whitespace to NaN",
            missing_converted
        )
        report.issues_detected.append({
            "issue": "empty_strings",
            "count": missing_converted,
        })
    
    return df_normalized


def _fill_missing_values(
    df: pd.DataFrame,
    report: PreprocessingReport,
    strategy: str = "leave",
    numeric_fill_value: Optional[str] = None,
    categorical_fill_value: Optional[Any] = None,
    column_strategies: Optional[Dict[str, str]] = None,
) -> pd.DataFrame:
    """
    Fills missing values based on strategy.
    
    Args:
        strategy: "leave", "median", "mean", "mode", or "fill_with_value"
        numeric_fill_value: Value to use if strategy is "fill_with_value"
        categorical_fill_value: Value to use for categorical columns if strategy is "fill_with_value"
        column_strategies: Dict mapping column names to specific strategies
    """
    df_filled = df.copy()
    
    for col in df_filled.columns:
        # Check column-specific strategy
        col_strategy = strategy
        if column_strategies and col in column_strategies:
            col_strategy = column_strategies[col]
        
        missing_count = df_filled[col].isna().sum()
        if missing_count == 0:
            continue
        
        # Skip if entire column is missing
        if missing_count == len(df_filled):
            report.issues_unresolved.append({
                "issue": "column_entirely_null",
                "column": col,
                "rows": missing_count,
            })
            continue
        
        # Skip critical business fields without explicit approval
        critical_fields = ["id", "transaction_id", "product_id", "sales_amount", "amount", "date"]
        if any(field in col.lower() for field in critical_fields):
            if col_strategy in ["median", "mean", "mode"]:
                report.issues_unresolved.append({
                    "issue": "critical_field_not_filled",
                    "column": col,
                    "missing_count": missing_count,
                    "reason": "critical business field; user approval required",
                })
                continue
        
        # Apply strategy
        if col_strategy == "leave":
            pass
        elif col_strategy == "median":
            if pd.api.types.is_numeric_dtype(df_filled[col]):
                fill_val = df_filled[col].median()
                df_filled[col] = df_filled[col].fillna(fill_val)
                report.add_transformation(
                    "missing_values",
                    f"Column '{col}': filled {missing_count} missing values with median",
                    missing_count
                )
                report.issues_resolved.append({
                    "issue": "missing_values",
                    "column": col,
                    "count": missing_count,
                    "method": "median",
                })
        elif col_strategy == "mean":
            if pd.api.types.is_numeric_dtype(df_filled[col]):
                fill_val = df_filled[col].mean()
                df_filled[col] = df_filled[col].fillna(fill_val)
                report.add_transformation(
                    "missing_values",
                    f"Column '{col}': filled {missing_count} missing values with mean",
                    missing_count
                )
                report.issues_resolved.append({
                    "issue": "missing_values",
                    "column": col,
                    "count": missing_count,
                    "method": "mean",
                })
        elif col_strategy == "mode":
            if missing_count < len(df_filled):
                mode_vals = df_filled[col].mode()
                if len(mode_vals) > 0:
                    fill_val = mode_vals.iloc[0]
                    df_filled[col] = df_filled[col].fillna(fill_val)
                    report.add_transformation(
                        "missing_values",
                        f"Column '{col}': filled {missing_count} missing values with mode",
                        missing_count
                    )
                    report.issues_resolved.append({
                        "issue": "missing_values",
                        "column": col,
                        "count": missing_count,
                        "method": "mode",
                    })
        elif col_strategy == "fill_with_value":
            if pd.api.types.is_numeric_dtype(df_filled[col]):
                fill_val = numeric_fill_value
            else:
                fill_val = categorical_fill_value

            if fill_val is not None:
                df_filled[col] = df_filled[col].fillna(fill_val)
                report.add_transformation(
                    "missing_values",
                    f"Column '{col}': filled {missing_count} missing values with configured value",
                    missing_count,
                )
                report.issues_resolved.append({
                    "issue": "missing_values",
                    "column": col,
                    "count": missing_count,
                    "method": "fill_with_value",
                })
    
    return df_filled


def _clean_whitespace(df: pd.DataFrame, report: PreprocessingReport) -> pd.DataFrame:
    """
    Trims leading/trailing whitespace from string columns.
    """
    df_cleaned = df.copy()
    changes = 0

    for col in df_cleaned.columns:
        before = df_cleaned[col].copy()
        df_cleaned[col] = df_cleaned[col].map(
            lambda value: value.strip() if isinstance(value, str) else value
        )
        # NaN != NaN compares True in pandas; only count real value changes.
        changes += int(((before != df_cleaned[col]) & before.notna()).sum())

    if changes > 0:
        report.add_transformation(
            "text_cleaning",
            "Trimmed leading/trailing whitespace from string columns",
            changes
        )

    return df_cleaned


def _convert_data_types(df: pd.DataFrame, report: PreprocessingReport) -> pd.DataFrame:
    """
    Safely converts numeric strings and dates where unambiguous.
    """
    df_converted = df.copy()
    conversions = []
    
    for col in df_converted.columns:
        name = re.sub(r"[^a-z0-9]+", "_", str(col).lower()).strip("_")
        tokens = set(name.split("_"))
        identifier_tokens = {"id", "identifier", "code", "zip", "postal", "postcode", "phone", "account", "sku", "isbn", "ssn"}
        if tokens.intersection(identifier_tokens) or name.endswith(("id", "code")):
            # Preserve identifiers as text: numeric coercion would erase leading zeros.
            continue
        if pd.api.types.is_object_dtype(df_converted[col]) or pd.api.types.is_string_dtype(df_converted[col]):
            # Try numeric conversion
            non_null = df_converted[col].dropna()
            if len(non_null) > 0:
                # Check if it looks numeric
                try:
                    numeric_attempt = pd.to_numeric(df_converted[col], errors="coerce")
                    null_before = df_converted[col].isna().sum()
                    null_after = numeric_attempt.isna().sum()
                    
                    # Only convert if we're not creating new nulls (except for empty strings already NA)
                    if (null_after - null_before) < 0.1 * len(df_converted):
                        # Check if all non-null values converted successfully
                        if numeric_attempt[~df_converted[col].isna()].notna().all():
                            df_converted[col] = numeric_attempt
                            conversions.append({
                                "column": col,
                                "from_type": "object",
                                "to_type": "numeric",
                            })
                except (TypeError, ValueError):
                    pass
    
    if conversions:
        report.add_transformation(
            "type_conversion",
            f"Converted {len(conversions)} columns to numeric types",
            len(conversions)
        )
        report.issues_resolved.append({
            "issue": "numeric_string_conversion",
            "count": len(conversions),
            "conversions": conversions,
        })
    
    return df_converted


def _remove_empty_rows(df: pd.DataFrame, report: PreprocessingReport) -> pd.DataFrame:
    """Drops rows in which every value is missing."""
    if df.empty:
        return df
    all_missing = pd.Series(True, index=df.index)
    for col in df.columns:
        all_missing &= _missing_mask(df[col])
    removed = int(all_missing.sum())
    if removed:
        df = df[~all_missing].reset_index(drop=True)
        report.add_transformation("empty_rows", "Removed completely empty rows", removed)
        report.issues_resolved.append({"issue": "empty_rows", "count": removed, "action": "removed"})
    return df


def _remove_empty_columns(df: pd.DataFrame, report: PreprocessingReport) -> pd.DataFrame:
    """Drops columns in which every value is missing."""
    empty = [col for col in df.columns if int(_missing_mask(df[col]).sum()) == len(df)] if len(df) else []
    if empty:
        df = df.drop(columns=empty)
        report.add_transformation("empty_columns", f"Removed completely empty columns: {', '.join(map(str, empty))}", len(empty))
        report.issues_resolved.append({"issue": "empty_columns", "columns": empty, "action": "removed"})
    return df


def _standardize_categorical(df: pd.DataFrame, report: PreprocessingReport, max_distinct: int = 50) -> pd.DataFrame:
    """Normalizes whitespace and merges case-only variants in low-cardinality text columns."""
    df_std = df.copy()
    changed_total = 0
    for col in df_std.columns:
        if not (df_std[col].dtype == object or pd.api.types.is_string_dtype(df_std[col])):
            continue
        non_missing = df_std[col][~_missing_mask(df_std[col])]
        if len(non_missing) == 0 or non_missing.nunique() > max_distinct:
            continue
        normalized = df_std[col].map(
            lambda value: re.sub(r"\s+", " ", value.strip()) if isinstance(value, str) else value
        )
        changed = int(((normalized != df_std[col]) & df_std[col].notna()).sum())
        lowered = normalized.map(lambda value: value.lower() if isinstance(value, str) else value)
        valid = normalized.dropna()
        if len(valid) and valid.nunique() > lowered[valid.index].nunique():
            changed += int(((lowered != normalized) & normalized.notna()).sum())
            normalized = lowered
        if changed:
            df_std[col] = normalized
            changed_total += changed
            report.add_transformation("categorical", f"Standardized categorical values in '{col}'", changed)
    if changed_total:
        report.issues_resolved.append({"issue": "categorical_inconsistency", "count": changed_total, "action": "standardized"})
    return df_std


def _handle_invalid_values(df: pd.DataFrame, report: PreprocessingReport, strategy: str = "leave") -> pd.DataFrame:
    """Treats values that violate their column's dominant type.

    convert_to_null replaces them with NaN; drop_rows removes the rows; leave only reports.
    """
    if df.empty or strategy == "leave":
        return df
    df_fixed = df.copy()
    drop_mask = pd.Series(False, index=df.index)
    handled = 0
    for col in df_fixed.columns:
        info = _infer_column_type(df_fixed[col])
        mask = info.get("mismatch_mask")
        if mask is None or not mask.any():
            continue
        count = int(mask.sum())
        handled += count
        if strategy == "convert_to_null":
            df_fixed.loc[mask[mask].index, col] = np.nan
        elif strategy == "drop_rows":
            drop_mask |= mask
    if strategy == "drop_rows" and drop_mask.any():
        removed = int(drop_mask.sum())
        df_fixed = df_fixed[~drop_mask].reset_index(drop=True)
        report.add_transformation("invalid_values", f"Dropped {removed} rows containing invalid values", removed)
    elif handled:
        report.add_transformation("invalid_values", f"Converted {handled} invalid values to null", handled)
    if handled:
        report.issues_resolved.append({"issue": "invalid_values", "count": handled, "action": strategy})
    return df_fixed


def _treat_outliers(df: pd.DataFrame, report: PreprocessingReport, strategy: str = "leave") -> pd.DataFrame:
    """Caps (winsorizes) or removes IQR outliers in numeric-dominant columns."""
    if df.empty or strategy == "leave":
        return df
    df_treated = df.copy()
    drop_mask = pd.Series(False, index=df.index)
    treated = 0
    for col in df_treated.columns:
        info = _infer_column_type(df_treated[col])
        if info["type"] != "numeric":
            continue
        numeric = pd.to_numeric(df_treated[col], errors="coerce")
        valid = numeric.dropna()
        if len(valid) < 4:
            continue
        q1 = valid.quantile(0.25)
        q3 = valid.quantile(0.75)
        iqr = q3 - q1
        lower, upper = q1 - 1.5 * iqr, q3 + 1.5 * iqr
        outside = (numeric < lower) | (numeric > upper)
        if not bool(outside.any()):
            continue
        count = int(outside.sum())
        treated += count
        if strategy == "cap":
            numeric = numeric.clip(lower=lower, upper=upper)
            df_treated[col] = numeric
        elif strategy == "remove_rows":
            drop_mask |= outside.fillna(False)
    if strategy == "remove_rows" and drop_mask.any():
        removed = int(drop_mask.sum())
        df_treated = df_treated[~drop_mask].reset_index(drop=True)
        report.add_transformation("outliers", f"Removed {removed} rows containing outliers", removed)
    elif treated:
        report.add_transformation("outliers", f"Capped {treated} outliers to IQR bounds", treated)
    if treated:
        report.issues_resolved.append({"issue": "outliers", "count": treated, "action": strategy})
    return df_treated


def _drop_selected_columns(df: pd.DataFrame, report: PreprocessingReport, columns: List[str]) -> pd.DataFrame:
    """Drops user-selected columns; unknown names are reported, not fatal."""
    existing = [c for c in columns if c in df.columns]
    unknown = [c for c in columns if c not in df.columns]
    if unknown:
        report.issues_unresolved.append({
            "issue": "unknown_column_requested",
            "columns": unknown,
            "reason": "selected for removal but not present in the dataset",
        })
    if existing:
        df = df.drop(columns=existing)
        report.add_transformation("drop_columns", f"Dropped selected columns: {', '.join(existing)}", len(existing))
    return df


def preprocess_dataframe(
    df: pd.DataFrame,
    options: Optional[Dict[str, Any]] = None,
) -> Tuple[pd.DataFrame, PreprocessingReport]:
    """
    Main preprocessing pipeline.

    Args:
        df: Input DataFrame
        options: Dict with keys:
            - remove_duplicates: bool (default True)
            - duplicate_subset: List[str] (default None)
            - missing_value_strategy: "leave", "drop_rows", "median", "mean",
              "mode", "fill_with_value" (default "leave")
            - numeric_fill_value / categorical_fill_value: values for fill_with_value
            - column_strategies: Dict[col_name, strategy]
            - drop_columns: List[str] of user-selected columns to drop
            - clean_whitespace: bool (default True)
            - standardize_column_names: bool (default True)
            - convert_data_types: bool (default True)
            - standardize_categorical: bool (default False)
            - invalid_value_strategy: "leave", "convert_to_null", "drop_rows" (default "leave")
            - outlier_strategy: "leave", "cap", "remove_rows" (default "leave")
            - remove_empty_rows: bool (default False)
            - remove_empty_columns: bool (default False)

    Returns:
        Tuple of (cleaned_df, preprocessing_report). The input frame is never modified.
    """
    if df is None or df.empty:
        report = PreprocessingReport()
        report.original_rows = 0 if df is None else len(df)
        report.original_cols = 0 if df is None else len(df.columns)
        report.final_rows = report.original_rows
        report.final_cols = report.original_cols
        return df.copy() if df is not None else pd.DataFrame(), report

    # Initialize report
    report = PreprocessingReport()
    report.original_rows = len(df)
    report.original_cols = len(df.columns)

    # Parse options with defaults
    opts = options or {}
    remove_duplicates = opts.get("remove_duplicates", True)
    duplicate_subset = opts.get("duplicate_subset", None)
    missing_strategy = opts.get("missing_value_strategy", "leave")
    numeric_fill = opts.get("numeric_fill_value", None)
    categorical_fill = opts.get("categorical_fill_value", None)
    col_strategies = opts.get("column_strategies", None)
    drop_columns = [str(c) for c in opts.get("drop_columns", []) or []]
    clean_ws = opts.get("clean_whitespace", True)
    std_cols = opts.get("standardize_column_names", True)
    convert_types = opts.get("convert_data_types", True)
    std_categorical = opts.get("standardize_categorical", False)
    invalid_strategy = opts.get("invalid_value_strategy", "leave")
    outlier_strategy = opts.get("outlier_strategy", "leave")
    remove_empty_rows_opt = opts.get("remove_empty_rows", False)
    remove_empty_columns_opt = opts.get("remove_empty_columns", False)

    allowed_missing = {"leave", "drop_rows", "median", "mean", "mode", "fill_with_value"}
    allowed_invalid = {"leave", "convert_to_null", "drop_rows"}
    allowed_outlier = {"leave", "cap", "remove_rows"}
    if missing_strategy not in allowed_missing:
        raise ValueError(f"missing_value_strategy must be one of {sorted(allowed_missing)}")
    if invalid_strategy not in allowed_invalid:
        raise ValueError(f"invalid_value_strategy must be one of {sorted(allowed_invalid)}")
    if outlier_strategy not in allowed_outlier:
        raise ValueError(f"outlier_strategy must be one of {sorted(allowed_outlier)}")

    # Work with a copy to preserve original
    df_result = df.copy()

    # Pipeline (order matters: text normalization and invalid handling run before
    # type conversion so that fills and duplicate detection see canonical values).
    if std_cols:
        df_result, _ = _standardize_column_names(df_result, report)

    if clean_ws:
        df_result = _clean_whitespace(df_result, report)

    # Normalize empty strings to NaN
    df_result = _normalize_missing_values(df_result, report)

    if remove_empty_rows_opt:
        df_result = _remove_empty_rows(df_result, report)

    if remove_empty_columns_opt:
        df_result = _remove_empty_columns(df_result, report)

    if drop_columns:
        df_result = _drop_selected_columns(df_result, report, drop_columns)

    if std_categorical:
        df_result = _standardize_categorical(df_result, report)

    if invalid_strategy != "leave":
        df_result = _handle_invalid_values(df_result, report, strategy=invalid_strategy)

    if convert_types:
        df_result = _convert_data_types(df_result, report)

    if missing_strategy == "drop_rows":
        row_mask = pd.Series(False, index=df_result.index)
        for col in df_result.columns:
            row_mask |= _missing_mask(df_result[col])
        removed = int(row_mask.sum())
        if removed:
            df_result = df_result[~row_mask].reset_index(drop=True)
            report.add_transformation("missing_values", f"Dropped {removed} rows containing missing values", removed)
            report.issues_resolved.append({"issue": "missing_values", "count": removed, "method": "drop_rows"})
    elif missing_strategy != "leave":
        df_result = _fill_missing_values(
            df_result,
            report,
            strategy=missing_strategy,
            numeric_fill_value=numeric_fill,
            categorical_fill_value=categorical_fill,
            column_strategies=col_strategies,
        )

    # Remove duplicates
    if remove_duplicates:
        df_result = _handle_duplicates(df_result, report, subset=duplicate_subset)

    if outlier_strategy != "leave":
        df_result = _treat_outliers(df_result, report, strategy=outlier_strategy)

    # Finalize report
    report.final_rows = len(df_result)
    report.final_cols = len(df_result.columns)

    return df_result, report
