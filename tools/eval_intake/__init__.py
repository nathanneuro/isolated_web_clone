"""Eval intake: eval bundles from the receiver -> choreographies an episode may use.

An eval bundle carries a choreography (synthetic-population-spec §4.4): a
dev-signed, per-question schedule of what named users do at which step, with the
text they post in encrypted pools. Intake unseals it through go-live, checks it
against the *live* spec of the site it names (the bundle cannot carry that spec,
and a schedule naming a form the site does not have is a schedule that fails
inside where nobody can read why), and files it by question id.

Codes out, like the worker: 30 unsealed and filed, 21 does not fit the live site,
32 decrypt failed, 53 site not live.
"""

from .intake import EvalDefinitions, EvalIntake, IntakeStatus

__all__ = ["EvalDefinitions", "EvalIntake", "IntakeStatus"]
