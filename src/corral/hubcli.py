"""`corral hub`: the multi-host hub service and its operator verbs.

H1 ships two verbs: `add-host` mints a host id and its bearer token
(printed once), and `serve` runs the claim service. Every verb acts on
the hub database from `paths.hub_db_path`, never on a worker host's
state. Exit codes follow the public table: 0 ok, 2 a held port, 4 a
usage or configuration error.
"""

import argparse
import errno
import signal
import sys

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


def cmd_hub(args: argparse.Namespace) -> int:
    if args.hub_verb == "add-host":
        return cmd_add_host(args)
    if args.hub_verb == "serve":
        return cmd_serve(args)
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
    p = verbs.add_parser("serve", help="run the hub claim service")
    p.add_argument("--bind", default="127.0.0.1", help="address to bind (default 127.0.0.1)")
    p.add_argument(
        "--port", type=int, default=0, help="port to bind (default 0, an ephemeral port)"
    )
    hub_cmd.set_defaults(func=cmd_hub)
