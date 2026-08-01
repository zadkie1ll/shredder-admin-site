from django import template

register = template.Library()


@register.filter
def floor_div(value, divisor):
    """Целочисленное деление вниз для «маркетинговой» цены в месяц.

    Штатный {% widthratio %} округляет к ближайшему (599/3 → 200),
    а для витрины нужна цена вида 199/149 (599/3 → 199, 1799/12 → 149).
    """
    try:
        return int(value) // int(divisor)
    except (TypeError, ValueError, ZeroDivisionError):
        return ""
