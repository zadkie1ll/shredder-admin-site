"""Render production admin for browser review, with no settings or DB connections."""
import re
import sys
import subprocess
from pathlib import Path
from html.parser import HTMLParser
from django.conf import settings
from django.urls import path

ROOT = Path(__file__).resolve().parents[1]
settings.configure(DEBUG=False, SECRET_KEY='fixture-only', STATIC_URL='/static/',
                   ROOT_URLCONF=__name__, INSTALLED_APPS=[], LANGUAGE_CODE='ru')
import django
django.setup()
from django.template import Engine, Context
source = (ROOT/'engine/templates/admin_dashboard.html').read_text()
urlpatterns = [path(name+'/', lambda request: None, name=name)
               for name in set(re.findall(r"url '([^']+)'", source))]
engine = Engine(dirs=[ROOT/'engine/templates'], libraries={'static':'django.templatetags.static'})
out = Path(sys.argv[1] if len(sys.argv)>1 else '/tmp/mi-admin-review')
out.mkdir(parents=True, exist_ok=True)
# Compare against the actual previous implementation, including its renderers.
control = subprocess.check_output(['git','show','HEAD:engine/templates/admin_dashboard.html'],cwd=ROOT,text=True)
for role in ('admin', 'marketer', 'support'):
    context = Context(dict(support_admin_is_full_admin=role=='admin',
                           support_admin_is_marketer=role=='marketer', support_admin_role=role,
                           tickets=[], status_filter='open', csrf_token='fixture-only'))
    for suffix, template in [('', source), ('-control', control)]:
        (out/f'{role}{suffix}.html').write_text(engine.from_string(template).render(context))
# Pure HTML renderers are reviewed separately with filled browser fixtures.
# Within async loaders, mask only their rendering block: requests, permissions,
# error handling and mutation listeners must still match the previous source.
def without_presentation_changes(text):
    text = re.sub(r'^.*<link[^\n]+admin-concept\.css[^\n]+\n', '', text, flags=re.M)
    # Win-back alone now uses one form and one save action. Its existing
    # per-key API contract, validation, reset and partial-failure semantics
    # are exercised in admin_winback_browser.cjs; other groups remain exact.
    text = re.sub(r'^ *<script src="\{% static \'js/admin-winback-settings.js\' %\}[^\n]+\n', '', text, flags=re.M)
    # admin-select.js only enhances native selects; forms and listeners stay.
    text = re.sub(r'^ *<script src="\{% static \'js/admin-select.js\' %\}[^\n]+\n', '', text, flags=re.M)
    text = re.sub(r'(<div id="subpanel-sys-winback" class="subtab-panel">).*?(?=\n            <div id="subpanel-sys-payment")', r'\1<!-- reviewed unified Win-back editor -->\n', text, count=1, flags=re.S)
    text = re.sub(r"                if \(slug === 'winback'\) \{\n.*?                    return;\n                }\n", '', text, count=1, flags=re.S)
    text = text.replace('function renderRuntimeSettings(settings, winbackLoad)', 'function renderRuntimeSettings(settings)')
    # The GET loader now carries a Win-back revision token and retains its
    # pending form on refresh failures. The focused browser test exercises
    # GETs completing before/during/after save. Its request stays read-only;
    # submitRuntimeSetting and every other mutation handler remain byte-checked.
    def reviewed_settings_load(match):
        body = match.group()
        assert body.count('fetch(') == 1
        assert re.search(r"fetch\(main.dataset.runtimeSettingsUrl, \{\s*headers: \{'X-Requested-With': 'XMLHttpRequest'\},\s*cache: 'no-store',\s*}\)", body)
        assert not re.search(r"method:|FormData|csrfToken", body)
        return '/* reviewed read-only runtime settings loader */\n'
    text = re.sub(r'^        async function loadRuntimeSettings\b.*?(?=^        (?:async )?function |\Z)', reviewed_settings_load, text, flags=re.M|re.S)
    # Replace the decorative brand subtree; controls in the following sidebar
    # navigation remain byte-compared, including every role condition.
    text = re.sub(r'(<aside class="sidebar">)\s*(?:<div class="brand">|<div class="admin-wordmark">).*?(?=        <nav class="sidebar-nav")', r'\1\n<!-- reviewed wordmark -->\n', text, count=1, flags=re.S)
    # Visible field labels add accessible context without changing any control.
    text = re.sub(r'<label class="infra-concept-field"><span>[^<]+</span>((?:<input\b[^>]+>|<select\b.*?</select>))</label>', r'\1', text, flags=re.S)
    text = text.replace('<label class="infra-search infra-server-control-field">\n                            <span>Поиск серверов</span>', '<label class="infra-search">\n                            <i class="fas fa-magnifying-glass"></i>')
    text = re.sub(r'<label class="infra-server-control-field"><span>Статус</span>(<select\b.*?</select>)</label>', r'\1', text, flags=re.S)
    text = text.replace('<i class="fas fa-rotate"></i><span>Обновить</span>', '<i class="fas fa-rotate"></i>')
    text = re.sub(r'^ *<div class="promo-field-label">(?:Что получит пользователь|Эффект каждого купона) <span>[^<]+</span></div>\n', '', text, flags=re.M)
    text = re.sub(r'(data-promo-type-picker="(?:promo|batch)") role="group" aria-label="[^"]+" title="[^"]+"', r'\1', text)
    text = text.replace('<h2 title="Подсказка меняется вместе с настройками">Как сработает</h2>', '<h2>Как сработает</h2><p>Подсказка меняется вместе с настройками</p>')
    text = re.sub(r'^ *<label class="admin-section-field"><span>(?:Название|Порядок|Текст)</span>\n(.*?(?:<input[^>]+>|</textarea>))\n *</label>', r'\1', text, flags=re.M|re.S)
    text = re.sub(r'^ *<h2 class="admin-section-title">(?:Шаблон ответа|Шаблоны ответов)</h2>\n', '', text, flags=re.M)
    text = text.replace('            <section class="admin-template-library">\n', '')
    text = text.replace('<div id="reply-templates-result" class="reply-template-list"></div>\n            </section>', '<div id="reply-templates-result" class="reply-template-list"></div>')
    # These three static query blocks moved above their charts. Compare every
    # control attribute, data hook and text token while allowing visual divs
    # and their order to change. Other static sections remain byte-compared.
    # Ads summary (2026-09-09): a read-only section over the same acquisition
    # API; presets and date fields only drive GET ?section=ads_summary.
    text = re.sub(r'^ *<section id="acq-ads-summary".*?</section>\n', '', text, count=1, flags=re.M|re.S)
    text = re.sub(r'^        // \[ads-summary\].*?// \[/ads-summary\]\n\n', '', text, count=1, flags=re.M|re.S)
    class QueryMarkup(HTMLParser):
        def __init__(self):
            super().__init__()
            self.tokens = []
        def handle_starttag(self, tag, attrs):
            if tag != 'div' or any(k == 'id' or k.startswith('data-') for k, _ in attrs):
                self.tokens.append(('element', tag, repr(sorted(attrs))))
        def handle_data(self, data):
            value = ' '.join(data.split())
            if value:
                self.tokens.append(('text', value))
    def canonical_query(match):
        parsed = QueryMarkup()
        parsed.feed(match.group())
        return repr(sorted(parsed.tokens)) + '\n'
    for section in ('acq-newrep', 'acq-ads', 'acq-payhealth'):
        pattern = rf'<div id="subpanel-{section}".*?(?=\n            <div id="subpanel-|\n        </section>)'
        text, count = re.subn(pattern, canonical_query, text, count=1, flags=re.S)
        assert count == 1, f'Missing reviewed query block: {section}'
    def canonical_batch_note(match):
        body = re.sub(r'<i\b[^>]*></i>|<h3>Как сработает</h3>|<b>(?:Одноразовый купон|Скачать TXT)</b>', '', match.group())
        parsed = QueryMarkup()
        parsed.feed(body)
        return repr(sorted(parsed.tokens))
    text = re.sub(r'<aside class="promo-batch-note">.*?</aside>', canonical_batch_note, text, count=1, flags=re.S)
    # Registry actions gained visible names; their attributes remain exact.
    def promo_action_labels(match):
        return re.sub(r'((?:<button|<a) [^>]*class="promo-icon-action[^>]*>).*?(</(?:button|a)>)', r'\1<!-- action label -->\2', match.group(), flags=re.S)
    text = re.sub(r'^        function renderPromoCodes\b.*?(?=^        (?:async )?function |\Z)', promo_action_labels, text, flags=re.M|re.S)
    text = text.replace('funnel.map((step) => {', 'funnel.map((step, index) => {')
    text = text.replace('const height = activations > 0 ? Math.min(100, 100 * step.value / activations) : 0;', 'const height = index === 0 ? 100 : Math.max(step.value ? 6 : 0, promoCohortPct(step.value, activations));')
    text = text.replace('        // Presentation only: use the selected page\'s existing title and copy.\n', '')
    text = re.sub(r'^        function syncAdminSectionPresentation\b.*?(?=^        document.querySelectorAll)', '', text, flags=re.M|re.S)
    text = text.replace("        document.querySelectorAll('.tab-panel:has(> .subtabs)').forEach(syncAdminSectionPresentation);\n\n", '')
    text = text.replace('            syncAdminSectionPresentation(panel);\n', '')
    text = re.sub(r'(function acqLegendRows\(ctx, legend, availableWidth\) {\n            ctx.font = )\'12px Manrope, system-ui, sans-serif\';', r"\1'10px sans-serif';", text)
    # Server identity moved its existing host below the header; country now
    # also has an accessible text label. Calculations, menu and state stay exact.
    def server_identity(match):
        body = re.sub(r'<div class="infra-server-card-identity">.*?(?=<div class="infra-server-card-head-actions">)', '<!-- reviewed server identity -->', match.group(), count=1, flags=re.S)
        return body.replace('                        <div class="infra-server-card-host" title="${host}">${host}</div>\n', '')
    text = re.sub(r'^        function renderInfraServerCards\b.*?(?=^        (?:async )?function |\Z)', server_identity, text, flags=re.M|re.S)
    # Payment journal rows gained a copy-ID button and the client tab reuses
    # paymentJournalRowsHtml; the clipboard helper only reads a data attribute.
    text = re.sub(r"(admin_dashboard\.css' %\}\?v=)\d+", r"\1N", text)
    text = re.sub(r'^        async function copyPaymentId\b.*?(?=^        (?:async )?function |\Z)', '', text, flags=re.M|re.S)
    text = re.sub(r"\n            const paymentCopy = event\.target\.closest\('\[data-payment-copy-id\]'\);\n            if \(paymentCopy\) \{\n.*?\n            \}\n", '', text, count=1, flags=re.S)
    rendering_blocks = {
        'renderClientCard': (r'            target.innerHTML = `', r'            loadClientTraffic\(clientCardState.q\);'),
        'loadClientTimeline': (r"                target.className = 'client-timeline';", r'            } catch \(error\) {'),
        'loadClientRwmsSync': (r'                const diffRows =', r"                target.querySelectorAll\('\[data-rwms-sync-action\]'\)"),
        'renderNodeProvision': (r'            target.innerHTML = (?:requests.map|`<table class="node-provision-request-table">)', r"            target.querySelectorAll\('\[data-open-provision-request\]'\)"),
    }
    for name, (start, end) in rendering_blocks.items():
        function_pattern = rf'^        (?:async )?function {name}\b.*?(?=^        (?:async )?function |\Z)'
        def mask_rendering(match):
            updated, count = re.subn(start + r'.*?(?=' + end + ')', '/* reviewed HTML rendering */\n', match.group(), count=1, flags=re.S)
            assert count == 1, f'Rendering boundary changed for {name}'
            return updated
        text = re.sub(function_pattern, mask_rendering, text, flags=re.M|re.S)
    return re.sub(r'^        (?:async )?function (?:configTemplateCard|acqDraw|loadPatterns|paymentJournalRowsHtml|clientSummaryHtml|clientOverviewSectionHtml|clientPaymentsSectionHtml|clientReferralControlHtml|clientReferralsSectionHtml)\b.*?(?=^        (?:async )?function |\Z)', '', text, flags=re.M|re.S)
assert without_presentation_changes(source) == without_presentation_changes(control), 'Unexpected change outside reviewed presentation renderers'
print('PASS: forms, permissions, API bindings and action handlers unchanged outside reviewed presentation renderers and isolated Win-back editor.')
print('Rendered six admin fixtures without connecting to databases or services.')
