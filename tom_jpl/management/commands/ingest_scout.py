"""Re-run a saved broad Scout query and reconcile the stored candidate population.

The interactive Scout flow only creates targets the user hand-picks, so it never
sees the full Scout list and cannot tell when an object *leaves* Scout. This command
re-runs a saved, deliberately-broad ``DataServiceQuery`` (minimal cuts) against the
whole current Scout list, upserting every match (``ScoutDetail`` + history row), then
marks any previously-active candidate that was *not* in the response as inactive
(it has been designated, removed, or impacted), and finally queries the MPC
"Previous NEOCP Objects" page to record official IAU designations as target aliases.
Suitable for running from cron.
"""

import requests
from html.parser import HTMLParser

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.http import HttpRequest
from django.utils import timezone

from tom_dataservices.dataservices import get_data_service_class
from tom_dataservices.models import DataServiceQuery

from tom_jpl.models import ScoutDetail

_MPC_PREV_DES_URL = "https://minorplanetcenter.net/mpcops/neocp/neocp_prev_des/"


def _fetch_mpc_prev_designations():
    """Fetch the 100 most-recent NEOCP-to-official-designation mappings from the MPC.

    Parses the HTML table at :data:`_MPC_PREV_DES_URL` and returns a dict
    ``{trksub: iau_desig}`` for objects that received a real IAU provisional
    designation.  Objects with status ``lost``, ``dne``, etc. are excluded.
    Network or parse failures propagate as exceptions; callers should catch and
    warn rather than abort the sweep.
    """

    class _TableParser(HTMLParser):
        """Collect text content of every <td> cell, grouped by <tr>."""

        def __init__(self):
            super().__init__()
            self._in_cell = False
            self._row = []
            self.rows = []

        def handle_starttag(self, tag, attrs):
            if tag == "tr":
                self._row = []
            elif tag == "td":
                self._in_cell = True
                self._row.append("")

        def handle_endtag(self, tag):
            if tag == "td":
                self._in_cell = False
            elif tag == "tr" and self._row:
                self.rows.append(list(self._row))
                self._row = []

        def handle_data(self, data):
            if self._in_cell and self._row:
                self._row[-1] += data

    response = requests.get(_MPC_PREV_DES_URL, timeout=30)
    response.raise_for_status()

    parser = _TableParser()
    parser.feed(response.text)

    mapping = {}
    for row in parser.rows:
        if len(row) < 2:
            continue
        trksub = row[0].strip()
        iau_desig = row[1].strip()
        # Skip header rows and rows where no real designation was assigned
        if not trksub or not iau_desig:
            continue
        if iau_desig in ("—", "–", "-", "None", "iau_desig"):
            continue
        mapping[trksub] = iau_desig
    return mapping


class _SilentMessages:
    """Minimal message store so ``DataService.to_target``'s ``messages`` calls work headless.

    The base ``to_target`` emits a ``messages.warning`` on the IntegrityError path (an
    already-existing target) *without* guarding on ``request``, so a request with a
    working message store is required when re-ingesting existing objects.
    """

    def add(self, level, message, extra_tags=""):
        pass

    def __iter__(self):
        return iter(())

    def __len__(self):
        return 0


class Command(BaseCommand):
    help = (
        "Re-run a saved broad Scout DataServiceQuery, ingesting every matching candidate "
        "(ScoutDetail + history) and marking candidates that have left Scout inactive."
    )

    def add_arguments(self, parser):
        selector = parser.add_mutually_exclusive_group(required=True)
        selector.add_argument(
            "--query-name", help="Name of the saved Scout DataServiceQuery to run."
        )
        selector.add_argument(
            "--query-id",
            type=int,
            help="Primary key of the saved Scout DataServiceQuery to run.",
        )
        parser.add_argument(
            "--no-sweep",
            action="store_true",
            help="Do not mark unseen active candidates as inactive.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would happen without writing to the database.",
        )

    def handle(self, *args, **options):
        query = self._get_query(options)
        if query.data_service != "Scout":
            raise CommandError(
                f"Query '{query.name}' targets data service '{query.data_service}', not 'Scout'."
            )

        data_service = get_data_service_class(query.data_service)()
        self.stdout.write(
            f"Running saved Scout query '{query.name}' (id={query.pk})..."
        )
        results = data_service.query_targets(query.parameters)
        self.stdout.write(
            f"  Scout returned {len(results)} candidate(s) passing the query cuts."
        )

        if options["dry_run"]:
            for result in results:
                self.stdout.write(
                    f"  [dry-run] would ingest {result.get('objectName')}"
                )
            self.stdout.write(self.style.WARNING("Dry run: no changes written."))
            self._update_designations(dry_run=True)
            return

        request = self._build_request()
        seen_ids = set()
        for target_data in results:
            target = data_service.to_target(target_data, request=request)
            if target is not None:
                seen_ids.add(target.pk)
        self.stdout.write(
            self.style.SUCCESS(f"  Ingested/updated {len(seen_ids)} target(s).")
        )

        if not options["no_sweep"]:
            self._sweep(seen_ids)

        self._update_designations(dry_run=False)

        query.last_run = timezone.now()
        query.save(update_fields=["last_run", "modified"])
        self.stdout.write(
            self.style.SUCCESS(
                f"Done. last_run updated to {query.last_run.isoformat()}."
            )
        )

    def _update_designations(self, dry_run=False):
        """Query the MPC 'Previous NEOCP Objects' page and add IAU designations as aliases.

        When a Scout candidate leaves the NEOCP it either receives an official IAU
        provisional designation (e.g. ``A11Df9S`` → ``2026 LX``) or is marked lost/DNE.
        The MPC page maps the original tracking-submission code (``trksub``) to the new
        designation.  For each Scout target whose ``Target.name`` appears in that mapping,
        this adds the IAU designation as a :class:`~tom_targets.models.TargetName` alias
        so the official name becomes searchable alongside the Scout provisional.
        """
        from tom_targets.models import Target, TargetName

        self.stdout.write("Checking MPC for new IAU designations...")
        try:
            designation_map = _fetch_mpc_prev_designations()
        except Exception as exc:
            self.stdout.write(
                self.style.WARNING(f"  Could not fetch MPC designations: {exc}")
            )
            return

        if not designation_map:
            self.stdout.write("  No designations returned from MPC.")
            return

        # Only consider targets that were ever ingested via Scout
        scout_targets = Target.objects.filter(
            name__in=designation_map.keys(),
            scoutdetail__isnull=False,
        )

        updated = 0
        for target in scout_targets:
            iau_desig = designation_map[target.name]
            if dry_run:
                exists = TargetName.objects.filter(name=iau_desig).exists()
                if not exists:
                    self.stdout.write(
                        f"  [dry-run] would alias {target.name} → {iau_desig}"
                    )
                    updated += 1
            else:
                # Use name as the lookup key so two targets never compete for the same alias
                _obj, created = TargetName.objects.get_or_create(
                    name=iau_desig,
                    defaults={"target": target},
                )
                if created:
                    self.stdout.write(f"  {target.name} → aliased as {iau_desig}")
                    updated += 1

        if updated:
            verb = "would add" if dry_run else "added"
            self.stdout.write(
                self.style.SUCCESS(f"  {verb} {updated} new IAU designation alias(es).")
            )
        else:
            self.stdout.write("  No new designations to record.")

    def _sweep(self, seen_ids):
        """Mark active candidates absent from this run's results as inactive."""
        if not seen_ids:
            # A broad query returning nothing almost certainly means a Scout API error;
            # deactivating the entire population on that basis would be wrong.
            self.stdout.write(
                self.style.WARNING(
                    "  No candidates returned; skipping sweep to avoid deactivating everything."
                )
            )
            return
        deactivated = (
            ScoutDetail.objects.filter(active=True)
            .exclude(target_id__in=seen_ids)
            .update(active=False)
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"  Marked {deactivated} candidate(s) that have left Scout inactive."
            )
        )

    def _get_query(self, options):
        try:
            if options.get("query_id") is not None:
                return DataServiceQuery.objects.get(pk=options["query_id"])
            return DataServiceQuery.objects.get(name=options["query_name"])
        except DataServiceQuery.DoesNotExist:
            ident = options.get("query_id") or options.get("query_name")
            raise CommandError(f"No saved DataServiceQuery found matching '{ident}'.")
        except DataServiceQuery.MultipleObjectsReturned:
            raise CommandError(
                f"Multiple saved queries named '{options['query_name']}'. Use --query-id instead."
            )

    def _build_request(self):
        """Build a minimal request so the headless ingest satisfies to_target's request use."""
        request = HttpRequest()
        request.method = "GET"
        user_model = get_user_model()
        user = (
            user_model.objects.filter(is_superuser=True).order_by("pk").first()
            or user_model.objects.order_by("pk").first()
        )
        if user is None:
            from django.contrib.auth.models import AnonymousUser

            user = AnonymousUser()
        request.user = user
        request._messages = _SilentMessages()
        return request
