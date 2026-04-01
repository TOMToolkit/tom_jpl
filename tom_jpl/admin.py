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

        def has_output(self) -> bool:
            return True

        def expected_parameters(self) -> list[str]:
            return [f"{field_name}__gte", f"{field_name}__lte"]

        def lookups(self, request, model_admin):
            # Required by SimpleListFilter but unused — the template handles input
            return (("dummy", "dummy"),)

        def queryset(self, request, queryset):
            params = request.GET
            gte = params.get(f"{field_name}__gte")
            lte = params.get(f"{field_name}__lte")
            try:
                if gte:
                    queryset = queryset.filter(**{f"{field_name}__gte": int(gte)})
                if lte:
                    queryset = queryset.filter(**{f"{field_name}__lte": int(lte)})
            except ValueError:
                pass
            return queryset

    return IntegerGtLtFilter


NEOScoreFilter = make_score_filter('neo_score', _('NEO Score'))
PHAScoreFilter = make_score_filter('pha_score', _('PHA Score'))
GeocentricScoreFilter = make_score_filter('geocentric_score', _('Geocentric Score'))


@admin.register(ScoutDetail)
class ScoutDetailAdmin(admin.ModelAdmin):
    list_display = ('target', 'neo_score', 'pha_score', 'geocentric_score', 'impact_rating',
                    'ca_dist', 'uncertainty', 'uncertainty_p1', 'last_run')
    search_fields = ('target__name', )
    list_filter = [NEOScoreFilter, PHAScoreFilter, GeocentricScoreFilter, 'impact_rating']
