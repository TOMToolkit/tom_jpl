"""Reconcile stored Scout candidates and record their post-NEOCP outcomes.

Ingesting *new* Scout candidates is the job of tom_dataservices' ``rundataquery``
management command run against a saved Scout ``DataServiceQuery``. This command covers
the rest of an already-ingested candidate's lifecycle:

- Reconciliation: fetch the current Scout roster in one unconstrained call and compare it
  against every target marked ``active`` on its ``ScoutDetail``. Absent from the roster
  means the object has left the NEOCP; still on it means refresh. The Scout API applies no
  cuts of its own -- ``build_query_parameters`` forwards only ``tdes``/``orbits``, and the
  score thresholds are ours, applied client-side in ``_passes_filters`` -- so an
  unconstrained call returns the whole list and absence from it is unambiguous.
- Outcome recording: fetch the MPC "Previous NEOCP Objects" listing (the long-window page
  at /iau/NEO/ToConfirm_PrevDes.html, months of departures in one request) and settle every
  departed candidate: promote new IAU designations onto ``Target.name`` (keeping the
  original trksub as a ``TargetName`` alias), record why the unlucky ones left
  (lost/dne/na/ns), and link retired duplicate submissions to the object that survived
  (``ScoutDetail.merged_into``). A settled candidate (non-null ``mpc_status``) is final and
  is never reprocessed.

The two phases run on different natural cadences: reconciliation tracks a roster that
changes hourly, while the outcome page's window spans months, so consulting it daily is
plenty. Run from cron as two entries rather than one:

    17 * * * *  python manage.py updatescout --skip-designations
    47 4 * * *  python manage.py updatescout --skip-reconcile

The departure feed is deliberately isolated behind :func:`_fetch_mpc_departures`, which
returns normalized outcome records; if the MPC ships a machine-readable API for previous
NEOCP designations, only that transport function needs to change. Per-object lookups
against the MPC were removed deliberately -- the long-window page makes them unnecessary,
and observation-level sources (WAMO) were found to misstate candidate-level fate (trksub
strings are reused across years, and a follow-up tracklet's attribution is not the
candidate's outcome).
"""

import re
import requests
from datetime import datetime, timezone
from html.parser import HTMLParser

from django.core.management.base import BaseCommand
from django.db import IntegrityError, transaction

from tom_dataservices.dataservices import get_data_service_class

from tom_jpl.models import ScoutDetail

_MPC_PREV_DES_URL = 'https://data.minorplanetcenter.net/iau/NEO/ToConfirm_PrevDes.html'

# One departure per line of page text, in one of two shapes. Designation/identification:
#   2026 QT = ST26H67 (Aug. 21.34 UT)   [see MPEC 2026-Q53]
#   Comet P/2026 P1 = P12p4Jx (Aug. 25.75 UT)   [see MPEC 2026-Q98]
#   ST26H89 = TF26HD1 (Aug. 24.69 UT)              <- two submissions, same body
# The right-hand side is always the retired trksub; the left is what it became -- an IAU
# designation, or the trksub that survived when the MPC identified two submissions with
# each other. Outcome-without-designation:
#   ST26H76 was not confirmed (Aug. 27.68 UT)
#   19E0001 does not exist (June 20.19 UT)
#   6EI3621 was not a minor planet (May 19.65 UT)
_DESIG_LINE_RE = re.compile(
    r'^(?P<left>\S(?:.*?\S)?)\s*=\s*(?P<right>\S+)\s*'
    r'\((?P<date>[^)]*)\)\s*(?:\[\s*see\s+(?P<ref>[^\]]+?)\s*\])?$'
)

# Ordered: 'was not a minor planet' must not be shadowed by a looser artificial match.
_STATUS_PHRASES = (
    ('was not confirmed', 'lost'),
    ('does not exist', 'dne'),
    ('suspected artificial', 'ns'),
    ('was not a minor planet', 'na'),
)

# Trksubs are short packed identifiers that always contain a digit and never a space;
# requiring the digit keeps a stray word from being read as one.
_TRKSUB_RE = re.compile(r'(?=.*\d)[A-Za-z0-9]{5,10}\Z')


class _TextExtractor(HTMLParser):
    """Flatten HTML to its text content; the page is one <li> per departure and the
    interesting structure survives as one line of text per entry (references arrive as
    '[see MPEC 2026-Q53]' after anchor/italic markup is stripped)."""

    def __init__(self):
        super().__init__()
        self.chunks = []

    def handle_data(self, data):
        self.chunks.append(data)

    def text(self):
        return ''.join(self.chunks)


def _is_designation(token):
    """Whether a token is a real designation as the page renders one: a provisional or
    comet designation containing a space ('2026 QD3', 'P/2026 P1'), a parenthesized
    number ('(452639)'), or a comet permanent ID ('161P', '161P/Hartley-IRAS').
    Everything else is another trksub."""
    return bool(
        ' ' in token
        or re.fullmatch(r'\(\d+\)', token)
        or re.fullmatch(r'\d+[PCDXAI](/\S+)?', token)
    )


def _parse_departures(html):
    """Parse the Previous-NEOCP page into normalized outcome records, keyed by trksub:

        {'status': 'designated'|'lost'|'dne'|'na'|'ns',
         'designation': str | None,   # set when status == 'designated'
         'reference':   str | None,   # announcing publication, e.g. 'MPEC 2026-Q53'
         'merged_into': str | None}   # immediate surviving identifier, for merge rows

    Identification chains are followed within the page: a submission identified with a
    second trksub inherits that object's own outcome (its designation, or the reason it
    was dropped). A chain that dead-ends at a trksub the page doesn't know -- typically a
    survivor still alive on the NEOCP -- yields no record, leaving that object pending
    until a later fetch. Lines fitting no known shape are ignored.
    """
    extractor = _TextExtractor()
    extractor.feed(html)

    became = {}    # trksub -> {'became': ..., 'reference': ...} from designation/merge lines
    statuses = {}  # trksub -> status code from outcome-without-designation lines
    for line in extractor.text().splitlines():
        line = line.strip()
        if not line:
            continue
        match = _DESIG_LINE_RE.match(line)
        if match:
            trksub = match.group('right')
            left = match.group('left').removeprefix('Comet ')
            reference = ' '.join(match.group('ref').split()) if match.group('ref') else None
            if _TRKSUB_RE.match(trksub):
                became.setdefault(trksub, {'became': left, 'reference': reference})
            continue
        for phrase, code in _STATUS_PHRASES:
            if phrase in line:
                token = line.split()[0]
                if _TRKSUB_RE.match(token):
                    statuses.setdefault(token, code)
                break

    outcomes = {}
    for trksub, row in became.items():
        current, reference = row['became'], row['reference']
        hops = {trksub}
        while not _is_designation(current):
            if current in statuses:
                # The surviving partner itself left without a designation; this
                # submission shares that fate.
                outcomes[trksub] = {'status': statuses[current], 'designation': None,
                                    'reference': None, 'merged_into': row['became']}
                break
            parent = became.get(current)
            if parent is None or current in hops:
                break  # survivor unknown to the page (or a cycle): not resolvable yet
            hops.add(current)
            current, reference = parent['became'], parent['reference']
        else:
            outcomes[trksub] = {
                'status': 'designated', 'designation': current, 'reference': reference,
                'merged_into': row['became'] if row['became'] != current else None,
            }
    for trksub, code in statuses.items():
        outcomes.setdefault(trksub, {'status': code, 'designation': None,
                                     'reference': None, 'merged_into': None})
    return outcomes


def _fetch_mpc_departures():
    """Transport for the departure feed; the single place to swap if the MPC ever ships a
    machine-readable API for previous NEOCP designations. Network or parse failures
    propagate as exceptions; callers should catch and warn rather than abort."""
    response = requests.get(_MPC_PREV_DES_URL, timeout=30)
    response.raise_for_status()
    return _parse_departures(response.text)


class Command(BaseCommand):
    help = (
        'Reconcile active Scout candidates against the live Scout API, retiring those that '
        'have left, and settle departed candidates from the MPC Previous NEOCP Objects page: '
        'rename to new IAU designations, record lost/dne/artificial outcomes, and link '
        'retired duplicate submissions to their surviving object. The page covers months of '
        'departures in one request, so run reconciliation hourly (--skip-designations) and '
        'the outcome pass daily (--skip-reconcile).'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--skip-reconcile',
            action='store_true',
            help='Do not re-check active candidates against Scout / do not retire departed ones.',
        )
        parser.add_argument(
            '--skip-designations',
            action='store_true',
            help='Do not check the MPC page for outcomes of departed candidates.',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Report what would happen without writing to the database.',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        if not options['skip_reconcile']:
            self._reconcile(dry_run=dry_run)
        if not options['skip_designations']:
            self._update_designations(dry_run=dry_run)

    def _reconcile(self, dry_run=False):
        """Reconcile active Scout candidates against the current roster, retiring departed ones.

        One unconstrained call returns every object currently on Scout, so membership is
        settled with a single request instead of one per candidate. The API applies no cuts of
        its own (see the module docstring), which is what makes absence from that list mean
        "has left" rather than "no longer matches some query".

        A candidate still on the roster is refreshed from its own roster row, which carries
        every field ``ScoutDetail`` stores -- ``_parse_detail_data`` reads nothing a listing
        row lacks -- so reconciliation costs exactly one request no matter how many candidates
        are being tracked. Roster rows are matched by ``Target.name`` or any alias, so a
        candidate already renamed to its IAU designation (Scout can keep listing an object
        briefly after the MPC designates it) is still recognized under its original trksub.

        Orbital elements are deliberately not touched here. Those live on the ``Target`` and
        come from the ``orbits`` array only a per-object response carries; refreshing them is
        the ingest path's job (``rundataquery``, which already fetches per-object data for
        every candidate passing the query's cuts). Reconciliation's concern is which
        candidates are still on Scout and what their current scores are.
        """
        data_service = get_data_service_class('Scout')()
        active_details = list(
            ScoutDetail.objects.filter(active=True)
            .select_related('target').prefetch_related('target__aliases')
        )
        self.stdout.write(f'Reconciling {len(active_details)} active Scout candidate(s)...')
        if not active_details:
            return

        roster = data_service.query_service(data_service.build_query_parameters({}))

        # Guard the two ways a bad response could look like a mass departure. Scout is never
        # legitimately empty, and the API reports its own row count, so a short payload is a
        # truncated fetch rather than news. Either way, retiring the whole candidate pool on
        # the strength of one malformed response is not a recoverable mistake -- do nothing.
        if not roster:
            self.stdout.write(self.style.WARNING(
                '  Scout returned no candidates at all; skipping reconciliation rather than '
                'retiring every target.'))
            return
        if data_service.total_results is not None and data_service.total_results != len(roster):
            self.stdout.write(self.style.WARNING(
                f'  Scout reported {data_service.total_results} candidate(s) but returned '
                f'{len(roster)}; skipping reconciliation rather than acting on a partial list.'))
            return

        roster_rows = {row['objectName']: row for row in roster if row.get('objectName')}

        retired = refreshed = unchanged = 0
        for scout_detail in active_details:
            target = scout_detail.target
            row = roster_rows.get(target.name)
            if row is None:
                for alias in target.aliases.all():
                    row = roster_rows.get(alias.name)
                    if row is not None:
                        break

            if row is None:
                retired += 1
                if dry_run:
                    self.stdout.write(f'  [dry-run] would retire {target.name} (no longer on Scout)')
                else:
                    ScoutDetail.objects.filter(pk=scout_detail.pk).update(active=False)
                    self.stdout.write(f'  {target.name} has left Scout; marked inactive.')
                continue

            # lastRun is Scout's own timestamp for the orbit solution. Unchanged means Scout
            # has not recomputed since the stored snapshot, so there is nothing new to store.
            detail_data = data_service._parse_detail_data(row)
            last_run = detail_data.get('last_run')
            if last_run is not None and scout_detail.last_run is not None and last_run <= scout_detail.last_run:
                unchanged += 1
                continue

            refreshed += 1
            if dry_run:
                self.stdout.write(f'  [dry-run] would refresh {target.name} (Scout has recomputed it)')
                continue
            data_service.store_scout_detail(target, detail_data)

        verb = 'would refresh' if dry_run else 'refreshed'
        retire_verb = 'would retire' if dry_run else 'retired'
        self.stdout.write(self.style.SUCCESS(
            f'  {verb} {refreshed} candidate(s); {retire_verb} {retired} candidate(s); '
            f'{unchanged} unchanged.'))

    def _update_designations(self, dry_run=False):
        """Settle every unsettled Scout candidate the MPC's departure page has an outcome for.

        For a designated object the ``Target`` is renamed to the designation (community
        convention), keeping the trksub as a ``TargetName`` alias. When the designation is
        already held by another target -- two surveys submitted the same body under different
        trksubs, or the object was ingested independently from MPC/JPL SBDB -- the name is not
        copied; ``merged_into`` records which object this submission became, resolvable to
        that target by name or alias. Objects that left without a designation settle with the
        MPC's reason. Active candidates are considered too: an object can be designated while
        Scout still lists it, and reconciliation keeps refreshing it via its trksub alias
        until the roster drops it.

        Candidates are matched against the page by ``Target.name`` and every alias, and
        finally by the designation itself: a target that an admin or another process has
        already renamed to its IAU designation without settling ``mpc_status`` is thereby
        recognized and settled in place instead of being retried as unlisted forever.
        """
        from tom_targets.models import Target, TargetName

        self.stdout.write('Checking the MPC Previous NEOCP Objects page for outcomes...')
        try:
            departures = _fetch_mpc_departures()
        except Exception as exc:
            self.stdout.write(self.style.WARNING(f'  Could not fetch the MPC departure page: {exc}'))
            return
        if not departures:
            self.stdout.write(self.style.WARNING('  No departures parsed from the MPC page.'))
            return

        now = datetime.now(timezone.utc)
        pending = list(
            ScoutDetail.objects.filter(mpc_status__isnull=True)
            .select_related('target').prefetch_related('target__aliases')
        )

        # The departure records are keyed by trksub. A target that an admin or another
        # pipeline has already renamed to its IAU designation (without settling
        # ``mpc_status``) can only be recognized the other way round, by the designation.
        by_designation = {
            row['designation']: row for row in departures.values() if row['designation']
        }

        renamed = settled = linked = 0
        unlisted = []
        for scout_detail in pending:
            target = scout_detail.target
            names = [target.name] + [alias.name for alias in target.aliases.all()]
            row = next((departures[name] for name in names if name in departures), None)
            if row is None:
                row = next((by_designation[name] for name in names if name in by_designation), None)
            if row is None:
                if not scout_detail.active:
                    unlisted.append(target.name)
                continue

            if row['status'] != 'designated':
                settled += 1
                if dry_run:
                    self.stdout.write(f'  [dry-run] would settle {target.name} as {row["status"]}')
                else:
                    self._settle(scout_detail, row, now)
                    self.stdout.write(f'  {target.name} left the NEOCP: {row["status"]}.')
                continue

            iau_desig = row['designation']
            if target.name == iau_desig:
                # Already renamed by an admin or another pipeline; the outcome just needs
                # recording -- the rename path would alias the target to its own name.
                settled += 1
                if dry_run:
                    self.stdout.write(
                        f'  [dry-run] would settle {target.name} (already carries its designation)')
                else:
                    self._settle(scout_detail, row, now)
                    self.stdout.write(f'  {target.name} already carries its designation; outcome recorded.')
                continue

            claimed = (
                Target.objects.filter(name=iau_desig).exclude(pk=target.pk).exists()
                or TargetName.objects.filter(name=iau_desig).exclude(target=target).exists()
            )
            if claimed:
                # Another target already is (or aliases) this object; link rather than
                # copying the name -- names live on Target/TargetName only.
                row = {**row, 'merged_into': row['merged_into'] or iau_desig}
                settled += 1
                linked += 1
                if dry_run:
                    self.stdout.write(
                        f'  [dry-run] would link {target.name} to {row["merged_into"]} '
                        f'({iau_desig} is already held)')
                else:
                    self._settle(scout_detail, row, now)
                    self.stdout.write(
                        f'  {target.name} became {iau_desig}, already held elsewhere; linked '
                        f'via merged_into.')
                continue

            settled += 1
            renamed += 1
            if dry_run:
                self.stdout.write(
                    f'  [dry-run] would rename {target.name} -> {iau_desig} '
                    f'(keeping {target.name} as alias)')
                continue
            old_name = target.name
            try:
                with transaction.atomic():
                    target.name = iau_desig
                    target.save(update_fields=['name', 'modified'])
                    # An older updatescout recorded the designation as an alias without
                    # renaming; reuse that row as the trksub alias instead of adding one.
                    own_alias = TargetName.objects.filter(target=target, name=iau_desig).first()
                    if own_alias:
                        own_alias.name = old_name
                        own_alias.save()
                    else:
                        TargetName.objects.get_or_create(target=target, name=old_name)
                    self._settle(scout_detail, row, now)
            except IntegrityError as exc:
                self.stdout.write(self.style.WARNING(
                    f'  Could not rename {old_name} -> {iau_desig}: {exc}'))
                settled -= 1
                renamed -= 1
                continue
            self.stdout.write(f'  {old_name} -> renamed to {iau_desig} (kept {old_name} as alias)')

        self._alias_retired_duplicates(departures, dry_run=dry_run)

        if unlisted and not dry_run:
            # Stamp the attempt: these departed before the page's window opened, or the page
            # hasn't caught up yet. There is nothing to look up individually -- the page is
            # the candidate-level source -- so they are simply retried on later runs.
            ScoutDetail.objects.filter(target__name__in=unlisted).update(mpc_status_checked=now)
        if unlisted:
            preview = ', '.join(unlisted[:5]) + ('...' if len(unlisted) > 5 else '')
            self.stdout.write(
                f'  {len(unlisted)} departed object(s) have no outcome on the page yet '
                f'({preview}); will retry on later runs.')

        if settled:
            verb = 'would settle' if dry_run else 'settled'
            self.stdout.write(self.style.SUCCESS(
                f'  {verb} {settled} candidate(s): {renamed} renamed to their designation, '
                f'{linked} linked to an already-held object, '
                f'{settled - renamed - linked} recorded as lost/dne/artificial.'))
        else:
            self.stdout.write('  No new outcomes to record.')

    def _settle(self, scout_detail, row, now):
        """Record a candidate's final outcome; a non-null ``mpc_status`` is a terminal state
        and takes the object out of every future designation pass."""
        ScoutDetail.objects.filter(pk=scout_detail.pk).update(
            mpc_status=row['status'],
            mpc_reference=row.get('reference'),
            merged_into=row.get('merged_into'),
            mpc_status_checked=now,
        )

    def _alias_retired_duplicates(self, departures, dry_run=False):
        """Record retired duplicate trksubs as aliases on the surviving target we hold.

        When the MPC identifies two submissions as one body and we only ever ingested the
        survivor, the retired trksub would otherwise be a dead end: the MPEC cross-lists
        both, but a search here would find nothing. Adding it as an alias keeps both
        identifiers resolvable. Duplicates we *do* hold as targets are handled in the main
        pass (linked via ``merged_into``), not here.
        """
        from tom_targets.models import Target, TargetName

        aliased = 0
        for trksub, row in departures.items():
            if not row['merged_into'] or row['status'] != 'designated':
                continue
            if (Target.objects.filter(name=trksub).exists()
                    or TargetName.objects.filter(name=trksub).exists()):
                continue
            survivor = (
                Target.objects.filter(name=row['designation'], scout_detail__isnull=False).first()
                or Target.objects.filter(name=row['merged_into'], scout_detail__isnull=False).first()
            )
            if survivor is None:
                alias = (TargetName.objects.filter(name=row['merged_into'],
                                                   target__scout_detail__isnull=False)
                         .select_related('target').first())
                survivor = alias.target if alias else None
            if survivor is None:
                continue
            aliased += 1
            if dry_run:
                self.stdout.write(f'  [dry-run] would alias retired duplicate {trksub} '
                                  f'onto {survivor.name}')
                continue
            try:
                TargetName.objects.get_or_create(target=survivor, name=trksub)
            except IntegrityError as exc:
                self.stdout.write(self.style.WARNING(f'  Could not alias {trksub}: {exc}'))
                continue
            self.stdout.write(f'  retired duplicate {trksub} kept as an alias of {survivor.name}.')
        if aliased:
            verb = 'would record' if dry_run else 'recorded'
            self.stdout.write(f'  {verb} {aliased} retired duplicate alias(es).')
