from django.test import SimpleTestCase, override_settings
from django.urls import resolve, reverse


@override_settings(ROOT_URLCONF="web_app.admin_urls")
class ShredderAdminUrlIsolationTests(SimpleTestCase):
    def test_root_redirects_to_admin(self):
        response = self.client.get("/")
        self.assertRedirects(
            response,
            reverse("support_admin_tickets"),
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
