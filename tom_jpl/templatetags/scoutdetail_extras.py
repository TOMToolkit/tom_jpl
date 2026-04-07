from django import template
from django.core.exceptions import FieldDoesNotExist

from tom_jpl.models import ScoutDetail

register = template.Library()


@register.inclusion_tag('tom_jpl/partials/scoutdetails_list.html', takes_context=True)
def tab_context(context):
    """
    Returns the ScoutDetails for the specific Target as context for the Target tab
      for rendering into the template.
    """

    target = context.get('target')
    try:
        scoutdetail = ScoutDetail.objects.get(target=target)
    except ScoutDetail.DoesNotExist:
        scoutdetail = {}
    context = {'scoutdetail': scoutdetail}
    return context


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
