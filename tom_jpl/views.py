from django.views.generic.list import ListView

from tom_common.htmx_table import HTMXTableViewMixin
from tom_jpl.models import ScoutDetail
from tom_jpl.tables import ScoutDetailTable


class ScoutDetailListView(HTMXTableViewMixin, ListView):
    template_name = 'tom_jpl/scoutdetail_list.html'
    model = ScoutDetail
    table_class = ScoutDetailTable
    paginate_by = 20
    ordering = ['-last_run']
