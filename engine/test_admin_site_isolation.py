from unittest.mock import patch

from django.test import SimpleTestCase, override_settings
from django.urls import resolve, reverse


@override_settings(
    ROOT_URLCONF="web_app.admin_urls",
    SESSION_ENGINE="django.contrib.sessions.backends.signed_cookies",
)
class ShredderAdminUrlIsolationTests(SimpleTestCase):
    def test_root_redirects_to_admin(self):
        response = self.client.get("/")
        self.assertRedirects(
            response,
            reverse("support_admin_dashboard"),
            fetch_redirect_response=False,
        )

    def test_health_is_available(self):
        response = self.client.get("/health/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["service"], "shredder-admin-site")

    def test_customer_routes_are_not_exposed(self):
        for path in ("/dashboard/", "/login/", "/pay/", "/api/mobile/v1/me"):
            self.assertEqual(self.client.get(path).status_code, 404)

    def test_admin_login_remains_routed(self):
        match = resolve("/support-admin/login/")
        self.assertEqual(match.url_name, "support_admin_login")

    def test_api_requires_authentication(self):
        self.assertEqual(self.client.get("/support-admin/api/users/").status_code, 401)
        self.assertEqual(
            self.client.get("/support-admin/api/payments/").status_code, 401
        )
        self.assertEqual(
            self.client.get("/support-admin/api/referrals/").status_code, 401
        )

    @patch("engine.shredder_admin_views.repository.load_stats")
    @override_settings(
        SUPPORT_ADMIN_PASSWORD="test-password",
        SHREDDER_ADMIN_SITE_USERNAME="admin",
    )
    def test_authenticated_stats_are_read_only(self, load_stats):
        load_stats.return_value = {"users": 7}
        login = self.client.post(
            "/support-admin/login/",
            {"login": "admin", "password": "test-password"},
        )
        self.assertEqual(login.status_code, 302)
        response = self.client.get("/support-admin/api/stats/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["stats"]["users"], 7)
        load_stats.assert_called_once_with()

    @override_settings(
        SUPPORT_ADMIN_PASSWORD="test-password",
        SHREDDER_ADMIN_SITE_USERNAME="zadkiel",
    )
    def test_authenticated_dashboard_renders_transferred_sections(self):
        self.client.post(
            "/support-admin/login/",
            {"login": "zadkiel", "password": "test-password"},
        )
        response = self.client.get("/support-admin/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Пользователи")
        self.assertContains(response, "Платежи")
        self.assertContains(response, "Рефералы")

    @override_settings(
        SUPPORT_ADMIN_PASSWORD="test-password",
        SHREDDER_ADMIN_SITE_USERNAME="zadkiel",
    )
    def test_session_is_invalidated_when_credentials_change(self):
        login = self.client.post(
            "/support-admin/login/",
            {"login": "zadkiel", "password": "test-password"},
        )
        self.assertEqual(login.status_code, 302)
        with self.settings(SUPPORT_ADMIN_PASSWORD="rotated-password"):
            response = self.client.get("/support-admin/api/stats/")
        self.assertEqual(response.status_code, 401)

    @patch("engine.shredder_admin_views.repository.search_payments")
    @override_settings(
        SUPPORT_ADMIN_PASSWORD="test-password",
        SHREDDER_ADMIN_SITE_USERNAME="zadkiel",
    )
    def test_payments_api_forwards_filters(self, search_payments):
        search_payments.return_value = []
        self.client.post(
            "/support-admin/login/",
            {"login": "zadkiel", "password": "test-password"},
        )
        response = self.client.get(
            "/support-admin/api/payments/?q=payment-1&status=succeeded&limit=500"
        )
        self.assertEqual(response.status_code, 200)
        search_payments.assert_called_once_with(
            query="payment-1", status="succeeded", limit=100
        )

    @patch("engine.shredder_admin_views.repository.search_referrers")
    @override_settings(
        SUPPORT_ADMIN_PASSWORD="test-password",
        SHREDDER_ADMIN_SITE_USERNAME="zadkiel",
    )
    def test_referrals_api_is_read_only(self, search_referrers):
        search_referrers.return_value = []
        self.client.post(
            "/support-admin/login/",
            {"login": "zadkiel", "password": "test-password"},
        )
        response = self.client.get("/support-admin/api/referrals/?q=42")
        self.assertEqual(response.status_code, 200)
        search_referrers.assert_called_once_with(query="42", limit=50)
