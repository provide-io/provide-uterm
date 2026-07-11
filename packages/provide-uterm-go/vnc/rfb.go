package vnc

import (
	"encoding/binary"
)

// RFB Message Types
const (
	ClientSetPixelFormat = 0
	ClientSetEncodings   = 2
	ClientFramebufferUpdateRequest = 3
	ClientKeyEvent       = 4
	ClientPointerEvent   = 5
	
	ServerFramebufferUpdate = 0
)

// EncodePointerEvent returns an encoded RFB PointerEvent.
func EncodePointerEvent(x, y int, buttonMask uint8) []byte {
	if x < 0 {
		x = 0
	} else if x > 65535 {
		x = 65535
	}
	if y < 0 {
		y = 0
	} else if y > 65535 {
		y = 65535
	}
	
	buf := make([]byte, 6)
	buf[0] = ClientPointerEvent
	buf[1] = buttonMask
	binary.BigEndian.PutUint16(buf[2:4], uint16(x))
	binary.BigEndian.PutUint16(buf[4:6], uint16(y))
	return buf
}

// EncodeKeyEvent returns an encoded RFB KeyEvent.
func EncodeKeyEvent(keySym uint32, down bool) []byte {
	buf := make([]byte, 8)
	buf[0] = ClientKeyEvent
	if down {
		buf[1] = 1
	} else {
		buf[1] = 0
	}
	// bytes 2-3 are padding (zero)
	binary.BigEndian.PutUint32(buf[4:8], keySym)
	return buf
}
