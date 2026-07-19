from provide.uterm.bridge.schemas import HelloFrame


def test_hello_capabilities_parsing():
    caps = HelloFrame(type="hello", hijack_control="ws", resume_supported=True, mcp_supported=True, vnc_supported=False)
    assert caps.mcp_supported is True
    assert caps.vnc_supported is False
