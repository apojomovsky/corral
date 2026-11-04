"""The drive side of the hub: composition the read paths never reach.

`corral.serve` (through `snapshot`) imports `corral.drive`, so `drive`
itself never names `corral.hub`: the suite asserts the hub client is
unreachable from the host read paths. Everything that touches the hub
module therefore lives here, behind a facade the pass only
duck-types. `drive` catches the facade's drive-local signals
(`drive.HubGone`, `drive.HubConflict`, `drive.HubConfigError`) and
never the hub's own errors. Only CLI-side code (`cli`, this module's
`cmd_drive`) imports this module.
"""

import argparse
import sqlite3
import sys
import time

from corral import board, drive
from corral import hub as hubmod
from corral import paths, projects, settings, syncli, trackers, units


class HubMode:
    """One pass's hub conversation: propose first, short-circuit when down.

    `client` is the injected hub seam (a `hub.HubClient` or a fake with
    the same methods); `host_id` is this host's operator-assigned id.
    The first `HubUnreachable` flips `down` and every later call raises
    `drive.HubGone` without touching the client, so a dead hub costs
    one failed call per pass. A hub conflict surfaces as
    `drive.HubConflict` carrying the holder, and a hub usage error as
    `drive.HubConfigError`, so callers never name the hub module. Hub
    mode is the presence of this object: with no hub configured the
    pass never builds one and makes zero hub calls (single-host
    behavior is the absence of the step, not a client pointed at
    localhost).
    """

    def __init__(self, client, host_id):
        self.client = client
        self.host_id = host_id
        self.down = False
        self.table: dict = {}

    def epoch_of(self, repo, number):
        """The hub's fencing epoch for (`repo`, `number`), for the stuck check.

        `None` when the hub shows no row; raises `drive.HubGone` when
        the hub is down, which the stuck sweep reports as unreadable
        and skips. The pass refreshes `table` from the reconnect, so
        this never serves an epoch a re-grant just replaced.
        """
        if self.down:
            raise drive.HubGone("hub unreachable")
        return self.table.get((repo, number))

    def _call(self, call):
        if self.down:
            raise drive.HubGone("hub unreachable")
        try:
            return call()
        except hubmod.HubUnreachable as err:
            self.down = True
            raise drive.HubGone(str(err)) from None
        except hubmod.HubVersionError as err:
            raise drive.HubConfigError(str(err)) from None
        except hubmod.HubError as err:
            raise drive.HubConfigError(str(err)) from None

    def _proposing(self, call):
        try:
            return self._call(call)
        except hubmod.Conflict as err:
            raise drive.HubConflict(err.holder) from None

    def fetch(self):
        """Every hub claim row, for the reconnect compare."""
        return self._call(lambda: self.client.claims())

    def propose(self, repo, number, agent):
        """Grant or refuse one claim: the receipt, or `HubConflict`."""
        return self._proposing(lambda: self.client.propose(repo, number, agent))

    def renew(self, repo, number, agent, epoch):
        """Extend a live lease without changing its epoch, or `HubConflict`."""
        return self._proposing(lambda: self.client.renew(repo, number, agent, epoch))

    def release(self, repo, number, agent, epoch):
        """Release a hub row the host holds nothing for. Returns the kept epoch."""
        return self._proposing(lambda: self.client.release(repo, number, agent, epoch))

    def reconcile(self, store, *, stop_unit, out, err, now):
        """Section-3 steps 2-4: fetch, fence the stale, re-adopt the live.

        Compares the hub claims table against locally held claims,
        ticket by ticket, in store order. A local claim the hub shows
        under a newer epoch or a different holder is fenced through
        `drive.drop_stale_claim` (stop, drop locally, never touch the
        hub row). A live row renews a live lease or re-grants a lapsed
        one, adopting the bumped epoch locally; a hub 409 in either
        case fences instead. Hub rows naming this host with no local
        record are released unconditionally. A local claim the hub
        shows no row for, or an older epoch for, is kept with a log
        line, as is any foreign local claim: the pass never touches
        those. Raises `drive.HubGone` when the hub is down. Returns
        the post-reconcile hub epoch table {(repo, number): epoch} for
        the stuck check and stores it as `table`: re-grants update it,
        so the sweep never reads an epoch it just replaced. A failing
        row defers that ticket instead of aborting the loop. Steps are
        idempotent, so a pass cut mid-reconcile is safe to re-run.
        """
        remote = self.fetch()
        by_ticket = {(row["repo"], row["number"]): row for row in remote}
        table = {key: row["epoch"] for key, row in by_ticket.items()}
        self.table = table
        try:
            items = store.items()
        except (board.StoreError, sqlite3.Error) as problem:
            print(f"corral drive: hub reconcile unreadable ({problem}), skipped", file=err)
            return table
        held: set = set()
        for item in items:
            ref = f"{item.repo}#{item.number}"
            try:
                claim = store.claim_of(item.repo, item.number)
            except (board.StoreError, sqlite3.Error) as problem:
                print(f"{ref}: hub reconcile unreadable ({problem}), left held", file=out)
                continue
            if claim is None or not claim.agent:
                continue
            held.add((claim.repo, claim.number))
            row = by_ticket.get((claim.repo, claim.number))
            if row is None:
                print(f"{ref}: hub shows no row, claim kept", file=out)
                continue
            if not drive.is_managed_agent(claim.agent):
                print(f"{ref}: foreign local claim by `{claim.agent}`, never touched", file=out)
                continue
            if (
                row["host_id"] == self.host_id
                and row["agent"] == claim.agent
                and row["epoch"] == claim.epoch
            ):
                if row["lease_expires"] > now:
                    try:
                        self.renew(claim.repo, claim.number, claim.agent, claim.epoch)
                    except drive.HubConflict:
                        drive.drop_stale_claim(
                            store, claim.repo, claim.number, stop_unit=stop_unit, out=out, now=now
                        )
                    continue
                try:
                    receipt = self.propose(claim.repo, claim.number, claim.agent)
                except drive.HubConflict:
                    drive.drop_stale_claim(
                        store, claim.repo, claim.number, stop_unit=stop_unit, out=out, now=now
                    )
                    continue
                try:
                    store.claim(
                        claim.repo,
                        claim.number,
                        claim.agent,
                        claim.host,
                        now=now,
                        epoch=receipt.epoch,
                    )
                except (
                    board.Claimed,
                    board.ExternalItem,
                    board.StoreError,
                    sqlite3.Error,
                ) as problem:
                    print(f"{ref}: re-grant adoption refused ({problem}), left held", file=out)
                    continue
                table[(claim.repo, claim.number)] = receipt.epoch
                print(
                    f"{ref}: re-granted on reconnect, adopted hub epoch {receipt.epoch}",
                    file=out,
                )
                continue
            if (
                row["host_id"] == self.host_id
                and row["agent"] == claim.agent
                and row["epoch"] < claim.epoch
            ):
                print(
                    f"{ref}: hub epoch {row['epoch']} is older than local epoch"
                    f" {claim.epoch}, claim kept",
                    file=out,
                )
                continue
            drive.drop_stale_claim(
                store, claim.repo, claim.number, stop_unit=stop_unit, out=out, now=now
            )
        for (repo, number), row in by_ticket.items():
            if row["host_id"] != self.host_id or not row["agent"]:
                continue
            if (repo, number) in held:
                continue
            ref = f"{repo}#{number}"
            try:
                self.release(repo, number, row["agent"], row["epoch"])
            except drive.HubConflict as problem:
                print(f"{ref}: hub release raced ({problem}), left to lapse", file=out)
                continue
            print(f"{ref}: released a hub row with no local claim", file=out)
        return table


def hub_client_for(resolved: settings.Resolved, *, token_path):
    """The hub client for this pass, or None in single-host mode.

    Pure except for storing the token path: with no `hub.url` there
    is no client and the pass makes zero hub calls. With one but no
    `hub.host_id` the pass is refused (`drive.HubConfigError`, exit
    4): a host that cannot name itself must never propose. The client
    reads the token at call time, so construction touches no file.
    """
    url = str(resolved["hub.url"] or "").strip()
    if not url:
        return None
    host_id = str(resolved["hub.host_id"] or "").strip()
    if not host_id:
        raise drive.HubConfigError(
            "hub.url is set but hub.host_id is empty: register this host first"
        )
    return hubmod.HubClient(url, host_id, token_path)


def hub_mode_for(resolved: settings.Resolved, *, token_path):
    """The pass's hub conversation, or None in single-host mode.

    One place for the CLI entry: builds the client and wraps it in a
    `HubMode`, so the pass proposes through the hub and reconciles on
    reconnect. With no `hub.url` there is no mode and the pass makes
    zero hub calls.
    """
    client = hub_client_for(resolved, token_path=token_path)
    if client is None:
        return None
    return HubMode(client, str(resolved["hub.host_id"] or "").strip())


def cmd_drive(args: argparse.Namespace) -> int:
    """Run one dispatch pass over the board store (the `drive` verb)."""
    if getattr(args, "drive_verb", None) == "install-timer":
        if getattr(args, "plan", False):
            print(
                "corral drive: --plan changes nothing, install-timer installs; pick one",
                file=sys.stderr,
            )
            return drive.USAGE
        return drive.cmd_install_timer(args)
    try:
        store = board.open_store()
    except board.StoreError as problem:
        print(f"corral drive: {problem}", file=sys.stderr)
        return drive.USAGE
    with store:
        try:
            host_settings = settings.load(root=None)
            max_starts = host_settings["drive.max_starts_per_day"]
            hold_label = host_settings["board.hold_label"]
            hub_url = host_settings["hub.url"]
        except settings.SettingsError as problem:
            print(f"corral drive: {problem}", file=sys.stderr)
            return drive.USAGE
        hub = None
        if not args.plan and str(hub_url or "").strip():
            try:
                hub = hub_mode_for(host_settings, token_path=paths.hub_token_path())
            except (drive.HubConfigError, paths.PathError) as problem:
                print(f"corral drive: {problem}", file=sys.stderr)
                return drive.USAGE
        if args.plan:
            try:
                registered = projects.load_all()
            except projects.ProjectsError as problem:
                print(f"corral drive: {problem}", file=sys.stderr)
                return drive.USAGE
            moment = time.time()
            try:
                planned_states, _, planned_tickets = drive.production_stuck_inputs(
                    registered, now=moment
                )
                planned_finished = drive.production_finished_for(now=moment)
                planned_roles = drive.production_agent_roles(now=moment)
            except settings.SettingsError as problem:
                print(f"corral drive: {problem}", file=sys.stderr)
                return drive.USAGE
            try:
                _, planned_live, _ = drive.production_orphan_inputs(registered, now=moment)
            except settings.SettingsError as problem:
                print(f"corral drive: {problem}", file=sys.stderr)
                return drive.USAGE
            except units.Unreachable as problem:
                print(
                    f"corral drive: cannot read live units,"
                    f" plan treats finished rows as released ({problem})",
                    file=sys.stderr,
                )
                planned_live = frozenset()
            return drive.run_pass(
                plan=True,
                store=store,
                lock_path=drive.lock_path(),
                out=sys.stdout,
                err=sys.stderr,
                hold_label=hold_label,
                pause_path=drive.pause_path(),
                agent_states=planned_states,
                finished_for=planned_finished,
                agent_roles=planned_roles,
                agent_tickets=planned_tickets,
                live_units=planned_live,
            )
        try:
            registered = projects.load_all()
        except projects.ProjectsError as problem:
            print(f"corral drive: {problem}", file=sys.stderr)
            return drive.USAGE
        operators, problem = drive.production_operators(registered, syncli._git)
        if problem is not None:
            print(f"corral drive: {problem}", file=sys.stderr)
            return drive.USAGE
        by_repo: dict = {}
        for project in registered:
            try:
                names = settings.repo_names(project.root)
            except settings.SettingsError as err:
                print(f"corral drive: {err}", file=sys.stderr)
                return drive.USAGE
            mapping, problem = trackers.production_trackers(project.root, names, git=syncli._git)
            if problem is not None:
                print(f"corral drive: {problem}", file=sys.stderr)
                return drive.USAGE
            for entry in mapping.values():
                by_repo.setdefault(entry.tracker_repo, entry.tracker)

        def tracker_factory(repo, _by_repo=by_repo):
            return _by_repo[repo]

        moment = time.time()
        try:
            stuck_states, stuck_stale, stuck_tickets = drive.production_stuck_inputs(
                registered, now=moment
            )
            finished_for = drive.production_finished_for(now=moment)
            agent_roles = drive.production_agent_roles(now=moment)
        except settings.SettingsError as problem:
            print(f"corral drive: {problem}", file=sys.stderr)
            return drive.USAGE
        return drive.run_pass(
            plan=False,
            store=store,
            lock_path=drive.lock_path(),
            out=sys.stdout,
            err=sys.stderr,
            hold_label=hold_label,
            projects_fn=lambda: registered,
            tracker_factory=tracker_factory,
            operators=operators,
            launcher=drive.production_launcher(registered, store=store, hold_label=hold_label),
            agent_states=stuck_states,
            stale_s=stuck_stale,
            stop_unit=drive.production_stopper(registered),
            restarts_path=drive.restarts_path(),
            stuck_project=lambda repo, number: drive.production_stuck_project(registered, repo),
            agent_roles=agent_roles,
            agent_tickets=stuck_tickets,
            finished_for=finished_for,
            journal_path=drive.journal_path(),
            pause_path=drive.pause_path(),
            day_path=drive.day_path(),
            max_starts=max_starts,
            hub=hub,
        )


def add_parser(sub) -> None:
    """Register `drive` on the top-level subparsers."""
    p = sub.add_parser("drive", help="run one dispatch pass over the board store")
    p.add_argument(
        "--plan",
        action="store_true",
        help="print the candidate table and change nothing",
    )
    verbs = p.add_subparsers(dest="drive_verb", required=False)
    install = verbs.add_parser(
        "install-timer",
        help="install and enable the user timer that repeats the pass",
    )
    # SUPPRESS keeps a verb-last `--plan` intact: a plain default here
    # would overwrite it when the subparser runs.
    install.add_argument(
        "--plan",
        action="store_true",
        default=argparse.SUPPRESS,
        help="refused: installing changes the host, so there is no dry run",
    )
    p.set_defaults(func=cmd_drive)
