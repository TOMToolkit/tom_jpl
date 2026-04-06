from django import template

register = template.Library()


@register.simple_tag(takes_context=True)
def filter_clear_url(context, base_field_name):
    """
    Returns a URL that clears the filter params for the given field,
    preserving all other active GET params (search, pagination, other filters).
    Handles both range filters (__gte/__lte) and plain choice filters.

    Usage: {% filter_clear_url spec.base_field_name as clear_url %}
    """
    spec = context.get('spec')
    if not spec:
        return "?"
    params = spec.request.GET.copy()
    params.pop(base_field_name, None)
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


@register.simple_tag(takes_context=True)
def choice_filter_url(context, field_name, value):
    """
    Returns a URL that sets the given choice filter param to value,
    preserving all other active GET params (search, pagination, other filters).

    Usage: {% choice_filter_url spec.parameter_name value as choice_url %}
    """
    spec = context.get('spec')
    if not spec:
        return "?"
    params = spec.request.GET.copy()
    params[field_name] = value
    return f"?{params.urlencode()}"
