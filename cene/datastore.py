"""Where data products, load records, lineage health and reference profiles live.

LocalDataStore    - pandas DataFrames in memory (tests, scenario harness, laptop runs).
BigQueryDataStore - BigQuery tables on Google Cloud.

BigQuery layout (dataset = CENE_DATASET):
  <product>                      the data product table
  <product>__ref_<mode>          reference sample for drift/anomaly checks (mode = baseline, stress, ...)
  _cene_load_log(product STRING, load_commit_ts TIMESTAMP)       one row per committed load
  _cene_lineage_health(node STRING, health FLOAT64, updated TIMESTAMP)
"""
from __future__ import annotations

import pandas as pd


class LocalDataStore:
    def __init__(self):
        self.tables: dict[str, pd.DataFrame] = {}
        self.refs: dict[tuple[str, str], pd.DataFrame] = {}
        self.load_commits: dict[str, float] = {}
        self.lineage: dict[str, float] = {}

    # writes (used by the seeder and fault injector)
    def put_table(self, product: str, df: pd.DataFrame, load_commit_ts: float) -> None:
        self.tables[product] = df.reset_index(drop=True)
        self.load_commits[product] = load_commit_ts

    def put_reference(self, product: str, mode: str, df: pd.DataFrame) -> None:
        self.refs[(product, mode)] = df.reset_index(drop=True)

    def set_lineage_health(self, node: str, health: float) -> None:
        self.lineage[node] = float(health)

    # reads (used by the quality engine and tools)
    def sample(self, product: str, limit: int = 5000) -> pd.DataFrame:
        df = self.tables.get(product)
        if df is None:
            raise KeyError(f"no table for product '{product}'")
        return df.tail(limit).reset_index(drop=True)

    def reference(self, product: str, mode: str) -> pd.DataFrame | None:
        return self.refs.get((product, mode))

    def last_load_commit(self, product: str) -> float | None:
        return self.load_commits.get(product)

    def lineage_health(self, node: str) -> float | None:
        return self.lineage.get(node)

    def lookup(self, product: str, column: str, value) -> dict | None:
        df = self.tables.get(product)
        if df is None or column not in df.columns:
            return None
        hit = df[df[column] == value]
        return None if hit.empty else hit.iloc[-1].to_dict()


class BigQueryDataStore:
    def __init__(self, project: str, dataset: str):
        from google.cloud import bigquery  # lazy import

        self._bq = bigquery.Client(project=project)
        self._ds = f"{project}.{dataset}"

    def _q(self, sql: str, params: list | None = None) -> pd.DataFrame:
        from google.cloud import bigquery

        cfg = bigquery.QueryJobConfig(query_parameters=params or [])
        return self._bq.query(sql, job_config=cfg).to_dataframe()

    def sample(self, product: str, limit: int = 5000) -> pd.DataFrame:
        # Latest rows by ingestion order if the table has _ingested_at, otherwise an arbitrary sample.
        return self._q(f"SELECT * FROM `{self._ds}.{product}` LIMIT {int(limit)}")

    def reference(self, product: str, mode: str) -> pd.DataFrame | None:
        try:
            return self._q(f"SELECT * FROM `{self._ds}.{product}__ref_{mode}` LIMIT 5000")
        except Exception:
            return None

    def last_load_commit(self, product: str) -> float | None:
        from google.cloud import bigquery

        df = self._q(
            f"SELECT UNIX_SECONDS(MAX(load_commit_ts)) AS t FROM `{self._ds}._cene_load_log` WHERE product=@p",
            [bigquery.ScalarQueryParameter("p", "STRING", product)],
        )
        t = df["t"].iloc[0] if len(df) else None
        return None if pd.isna(t) else float(t)

    def lineage_health(self, node: str) -> float | None:
        from google.cloud import bigquery

        df = self._q(
            f"SELECT health FROM `{self._ds}._cene_lineage_health` WHERE node=@n ORDER BY updated DESC LIMIT 1",
            [bigquery.ScalarQueryParameter("n", "STRING", node)],
        )
        return None if df.empty else float(df["health"].iloc[0])

    def lookup(self, product: str, column: str, value) -> dict | None:
        from google.cloud import bigquery

        df = self._q(
            f"SELECT * FROM `{self._ds}.{product}` WHERE CAST({column} AS STRING)=@v LIMIT 1",
            [bigquery.ScalarQueryParameter("v", "STRING", str(value))],
        )
        return None if df.empty else df.iloc[0].to_dict()
