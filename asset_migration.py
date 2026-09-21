"""Re-resolve cached category labels against normal Assets, preserving ticket history.

The legacy dataframe stores category IDs copied directly from production Jira
issue fields. Only their display labels came from the unversioned static lookup.
Customer/branch columns are reference IDs, not enriched customer records.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor

import requests

from jira_loader import CLOUD_ID, WORKSPACE_ID, fetch_asset_object


def migrate_categories(frame, *, fetch=fetch_asset_object, workers=4):
    result = frame.copy()
    ids = set()
    for column in ("main_category_id", "sub_category_id"):
        ids.update(result[column].fillna("").astype(str))
    ids.discard("")

    def resolve(oid):
        try:
            asset = fetch(CLOUD_ID, WORKSPACE_ID, oid)
            if str(asset.get("id")) != oid:
                raise ValueError("Assets returned a different object identity")
            label = asset.get("label") or asset.get("name")
            if not label:
                raise ValueError("Assets object has no label")
            return oid, label, ""
        except (requests.RequestException, PermissionError, ValueError) as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            return oid, "Unbekannt", f"HTTP {status}" if status else type(exc).__name__

    with ThreadPoolExecutor(max_workers=workers) as pool:
        resolved = list(pool.map(resolve, sorted(ids)))
    labels = {oid: label for oid, label, _ in resolved}
    errors = {oid: error for oid, _, error in resolved if error}
    for source, target in [
        ("main_category_id", "Hauptkategorie"),
        ("sub_category_id", "Unterkategorie"),
    ]:
        result[target] = (
            result[source].fillna("").astype(str).map(labels).fillna("Unbekannt")
        )
    return result, {
        "objects": len(ids),
        "failed_objects": len(errors),
        "rows": len(result),
    }


def main():
    from data_loading import load_data_snapshot, save_data_if_unchanged

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be positive")
    frame, revision = load_data_snapshot()
    if frame is None or frame.empty:
        print("No cached categories to migrate.")
        return
    # Refuse a stale overwrite if another process refreshed during the API reads.
    result, report = migrate_categories(frame, workers=args.workers)
    if report["failed_objects"]:
        raise SystemExit(
            f"Category migration: {report}. Cache unchanged: category lookups "
            "failed. Check Assets schema/object-type permissions for the account "
            "configured as JIRA_USERNAME and retry."
        )
    backup = save_data_if_unchanged(result, revision, backup_prefix="assets")
    print(f"Cache backup: {backup}")
    print(f"Category migration: {report}")


if __name__ == "__main__":
    main()
