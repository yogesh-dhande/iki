"""`iki bundle verify`: checks a bundle and its electronic signatures, offline.

A bundle (ADR 0096) carries its records' electronic signatures (ADR 0126). This reads it
as an importing deployment would, trusting nothing until its bytes and its signature
check out, then checks each signature against the bundle's own history: no deployment,
database or network is needed, so QA or an auditor can check one anywhere.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, TextIO

#: The most a bundle may come to, uncompressed, when it's checked here.
LIMIT = 1024**4


def add_commands(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    bundle = subparsers.add_parser(
        "bundle",
        help="Check a bundle of records exported from Iki.",
        description="Commands for bundles: records exported from a deployment.",
    )
    commands = bundle.add_subparsers(dest="bundle_command", metavar="COMMAND")
    verify = commands.add_parser(
        "verify",
        help="Check a bundle and its electronic signatures, offline.",
        description=(
            "Check a bundle: each member's SHA-256, and its signature by its deployment's "
            "key. Then check each electronic signature (21 CFR Part 11) it carries: signed "
            "by a key you trust, for its record and revision, and that the bundle's history "
            "of that revision holds what was signed. Exits 0 only if everything checks out."
        ),
    )
    verify.add_argument("bundle", type=Path, help="The bundle: a .tar.gz exported from Iki.")
    verify.add_argument(
        "--trust",
        action="append",
        default=[],
        metavar="DEPLOYMENT=KEY",
        help=(
            "Trust KEY (ed25519:...) as DEPLOYMENT's bundle key, such as "
            "prod=ed25519:riAtz4... The keys that signed its signatures, which it lists, "
            "are then trusted too. Repeat for more."
        ),
    )
    verify.add_argument(
        "--key",
        action="append",
        default=[],
        metavar="KEY[=DEPLOYMENT]",
        help=(
            "Trust KEY for signatures, whatever the bundle lists: a deployment's signing key, "
            "from GET /api/v1/signing-keys there. Repeat for more."
        ),
    )
    verify.add_argument("--json", action="store_true", help="Print the result as JSON.")
    verify.set_defaults(handler=run_verify)
    bundle.set_defaults(handler=lambda args: _help(bundle))


def _help(parser: argparse.ArgumentParser) -> int:
    parser.print_help()
    return 0


def _pairs(values: list[str], *, key_first: bool) -> dict[str, str] | None:
    out: dict[str, str] = {}
    for value in values:
        left, sep, right = value.partition("=")
        if key_first:
            key, name = left, right or "given"
        else:
            if not sep:
                return None
            name, key = left, right
        if not key.startswith("ed25519:") or not name:
            return None
        out[key] = name
    return out


def run_verify(args: argparse.Namespace, out: TextIO = sys.stdout, err: TextIO = sys.stderr) -> int:
    from iki_server.bundles import reading
    from iki_server.signatures import offline

    trusts = _pairs(args.trust, key_first=False)
    keys = _pairs(args.key, key_first=True)
    if trusts is None or keys is None:
        print("iki: give --trust as DEPLOYMENT=ed25519:KEY and --key as ed25519:KEY.", file=err)
        return 2
    by_deployment: dict[str, tuple[str, ...]] = {}
    for key, name in trusts.items():
        by_deployment[name] = (*by_deployment.get(name, ()), key)
    try:
        bundle = reading.read(args.bundle, by_deployment, limit=LIMIT)
        verified = offline.verify(bundle, keys)
    except (reading.BundleError, OSError) as error:
        print(f"iki: {args.bundle} doesn't check out: {error}", file=err)
        return 1
    valid = all(v.valid for v in verified)
    if args.json:
        json.dump(_as_json(bundle, verified), out, indent=2, ensure_ascii=False)
        out.write("\n")
        return 0 if valid else 1
    source = bundle.source
    if bundle.trusted_as is None:
        print(
            f"The bundle checks out, but you don't trust {source}'s key "
            f"{bundle.manifest['source']['key']}: give it with --trust {source}=KEY, or give "
            "the keys that signed its signatures with --key.",
            file=out,
        )
    else:
        print(f"The bundle checks out, signed by {source}'s key.", file=out)
    if not verified:
        print("It carries no electronic signatures.", file=out)
    for v in verified:
        mark = "ok  " if v.valid else "FAIL"
        print(f"{mark} {v.hrid}: {v.manifestation} ({v.status})", file=out)
        for check in v.checks:
            if not check.ok:
                print(f"       {check.name}: {check.detail}", file=out)
    failed = sum(1 for v in verified if not v.valid)
    print(
        f"{len(verified)} signature{'s' * (len(verified) != 1)}, "
        + (f"{failed} failed." if failed else "all valid."),
        file=out,
    )
    return 0 if valid else 1


def _as_json(bundle: Any, verified: list[Any]) -> dict[str, Any]:  # noqa: ANN401
    return {
        "bundle": {
            "id": bundle.manifest["id"],
            "sha256": bundle.sha256,
            "source": bundle.source,
            "trusted": bundle.trusted_as is not None,
        },
        "valid": all(v.valid for v in verified),
        "signatures": [
            {
                "id": v.id,
                "record": v.record,
                "hrid": v.hrid,
                "manifestation": v.manifestation,
                "status": v.status,
                "valid": v.valid,
                "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in v.checks],
            }
            for v in verified
        ],
    }
