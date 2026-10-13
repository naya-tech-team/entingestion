"""scripts/teardown.py against stub terraform and aws CLIs that share a small simulated world:
the stack's state, the live Aurora clusters and the live tables. Every call is logged."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

from scripts import teardown

STUB = r"""
import json, os, sys
world_file = os.environ["STUB_WORLD"]
world = json.load(open(world_file))
tool, args = os.path.basename(sys.argv[0]), sys.argv[1:]
world["calls"].append([tool, *args])

def done(out="", code=0, err=""):
    json.dump(world, open(world_file, "w"))
    sys.stdout.write(out); sys.stderr.write(err); sys.exit(code)

for token in list(world["fail_once"]):
    if token in " ".join([tool, *args]):
        world["fail_once"].remove(token)
        done(code=1, err=f"injected failure: {token}")

def arg(name):
    return args[args.index(name) + 1]

if tool == "terraform":
    if args[:2] == ["show", "-json"]:
        resources = world["state"]
        done(json.dumps({"values": {"root_module": {"resources": resources}}} if resources else {}))
    if args[:2] == ["state", "rm"]:
        world["state"] = [r for r in world["state"] if r["address"] != args[2]]
        done()
    if args[0] == "destroy":
        world["state"] = []
        done()
if tool == "aws":
    if args[:2] == ["rds", "describe-db-clusters"]:
        cid = arg("--db-cluster-identifier")
        if cid not in world["clusters"]:
            done(code=254, err=f"An error occurred (DBClusterNotFoundFault): {cid} not found")
        done(json.dumps(world["clusters"][cid]))
    if args[:2] == ["rds", "delete-db-instance"]:
        for members in world["clusters"].values():
            if arg("--db-instance-identifier") in members:
                members.remove(arg("--db-instance-identifier"))
        done()
    if args[:2] == ["rds", "delete-db-cluster"]:
        cid = arg("--db-cluster-identifier")
        if world["clusters"].get(cid):
            done(code=254, err="InvalidDBClusterStateFault: cluster still has instances")
        world["clusters"].pop(cid, None)
        done()
    if args[:2] == ["dynamodb", "delete-table"]:
        name = arg("--table-name")
        if name not in world["tables"]:
            done(code=254, err=f"An error occurred (ResourceNotFoundException): {name}")
        world["tables"].remove(name)
        done()
done()
"""


def res(address: str, type_: str, **values: Any) -> dict[str, Any]:
    return {"address": address, "mode": "managed", "type": type_, "values": values}


TABLE = res(
    "module.status_table.aws_dynamodb_table.this",
    "aws_dynamodb_table",
    name="dev-ingestion_status",
    deletion_protection_enabled=False,
)
CACHE = res(
    "module.cache_table.aws_dynamodb_table.this",
    "aws_dynamodb_table",
    name="dev-embedding_cache",
    deletion_protection_enabled=False,
)
BUCKET = res(
    "module.staging.aws_s3_bucket.this",
    "aws_s3_bucket",
    bucket="ingest-staging-dev",
    force_destroy=True,
)
CLUSTER = res(
    "module.db[0].aws_rds_cluster.this",
    "aws_rds_cluster",
    cluster_identifier="ingestion-dev",
    deletion_protection=False,
)
INSTANCE = res(
    "module.db[0].aws_rds_cluster_instance.this[0]",
    "aws_rds_cluster_instance",
    identifier="ingestion-dev-1",
)
SUBNETS = res("module.db[0].aws_db_subnet_group.this", "aws_db_subnet_group", name="ingestion-dev")
RDS = res(
    "module.rds[0].aws_db_instance.this",
    "aws_db_instance",
    identifier="ingestion-dev",
    deletion_protection=False,
    skip_final_snapshot=True,
)

AURORA_STACK = [TABLE, CACHE, BUCKET, CLUSTER, INSTANCE, SUBNETS]


class TeardownTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        for tool in ("terraform", "aws"):
            path = bin_dir / tool
            path.write_text(f"#!{sys.executable}\n{STUB}")
            path.chmod(0o755)
        (self.tmp / "stacks" / "ingestion").mkdir(parents=True)
        self.world_file = self.tmp / "world.json"
        env = {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "STUB_WORLD": str(self.world_file),
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def world(
        self,
        state: list[dict[str, Any]],
        clusters: dict[str, list[str]] | None = None,
        fail_once: list[str] | None = None,
    ) -> None:
        tables = [r["values"]["name"] for r in state if r["type"] == "aws_dynamodb_table"]
        world = {"state": state, "clusters": clusters or {}, "tables": tables}
        world |= {"calls": [], "fail_once": fail_once or []}
        self.world_file.write_text(json.dumps(world))

    def read(self) -> dict[str, Any]:
        return json.loads(self.world_file.read_text())

    def calls(self) -> list[str]:
        return [" ".join(c) for c in self.read()["calls"]]

    def teardown(
        self, *flags: str, env: str = "dev", answer: str | BaseException = "dev"
    ) -> tuple[int, str]:
        args = [
            "--env",
            env,
            "--stack",
            "ingestion",
            "--var-file",
            "x.tfvars",
            "--root",
            str(self.tmp),
            *flags,
        ]
        out, err = io.StringIO(), io.StringIO()
        with (
            redirect_stdout(out),
            redirect_stderr(err),
            mock.patch("builtins.input", side_effect=[answer]),
        ):
            code = teardown.main(args)
        return code, out.getvalue() + err.getvalue()

    @property
    def pending(self) -> Path:
        return self.tmp / "stacks" / "ingestion" / ".terraform" / "dev" / "teardown-pending.json"

    # -- safety ---------------------------------------------------------------------------------

    def test_refuses_prod_without_running_anything(self) -> None:
        self.world(AURORA_STACK)
        code, out = self.teardown(env="prod", answer="prod")
        self.assertEqual(code, 1)
        self.assertIn("refusing to destroy prod", out)
        self.assertEqual(self.calls(), [])

    def test_deletion_protection_blocks_before_any_change(self) -> None:
        protected = res(
            TABLE["address"], "aws_dynamodb_table", name="t", deletion_protection_enabled=True
        )
        self.world([protected, BUCKET])
        code, out = self.teardown()
        self.assertEqual(code, 1)
        self.assertIn("table t has deletion protection on", out)
        self.assertEqual(self.calls(), ["terraform show -json"])

    def test_every_kind_of_blocker_is_reported(self) -> None:
        cluster = res(CLUSTER["address"], "aws_rds_cluster", cluster_identifier="c")
        cluster["values"]["deletion_protection"] = True
        db = res(RDS["address"], "aws_db_instance", identifier="i", skip_final_snapshot=False)
        db["values"]["deletion_protection"] = True
        bucket = res(BUCKET["address"], "aws_s3_bucket", bucket="b", force_destroy=False)
        self.world([cluster, db, bucket])
        code, out = self.teardown()
        self.assertEqual(code, 1)
        for text in (
            "Aurora cluster c has deletion protection",
            "RDS instance i has deletion protection",
            "bucket b has force_destroy off",
        ):
            self.assertIn(text, out)

    def test_dry_run_lists_and_changes_nothing(self) -> None:
        self.world(AURORA_STACK, clusters={"ingestion-dev": ["ingestion-dev-1"]})
        code, out = self.teardown("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("6 resources in state", out)
        self.assertIn("Aurora cluster ingestion-dev and its instances (no final snapshot)", out)
        self.assertIn("DynamoDB table dev-embedding_cache", out)
        self.assertEqual(self.calls(), ["terraform show -json"])
        self.assertFalse(self.pending.exists())

    def test_wrong_confirmation_changes_nothing(self) -> None:
        self.world(AURORA_STACK, clusters={"ingestion-dev": ["ingestion-dev-1"]})
        code, _ = self.teardown(answer="yes")
        self.assertEqual(code, 1)
        self.assertEqual(self.calls(), ["terraform show -json"])

    def test_no_terminal_means_no_destroy(self) -> None:
        self.world(AURORA_STACK, clusters={"ingestion-dev": ["ingestion-dev-1"]})
        code, _ = self.teardown(answer=EOFError())
        self.assertEqual(code, 1)
        self.assertEqual(self.calls(), ["terraform show -json"])

    # -- order ----------------------------------------------------------------------------------

    def test_aurora_goes_first_and_tables_last(self) -> None:
        self.world(AURORA_STACK, clusters={"ingestion-dev": ["ingestion-dev-1"]})
        code, out = self.teardown()
        self.assertEqual(code, 0, out)
        mutating = [
            c for c in self.calls() if "show -json" not in c and "describe-db-clusters" not in c
        ]
        self.assertEqual(mutating, [
            "aws rds delete-db-instance --db-instance-identifier ingestion-dev-1",
            "aws rds wait db-instance-deleted --db-instance-identifier ingestion-dev-1",
            "aws rds delete-db-cluster --db-cluster-identifier ingestion-dev --skip-final-snapshot",
            "aws rds wait db-cluster-deleted --db-cluster-identifier ingestion-dev",
            "terraform state rm module.db[0].aws_rds_cluster.this",
            "terraform state rm module.status_table.aws_dynamodb_table.this",
            "terraform state rm module.cache_table.aws_dynamodb_table.this",
            "terraform destroy -input=false -auto-approve -var-file=x.tfvars",
            "aws dynamodb delete-table --table-name dev-ingestion_status --output json",
            "aws dynamodb wait table-not-exists --table-name dev-ingestion_status",
            "aws dynamodb delete-table --table-name dev-embedding_cache --output json",
            "aws dynamodb wait table-not-exists --table-name dev-embedding_cache",
        ])  # fmt: skip
        world = self.read()
        self.assertEqual((world["state"], world["clusters"], world["tables"]), ([], {}, []))
        self.assertFalse(self.pending.exists())

    def test_every_var_file_reaches_destroy_in_order(self) -> None:
        self.world([BUCKET])
        code, out = self.teardown("--var-file", "y.tfvars")
        self.assertEqual(code, 0, out)
        self.assertIn(
            "terraform destroy -input=false -auto-approve -var-file=x.tfvars -var-file=y.tfvars",
            self.calls(),
        )

    def test_final_snapshot_is_optional(self) -> None:
        self.world(AURORA_STACK, clusters={"ingestion-dev": ["ingestion-dev-1"]})
        code, _ = self.teardown("--final-snapshot")
        self.assertEqual(code, 0)
        delete = next(c for c in self.calls() if "delete-db-cluster" in c)
        self.assertIn("--final-db-snapshot-identifier ingestion-dev-teardown-", delete)
        self.assertNotIn("--skip-final-snapshot", delete)

    def test_rds_mode_needs_no_database_calls(self) -> None:
        self.world([TABLE, BUCKET, RDS])
        code, _ = self.teardown()
        self.assertEqual(code, 0)
        self.assertFalse([c for c in self.calls() if c.startswith("aws rds")])

    # -- resume ---------------------------------------------------------------------------------

    def test_a_failed_destroy_resumes_and_still_deletes_the_tables(self) -> None:
        self.world(
            AURORA_STACK,
            clusters={"ingestion-dev": ["ingestion-dev-1"]},
            fail_once=["terraform destroy"],
        )
        code, out = self.teardown()
        self.assertEqual(code, 1)
        self.assertIn("resumes where it stopped", out)
        self.assertTrue(self.pending.exists())  # the tables have left the state, but not AWS
        self.assertEqual(
            sorted(self.read()["tables"]), ["dev-embedding_cache", "dev-ingestion_status"]
        )

        code, out = self.teardown()
        self.assertEqual(code, 0, out)
        self.assertIn("Resuming an earlier teardown", out)
        self.assertIn("Aurora cluster ingestion-dev: already gone", out)
        self.assertEqual(self.read()["tables"], [])
        self.assertEqual(sum("delete-db-cluster" in c for c in self.calls()), 1)
        self.assertFalse(self.pending.exists())

    def test_tables_already_gone_are_fine(self) -> None:
        self.world([TABLE, BUCKET])
        world = self.read()
        world["tables"] = []
        self.world_file.write_text(json.dumps(world))
        code, out = self.teardown()
        self.assertEqual(code, 0, out)

    def test_an_unexpected_aws_error_stops_the_teardown(self) -> None:
        self.world(
            AURORA_STACK,
            clusters={"ingestion-dev": ["ingestion-dev-1"]},
            fail_once=["delete-db-instance"],
        )
        code, out = self.teardown()
        self.assertEqual(code, 1)
        self.assertIn("injected failure", out)
        self.assertFalse([c for c in self.calls() if "destroy" in c or "state rm" in c])


if __name__ == "__main__":
    unittest.main()
