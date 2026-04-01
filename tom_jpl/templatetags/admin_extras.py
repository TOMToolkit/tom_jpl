from django import template

register = template.Library()


@register.simple_tag(takes_context=True)
def filter_clear_url(context, base_field_name):
    """
    Returns a URL that clears the gte/lte filter params for the given field,
    preserving all other active GET params (search, pagination, other filters).

    Usage: {% filter_clear_url spec.base_field_name as clear_url %}
    """
    spec = context.get('spec')
    if not spec:
        return "?"
    params = spec.request.GET.copy()
    params.pop(f"{base_field_name}__gte", None)
    params.pop(f"{base_field_name}__lte", None)
    qs = params.urlencode()
    return f"?{qs}" if qs else "?"


@register.simple_tag(takes_context=True)
def filter_current_value(context, base_field_name, suffix):
    """
    Returns the current GET value for a gte/lte filter input, so that
    previously applied filter values are preserved in the form fields
    after submission.

    Usage: {% filter_current_value spec.base_field_name 'gte' %}
           {% filter_current_value spec.base_field_name 'lte' %}
    """
    spec = context.get('spec')
    if not spec:
        return ""
    return spec.request.GET.get(f"{base_field_name}__{suffix}", "")
