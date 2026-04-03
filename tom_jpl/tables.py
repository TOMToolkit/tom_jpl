import django_tables2 as tables

from tom_common.htmx_table import HTMXTable  # HTMXTable is a django_tables2.Table subclass
from tom_jpl.models import ScoutDetail


class ScoutDetailTable(HTMXTable):

    # linkify makes the entry in the "target_name" column a link-through to the
    # Target model detail page from the ScoutDetail model.
    target_name = tables.Column(
        accessor='target.name',
        verbose_name='Target',
        linkify=lambda record: record.target.get_absolute_url(),
        attrs={"a": {"hx-boost": "false"}}
    )

    ca_dist = tables.Column(verbose_name='CA Dist (LD)')

    class Meta(HTMXTable.Meta):
        model = ScoutDetail
        fields = ['selection', 'target_name', 'num_obs', 'neo_score', 'neo1km_score', 'pha_score', 'ieo_score',
                  'geocentric_score', 'impact_rating', 'ca_dist']
