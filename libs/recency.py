"""Time-windowed health-signal primitive: is a bad signal recent, or historical?

Why this module exists (#531): `libs/vault_self_refresh_audit.py` accumulated
FOUR separate instances of the same bug -- a check that counts/inspects
something without ever asking *when* it happened, so it eventually reports
long-resolved history as an ongoing problem. The concrete instance that made
this a structural fix rather than a one-off patch: Docker's `RestartCount` is
a *lifetime cumulative* counter (it only resets on container recreation), so
`restart_count > threshold` alone reports a container as permanently FAILING
for as long as it keeps running, even after it has been rock-solid for
weeks. Docker exposes no "restarts in the last N minutes" API -- only the
cumulative count and `State.StartedAt` (when the *current* run began, i.e.
the time of the most-recent restart). Deriving recency from that pair is the
only Docker-data-model-honest fix; there is no count to "decay" because
Docker never gave us per-restart timestamps to decay against.

This module is the one place that reasoning lives, instead of every caller
re-deriving (and likely re-getting wrong) the same "count + last-seen-time"
logic independently. It is intentionally generic over "count + timestamp +
now + thresholds" rather than shaped around Docker's field names, so it can
be reused anywhere the same question comes up.

See #531 for the full investigation (4 confirmed instances, 2 fixed in
PR #532, the remaining 2 -- including this module's own first consumer,
`classify_container`'s restart-count check -- fixed alongside this module).

## A second, complementary primitive: consecutive-poll hysteresis (#475)

`is_recently_flapping` answers a ONE-SHOT snapshot question: given a
cumulative count and a last-occurrence timestamp read once, is the bad
signal still live? That fits a check that samples Docker's lifetime
``RestartCount`` a single time (e.g. a daily audit).

`tools/container_breakdown_watch.py` (#475) is a different shape: a
continuously-running poller that calls its sweep function repeatedly and
needs to know "how many CONSECUTIVE polls in a row has this thing been
broken" and, symmetrically, "how many consecutive polls has it now been
healthy" -- state that must be threaded across polling invocations, not
evaluated from a single count+timestamp pair. Trying to force that through
`is_recently_flapping` doesn't fit: its ``event_count`` is monotonic
(cumulative, never decremented -- there is deliberately no way to "decay"
it, per the module docstring above), whereas consecutive-streak tracking
requires the streak to reset to zero the instant an opposite observation
is seen. That reset-on-opposite-observation behavior is the entire point
of hysteresis and is fundamentally different math, not a parameterization
of the same function.

`ConsecutiveObservationState` + `evaluate_consecutive_hysteresis` below are
that second primitive. They generalize the state machine already proven in
`tools/infra_probe_runner.py`'s `_should_send`/`_record_sent`/
`_record_resolved` (failure-count-to-fire, recovery-count-to-resolve,
renotify-gated re-alerts on an already-active incident) so
`container_breakdown_watch.py` doesn't reimplement that reasoning from
scratch. `infra_probe_runner`'s own fingerprint-based per-probe-group
dedup is left as-is (a probe GROUP's failing-member-set changing is a
distinct concern with no analogue here); only the fire/recover/renotify
skeleton is shared.
"""

from __future__ import annotations

from dataclasses import dataclass, field


def is_recently_flapping(
    *,
    event_count: int,
    last_event_at: float,
    now: float,
    count_threshold: int,
    recency_window_seconds: int,
) -> bool:
    """True only if a bad signal is BOTH frequent AND still recent.

    Answers "is this thing's bad signal recent, or just historical" for any
    counter-plus-last-seen-timestamp pair:

    - ``event_count``: cumulative/lifetime count of the bad event (e.g. a
      container's Docker ``RestartCount``, or a future consumer's own tally).
    - ``last_event_at``: epoch seconds of the most recent occurrence (e.g.
      Docker's ``State.StartedAt`` -- the start of the run following the last
      restart). Pass ``0`` (or any epoch far in the past) when there is no
      known last-occurrence time; that naturally fails the recency check
      below rather than needing special-cased handling here.
    - ``now``: epoch seconds "now" -- always pass this explicitly (don't let
      this function call ``time.time()`` itself) so callers stay
      deterministic and unit-testable.
    - ``count_threshold``: the count must EXCEED this (strictly greater than,
      matching the pre-#531 `restart_count > max_restart_count` convention)
      for the signal to matter at all, regardless of recency.
    - ``recency_window_seconds``: the most recent occurrence must be within
      this many seconds of ``now`` (inclusive: ``age <= window`` counts as
      recent) for the signal to still be considered "live."

    Both conditions must hold. A count that is high but whose last occurrence
    is outside the window is historical, not current -- e.g. a container that
    restart-looped 1700+ times weeks ago and has been stable ever since. A
    count that is still below/at the threshold never flags regardless of
    recency, including when there has never been a qualifying event at all
    (``event_count`` starts at/below the threshold, e.g. 0).

    If ``last_event_at`` is in the future relative to ``now`` (clock skew, or
    a caller passing a not-yet-elapsed timestamp), age is clamped to 0 --
    i.e. treated as maximally recent. This is a deliberate fail-safe: it is
    better to over-flag on a clock anomaly than to silently swallow a real
    signal because of a negative age.
    """
    if event_count <= count_threshold:
        return False
    age_seconds = max(0.0, now - last_event_at)
    return age_seconds <= recency_window_seconds


@dataclass
class ConsecutiveObservationState:
    """Persisted per-key hysteresis state for a repeatedly-polled bad/healthy signal.

    One instance per monitored thing (e.g. one per container name). The caller
    owns storage/keying (a plain ``dict[key, ConsecutiveObservationState]`` kept
    in memory for the lifetime of a long-running poll loop is the expected
    usage -- see ``tools/container_breakdown_watch.py``); this class only holds
    the counters, it does no I/O and does not know about keys.
    """

    active: bool = False
    bad_streak: int = 0
    good_streak: int = 0
    last_alert_at: float = 0.0
    # Free-form payload the caller can stash and retrieve on each poll (e.g. the
    # most recent breakdown reason/detail), so the eventual RESOLVED message can
    # reference the same incident it originally fired on. Never inspected here.
    context: object = field(default=None)


def evaluate_consecutive_hysteresis(
    *,
    state: ConsecutiveObservationState,
    is_bad_now: bool,
    now: float,
    failure_threshold: int,
    recovery_threshold: int,
    renotify_seconds: int,
) -> str:
    """Advance ``state`` by one poll observation; return ``"fire"``, ``"resolve"``,
    or ``"none"``.

    Mutates ``state`` in place (the streak counters ARE the persisted state --
    there is nothing else to save) and returns what the caller should do this
    poll:

    - ``"fire"``: post/refresh the alert. Happens either the first time
      ``bad_streak`` reaches ``failure_threshold`` (a NEW incident), or on any
      later bad poll of an already-active incident once ``renotify_seconds``
      has elapsed since the last alert (a periodic re-notify of the SAME
      incident -- ``last_alert_at`` advances, but the incident's identity
      does not reset).
    - ``"resolve"``: post RESOLVED. Only once ``good_streak`` reaches
      ``recovery_threshold`` CONSECUTIVE healthy polls while an incident is
      active.
    - ``"none"``: no action -- either the signal hasn't crossed a threshold
      yet, or an active incident's renotify window hasn't elapsed, or there
      was never an active incident and this poll is healthy (steady-state).

    The critical hysteresis property (#475's ``ContainerBreakdown``
    fire/resolve storm -- 333 firing + ~equal resolved in 48h from 15-minute
    blips): a bad observation seen WHILE an incident is active but before
    ``recovery_threshold`` is reached does NOT start a new incident and does
    NOT reset the renotify timer. ``active`` only flips back to ``False`` via
    a full ``"resolve"``, so a blip during the recovery window just resets
    ``good_streak`` to 0 and falls through to the "already active" renotify
    check -- ``last_alert_at`` is untouched unless the renotify window has
    actually elapsed. A transient blip therefore never produces a fresh
    firing/resolved pair.

    Streaks are strictly consecutive: any bad poll zeroes ``good_streak``,
    any good poll zeroes ``bad_streak``, every poll (regardless of direction).
    """
    if is_bad_now:
        state.good_streak = 0
        state.bad_streak += 1
        if not state.active:
            if state.bad_streak < max(1, failure_threshold):
                return "none"
            state.active = True
            state.last_alert_at = now
            return "fire"
        # Already active: same still-active incident. Only re-notify (and
        # advance the renotify clock) once the window has elapsed -- a bad
        # poll on an already-firing incident must never look like a new one.
        if now - state.last_alert_at >= renotify_seconds:
            state.last_alert_at = now
            return "fire"
        return "none"

    state.bad_streak = 0
    if not state.active:
        return "none"
    state.good_streak += 1
    if state.good_streak < max(1, recovery_threshold):
        return "none"
    state.active = False
    state.good_streak = 0
    state.last_alert_at = 0.0
    return "resolve"
