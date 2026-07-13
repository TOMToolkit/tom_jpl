from django import template
from django.core.exceptions import FieldDoesNotExist

from tom_jpl.models import HISTORY_DISPLAY_FIELDS, ScoutDetail, ScoutDetailHistory

register = template.Library()

# Number of Scout runs with changes shown in the compact log on the Scout Details tab.
RECENT_CHANGES_LIMIT = 5


@register.inclusion_tag('tom_jpl/partials/scoutdetails_list.html', takes_context=True)
def tab_context(context):
    """
    Returns the ScoutDetails for the specific Target as context for the Target tab
      for rendering into the template.

    Also includes `recent_changes`: the most recent history rows (newest first) whose
    tracked fields changed relative to the previous Scout run, for the compact
    "Recent changes" log.
    """

    target = context.get('target')
    try:
        scoutdetail = ScoutDetail.objects.get(target=target)
    except ScoutDetail.DoesNotExist:
        scoutdetail = {}
    recent_changes = [row for row in ScoutDetailHistory.annotated_history(target)
                      if row.changes][:RECENT_CHANGES_LIMIT]
    context = {'scoutdetail': scoutdetail, 'recent_changes': recent_changes}
    return context


def history_tab_context(context):
    """
    Returns the full ScoutDetailHistory for the specific Target (newest first, each row
    annotated with its row-to-row `changes`) as context for the Scout History tab.
    """

    target = context.get('target')
    history_rows = ScoutDetailHistory.annotated_history(target)
    history_fields = [(field, ScoutDetailHistory._meta.get_field(field).verbose_name)
                      for field in HISTORY_DISPLAY_FIELDS]
    return {'history_rows': history_rows, 'history_fields': history_fields}


@register.filter
def field_display(instance, field_name):
    """Return the display value of a model field, honouring choices (get_FOO_display)."""
    display = getattr(instance, f'get_{field_name}_display', None)
    if callable(display):
        return display()
    return getattr(instance, field_name)


@register.filter
def get_change(changes, field_name):
    """Look up a field's (old, new) tuple in a row's `changes` dict; None if unchanged."""
    return changes.get(field_name)


@register.simple_tag
def verbose_name(instance, field_name):
    """
    Displays the more descriptive field name from a Django model field.
    This is different from the version in tom_common_extras in that it
    doesn't call .title() to preserve capitalization in the verbose name.
    """
    try:
        return instance._meta.get_field(field_name).verbose_name
    except (FieldDoesNotExist, AttributeError):
        return field_name.title()
