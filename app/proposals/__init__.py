"""UNTRUSTED ADVISORY INPUT: imported LLM proposals (Phase 7).

A proposal is text from outside the system. It is stored as opaque bytes under a generated name,
parsed only by the host validator into a strict schema, triaged by a policy, risk-assessed, shown as
escaped plain text and reviewed by an ADMIN. Nothing in it is ever executed, evaluated, templated,
deserialised unsafely, rendered as HTML or applied. Approval creates a tracked MANUAL change request
(a record); implementation is a separate, ordinary, reviewed project change.
"""
