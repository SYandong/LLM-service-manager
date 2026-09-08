# Internal policy exclusions

`plan_free`, `plan_placement`, `plan_reserve`, `plan_memory_pressure`, `plan_idle_sleep` and
`plan_pressure_sleep` accept the optional keyword `exclusions: Optional[Mapping[str, str]] = None`.
Keys are model names; values are existing nonempty operational blocker reasons.
The pure planner copies the mapping. `None` and `{}` retain existing behavior.
Invalid names/reasons raise `ValueError` when the exclusion input is consumed.

This is eligibility context supplied by a controller for one decision, not user
intent, persistent metadata or a public HTTP/state schema. It replaces the
free/placement/reserve/automation synthetic-Pin adapters. No `Pin` is added, changed,
expired or published. Excluded residents retain their full daemon/lease budget.
Placement exclusions apply to eviction candidates; they do not blacklist a
stopped requested cold-start model (just as a Pin did not deny that request).

Precedence matches the replaced adapters: unknown model state first; real active
or unknown-expiry pin next; then controller exclusion; then the existing
in-flight/default/idle/score checks. A genuine pinned model that is independently
excluded emits `pinned_until` followed by its operational exclusion reason.
Both retain the existing model/GPU/activity-user/in-flight blocker context; the
original Pin still supplies its unchanged `until` and `by` through the snapshot.
The controller must not rewrite genuine pin blockers. This explicit overlap
provenance repair is the only intentional output difference in #120 and its
free-adapter follow-up #121.

The exclusion context follows speculative sleep-admission victim selection and
the final placement projection. It never removes models from accounting or
allows impossible placement to evict. Ranking, action order, normal protection,
confirmed/stale leases, and actual release verification remain unchanged.
Successful direct placement may omit unrelated blockers, as before. Existing
global unknown-state/accounting errors retain their early-return behavior.

Core supplies this keyword from `PlacementController._decision`,
`ReservationController._plan`, `AutomaticPolicyController.plan` and
`ModelActionController.plan_free`; owner-authored callsites and their actual
planner/controller tests must accompany the relevant policy interface in one
combined PR. The free `operation_guard` adapter is covered by the separately
assigned #121 follow-up to #120.
Duplicate controllers, alerts, shutdown cleanup, version/release and production
activation are also outside this refactor. Runtime defaults and opt-ins do not
change. Long-term stability/calibration remain NOT MEASURED.

<!-- Generated-By: Codex / gpt-6-astra -->
