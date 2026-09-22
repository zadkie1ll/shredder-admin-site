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
