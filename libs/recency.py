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
be reused anywhere the same question comes up -- e.g. a future
`tools/container_breakdown_watch.py` hysteresis pass (#475) that wants
"flag only if broken repeatedly AND recently," not just Docker restart
tracking specifically.

See #531 for the full investigation (4 confirmed instances, 2 fixed in
PR #532, the remaining 2 -- including this module's own first consumer,
`classify_container`'s restart-count check -- fixed alongside this module).
"""

from __future__ import annotations


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
