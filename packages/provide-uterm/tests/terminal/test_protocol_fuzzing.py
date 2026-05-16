import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from provide.uterm.control_channel import (
    DLE,
    STX,
    ControlChannelDecoder,
    ControlChannelProtocolError,
    ControlChunk,
    DataChunk,
    encode_control,
)


@given(
    chaos_data=st.lists(st.binary(min_size=1, max_size=100), min_size=1, max_size=50),
    inject_control=st.booleans()
)
@settings(max_examples=100, deadline=None)
def test_decoder_resilience_under_binary_chaos(chaos_data, inject_control):
    decoder = ControlChannelDecoder()
    valid_payload = {"type": "ping"}
    control_frame = encode_control(valid_payload)

    # Mix chaos with valid control frames
    for chunk in chaos_data:
        try:
            # We use decode('latin-1') because the decoder expects 'str'
            decoder.feed(chunk.decode("latin-1"))
            if inject_control:
                decoder.feed(control_frame)
        except ControlChannelProtocolError:
            # Expected protocol errors from random data are fine; we check for NO CRASHES.
            pass

    try:
        decoder.finish()
    except ControlChannelProtocolError:
        pass

def test_decoder_boundary_splitting_on_control_frames():
    decoder = ControlChannelDecoder()
    payload = {"type": "test", "data": "A" * 100}
    encoded = encode_control(payload)

    events = []
    for char in encoded:
        events.extend(decoder.feed(char))

    assert len(events) == 1
    assert isinstance(events[0], ControlChunk)
    assert events[0].control == payload

def test_decoder_rejects_truncated_header_at_finish():
    decoder = ControlChannelDecoder()
    decoder.feed(f"{DLE}{STX}0000") # Truncated length
    with pytest.raises(ControlChannelProtocolError, match="truncated control frame"):
        decoder.finish()

def test_decoder_rejects_invalid_hex_length():
    decoder = ControlChannelDecoder()
    with pytest.raises(ControlChannelProtocolError, match="invalid control header"):
        decoder.feed(f"{DLE}{STX}G0000000:{{}}") # G is not hex

def test_decoder_preserves_complex_ansi_sequences():
    decoder = ControlChannelDecoder()
    # A mix of colors, cursor movements, and a fake Sixel-like sequence
    ansi_data = "\x1b[31mRed\x1b[0m\x1b[H\x1b[2J\x1bP0;0;1q\"1;1;100;100#0;2;0;0;0#1;2;100;0;0\x1b\\"

    events = decoder.feed(ansi_data)
    assert len(events) == 1
    assert isinstance(events[0], DataChunk)
    assert events[0].data == ansi_data
