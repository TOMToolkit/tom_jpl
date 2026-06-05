from django.db import models
from django.forms.models import model_to_dict

from tom_targets.models import BaseTarget


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
        unique_together = ('target', 'last_run')
        ordering = ['target', 'last_run']

    def __str__(self):
        return self.target.name + f' (lastRun: {self.last_run})'
