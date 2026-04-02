from django.db import models

from tom_targets.models import BaseTarget


class ScoutDetail(models.Model):
    class ScoutImpactRating(models.IntegerChoices):
        NEGLIGIBLE = 0, 'Negligible'
        SMALL = 1, 'Small'
        MODEST = 2, 'Modest'
        MODERATE = 3, 'Moderate'
        ELEVATED = 4, 'Elevated'
    target = models.OneToOneField(BaseTarget, on_delete=models.CASCADE, related_name='scout_detail')
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
    last_run = models.DateTimeField(null=True, blank=True, help_text='Last time the data was updated from Scout')

    class Meta:
        verbose_name = 'Scout Detail'

    def __str__(self):
        return self.target.name + f' (Impact rating: {self.impact_rating})'
