"""
Bootstrap-styled admin pagination for jazzmin on Django 6.0.

Older django-jazzmin ends its jazzmin_paginator_number tag with
`format_html(html_str)` (no interpolation args), which Django 6.0 rejects.
This tag rebuilds the same Bootstrap markup jazzmin expects, using mark_safe
on an already-escaped fragment, so the .pagination / .page-item / .page-link
styling renders as proper buttons.

Place this file at:
    apps/sims/templatetags/zmuk_admin.py
and create an empty:
    apps/sims/templatetags/__init__.py
"""
from django import template
from django.contrib.admin.views.main import PAGE_VAR
from django.utils.html import format_html

register = template.Library()


@register.simple_tag
def zmuk_paginator_number(cl, i):
    """Render a single Bootstrap page item for the admin changelist."""
    # The gap marker yielded by ChangeList.paginator.get_elided_page_range().
    if i == cl.paginator.ELLIPSIS:
        return format_html(
            '<li class="page-item disabled">'
            '<a class="page-link" href="javascript:void(0);">{}</a></li>',
            cl.paginator.ELLIPSIS,
        )
    # Current page: highlighted, non-navigating.
    if i == cl.page_num:
        return format_html(
            '<li class="page-item active">'
            '<a class="page-link" href="javascript:void(0);">{}</a></li>',
            i,
        )
    # Any other page: real link, preserving filters/search/date hierarchy.
    return format_html(
        '<li class="page-item"><a class="page-link" href="{}">{}</a></li>',
        cl.get_query_string({PAGE_VAR: i}),
        i,
    )
