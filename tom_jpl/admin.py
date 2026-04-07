from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from tom_jpl.models import ScoutDetail


def make_score_filter(field_name, filter_title):
    """
    Factory function that creates a Django admin SimpleListFilter for filtering
    an IntegerField by a minimum and/or maximum value.

    The filter renders a custom template with Min (>=) and Max (<=) number inputs
    rather than the standard list of discrete choices.

    Args:
        field_name (str): The model field name to filter on (e.g. 'neo_score').
                          Also used to construct the URL query params
                          (e.g. 'neo_score__gte', 'neo_score__lte').
        filter_title (str or lazy string): The human-readable title displayed
                          in the admin sidebar (e.g. _('NEO Score')).

    Returns:
        A SimpleListFilter subclass configured for the given field.

    Usage:
        NEOScoreFilter = make_score_filter('neo_score', _('NEO Score'))

        @admin.register(MyModel)
        class MyModelAdmin(admin.ModelAdmin):
            list_filter = [NEOScoreFilter]
    """
    class IntegerGtLtFilter(admin.SimpleListFilter):
        title = filter_title
        parameter_name = field_name
        template = "admin/integer_gt_lt_filter.html"

        def __init__(self, request, params, model, model_admin):
            super().__init__(request, params, model, model_admin)
            for param in self.expected_parameters():
                if param in params:
                    # extract last value from list in QueryDict
                    self.used_parameters[param] = params.pop(param)[-1]

        def value(self):
            gte = self.used_parameters.get(f"{field_name}__gte")
            lte = self.used_parameters.get(f"{field_name}__lte")
            return gte or lte

        def has_output(self) -> bool:
            return True

        def expected_parameters(self) -> list[str]:
            return [f"{field_name}__gte", f"{field_name}__lte"]

        def lookups(self, request, model_admin):
            # Required by SimpleListFilter but unused — the template handles input
            return (("dummy", "dummy"),)

        def queryset(self, request, queryset):
            gte = self.used_parameters.get(f"{field_name}__gte")
            lte = self.used_parameters.get(f"{field_name}__lte")
            try:
                if gte:
                    queryset = queryset.filter(**{f"{field_name}__gte": int(gte)})
                if lte:
                    queryset = queryset.filter(**{f"{field_name}__lte": int(lte)})
            except ValueError:
                pass
            return queryset

    return IntegerGtLtFilter


def make_choice_filter(field_name, filter_title, choices):
    """
    Factory function that creates a Django admin SimpleListFilter for filtering
    a field by a set of discrete choices, while preserving other active filters
    (including make_score_filter params) when a choice link is clicked.

    Args:
        field_name (str): The model field name to filter on (e.g. 'impact_rating').
        filter_title (str or lazy string): The human-readable title displayed
                          in the admin sidebar.
        choices (list of tuples): List of (value, label) pairs e.g.
                          [(0, _('Negligible')),
                           (1, _('Small')),
                           (2, _('Modest')),
                           (3, _('Moderate')),
                           (4, _('Elevated'))]

    Returns:
        A SimpleListFilter subclass configured for the given field.

    Usage:
        ImpactRatingFilter = make_choice_filter('impact_rating', _('Impact Rating'), [
            (0, _('Negligible')),
            (1, _('Small')),
            (2, _('Modest')),
            (3, _('Moderate')),
            (4, _('Elevated'))
        ])
    """
    class ChoiceFilter(admin.SimpleListFilter):
        title = filter_title
        parameter_name = field_name
        template = "admin/choice_filter.html"

        def lookups(self, request, model_admin):
            return choices

        def queryset(self, request, queryset):
            if self.value():
                return queryset.filter(**{field_name: self.value()})
            return queryset

    return ChoiceFilter


NEOScoreFilter = make_score_filter('neo_score', _('NEO Score'))
PHAScoreFilter = make_score_filter('pha_score', _('PHA Score'))
GeocentricScoreFilter = make_score_filter('geocentric_score', _('Geocentric Score'))
ImpactRatingFilter = make_choice_filter('impact_rating', _('Impact Rating'), [
    (0, _('Negligible')),
    (1, _('Small')),
    (2, _('Modest')),
    (3, _('Moderate')),
    (4, _('Elevated'))
])


@admin.register(ScoutDetail)
class ScoutDetailAdmin(admin.ModelAdmin):
    list_display = ('target', 'neo_score', 'pha_score', 'geocentric_score', 'impact_rating',
                    'ca_dist', 'uncertainty', 'uncertainty_p1', 'last_run')
    search_fields = ('target__name', )
    list_filter = [NEOScoreFilter, PHAScoreFilter, GeocentricScoreFilter, ImpactRatingFilter]
