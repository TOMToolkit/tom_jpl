import django_filters
from django import forms
from django.db.models import Q

from tom_common.htmx_table import HTMXTableFilterSet, htmx_attributes_delayed, htmx_attributes_instant
from tom_jpl.models import ScoutDetail


class ScoutDetailFilterSet(HTMXTableFilterSet):

    impact_rating = django_filters.ChoiceFilter(
        choices=ScoutDetail.ScoutImpactRating.choices,
        widget=forms.Select(attrs=htmx_attributes_instant)
    )

    target_name = django_filters.CharFilter(
        field_name='target__name',
        lookup_expr='contains',
        widget=forms.TextInput(attrs={**htmx_attributes_delayed, 'placeholder': 'Target Name'})
    )

    # Override the default general search method to:
    # 1) not do an 'icontains' search of the related Target's name field,
    # 2) attempt to interpret the search value as an int to search integer score fields, and
    # 3) attempt to interpret the search value as a float to search the ca_dist field.
    def general_search(self, queryset, name, value):
        if not value:
            return queryset
        query = Q()
        # Search target name via relation (case sensitive)
        query |= Q(target__name__contains=value)
        # Search integer score fields — try to cast value to int first
        try:
            int_value = int(value)
            query |= Q(neo_score=int_value)
            query |= Q(pha_score=int_value)
            query |= Q(geocentric_score=int_value)
            query |= Q(neo1km_score=int_value)
            query |= Q(ieo_score=int_value)
        except ValueError:
            pass
        # Search float ca_dist
        try:
            float_value = float(value)
            query |= Q(ca_dist=float_value)
        except ValueError:
            pass
        return queryset.filter(query)

    class Meta:
        model = ScoutDetail
        fields = ['target_name', 'impact_rating']
        search_fields = ['target__name']
