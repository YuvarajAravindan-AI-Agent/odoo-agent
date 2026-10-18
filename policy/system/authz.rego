# OPA's own API authorization (opa run --authorization=basic). The agent may only ask for a refund
# decision; nobody can push, replace or delete policies or data over HTTP. Policy changes ship as files.
package system.authz

default allow := false

allow if {
	input.method == "POST"
	input.path == ["v1", "data", "refunds", "decision"]
}

allow if {
	input.method == "GET"
	input.path == ["health"]
}
