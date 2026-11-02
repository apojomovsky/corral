"""`corral hub`: the multi-host hub service and its operator verbs.

H1 ships two verbs: `add-host` mints a host id and its bearer token
(printed once), and `serve` runs the claim service. H4 adds
`remove-host`, `release` and `rotate-token`. H6 adds three operations
verbs: `export` writes the movable state as JSON (hosts, claims with
epochs, no token material), `import` loads it into a fresh hub, and
`backup` copies the database file. Every verb acts on the hub database
from `paths.hub_db_path`, never on a worker host's state. Exit codes
follow the public table: 0 ok, 2 a held port, 4 a usage or
configuration error.
"""

import argparse
import errno
import json
import signal
import sys
import time

OK, RACE, USAGE = 0, 2, 4


def _err(text: str) -> None:
    print(f"corral: {text}", file=sys.stderr)


def cmd_add_host(args: argparse.Namespace) -> int:
    from corral import hub, paths

    try:
        db_path = paths.hub_db_path()
    except paths.PathError as err:
        _err(str(err))
        return USAGE
    try:
        store = hub.open_db(db_path)
    except hub.HubError as err:
        _err(str(err))
        return USAGE
    try:
        token = hub.add_host(store, args.id)
    except hub.HubError as err:
        _err(str(err))
        return USAGE
    finally:
        store.close()
    print(token)
    return OK


def cmd_serve(args: argparse.Namespace) -> int:
    from corral import hub, paths, settings

    if args.port < 0 or args.port > 65535:
        _err(f"--port {args.port!r} must be 0-65535")
        return USAGE
    try:
        db_path = paths.hub_db_path()
    except paths.PathError as err:
        _err(str(err))
        return USAGE
    try:
        resolved = settings.load()
    except settings.SettingsError as err:
        _err(str(err))
        return USAGE
    lease_ttl_s = resolved["hub.lease_ttl_s"]
    if lease_ttl_s <= 0:
        _err(f"hub.lease_ttl_s {lease_ttl_s!r} must be positive")
        return USAGE
    stale_after_s = resolved["hub.stale_after_s"]
    if stale_after_s <= 0:
        _err(f"hub.stale_after_s {stale_after_s!r} must be positive")
        return USAGE
    try:
        server = hub.make_server(
            args.bind, args.port, db_path, lease_ttl_s=lease_ttl_s, stale_after_s=stale_after_s
        )
    except hub.HubError as err:
        _err(str(err))
        return USAGE
    except OSError as err:
        if err.errno == errno.EADDRINUSE:
            _err(f"hub bind {args.bind}:{args.port} is already in use")
            return RACE
        _err(f"cannot bind hub to {args.bind}:{args.port}: {err.strerror or err}")
        return USAGE
    warning = hub.bind_warning(args.bind)
    if warning is not None:
        print(f"corral: {warning}", file=sys.stderr)
    host, port = server.server_address[:2]
    print(f"listening on {host}:{port}")

    def _stop(signum: int, frame: object) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _stop)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return OK


def _open_store():
    from corral import hub, paths

    try:
        db_path = paths.hub_db_path()
    except paths.PathError as err:
        return None, str(err)
    try:
        return hub.open_db(db_path), ""
    except hub.HubError as err:
        return None, str(err)


def cmd_remove_host(args: argparse.Namespace) -> int:
    from corral import hub

    store, err = _open_store()
    if store is None:
        _err(err)
        return USAGE
    try:
        epoch = hub.remove_host(store, args.id, now=time.time())
    except hub.HubError as exc:
        _err(str(exc))
        return USAGE
    finally:
        store.close()
    print(f"removed {args.id} (membership epoch {epoch})")
    return OK


def cmd_release(args: argparse.Namespace) -> int:
    from corral import hub

    store, err = _open_store()
    if store is None:
        _err(err)
        return USAGE
    try:
        epoch = hub.release_claim(
            store, args.repo, args.number, now=time.time(), reason=args.reason
        )
    except hub.HubError as exc:
        _err(str(exc))
        return USAGE
    finally:
        store.close()
    print(f"released {args.repo}#{args.number} (epoch {epoch} kept for the next mint)")
    return OK


def cmd_rotate_token(args: argparse.Namespace) -> int:
    from corral import hub

    store, err = _open_store()
    if store is None:
        _err(err)
        return USAGE
    try:
        token = hub.rotate_token(store, args.id)
    except hub.HubError as exc:
        _err(str(exc))
        return USAGE
    finally:
        store.close()
    print(token)
    return OK


def cmd_export(args: argparse.Namespace) -> int:
    from corral import hub, paths

    try:
        db_path = paths.hub_db_path()
    except paths.PathError as err:
        _err(str(err))
        return USAGE
    try:
        store = hub.open_db(db_path)
    except hub.HubError as err:
        _err(str(err))
        return USAGE
    try:
        document = hub.export_hub(store)
    except hub.HubError as err:
        _err(str(err))
        return USAGE
    finally:
        store.close()
    raw = json.dumps(document, indent=2, sort_keys=True) + "\n"
    if not args.file:
        sys.stdout.write(raw)
        return OK
    try:
        with open(args.file, "w", encoding="utf-8") as handle:
            handle.write(raw)
    except OSError as err:
        _err(f"cannot write export {args.file}: {err}")
        return USAGE
    return OK


def cmd_import(args: argparse.Namespace) -> int:
    from corral import hub, paths

    try:
        with open(args.file, encoding="utf-8") as handle:
            raw = handle.read()
    except OSError as err:
        _err(f"cannot read import {args.file}: {err}")
        return USAGE
    try:
        document = json.loads(raw)
    except ValueError as err:
        _err(f"cannot parse import {args.file}: {err}")
        return USAGE
    try:
        db_path = paths.hub_db_path()
    except paths.PathError as err:
        _err(str(err))
        return USAGE
    try:
        store = hub.open_db(db_path)
    except hub.HubError as err:
        _err(str(err))
        return USAGE
    try:
        claims, host_ids = hub.import_hub(store, document)
    except hub.HubError as err:
        _err(str(err))
        return USAGE
    finally:
        store.close()
    print(f"loaded {claims} claims; re-add {len(host_ids)} hosts with their minted tokens:")
    for host_id in host_ids:
        print(f"  corral hub add-host --id {host_id}")
    return OK


def cmd_backup(args: argparse.Namespace) -> int:
    from corral import hub, paths

    try:
        db_path = paths.hub_db_path()
    except paths.PathError as err:
        _err(str(err))
        return USAGE
    try:
        dest = hub.backup_hub(db_path, args.to)
    except hub.HubError as err:
        _err(str(err))
        return USAGE
    print(dest)
    return OK


def cmd_hub(args: argparse.Namespace) -> int:
    if args.hub_verb == "add-host":
        return cmd_add_host(args)
    if args.hub_verb == "serve":
        return cmd_serve(args)
    if args.hub_verb == "remove-host":
        return cmd_remove_host(args)
    if args.hub_verb == "release":
        return cmd_release(args)
    if args.hub_verb == "rotate-token":
        return cmd_rotate_token(args)
    if args.hub_verb == "export":
        return cmd_export(args)
    if args.hub_verb == "import":
        return cmd_import(args)
    if args.hub_verb == "backup":
        return cmd_backup(args)
    _err(f"unknown hub verb {args.hub_verb!r}")
    return USAGE


def add_parser(sub) -> None:
    """Register `hub` and its verbs on the top-level subparsers."""
    hub_cmd = sub.add_parser(
        "hub", help="the multi-host hub: the claim service and its operator verbs"
    )
    verbs = hub_cmd.add_subparsers(dest="hub_verb", required=True)
    p = verbs.add_parser(
        "add-host", help="mint a host id and its bearer token (prints the token once)"
    )
    p.add_argument("--id", required=True, help="stable operator-assigned host id")
    p = verbs.add_parser(
        "remove-host", help="drop a host: bump the membership epoch and release its claims"
    )
    p.add_argument("--id", required=True, help="stable operator-assigned host id")
    p = verbs.add_parser("release", help="force-release one ticket's claim (operator override)")
    p.add_argument("repo", help="repository holding the ticket")
    p.add_argument("number", type=int, help="ticket number")
    p.add_argument("--reason", default="", help="why the claim is released (logged)")
    p = verbs.add_parser("rotate-token", help="revoke one host's token and mint its replacement")
    p.add_argument("--id", required=True, help="stable operator-assigned host id")
    p = verbs.add_parser("serve", help="run the hub claim service")
    p.add_argument("--bind", default="127.0.0.1", help="address to bind (default 127.0.0.1)")
    p.add_argument(
        "--port", type=int, default=0, help="port to bind (default 0, an ephemeral port)"
    )
    p = verbs.add_parser(
        "export",
        help="write the hub's movable state as JSON (hosts, claims with epochs, no secrets)",
    )
    p.add_argument("--file", default="", help="write to PATH instead of stdout")
    p = verbs.add_parser("import", help="load an export into a fresh hub, keeping epochs")
    p.add_argument("--file", required=True, help="the JSON file `hub export` wrote")
    p = verbs.add_parser("backup", help="copy the hub database to PATH (never clobbers)")
    p.add_argument("--to", required=True, help="destination path for the copy")
    hub_cmd.set_defaults(func=cmd_hub)
