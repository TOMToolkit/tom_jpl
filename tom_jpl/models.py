from django.db import models
from django.forms.models import model_to_dict

from tom_targets.models import BaseTarget

# Fields ignored by the history change-detection: identifiers/timestamps, plus the
# ephemeris quantities (ra, dec, vmag, rate, t_ephem) which change on every Scout run
# by definition and would drown out the interesting orbit-quality changes.
HISTORY_UNTRACKED_FIELDS = {'id', 'target', 'last_run', 'ra', 'dec', 'vmag', 'rate', 't_ephem'}

# Tracked fields shown as columns in the Scout History table, in display order.
HISTORY_DISPLAY_FIELDS = ['num_obs', 'neo_score', 'neo1km_score', 'pha_score', 'ieo_score',
                          'geocentric_score', 'impact_rating', 'ca_dist', 'arc', 'rms',
                          'uncertainty', 'uncertainty_p1']

# Known outcomes of an object leaving the NEOCP, as parsed from the MPC's "Previous NEOCP
# Objects" page. 'designated' is the happy path (a 'DESIG = trksub' line on the page); the
# rest are the page's phrasings for leaving without a designation, under MPC's own codes.
# The 'na'/'ns' distinction is MPC's published artificial-satellite policy: a tracklet whose
# motion matches the two-line element set of a known artificial object is removed as 'na',
# while one that can't be matched but has a geocentric score > 10 is only flagged 'ns'.
#
# An object retired because the MPC identified it with another submission of the same body
# gets no code of its own: the departure parser follows the identification chain to the
# surviving object and records *its* outcome here, keeping the pairing in
# ScoutDetail.merged_into.
MPC_STATUSES = (
    ('designated', 'received an IAU designation'),
    ('lost', 'was not confirmed'),
    ('dne', 'does not exist'),
    ('na', 'not a minor planet (matched to a known artificial satellite)'),
    ('ns', 'suspected artificial (geocentric orbit, no match to a known satellite)'),
)


class BaseScoutDetail(models.Model):
    """Abstract base holding the Scout snapshot fields shared by the current-state
    (:class:`ScoutDetail`) and append-only (:class:`ScoutDetailHistory`) models.

    Being ``abstract`` it creates no table of its own; its fields are copied into
    each concrete subclass, so the field list is maintained in exactly one place.
    """

    class ScoutImpactRating(models.IntegerChoices):
        NEGLIGIBLE = 0, 'Negligible'
        SMALL = 1, 'Small'
        MODEST = 2, 'Modest'
        MODERATE = 3, 'Moderate'
        ELEVATED = 4, 'Elevated'

    num_obs = models.IntegerField(null=True, blank=True, help_text='Number of observations')
    neo_score = models.IntegerField(null=True, blank=True, verbose_name='NEO Score',
                                    help_text='NEO digest score (0..100)')
    neo1km_score = models.IntegerField(null=True, blank=True, verbose_name='NEO >1km Score',
                                       help_text='NEO >1km digest score (0..100)')
    pha_score = models.IntegerField(null=True, blank=True, verbose_name='PHA Score',
                                    help_text='PHA digest score (0..100)')
    ieo_score = models.IntegerField(null=True, blank=True, verbose_name='IEO Score',
                                    help_text='IEO digest score (0..100)')
    geocentric_score = models.IntegerField(null=True, blank=True, verbose_name='Geocentric Score',
                                           help_text='Geocentric digest score (0..100)')
    impact_rating = models.IntegerField(null=True, blank=True, choices=ScoutImpactRating.choices,
                                        help_text='Impact rating (0=negligible, 1=small, 2=modest, 3=moderate, '
                                        '4=elevated)')
    ca_dist = models.FloatField(null=True, blank=True, help_text='Close approach distance (lunar distances)')
    arc = models.FloatField(null=True, blank=True, help_text='Arc length (days)')
    rms = models.FloatField(null=True, blank=True, help_text='RMS of the residuals to the orbit fit (arcsec)')
    uncertainty = models.FloatField(null=True, blank=True, verbose_name='Uncertainty',
                                    help_text='1-sigma plane-of-sky positional uncertainty (arcmin)')
    uncertainty_p1 = models.FloatField(null=True, blank=True, verbose_name='Uncertainty at +1 day',
                                       help_text='1-sigma plane-of-sky positional uncertainty at +1 day (arcmin)')
    vmag = models.FloatField(null=True, blank=True, verbose_name='V Magnitude',
                             help_text='Predicted V magnitude')
    rate = models.FloatField(null=True, blank=True, verbose_name='Sky motion',
                             help_text='Plane-of-sky motion rate (arcsec/min)')
    ra = models.FloatField(null=True, blank=True, verbose_name='Right Ascension',
                           help_text='Right ascension at the ephemeris time (degrees)')
    dec = models.FloatField(null=True, blank=True, verbose_name='Declination',
                            help_text='Declination at the ephemeris time (degrees)')
    t_ephem = models.DateTimeField(null=True, blank=True, verbose_name='Ephemeris time',
                                   help_text='Time the ephemeris quantities (ra, dec, vmag, rate) are valid for')
    last_run = models.DateTimeField(null=True, blank=True, help_text='Last time the data was updated from Scout')

    class Meta:
        abstract = True

    def as_dict(self):
        return model_to_dict(self, fields=[field.name for field in self._meta.fields])


class ScoutDetail(BaseScoutDetail):
    """Current Scout state for a target (one row per target)."""
    target = models.OneToOneField(BaseTarget, on_delete=models.CASCADE, related_name='scout_detail')
    active = models.BooleanField(default=True,
                                 help_text='Whether the object is still on the Scout list (False once it has left, '
                                 'e.g. designated, removed, or impacted)')
    mpc_status = models.CharField(max_length=12, null=True, blank=True, choices=MPC_STATUSES,
                                  help_text="Why the object left the NEOCP, per the MPC's Previous NEOCP Objects "
                                  'page. Null until that outcome has been established; once set, the object is '
                                  'settled and is no longer looked up against the MPC.')
    mpc_status_checked = models.DateTimeField(null=True, blank=True,
                                              help_text='UTC time when the MPC was last consulted about this object. '
                                              'Recorded even when no outcome was found, so a candidate the MPC has '
                                              'no row for yet is visibly pending rather than silently stale.')
    mpc_reference = models.CharField(max_length=32, null=True, blank=True,
                                     help_text="Publication announcing the outcome recorded in mpc_status (e.g. "
                                     "'MPEC 2026-Q53'), as given by the MPC Previous NEOCP Objects page.")
    merged_into = models.CharField(max_length=64, null=True, blank=True,
                                   help_text='Name or alias of the object this submission became, when its identity '
                                   'lives on another Target: the MPC identified two submissions as the same body, or '
                                   'the designation was already held (e.g. ingested independently from MPC/JPL). The '
                                   'designation itself is never copied here -- names live only on Target/TargetName; '
                                   'resolve with resolve_merged_into().')

    class Meta:
        verbose_name = 'Scout Detail'

    def __str__(self):
        return self.target.name + f' (Impact rating: {self.impact_rating})'

    def resolve_merged_into(self):
        """The Target this submission was folded into, resolved from ``merged_into`` by
        name or alias; None when unset or when no such target exists (e.g. the survivor
        was never ingested)."""
        if not self.merged_into:
            return None
        from tom_targets.models import Target, TargetName
        target = Target.objects.filter(name=self.merged_into).first()
        if target:
            return target
        alias = TargetName.objects.filter(name=self.merged_into).select_related('target').first()
        return alias.target if alias else None


class ScoutDetailHistory(BaseScoutDetail):
    """Append-only record of each Scout recomputation for a target; deduped on (target, last_run)."""
    target = models.ForeignKey(BaseTarget, on_delete=models.CASCADE, related_name='scout_detail_history')

    class Meta:
        verbose_name = 'Scout Detail History'
        verbose_name_plural = 'Scout Detail Histories'
        unique_together = ('target', 'last_run')
        ordering = ['target', 'last_run']

    def __str__(self):
        return self.target.name + f' (lastRun: {self.last_run})'

    def changes_from(self, previous):
        """Return the tracked fields that differ from `previous` as {field_name: (old, new)}.

        Fields in HISTORY_UNTRACKED_FIELDS are ignored. Returns an empty dict when
        `previous` is None (i.e. this is the first Scout record for the target).
        """
        if previous is None:
            return {}
        old = previous.as_dict()
        return {field: (old[field], value) for field, value in self.as_dict().items()
                if field not in HISTORY_UNTRACKED_FIELDS and value != old[field]}

    @classmethod
    def annotated_history(cls, target):
        """Return the target's history rows newest first, each annotated with a `changes` dict.

        `changes` holds the row-to-row differences (see :meth:`changes_from`) relative to the
        chronologically previous Scout run.
        """
        rows = list(cls.objects.filter(target=target).order_by('last_run'))
        previous = None
        for row in rows:
            row.changes = row.changes_from(previous)
            previous = row
        rows.reverse()
        return rows
