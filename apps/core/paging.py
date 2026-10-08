"""Pagination that treats a page number below 1 as the first page.

Django's `Paginator.get_page` sends an out-of-range number to the *last* page, which is sensible for a number
that is too big (the list shrank) but not for 0 or a negative one, where `?page=0` showed the oldest records."""
from __future__ import annotations

from django.core.paginator import Paginator as DjangoPaginator


class Paginator(DjangoPaginator):
    def get_page(self, number):
        try:
            if int(number) < 1:
                number = 1
        except (TypeError, ValueError):
            pass   # missing or not a number: the base class shows page 1
        return super().get_page(number)
