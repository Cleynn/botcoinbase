"""Capital policy: named profiles, the funds the venue reports, and what may be deployed.

Pure Decimal code with no I/O. The profile sets policy limits; the funds say what actually exists;
`usable_quote` combines them and never returns more than either allows.
"""
