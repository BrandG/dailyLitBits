"""The judgment layer for library acquisition: should this book come in?

The deterministic filter in `acquire.py` settles most candidates for free. What
reaches here is the residue it cannot settle — a title that might be the same
work under a different name, or might be a reference book dressed as one. That
is a question about meaning, so it goes to a model.

Scope, deliberately narrow: an adjudicator returns a decision and a confidence.
It never downloads, never writes to the database, never chooses what to ask
about next. Its answer enters `acquire.py` as a single `accept` boolean.

FAIL CLOSED, and note which way "closed" points here. Skipping a good book
costs nothing — there are 79,000 more and another run tomorrow. Admitting a
dictionary pollutes a library people read for pleasure. So every failure path
(no key, timeout, HTTP error, spend cap, malformed answer, off-menu answer,
confidence below the gate) returns a verdict that does **not** accept.

PIN THE MODEL. `model` is required and recorded on every decision. A silent
upgrade would change what the library admits with nothing appearing to break.

NOT DETERMINISTIC. Measured on the sibling Ricky project: same state, five
runs, the choice was stable but confidence and conviction moved. Here that only
matters for a candidate sitting exactly on a gate, where the two outcomes are
"acquire today" and "acquire tomorrow instead". Acceptable.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date

# The decision menu. These three exhaust what can happen to a candidate.
MENU = {
    "NEW": "a distinct readable work we do not hold",
    "DUPLICATE": "the same work as something in already_held",
    "NOT_SERIAL": "a reference work, word list, periodical or multi-work omnibus",
}

CONVICTION_LABELS = ("weak", "fair", "strong", "overwhelming")

# Sent as the question's instructions. The explicit decision rules are load
# bearing: Ricky's log records a discursive prompt producing a near-uniform
# answer that ignored the one field that mattered. Naming the rule is what made
# the verdict track the evidence.
GUIDANCE = (
    "A library serialises whole public-domain books to readers a few hundred "
    "words per day. Decide what to do with one candidate from Project Gutenberg. "
    "Answer DUPLICATE if the candidate is the same written work as any title in "
    "already_held, including a different edition, translation, spelling, "
    "subtitle, or a volume of a work already held. Answer NOT_SERIAL if it is "
    "not a single work meant to be read straight through: a dictionary, "
    "encyclopaedia, word list, index, bibliography, catalogue, almanac, "
    "periodical issue, sheet music, or a collected-works omnibus of many "
    "separate titles. Otherwise answer NEW. Prefer DUPLICATE or NOT_SERIAL "
    "when unsure; a wrongly skipped book costs nothing, a wrongly added one "
    "pollutes the library."
)


@dataclass(frozen=True)
class Decision:
    ok: bool
    verdict: str = ""             # NEW | DUPLICATE | NOT_SERIAL, or "" when not ok
    probability: float = 0.0      # the model's probability on the verdict it chose
    confidence: float = 0.0
    conviction: float = 0.0       # the Score answer, raw
    model: str = ""
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    reason: str = ""              # why it is not ok, or why it was gated out
    accept: bool = False          # the only field acquire.py acts on


@dataclass
class Candidate:
    """What the catalogue says about one book. Facts only, no judgement."""
    gutenberg_id: int
    title: str
    author: str
    language: str = "en"
    subjects: list = field(default_factory=list)
    bookshelves: list = field(default_factory=list)
    downloads: int = 0

    def state(self, already_held):
        """The state Jev sees.

        `already_held` is the shortlist the deterministic stage picked as the
        plausible collision set, not the whole library. Sending all 106 titles
        would cost more and bury the three that matter. Subjects are included
        because "English language -- Dictionaries" settles a case that the
        title alone leaves open.
        """
        return {
            "candidate_title": self.title,
            "candidate_author": self.author,
            "candidate_language": self.language,
            "candidate_subjects": self.subjects[:8],
            "candidate_bookshelves": self.bookshelves[:6],
            "already_held": [{"title": t, "author": a} for t, a in already_held],
        }


class Adjudicator:
    """Interface. Subclasses must fail closed."""

    name = "base"

    def judge(self, candidate: Candidate, already_held) -> Decision:
        raise NotImplementedError


class RefuseAdjudicator(Adjudicator):
    """What you get with no key configured: nothing is accepted.

    Deliberately not "accept everything". A misconfigured box should add no
    books, not add every book.
    """

    name = "refuse"

    def __init__(self, reason="no_adjudicator_configured"):
        self.reason = reason

    def judge(self, candidate, already_held):
        return Decision(ok=False, reason=self.reason, model=self.name)


class ScriptedAdjudicator(Adjudicator):
    """For tests: answers from a dict keyed by gutenberg id."""

    name = "scripted"

    def __init__(self, answers, default="NEW"):
        self.answers = answers
        self.default = default
        self.calls = 0

    def judge(self, candidate, already_held):
        self.calls += 1
        verdict = self.answers.get(candidate.gutenberg_id, self.default)
        if verdict is None:
            return Decision(ok=False, reason="scripted_failure", model=self.name)
        return Decision(ok=True, verdict=verdict, probability=1.0, confidence=1.0,
                        conviction=2.0, model=self.name,
                        accept=(verdict == "NEW"))


class JevAdjudicator(Adjudicator):
    """TypeSafe AI's `systemOne`, via the official `typesafe-sdk`.

    Calibration facts, measured against the live API on 2026-10-27 with 16
    known-answer cases and four controls (see CLAUDE.md). A
    default-guessing implementation gets all of these wrong:

    * Questions take `instructions`, not `prompt`. `Choice.criteria` is a dict
      of label -> meaning; `Score.criteria` is a flat list of labels.
    * **Conviction is question-dependent.** On this question it measured
      1.04..2.44, median 2.01. On the sibling Ricky project's much harder
      question it measured 0.39..1.74, median 0.88 — so a threshold borrowed
      from there would reject almost everything here, and the obvious
      `min_conviction=2` would reject most of it too. 1.4 below is calibrated
      on this question's own distribution: real cases sat at 1.50 and above,
      invented nonsense at 1.04 and 1.23.
    * Probability on the chosen label ran 0.96..1.00 on the known-answer set
      and 0.92..0.93 on nonsense, which is why the gate is 0.95.

    Both gates were then checked against 22 live verdicts from the real
    ranking, and they fall in a gap rather than through a cluster: everything
    accepted scored 1.50 and above, everything gated scored 1.36, 1.38, 1.38.
    What the probability gate caught was short-story collections - 'Poirot
    Investigates' at 0.67, 'The mysterious Mr. Quin' at 0.59 - where NEW and
    NOT_SERIAL are both defensible. Declining to decide those is right.
    """

    name = "jev"

    # Measured: 742 input tokens median per call at the listed $0.042/Mtok,
    # about three thousandths of a cent. The cap exists to bound a bug, not a
    # budget.
    USD_PER_MTOK = 0.042

    def __init__(self, api_key, model, *, timeout_s=20.0, daily_usd_cap=0.25,
                 min_probability=0.95, min_conviction=1.4,
                 base_url=None, client=None):
        if not api_key:
            raise ValueError("JevAdjudicator requires an API key")
        if not model:
            raise ValueError("pin the model version explicitly")
        self.api_key = api_key
        self.model = model
        self.timeout_s = timeout_s
        self.daily_usd_cap = daily_usd_cap
        self.min_probability = min_probability
        self.min_conviction = min_conviction
        self.spent_today = 0.0
        self.calls = 0
        self._day = date.today()
        self._client = client
        self._base_url = base_url

    def _get_client(self):
        if self._client is None:
            from typesafe_sdk import TypeSafeClient
            kw = {"api_key": self.api_key, "timeout": self.timeout_s}
            if self._base_url:
                kw["base_url"] = self._base_url
            self._client = TypeSafeClient(**kw)
        return self._client

    def _roll_day(self):
        if date.today() != self._day:
            self._day = date.today()
            self.spent_today = 0.0

    def _questions(self):
        from typesafe_sdk import Choice, Score
        return {
            "verdict": Choice(instructions=GUIDANCE, criteria=dict(MENU)),
            "conviction": Score(instructions="How clear-cut is this call?",
                                criteria=list(CONVICTION_LABELS)),
        }

    def judge(self, candidate, already_held):
        self._roll_day()
        if self.spent_today >= self.daily_usd_cap:
            return Decision(ok=False, reason="daily_spend_cap", model=self.model)

        t0 = time.monotonic()
        try:
            r = self._get_client().system_one(
                state=candidate.state(already_held),
                questions=self._questions(),
                model=self.model,
            )
        except Exception as e:                   # timeout, HTTP, TLS, DNS, auth
            return Decision(ok=False, reason=type(e).__name__, model=self.model,
                            latency_ms=1000 * (time.monotonic() - t0))
        latency = 1000 * (time.monotonic() - t0)

        try:
            a = r.choices["verdict"]
            s = r.scores["conviction"]
            verdict = a.choice
            prob = float(a.probabilities.get(verdict, a.confidence))
            conf = float(a.confidence)
            conviction = float(s.score)
            tokens = int(getattr(r.usage, "input_tokens", 0) or 0)
        except (KeyError, AttributeError, TypeError, ValueError) as e:
            return Decision(ok=False, reason=f"malformed:{type(e).__name__}",
                            model=self.model, latency_ms=latency)

        cost = tokens * self.USD_PER_MTOK / 1e6
        self.spent_today += cost
        self.calls += 1

        if verdict not in MENU:
            return Decision(ok=False, reason="off_menu", model=self.model,
                            cost_usd=cost, latency_ms=latency)

        # The gates apply to the accept path only. A low-confidence DUPLICATE
        # and a low-confidence NEW both come out as "do not take this book",
        # which is the same safe action, so there is no need to distinguish.
        gated = ""
        if prob < self.min_probability:
            gated = f"low_probability:{prob:.3f}"
        elif conviction < self.min_conviction:
            gated = f"low_conviction:{conviction:.2f}"

        return Decision(
            ok=True, verdict=verdict, probability=prob, confidence=conf,
            conviction=conviction, model=getattr(r, "model", self.model) or self.model,
            cost_usd=cost, latency_ms=latency, reason=gated,
            accept=(verdict == "NEW" and not gated),
        )


def from_config(cfg, *, client=None):
    """Build the configured adjudicator, or the refusing one if no key is set."""
    key = getattr(cfg, "JEV_API_KEY", None)
    if not key:
        return RefuseAdjudicator("no JEV_API_KEY configured")
    return JevAdjudicator(
        api_key=key,
        model=getattr(cfg, "JEV_MODEL", "jev-1.13.0"),
        daily_usd_cap=float(getattr(cfg, "JEV_DAILY_USD_CAP", 0.25)),
        client=client,
    )
