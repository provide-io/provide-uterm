package vnc

import "testing"

func TestCanInject(t *testing.T) {
	policy := &StrictPolicyEngine{}
	err := policy.CanInject("session123", "lease456", "viewer")
	if err == nil {
		t.Errorf("expected error for viewer role, got nil")
	}
}
