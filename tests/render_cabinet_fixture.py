"""Render the actual cabinet without importing project settings or accessing a DB."""
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from django.conf import settings
from django.urls import path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
settings.configure(DEBUG=False, SECRET_KEY='fixture-only', STATIC_URL='/static/', USE_TZ=True,
                   ROOT_URLCONF=__name__, INSTALLED_APPS=[], LANGUAGE_CODE='ru', TIME_ZONE='Europe/Moscow')
import django
django.setup()
from django.template import Engine, Context
source = (ROOT / 'engine/templates/dashboard.html').read_text()
urlpatterns = [path(name+'/', lambda request: None, name=name)
               for name in set(re.findall(r"url '([^']+)'", source))]
# The production template uses a ticket-id URL even for an empty support screen.
urlpatterns += [path('ticket/<int:ticket_id>/', lambda request: None, name=name)
                for name in ('create_support_ticket_message', 'support_ticket_messages_json')]
engine = Engine(dirs=[ROOT/'engine/templates'], libraries={
    'static':'django.templatetags.static', 'pricing':'engine.templatetags.pricing'})
base = dict(user=SimpleNamespace(username='alex', email='example@example.test', telegram_id=1,
                                expire_at=datetime(2026,10,7,tzinfo=timezone.utc)),
    has_subscription_access=True, has_recurrent=False, seconds_left=2592000, days_left=30,
    time_left_value=30, time_left_unit='дн.', time_left_label='дней осталось',
    tariffs=[dict(id=1,name='Месяц',price=299,days=30,description='Доступ на 30 дней')],
    tg_webapp_mode=False, use_new_setup_flow=True, support_ticket=SimpleNamespace(id=1),
    plain_subscription_url='https://example.test/subscription', happ_subscription_url='happ://test',
    apple_recommended_app='happ', bonus_days=0, ref_invited_count=0,
    ref_connected_count=0, ref_purchased_count=0, join_referrer_bonus_days=7,
    traffic_referrer_bonus_days=7, purchase_referrer_bonus_days=7, max_referral_bonus_days=21)
out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
for name, changes in [('active', {}), ('recurrent', {'has_recurrent':True}),
                      ('expiring', {'show_expiring_banner':True, 'days_left':2, 'time_left_value':2}),
                      ('expired', {'has_subscription_access':False, 'days_left':0, 'time_left_value':0}),
                      ('telegram', {'tg_webapp_mode':True})]:
    (out/(name+'.html')).write_text(engine.from_string(source).render(Context(base | changes)))
print('Rendered 5 actual-template fixtures; no database or service connections.')
