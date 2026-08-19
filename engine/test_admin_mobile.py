from pathlib import Path

from django.test import SimpleTestCase


class AdminMobileLayoutTests(SimpleTestCase):
    """Regression guards for the support admin mobile layout."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.template = Path("engine/templates/admin_dashboard.html").read_text()
        cls.css = Path("engine/static/css/admin_dashboard.css").read_text()

    def test_mobile_page_shrinks_while_wide_reports_keep_local_scrollers(self):
        self.assertIn(".main {\n        width: 100%;\n        max-width: 100%;", self.css)
        self.assertIn("overflow: visible;", self.css)
        for selector in (
            ".payments-board-scroll",
            ".node-traffic-scroll",
            ".sources-table-scroll",
            ".promo-cohort-table-scroll",
            ".acq-chart-data-scroll",
        ):
            self.assertIn(selector, self.css)
        self.assertIn("overscroll-behavior-x: contain;", self.css)

    def test_analytics_summary_and_cards_have_mobile_grids(self):
        self.assertIn("#panel-stats .sources-summary {", self.css)
        self.assertIn("grid-template-columns: repeat(2, minmax(0, 1fr));", self.css)
        self.assertIn("#panel-stats .sources-summary-lead {", self.css)
        self.assertIn("grid-column: 1 / -1;", self.css)
        self.assertIn("#panel-stats .source-metrics {", self.css)
        self.assertIn("#panel-stats .sources-grid {", self.css)
        self.assertIn("grid-template-columns: minmax(0, 1fr);", self.css)

    def test_analytics_charts_are_locally_scrollable_and_resize(self):
        self.assertIn("#panel-stats .sales-chart-canvas-wrap {", self.css)
        self.assertIn("overflow-x: auto;", self.css)
        self.assertIn("series,\n                buckets,", self.template)
        self.assertIn("let salesChartResizeFrame = null;", self.template)
        self.assertIn("drawSalesSeriesChart(canvas, series);", self.template)

    def test_source_users_modal_keeps_close_button_in_header(self):
        self.assertIn("#source-users-modal .modal-header {", self.css)
        self.assertIn("grid-template-columns: minmax(0, 1fr) 34px;", self.css)
        self.assertIn("#source-users-modal .modal-close {", self.css)
        self.assertIn("grid-column: 2;", self.css)

    def test_admin_css_cache_version_is_bumped(self):
        self.assertIn("admin_dashboard.css' %}?v=6", self.template)
