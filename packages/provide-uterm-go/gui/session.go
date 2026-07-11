package gui

import "image"

// GraphicalSession represents an active connection to a remote graphical console.
type GraphicalSession interface {
	// Screenshot captures the current state of the graphical console and returns an image.
	Screenshot() (image.Image, error)
	// InjectPointer moves the pointer to absolute coordinates (x, y) and sends a button state.
	// buttonMask is a bitmask: bit 0 (value 1) is left button, bit 1 (value 2) is middle, bit 2 (value 4) is right.
	InjectPointer(x, y int, buttonMask uint8) error
	// InjectKey sends a key press or release event.
	// keySym is the X11 keysym value of the key. down is true for press, false for release.
	InjectKey(keySym uint32, down bool) error
}
