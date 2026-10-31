"""Policy Decision Point (PDP).

Rules:
  * deny-by-default
  * explicit DENY overrides any ALLOW
  * a policy matches when subject ∈ principals, action matches (exact or '*'),
    and resource matches (exact or glob prefix 'type:prefix*')
Every decision is audit-logged and traced.
"""
from __future__ import annotations

import fnmatch

from sqlalchemy.orm import Session

from app.acl.models import Action, Decision, Effect, Resource, Subject
from app.db.session import ACLPolicy, AuditLog
from app.observability import acl_decisions_total, tracer


def _action_matches(policy_action: str, requested: Action) -> bool:
    return policy_action in (requested.value, Action.WILDCARD.value)


def _resource_matches(policy_resource: str, urn: str) -> bool:
    # Supports exact and glob (e.g. "object:reports/*").
    return policy_resource == urn or fnmatch.fnmatch(urn, policy_resource)


class ACLEngine:
    def __init__(self, session: Session) -> None:
        self._session = session

    def check(self, subject: Subject, action: Action, resource: Resource) -> Decision:
        urn = resource.urn()
        with tracer.start_as_current_span("acl.check") as span:
            span.set_attribute("acl.subject", subject.id)
            span.set_attribute("acl.action", action.value)
            span.set_attribute("acl.resource", urn)

            rows = (
                self._session.query(ACLPolicy)
                .filter(ACLPolicy.subject.in_(list(subject.principals)))
                .all()
            )

            matched = [
                r for r in rows if _action_matches(r.action, action) and _resource_matches(r.resource, urn)
            ]
            denied = [r for r in matched if r.effect == Effect.DENY.value]
            allowed = [r for r in matched if r.effect == Effect.ALLOW.value]

            if denied:
                decision = Decision(False, f"explicit deny by policy {denied[0].id}")
            elif allowed:
                decision = Decision(True, f"allowed by policy {allowed[0].id}")
            else:
                decision = Decision(False, "no matching allow (deny by default)")

            span.set_attribute("acl.allowed", decision.allowed)
            acl_decisions_total.labels(
                decision="allow" if decision.allowed else "deny", action=action.value
            ).inc()

        self._session.add(
            AuditLog(
                subject=subject.id,
                action=action.value,
                resource=urn,
                decision="allow" if decision.allowed else "deny",
                detail={"reason": decision.reason},
            )
        )
        self._session.commit()
        return decision

    def enforce(self, subject: Subject, action: Action, resource: Resource) -> None:
        """Raise PermissionError if not allowed (used by the PEP)."""
        decision = self.check(subject, action, resource)
        if not decision.allowed:
            raise PermissionError(decision.reason)
