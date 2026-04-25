import pytest
from unittest.mock import AsyncMock, MagicMock
from provide.terminal.bridge.hub import TermHub
from provide.terminal.bridge.identity import Principal, IdentityProvider
from fastapi import WebSocket

@pytest.mark.asyncio
async def test_hub_uses_identity_provider():
    # Setup mock IDP
    mock_principal = Principal(subject_id="test_user", roles=frozenset(["admin"]))
    mock_idp = MagicMock(spec=IdentityProvider)
    mock_idp.resolve_principal = AsyncMock(return_value=mock_principal)
    
    # Instantiate Hub with IDP
    # This should fail initially because __init__ doesn't accept identity_provider
    hub = TermHub(identity_provider=mock_idp)
    
    # Mock WebSocket
    mock_ws = MagicMock(spec=WebSocket)
    
    # Call prepare_policy_context
    context = await hub.prepare_policy_context(mock_ws, "worker1")
    
    # Verify IDP was called
    mock_idp.resolve_principal.assert_called_once_with(mock_ws)
    assert context.client_id == "test_user"
    assert context.metadata["principal"] == mock_principal

@pytest.mark.asyncio
async def test_hub_role_mapping_delegate_true():
    # Setup mock IDP with roles in principal
    mock_principal = Principal(subject_id="test_user", roles=frozenset(["operator"]))
    mock_idp = MagicMock(spec=IdentityProvider)
    mock_idp.resolve_principal = AsyncMock(return_value=mock_principal)
    
    # Hub with delegate_roles=True (default)
    hub = TermHub(identity_provider=mock_idp, delegate_roles=True)
    
    # Verify mapping
    roles = hub._map_roles(mock_principal)
    assert roles == frozenset(["operator"])

@pytest.mark.asyncio
async def test_hub_role_mapping_delegate_false():
    # Setup mock IDP with claims but no roles
    mock_principal = Principal(subject_id="test_user", claims={"admin": True})
    mock_idp = MagicMock(spec=IdentityProvider)
    mock_idp.resolve_principal = AsyncMock(return_value=mock_principal)
    
    # Hub with delegate_roles=False
    hub = TermHub(identity_provider=mock_idp, delegate_roles=False)
    
    # Verify mapping
    roles = hub._map_roles(mock_principal)
    assert roles == frozenset(["admin"])
