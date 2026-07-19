package vnc

import "errors"

type PolicyEngine interface {
	CanInject(sessionID, leaseID, principalRole string) error
}

type StrictPolicyEngine struct{}

func (p *StrictPolicyEngine) CanInject(sessionID, leaseID, principalRole string) error {
	if principalRole != "operator" && principalRole != "admin" {
		return errors.New("forbidden: insufficient role")
	}
	if leaseID == "" {
		return errors.New("forbidden: no active lease")
	}
	return nil
}
