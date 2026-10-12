"""Tear down one stack in a disposable environment: make destroy ENV=<env> STACK=<stack>.

`terraform destroy` alone can't remove a stack's data stores:
- DynamoDB tables and Aurora clusters carry `prevent_destroy` (modules dynamodb_table and
  aurora_postgres), which Terraform can't make conditional.
- An Aurora cluster must be gone before Terraform deletes its subnet group, security group and
  parameter group, or those deletions fail.

So, in order:
1. Preflight, changing nothing: refuse prod; read the state; refuse if a data store still has
   deletion protection or a bucket can't be force-destroyed (the destroy would stop half way).
2. Show what will be deleted and confirm by typing the environment name.
3. Record the protected resources in .terraform/<env>/teardown-pending.json. A rerun after a
   failure still finds them once they're out of state.
4. Aurora: delete the cluster's instances, then the cluster, and wait (no final snapshot unless
   --final-snapshot).
5. Remove the protected resources from state, then `terraform destroy -auto-approve`.
6. Delete the tables with the AWS CLI and wait. The KMS key outlives them by its deletion window.
7. Remove the pending file.

Only resources in this stack's state are touched; nothing else in the account is looked up by
name. Uses the terraform and aws CLIs with the caller's credentials (stdlib only).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROTECTED_ENVS = {"prod"}
_NOT_FOUND = ("NotFound", "ResourceNotFoundException")


class TeardownError(Exception):
    pass


@dataclass
class Plan:
    tables: dict[str, str] = field(default_factory=dict)  # state address -> table name
    clusters: dict[str, str] = field(default_factory=dict)  # state address -> cluster id
    resource_count: int = 0
    blockers: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def merge_pending(self, pending: dict[str, Any]) -> None:
        for kind in ("tables", "clusters"):
            for address, name in pending.get(kind, {}).items():
                getattr(self, kind).setdefault(address, name)


# -- commands -----------------------------------------------------------------------------------


class Runner:
    def __init__(self, stack_dir: Path, data_dir: str) -> None:
        self.stack_dir = stack_dir
        self.env = os.environ | {"TF_DATA_DIR": data_dir}

    def run(
        self, *args: str, cwd: Path | None = None, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(args, cwd=cwd, env=self.env, text=True, capture_output=True)
        if check and proc.returncode != 0:
            raise TeardownError(
                f"{' '.join(args)} failed:\n{proc.stderr.strip() or proc.stdout.strip()}"
            )
        return proc

    def terraform(self, *args: str, stream: bool = False) -> str:
        if stream:  # long-running and worth watching: pass output straight through
            if (
                subprocess.run(("terraform", *args), cwd=self.stack_dir, env=self.env).returncode
                != 0
            ):
                raise TeardownError(f"terraform {args[0]} failed")
            return ""
        return self.run("terraform", *args, cwd=self.stack_dir).stdout

    def aws(self, *args: str, missing_ok: bool = False) -> str:
        """Run an AWS CLI command; with missing_ok, a not-found error means already gone."""
        proc = self.run("aws", *args, check=False)
        if proc.returncode == 0:
            return proc.stdout
        if missing_ok and any(marker in proc.stderr for marker in _NOT_FOUND):
            return ""
        raise TeardownError(f"aws {' '.join(args)} failed:\n{proc.stderr.strip()}")


# -- preflight ----------------------------------------------------------------------------------


def resources(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Every managed resource in `terraform show -json` output, across child modules."""
    out: list[dict[str, Any]] = []

    def walk(module: dict[str, Any]) -> None:
        out.extend(r for r in module.get("resources", []) if r.get("mode") == "managed")
        for child in module.get("child_modules", []):
            walk(child)

    walk(state.get("values", {}).get("root_module", {}))
    return out


def plan_teardown(state: dict[str, Any]) -> Plan:
    plan = Plan()
    for r in resources(state):
        plan.resource_count += 1
        v, address = r.get("values") or {}, r["address"]
        if r["type"] == "aws_dynamodb_table":
            plan.tables[address] = v["name"]
            if v.get("deletion_protection_enabled"):
                plan.blockers.append(f"table {v['name']} has deletion protection on")
        elif r["type"] == "aws_rds_cluster":
            plan.clusters[address] = v["cluster_identifier"]
            if v.get("deletion_protection"):
                plan.blockers.append(
                    f"Aurora cluster {v['cluster_identifier']} has deletion protection on"
                )
        elif r["type"] == "aws_db_instance":
            if v.get("deletion_protection"):
                plan.blockers.append(f"RDS instance {v['identifier']} has deletion protection on")
            if not v.get("skip_final_snapshot"):
                plan.notes.append(f"RDS instance {v['identifier']} leaves a final snapshot")
        elif r["type"] == "aws_s3_bucket" and not v.get("force_destroy"):
            plan.blockers.append(
                f"bucket {v['bucket']} has force_destroy off (its objects would block the destroy)"
            )
    return plan


# -- teardown -----------------------------------------------------------------------------------


def delete_cluster(runner: Runner, cluster_id: str, final_snapshot: bool) -> None:
    out = runner.aws(
        "rds", "describe-db-clusters", "--db-cluster-identifier", cluster_id,
        "--query", "DBClusters[0].DBClusterMembers[].DBInstanceIdentifier", "--output", "json",
        missing_ok=True,
    )  # fmt: skip
    if not out.strip():
        print(f"   Aurora cluster {cluster_id}: already gone")
        return
    members = json.loads(out) or []
    for instance in members:
        print(f"   deleting Aurora instance {instance}")
        runner.aws(
            "rds", "delete-db-instance", "--db-instance-identifier", instance, missing_ok=True
        )
    for instance in members:
        runner.aws("rds", "wait", "db-instance-deleted", "--db-instance-identifier", instance)
    snapshot = (
        ["--final-db-snapshot-identifier", f"{cluster_id}-teardown-{datetime.now(UTC):%Y%m%d%H%M}"]
        if final_snapshot
        else ["--skip-final-snapshot"]
    )
    print(
        f"   deleting Aurora cluster {cluster_id}" + (" (final snapshot)" if final_snapshot else "")
    )
    runner.aws(
        "rds",
        "delete-db-cluster",
        "--db-cluster-identifier",
        cluster_id,
        *snapshot,
        missing_ok=True,
    )
    runner.aws("rds", "wait", "db-cluster-deleted", "--db-cluster-identifier", cluster_id)


def delete_table(runner: Runner, name: str) -> None:
    print(f"   deleting table {name}")
    runner.aws(
        "dynamodb", "delete-table", "--table-name", name, "--output", "json", missing_ok=True
    )
    runner.aws("dynamodb", "wait", "table-not-exists", "--table-name", name)


def teardown(args: argparse.Namespace) -> int:
    if args.env in PROTECTED_ENVS:
        print(f"refusing to destroy {args.env}", file=sys.stderr)
        return 1
    stack_dir = args.root / "stacks" / args.stack
    data_dir = f".terraform/{args.env}"
    pending_file = stack_dir / data_dir / "teardown-pending.json"
    runner = Runner(stack_dir, data_dir)

    # 1. Preflight
    plan = plan_teardown(json.loads(runner.terraform("show", "-json") or "{}"))
    if pending_file.exists():
        plan.merge_pending(json.loads(pending_file.read_text()))
        print(f"Resuming an earlier teardown ({pending_file.name}).")
    if plan.blockers:
        print(f"Can't tear down {args.stack} in {args.env}:")
        for b in plan.blockers:
            print(f"  - {b}")
        print("These are prod settings; a disposable env shouldn't have them. Nothing was changed.")
        return 1

    # 2. Confirm
    print(
        f"Tear down {args.stack} in {args.env}: {plan.resource_count} resources in state, including"
    )
    for cluster_id in plan.clusters.values():
        print(
            f"  - Aurora cluster {cluster_id} and its instances"
            + ("" if args.final_snapshot else " (no final snapshot)")
        )
    for name in plan.tables.values():
        print(f"  - DynamoDB table {name} and its data")
    for note in plan.notes:
        print(f"  - {note}")
    if args.dry_run:
        print("Dry run: nothing was changed.")
        return 0
    try:
        answer = input(f"Type the env name to destroy {args.stack} in {args.env}: ")
    except EOFError:  # no terminal: never destroy unattended
        answer = ""
    if answer.strip() != args.env:
        print("Not confirmed; nothing was changed.")
        return 1

    # 3. Remember what leaves the state
    pending_file.parent.mkdir(parents=True, exist_ok=True)
    pending_file.write_text(
        json.dumps({"tables": plan.tables, "clusters": plan.clusters}, indent=2)
    )

    # 4. Aurora before the resources it depends on
    for cluster_id in plan.clusters.values():
        delete_cluster(runner, cluster_id, args.final_snapshot)

    # 5. Everything else through Terraform
    in_state = {
        r["address"] for r in resources(json.loads(runner.terraform("show", "-json") or "{}"))
    }
    for address in [*plan.clusters, *plan.tables]:
        if address in in_state:
            runner.terraform("state", "rm", address)
    runner.terraform(
        "destroy", "-input=false", "-auto-approve", f"-var-file={args.var_file}", stream=True
    )

    # 6. Tables last
    for name in plan.tables.values():
        delete_table(runner, name)

    # 7. Done
    pending_file.unlink()
    print(f"{args.stack} in {args.env} is torn down. KMS keys are pending deletion (their window).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--env", required=True)
    parser.add_argument("--stack", required=True)
    parser.add_argument("--var-file", required=True, help="relative to the stack directory")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="the terraform/ directory",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="preflight and list what would be deleted"
    )
    parser.add_argument(
        "--final-snapshot",
        action="store_true",
        help="snapshot Aurora clusters before deleting them",
    )
    args = parser.parse_args(argv)
    try:
        return teardown(args)
    except TeardownError as err:
        print(f"teardown stopped: {err}", file=sys.stderr)
        print("Rerun the same command once fixed; it resumes where it stopped.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
