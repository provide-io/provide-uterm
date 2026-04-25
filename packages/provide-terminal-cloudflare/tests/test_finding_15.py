
import pytest
from provide.terminal.deckmux._presence import PresenceStore

def test_presence_store_update_unknown_field():
    store = PresenceStore()
    store.add("user1", "Alice", "blue", "admin")
    
    # Existing field should work
    store.update("user1", name="Bob")
    assert store.get("user1").name == "Bob"
    
    # Unknown field should raise ValueError
    with pytest.raises(ValueError, match="Unknown presence field: invalid_field"):
        store.update("user1", invalid_field="value")
