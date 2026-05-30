from __future__ import annotations

from api.admin_urls import local_proxy_root_url
from config.settings import Settings


def test_local_proxy_root_url_prefers_public_base_url_when_configured():
    settings = Settings()
    settings.gateway_public_base_url = "https://gateway.example.com/"

    assert local_proxy_root_url(settings) == "https://gateway.example.com"
