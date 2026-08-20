"""Reconcile stored Scout candidates and record new IAU designations.

Ingesting *new* Scout candidates is the job of tom_dataservices' ``rundataquery``
management command run against a saved Scout ``DataServiceQuery``. This command covers
the rest of an already-ingested candidate's lifecycle:

- Reconciliation: for every target currently marked ``active`` on its ``ScoutDetail``,
  re-query Scout for that *specific* object (via ``build_query_parameters_from_target``)
  and refresh its ``ScoutDetail``/history if it's still there, or mark it inactive if
  Scout no longer returns it. Checking each target individually (rather than diffing
  against the results of one broad query) means a target that merely stops matching some
  other query's cuts is never mistaken for one that has actually left Scout.
- Designation lookup: query the MPC "Previous NEOCP Objects" page to find which Scout
  candidates have received an official IAU provisional designation (e.g. ``A11Df9S`` ->
  ``2026 LX``), and promote that designation to the canonical ``Target.name``, keeping the
  original Scout provisional as a ``TargetName`` alias.

Suitable for running from cron.
"""

import re
import requests
import time
from html.parser import HTMLParser

from django.core.management.base import BaseCommand
from django.db import IntegrityError

from tom_dataservices.dataservices import get_data_service_class

from tom_jpl.models import ScoutDetail

_MPC_PREV_DES_URL = 'https://minorplanetcenter.net/mpcops/neocp/neocp_prev_des/'


class _TableParser(HTMLParser):
    """Collect text content of every <td> cell, grouped by <tr>."""

    def __init__(self):
        super().__init__()
        self._in_cell = False
        self._row = []
        self.rows = []

    def handle_starttag(self, tag, attrs):
        if tag == 'tr':
            self._row = []
        elif tag == 'td':
            self._in_cell = True
            self._row.append('')

    def handle_endtag(self, tag):
        if tag == 'td':
            self._in_cell = False
        elif tag == 'tr' and self._row:
            self.rows.append(list(self._row))
            self._row = []

    def handle_data(self, data):
        if self._in_cell and self._row:
            self._row[-1] += data


def _parse_designation_rows(html):
    """Parse an MPC 'Previous NEOCP Objects' table (full page or single-result AJAX
    fragment -- both share the same 5-column row shape) into a dict ``{trksub:
    iau_desig}`` for rows that represent a real IAU provisional designation. Objects
    with status ``lost``, ``dne``, etc. are excluded. Shared by the bulk rolling-table
    fetch and the individual-object fallback lookup.
    """
    parser = _TableParser()
    parser.feed(html)

    mapping = {}
    for row in parser.rows:
        if len(row) < 3:
            continue
        trksub = row[0].strip()
        iau_desig = row[1].strip()
        status = row[2].strip()
        # The 'status' column is the page's own authoritative signal for why an object left
        # the NEOCP: empty/'None' means it was resolved with a real designation, anything else
        # (lost/dne/ns/na/...) means it wasn't. Whitelisting the "clean" value rather than
        # blacklisting known-bad ones means a status value we haven't seen before is excluded
        # by default, instead of silently falling through.
        if status not in ('', 'None'):
            continue
        # Skip header rows. A "clean" status doesn't guarantee a real designation was assigned
        # (e.g. an object can be neither lost/dne nor yet-designated), so iau_desig itself still
        # needs its own placeholder check.
        if not trksub or not iau_desig:
            continue
        if iau_desig in ('—', '–', '-', 'None', 'iau_desig'):
            continue
        mapping[trksub] = iau_desig
    return mapping


def _fetch_mpc_prev_designations():
    """Fetch the 100 most-recent NEOCP-to-official-designation mappings from the MPC.

    Parses the HTML table at :data:`_MPC_PREV_DES_URL`. Network or parse failures
    propagate as exceptions; callers should catch and warn rather than abort.
    """
    response = requests.get(_MPC_PREV_DES_URL, timeout=30)
    response.raise_for_status()
    return _parse_designation_rows(response.text)


def _mpc_session_and_csrf_token():
    """Start a session against the MPC previous-designations page and scrape its CSRF
    token, for reuse across multiple individual lookups without re-fetching the page
    (and its cookie) for each one.
    """
    session = requests.Session()
    page = session.get(_MPC_PREV_DES_URL, timeout=30)
    page.raise_for_status()

    match = re.search(r'name=[\'"]csrfmiddlewaretoken[\'"]\s+value=[\'"]([^\'"]+)[\'"]', page.text)
    if not match:
        raise ValueError('Could not find a CSRF token on the MPC previous-designations page.')
    return session, match.group(1)


def _fetch_mpc_prev_designation_for(session, csrf_token, trksub):
    """Look up a single object's designation via the MPC page's own search form.

    This is the fallback for a target not covered by the rolling ~100-entry table
    :func:`_fetch_mpc_prev_designations` scrapes -- e.g. after a FOMO outage longer
    than that window covers. Submits the same AJAX request the page's own "Submit"
    button does (a POST to the page's own URL, since the visible button is JS-driven
    rather than a plain form submit) and returns the IAU designation string, or None
    if the object isn't found or hasn't been resolved yet. Network/parse failures
    propagate; callers should catch per-lookup so one failure doesn't abort the rest.
    """
    response = session.post(
        _MPC_PREV_DES_URL,
        data={'csrfmiddlewaretoken': csrf_token, 'desig': trksub},
        headers={'Referer': _MPC_PREV_DES_URL, 'X-Requested-With': 'XMLHttpRequest'},
        timeout=30,
    )
    response.raise_for_status()
    fragment = response.json().get('neocp_prev_des')
    if not fragment:
        return None
    return _parse_designation_rows(fragment).get(trksub)


class Command(BaseCommand):
    help = (
        'Reconcile active Scout candidates against the live Scout API (retiring ones that have '
        'left) and record new official IAU designations from the MPC as target aliases.'
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
            help='Do not check the MPC for new IAU designations.',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Report what would happen without writing to the database.',
        )
        parser.add_argument(
            '--max-fallback-lookups',
            type=int,
            default=50,
            help='Maximum number of individual MPC lookups to attempt per run (default: 50). '
                 'Bounds how many requests are sent to the MPC server in one invocation; a large '
                 'backlog of unresolved targets is worked down gradually across multiple runs '
                 'rather than all at once.',
        )
        parser.add_argument(
            '--fallback-lookup-delay',
            type=float,
            default=1.0,
            help='Seconds to sleep between individual MPC lookups (default: 1.0), to avoid '
                 'hammering the MPC server.',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        if not options['skip_reconcile']:
            self._reconcile(dry_run=dry_run)
        if not options['skip_designations']:
            self._update_designations(
                dry_run=dry_run,
                max_fallback_lookups=options['max_fallback_lookups'],
                fallback_lookup_delay=options['fallback_lookup_delay'],
            )

    def _reconcile(self, dry_run=False):
        """Re-check every active Scout candidate individually and retire departed ones."""
        data_service = get_data_service_class('Scout')()
        active_details = ScoutDetail.objects.filter(active=True).select_related('target')
        self.stdout.write(f'Reconciling {active_details.count()} active Scout candidate(s)...')

        retired = 0
        refreshed = 0
        for scout_detail in active_details:
            target = scout_detail.target
            query_parameters = data_service.build_query_parameters_from_target(target)
            built_parameters = data_service.build_query_parameters(query_parameters)
            target_data_list = data_service.query_service(built_parameters)

            if not target_data_list:
                retired += 1
                if dry_run:
                    self.stdout.write(f'  [dry-run] would retire {target.name} (no longer on Scout)')
                else:
                    ScoutDetail.objects.filter(pk=scout_detail.pk).update(active=False)
                    self.stdout.write(f'  {target.name} has left Scout; marked inactive.')
                continue

            refreshed += 1
            if not dry_run:
                # Reuses the same per-object parsing query_targets() applies to a fresh candidate,
                # so a reconciliation refresh and a first ingest populate ScoutDetail identically.
                target_data = target_data_list[0]
                target_data['scout_detail'] = data_service._parse_detail_data(target_data)
                data_service.to_target(target_data)

        verb = 'would refresh' if dry_run else 'refreshed'
        retire_verb = 'would retire' if dry_run else 'retired'
        self.stdout.write(
            self.style.SUCCESS(f'  {verb} {refreshed} candidate(s); {retire_verb} {retired} candidate(s).')
        )

    def _update_designations(self, dry_run=False, max_fallback_lookups=50, fallback_lookup_delay=1.0):
        """Promote a Scout target's Target.name to its official IAU designation once assigned.

        The MPC "Previous NEOCP Objects" page maps the original Scout tracking-submission
        code (trksub) to the new official designation once an object leaves the NEOCP. This
        renames Target.name to that designation (matching the community convention of naming
        by the official designation) and keeps the original Scout provisional as a TargetName
        alias, so both remain searchable.
        """
        from tom_targets.models import Target, TargetName

        self.stdout.write('Checking MPC for new IAU designations...')
        try:
            designation_map = _fetch_mpc_prev_designations()
        except Exception as exc:
            self.stdout.write(self.style.WARNING(f'  Could not fetch MPC designations: {exc}'))
            designation_map = {}

        designation_map.update(
            self._fallback_lookup_designations(
                designation_map, max_lookups=max_fallback_lookups, delay=fallback_lookup_delay
            )
        )

        if not designation_map:
            self.stdout.write('  No designations returned from MPC.')
            return

        # Only consider targets that were ever ingested via Scout and still carry their
        # original Scout provisional as Target.name (i.e. haven't already been renamed).
        scout_targets = Target.objects.filter(name__in=designation_map.keys(), scout_detail__isnull=False)

        updated = 0
        for target in scout_targets:
            iau_desig = designation_map[target.name]
            old_name = target.name

            # Another target already claims this designation (or it's already been recorded
            # as an alias elsewhere) -- skip rather than risk a unique-constraint collision.
            already_claimed = (
                Target.objects.filter(name=iau_desig).exclude(pk=target.pk).exists()
                or TargetName.objects.filter(name=iau_desig).exists()
            )
            if already_claimed:
                continue

            if dry_run:
                self.stdout.write(
                    f'  [dry-run] would rename {old_name} -> {iau_desig} (keeping {old_name} as alias)'
                )
                updated += 1
                continue

            try:
                target.name = iau_desig
                target.save(update_fields=['name', 'modified'])
                TargetName.objects.get_or_create(target=target, name=old_name)
            except IntegrityError as exc:
                self.stdout.write(self.style.WARNING(f'  Could not rename {old_name} -> {iau_desig}: {exc}'))
                continue

            self.stdout.write(f'  {old_name} -> renamed to {iau_desig} (kept {old_name} as alias)')
            updated += 1

        if updated:
            verb = 'would rename' if dry_run else 'renamed'
            self.stdout.write(self.style.SUCCESS(f'  {verb} {updated} target(s) to their IAU designation.'))
        else:
            self.stdout.write('  No new designations to record.')

    def _fallback_lookup_designations(self, designation_map, max_lookups=50, delay=1.0):
        """Individually re-check already-inactive Scout targets the rolling table doesn't
        cover -- e.g. after a FOMO outage longer than that ~100-entry window spans.

        A target whose name already looks like a resolved IAU designation (contains a
        space, or a comet-style '/', per IAU designation grammar -- Scout trksubs contain
        neither) was already renamed by a previous run, so it's excluded here rather than
        looked up again on every run forever. Returns a dict to merge into the caller's
        designation_map; never raises, so one failed lookup (or an unreachable MPC page)
        doesn't stop the rest of _update_designations from using the bulk-table results.

        The number of individual lookups is capped at ``max_lookups`` per call, with
        ``delay`` seconds between each, to avoid sending a burst of requests to the MPC
        server -- a large backlog (e.g. the first run after this fallback was added, with
        no cap the whole thing would fire at once) is worked down gradually across
        multiple runs instead.
        """
        from tom_targets.models import Target

        stuck_names = list(
            Target.objects.filter(scout_detail__active=False)
            .exclude(name__in=designation_map.keys())
            .values_list('name', flat=True)
        )
        stuck_names = [name for name in stuck_names if ' ' not in name and '/' not in name]
        if not stuck_names:
            return {}

        total_stuck = len(stuck_names)
        if total_stuck > max_lookups:
            stuck_names = stuck_names[:max_lookups]
            self.stdout.write(
                self.style.WARNING(
                    f'  {total_stuck} target(s) need an individual MPC lookup; only attempting '
                    f'{max_lookups} this run to avoid hammering the MPC server. The rest will be '
                    f'picked up on a later run.'
                )
            )

        self.stdout.write(f'  Falling back to {len(stuck_names)} individual MPC lookup(s)...')
        try:
            session, csrf_token = _mpc_session_and_csrf_token()
        except Exception as exc:
            self.stdout.write(self.style.WARNING(f'  Could not start an MPC lookup session: {exc}'))
            return {}

        found = {}
        for i, name in enumerate(stuck_names):
            if i > 0 and delay:
                time.sleep(delay)
            try:
                iau_desig = _fetch_mpc_prev_designation_for(session, csrf_token, name)
            except Exception as exc:
                self.stdout.write(self.style.WARNING(f'  Could not look up {name} individually: {exc}'))
                continue
            if iau_desig:
                found[name] = iau_desig
        return found
