from provide.uterm.server.app import create_server_app
from provide.uterm.server.auth import LocalIdentityProvider, WebhookIdentityProvider
from provide.uterm.server.models import AuthConfig, ServerConfig


def test_create_server_app_instantiates_correct_idp():
    # Local IDP
    config = ServerConfig(auth=AuthConfig(identity_provider="local", mode="none"))
    app = create_server_app(config, api_only=True)
    assert isinstance(app.state.uterm_hub.identity_provider, LocalIdentityProvider)

    # Webhook IDP
    config = ServerConfig(
        auth=AuthConfig(identity_provider="webhook", mode="none", webhook_idp_url="http://localhost:8080/auth")
    )
    app = create_server_app(config, api_only=True)
    assert isinstance(app.state.uterm_hub.identity_provider, WebhookIdentityProvider)
    assert app.state.uterm_hub.identity_provider.url == "http://localhost:8080/auth"
