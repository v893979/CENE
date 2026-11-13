"""Load a scenario's data products into BigQuery so the deployed services can be tested on Google Cloud.

    python -m harness.bq_load uc01 --project MY_PROJECT --dataset cene

Creates the dataset and the two control tables if they do not exist yet (so this works with or
without Terraform), then creates/replaces <product>, <product>__ref_baseline and
<product>__ref_<profile> for each profile, and appends rows to _cene_load_log and
_cene_lineage_health. The person running it needs BigQuery Data Editor + Job User on the project
(or BigQuery Admin to create the dataset). Faults are then applied with the SQL
in docs/05_Test_Scenarios.md section 4, or by re-running with --fault (applies one harness fault
locally and overwrites the table).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json

import pandas as pd

from cene.datastore import LocalDataStore
from cene.util import Clock

from .datagen import generate
from .faults import apply as apply_fault, make_rng
from .runner import load_scenarios

CONTROL_TABLES = {
    "_cene_load_log": [("product", "STRING"), ("load_commit_ts", "TIMESTAMP")],
    "_cene_lineage_health": [("node", "STRING"), ("health", "FLOAT64"), ("updated", "TIMESTAMP")],
}


def ensure_dataset(bq, project: str, dataset: str, location: str) -> list[str]:
    """Create the dataset and control tables if missing. Returns what was created."""
    from google.cloud import bigquery

    created = []
    ds_ref = bigquery.Dataset(f"{project}.{dataset}")
    ds_ref.location = location
    try:
        bq.get_dataset(ds_ref)
    except Exception:  # google.api_core.exceptions.NotFound
        bq.create_dataset(ds_ref, exists_ok=True)
        created.append(f"dataset {project}.{dataset} ({location})")
    for name, cols in CONTROL_TABLES.items():
        table_id = f"{project}.{dataset}.{name}"
        try:
            bq.get_table(table_id)
        except Exception:
            schema = [bigquery.SchemaField(c, t, mode="REQUIRED") for c, t in cols]
            bq.create_table(bigquery.Table(table_id, schema=schema), exists_ok=True)
            created.append(f"table {table_id}")
    return created


def commit_timestamp(landed: dt.datetime, generated_at: float, local_commit: float) -> dt.datetime:
    """The load commit time is when the table became readable in BigQuery. A fault such as 'stale' moves
    the local commit back in time; that offset is kept relative to the landing time."""
    return landed - dt.timedelta(seconds=max(0.0, generated_at - local_commit))


def record_load(bq, ds: str, product: str, commit: dt.datetime) -> None:
    """Write the load-log row with a parameterised DML INSERT straight after the table lands. A load job for
    one row took several seconds, which on its own exceeds a 5 s freshness SLA (UC09)."""
    from google.cloud import bigquery

    bq.query(f"INSERT INTO `{ds}._cene_load_log` (product, load_commit_ts) VALUES (@product, @ts)",
             job_config=bigquery.QueryJobConfig(query_parameters=[
                 bigquery.ScalarQueryParameter("product", "STRING", product),
                 bigquery.ScalarQueryParameter("ts", "TIMESTAMP", commit)])).result()


def main(argv=None):
    from google.cloud import bigquery

    ap = argparse.ArgumentParser()
    ap.add_argument("scenario")
    ap.add_argument("--project", required=True)
    ap.add_argument("--dataset", default="cene")
    ap.add_argument("--location", default="australia-southeast2",
                    help="used only when the dataset has to be created (must match Terraform's bq_location)")
    ap.add_argument("--fault", help='JSON, e.g. \'{"type": "null_spike", "product": "lab_results", '
                                    '"column": "egfr", "fraction": 0.3}\'')
    a = ap.parse_args(argv)
    sc = load_scenarios([a.scenario])[0]
    bq = bigquery.Client(project=a.project)
    ds = f"{a.project}.{a.dataset}"
    for item in ensure_dataset(bq, a.project, a.dataset, a.location):
        print("created", item)
    replace = bigquery.LoadJobConfig(write_disposition="WRITE_TRUNCATE")
    append = bigquery.LoadJobConfig(write_disposition="WRITE_APPEND")
    now = dt.datetime.now(dt.timezone.utc)

    local, clock = LocalDataStore(), Clock()
    generated_at: dict[str, float] = {}
    for prod, spec in sc["data"].items():
        seed = int(spec.get("seed", 1))
        generated_at[prod] = clock.now()
        local.put_table(prod, generate(spec["columns"], int(spec["rows"]), seed), generated_at[prod])
        if not a.fault:
            refs = {"baseline": generate(spec["columns"], 1000, seed + 1000, start=500000)}
            for name, prof in (spec.get("profiles") or {}).items():
                refs[name] = generate(spec["columns"], 1000, seed + 2000, start=600000, overrides=prof.get("overrides"))
            for mode, df in refs.items():
                bq.load_table_from_dataframe(df, f"{ds}.{prod}__ref_{mode}", job_config=replace).result()
    if a.fault:
        f = json.loads(a.fault)
        print(apply_fault(f, local, sc, clock, make_rng()))
        if f["type"] == "lineage":
            bq.load_table_from_dataframe(pd.DataFrame([{"node": f["node"], "health": float(f["health"]),
                                                        "updated": now}]),
                                         f"{ds}._cene_lineage_health", job_config=append).result()
            return
    for prod, df in local.tables.items():
        bq.load_table_from_dataframe(df, f"{ds}.{prod}", job_config=replace).result()
        commit = commit_timestamp(dt.datetime.now(dt.timezone.utc), generated_at.get(prod, local.load_commits[prod]),
                                  local.load_commits[prod])
        if a.fault and json.loads(a.fault)["type"] == "stale" and json.loads(a.fault).get("product", prod) == prod:
            # freshness uses the latest load record, so newer records must go for the stall to show
            bq.query(f"DELETE FROM `{ds}._cene_load_log` WHERE product=@p AND load_commit_ts > @t",
                     job_config=bigquery.QueryJobConfig(query_parameters=[
                         bigquery.ScalarQueryParameter("p", "STRING", prod),
                         bigquery.ScalarQueryParameter("t", "TIMESTAMP", commit)])).result()
        record_load(bq, ds, prod, commit)
        print(f"loaded {ds}.{prod} ({len(df)} rows)")
    if not a.fault:
        rows = [{"node": n, "health": float(h), "updated": now} for n, h in (sc.get("lineage") or {}).items()]
        if rows:
            bq.load_table_from_dataframe(pd.DataFrame(rows), f"{ds}._cene_lineage_health", job_config=append).result()


if __name__ == "__main__":
    main()
