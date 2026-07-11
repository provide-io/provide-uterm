package gui

import "image"

// GraphicalSession represents an active connection to a remote graphical console.
type GraphicalSession interface {
	Screenshot() (image.Image, error)
	InjectPointer(x, y int, buttonMask uint8) error
	InjectKey(keySym uint32, down bool) error
}
