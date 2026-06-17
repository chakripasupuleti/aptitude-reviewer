from pathlib import Path

import pandas as pd


def read_input(path: str) -> pd.DataFrame:
    file_path = Path(path)
    suffix = file_path.suffix.lower()

    if not file_path.exists():
        raise FileNotFoundError(f"Input file not found: {file_path}")

    if suffix == ".csv":
        return pd.read_csv(file_path, dtype=str, keep_default_na=False).fillna("")

    if suffix in {".xlsx", ".xlsm"}:
        return pd.read_excel(file_path, dtype=str, keep_default_na=False).fillna("")

    raise ValueError("Unsupported input file type. Use .csv or .xlsx")
