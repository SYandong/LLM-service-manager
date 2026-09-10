# Native generation caller binding

`BoundNativeGenerationReader` surrounds the existing `NativeGenerationReader`
with local process, running-image, socket and configuration observations. It
selects exactly one explicitly pinned dialect. It never probes a second protocol
or turns visibility into quiet/helper/backend settlement.

`SchedulerConfig.native_witness` is empty by default. An explicit mapping has
`images` (1–16 entries, each `source_commit`, `executable_sha256`, `dialect`),
optional `request_timeout_seconds` (default0.5, at most60), and
`max_image_bytes` (default128MiB, at most512MiB). Only the registry reader's two
known source/dialect pairs are accepted; duplicate image digests are rejected.
These are trusted local build provenance pins. A configuration label alone does
not prove a source revision built a binary: the deployment owner must establish
that mapping from its artifact receipt.

`build_bound_generation_reader(config, instance_provider=..., config_reader=...,
config_provider=...)` constructs without invoking either source. The current
configuration callback should return the scheduler's current config, so replacing
or disabling settings/origin invalidates an existing reader. The instance
provider is the configured native service inspector, not a tenant-supplied PID.
The config reader is the existing bounded safe queue read returning bytes/stat.

`read(expected: CandidateBinding, deadline=...)` checks the configured service's
current PID/start ticks and the caller's same PID/network namespaces. It hashes
the open `/proc/<pid>/exe` image, rechecks image stat and process identity,
selects the matching provenance pin, and binds the configured TCP listener inode
to that process's open descriptor. Another process running the same executable
cannot satisfy this endpoint check. Ambiguous listener rows, unknown processes,
configuration mismatch or an unpinned executable reject before native HTTP.

The one native query is bracketed by fresh matching image/instance/listener and
configuration observations. The existing strict generation parser and visibility
freshness/deadline checks are reused. Same-PID `exec` can preserve start ticks;
its changed running image is still rejected. The returned `BoundGenerationRead`
contains the native reading, `VisibilityCheck`, matched pin and instance for
internal use. It adds no public HTTP or snapshot fields and logs no raw replies.

These are bounded sampled observations, not authenticated server identity or
proof against every unobserved change-and-restore. Socket descriptor possession
does not rule out an unobserved inherited duplicate elsewhere. Namespace or
listener ambiguity must remain unknown. Local filesystem/kernel calls cannot be
magically preempted; size/deadline checks reject late results without claiming
hard-real-time I/O. No source process is signalled or changed by this reader.

## Combined feature integration

This caller is prepared for the single #170 feature. Its final maintenance
consumer must supply the phase's expected old/new/restored identity and preserve
its provenance binding across the durable claim. It must call this reader before
accepting generation visibility while retaining independent settlement and
cleanup checks. The ordinary main bootstrap does not activate a native source
from these settings alone. The reviewed #192 maintenance implementation and ops'
actual native fixture are still prerequisites to the final combined runtime
mount and merge; this is not an independently deployable source switch.

Tests use actual owned Python processes, executable images and loopback sockets.
Their Python-image/source mapping and protocol replies are explicitly synthetic,
not an attestation of the native candidate build. No native producer, live GPU,
site source-switch or long-term coverage claim follows from those fixtures.

<!-- Generated-By: Codex / gpt-6-astra -->
