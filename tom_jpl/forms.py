from typing import List

from django import forms

from tom_dataservices.forms import BaseQueryForm


class ScoutForm(BaseQueryForm):
    tdes = forms.CharField(required=False,
                           label='NEOCP temporary designation')
    neo_score_min = forms.IntegerField(required=False, min_value=0, max_value=100,
                                       label='Minimum NEO digest score (0..100)',
                                       help_text='Minimum NEO digest score (0..100) permissible')
    pha_score_min = forms.IntegerField(required=False, min_value=0, max_value=100,
                                       label='Minimum PHA digest score (0..100)',
                                       help_text='Minimum PHA digest score (0..100) permissible')
    geo_score_max = forms.IntegerField(required=False, initial=5, min_value=0, max_value=100,
                                       label='Maximum GEO digest score (0..100)',
                                       help_text='Maximum Geocentric digest score (0..100) permissible')
    help_text = 'Rating to character the chances of an Earth impact '
    help_text += '(0=negligible, 1=small, 2=modest, 3=moderate, 4=elevated)'
    impact_rating_min = forms.IntegerField(required=False, min_value=0, max_value=4,
                                           label='Minimum impact rating (0..4)',
                                           help_text=help_text)
    ca_dist_min = forms.FloatField(required=False,
                                   label='Minimum CA distance (LD)',
                                   help_text='Minimum close approach distance (lunar distances)')
    pos_unc_min = forms.FloatField(required=False,
                                   label='Minimum positional uncertainty (arcmin)')
    pos_unc_max = forms.FloatField(required=False,
                                   label='Maximum positional uncertainty (arcmin)')

    def simple_fields(self) -> List[str]:
        return ['tdes', 'neo_score_min']
