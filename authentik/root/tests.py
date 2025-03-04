"""root tests"""
from base64 import b64encode

from django.conf import settings
from django.test import Client, TestCase
from django.urls import reverse


class TestRoot(TestCase):
    """Test root application"""

    def setUp(self):
        super().setUp()
        self.client = Client()

    def test_monitoring_error(self):
        """Test monitoring without any credentials"""
        response = self.client.get(reverse("metrics"))
        self.assertEqual(response.status_code, 401)

    def test_monitoring_ok(self):
        """Test monitoring with credentials"""
        creds = "Basic " + b64encode(f"monitor:{settings.SECRET_KEY}".encode()).decode("utf-8")
        auth_headers = {"HTTP_AUTHORIZATION": creds}
        response = self.client.get(reverse("metrics"), **auth_headers)
        self.assertEqual(response.status_code, 200)
