from django import template
from django.core.exceptions import FieldDoesNotExist

from tom_jpl.models import HISTORY_DISPLAY_FIELDS, ScoutDetail, ScoutDetailHistory

register = template.Library()

# Number of Scout runs with changes shown in the compact log on the Scout Details tab.
RECENT_CHANGES_LIMIT = 5

# Lifecycle fields excluded from the generic field listing on the Scout Details tab;
# they are rendered as the "Outcome" section instead of as raw dict rows.
LIFECYCLE_FIELDS = ['active', 'mpc_status', 'mpc_status_checked', 'mpc_reference', 'merged_into']


@register.inclusion_tag('tom_jpl/partials/scoutdetails_list.html', takes_context=True)
def tab_context(context):
    """
    Returns the ScoutDetails for the specific Target as context for the Target tab
      for rendering into the template.

    Also includes `recent_changes`: the most recent history rows (newest first) whose
    tracked fields changed relative to the previous Scout run, for the compact
    "Recent changes" log, and `merged_into_target`: the Target this submission was folded
    into (resolved from ScoutDetail.merged_into), for linking the Outcome section to the
    surviving object.
    """

    target = context.get('target')
    try:
        scoutdetail = ScoutDetail.objects.get(target=target)
        merged_into_target = scoutdetail.resolve_merged_into()
    except ScoutDetail.DoesNotExist:
        scoutdetail = {}
        merged_into_target = None
    recent_changes = [row for row in ScoutDetailHistory.annotated_history(target)
                      if row.changes][:RECENT_CHANGES_LIMIT]
    context = {'scoutdetail': scoutdetail, 'recent_changes': recent_changes,
               'merged_into_target': merged_into_target, 'lifecycle_fields': LIFECYCLE_FIELDS}
    return context


def history_tab_context(context):
    """
    Returns the full ScoutDetailHistory for the specific Target (newest first, each row
    annotated with its row-to-row `changes`) as context for the Scout History tab.
    """

    target = context.get('target')
    history_rows = ScoutDetailHistory.annotated_history(target)
    history_fields = [(field, FIELD_DISPLAY_LABELS.get(field, ScoutDetailHistory._meta.get_field(field).verbose_name))
                      for field in HISTORY_DISPLAY_FIELDS]
    return {'history_rows': history_rows, 'history_fields': history_fields}


# Per-field format() specs for values shown in the history views.
FIELD_DISPLAY_FORMATS = {'arc': '.2f'}

# Display-only label overrides (e.g. to add units), applied on top of the model
# verbose_names so presentation tweaks don't need a model change and migration.
FIELD_DISPLAY_LABELS = {'ca_dist': 'C/A dist (LD)', 'arc': 'Arc (days)'}


@register.filter
def format_value(value, field_name):
    """Apply any per-field display format (see FIELD_DISPLAY_FORMATS); None passes through."""
    fmt = FIELD_DISPLAY_FORMATS.get(field_name)
    if value is None or fmt is None:
        return value
    return format(value, fmt)


@register.filter
def field_display(instance, field_name):
    """Return the display value of a model field, honouring choices (get_FOO_display)."""
    display = getattr(instance, f'get_{field_name}_display', None)
    if callable(display):
        return display()
    return format_value(getattr(instance, field_name), field_name)


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
    Display-only overrides in FIELD_DISPLAY_LABELS take precedence.
    """
    if field_name in FIELD_DISPLAY_LABELS:
        return FIELD_DISPLAY_LABELS[field_name]
    try:
        return instance._meta.get_field(field_name).verbose_name
    except (FieldDoesNotExist, AttributeError):
        return field_name.title()
