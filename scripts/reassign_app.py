"""Operator tool: hand an app to another author.

    .venv/bin/python scripts/reassign_app.py <app> <author-handle>

Find handles in the audit log, e.g. `aws logs tail /aws/lambda/<function> --since 1h | grep signin`.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]

from publish_site import bucket_name, stack_output  # noqa: E402

from rodney.apps import Apps  # noqa: E402
from rodney.datastore import DynamoDataStore  # noqa: E402
from rodney.registry import DynamoRegistry  # noqa: E402
from rodney.store import S3Store  # noqa: E402


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    app, new_author = sys.argv[1:]
    if not new_author.startswith("u-"):
        raise SystemExit("Author handles look like u-3f9a1c2b7e")
    apps = Apps(S3Store(bucket_name()), DynamoRegistry(stack_output("AppsTableName")), DynamoDataStore(stack_output("DataTableName")), "")
    before = apps.get_app(app)["meta"]["author"]
    apps.reassign(app, new_author)
    print(f"{app}: {before} -> {new_author}")


if __name__ == "__main__":
    main()
