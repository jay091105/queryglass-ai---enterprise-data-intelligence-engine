"""
file_loader.py - Multi-format tabular dataset ingestion for QueryGlass AI.
Supports CSV, XLSX, and XLS with robust format validation and metadata inspection.
"""

import pandas as pd


def load_file(uploaded_file, filename: str | None = None):
    """
    Original function signature:
    Loads an uploaded file (CSV, XLSX, or XLS) into a pandas DataFrame.
    """
    if uploaded_file is None:
        raise ValueError("No file provided.")

    file_name = (filename or getattr(uploaded_file, "name", "")).lower()

    if file_name.endswith(".csv"):
        try:
            # Preserve source tokens (notably identifiers such as 00123) until preprocessing.
            # keep_default_na=False keeps literal strings like "NA", "N/A", or "0" as valid
            # values; genuinely empty fields arrive as empty strings and are handled by the
            # missing-value rules in data_quality.py.
            df = pd.read_csv(uploaded_file, dtype=str, keep_default_na=False)
        except Exception:
            # Fallback with explicit encoding
            if hasattr(uploaded_file, "seek"):
                uploaded_file.seek(0)
            df = pd.read_csv(uploaded_file, encoding="latin1", dtype=str, keep_default_na=False)

    elif file_name.endswith((".xlsx", ".xls")):
        # Text-formatted identifiers remain text; genuine numeric cells are handled later.
        # Truly empty spreadsheet cells still parse as NaN.
        df = pd.read_excel(uploaded_file, dtype=str, keep_default_na=False)

    else:
        raise ValueError("Unsupported file format. Please upload a .csv, .xlsx, or .xls file.")

    if df.empty:
        raise ValueError("Uploaded file is empty (contains no data rows).")

    # Clean whitespace from column headers
    df.columns = [str(c).strip() for c in df.columns]

    return df


def inspect_dataset(df: pd.DataFrame) -> dict:
    """
    Extracts summary statistics for the dataset ingestion view:
    - row_count
    - col_count
    - memory_mb
    - columns_info (name, dtype, null_count, null_pct, sample_values)
    - total_nulls
    """
    if df is None or df.empty:
        return {
            "row_count": 0,
            "col_count": 0,
            "memory_mb": 0.0,
            "total_nulls": 0,
            "columns": [],
        }

    cols = []
    total_nulls = int(df.isnull().sum().sum())
    mem_mb = round(df.memory_usage(deep=True).sum() / (1024 * 1024), 2)

    for col in df.columns:
        null_count = int(df[col].isnull().sum())
        null_pct = round((null_count / len(df)) * 100, 1) if len(df) > 0 else 0
        sample_vals = df[col].dropna().head(3).tolist()
        cols.append({
            "name": col,
            "dtype": str(df[col].dtype),
            "null_count": null_count,
            "null_pct": null_pct,
            "sample_values": sample_vals,
        })

    return {
        "row_count": len(df),
        "col_count": len(df.columns),
        "memory_mb": mem_mb,
        "total_nulls": total_nulls,
        "columns": cols,
    }
