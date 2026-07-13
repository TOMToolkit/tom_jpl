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

    class Meta:
        verbose_name = 'Scout Detail'

    def __str__(self):
        return self.target.name + f' (Impact rating: {self.impact_rating})'


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
