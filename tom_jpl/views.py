from django_filters.views import FilterView

from tom_common.htmx_table import HTMXTableViewMixin
from tom_jpl.filters import ScoutDetailFilterSet
from tom_jpl.models import ScoutDetail
from tom_jpl.tables import ScoutDetailTable


class ScoutDetailListView(HTMXTableViewMixin, FilterView):
    template_name = 'tom_jpl/scoutdetail_list.html'
    model = ScoutDetail
    table_class = ScoutDetailTable
    filterset_class = ScoutDetailFilterSet
    paginate_by = 20
    ordering = ['-last_run']
