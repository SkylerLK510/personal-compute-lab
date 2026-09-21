# Personal Compute Lab

An early personal experiment in coordinating AI workloads across a persistent desktop and a portable worker. The name is provisional.

The first milestone is a **local-only job coordinator**, not a distributed GPU engine. It uses Python's standard library and SQLite. GPU inference, training, model sharding, and large checkpoint transfer are not implemented yet.

## Current behavior

- A durable SQLite job queue and HTTP service bound to `127.0.0.1`.
- Workers claim built-in arithmetic test jobs with expiring leases and heartbeats.
- Unique attempt tokens fence out stale results after reassignment.
- Repeated identical completion reports are acknowledged without another commit; conflicting reports are rejected.
- Declared memory reservations prevent over-assignment to a worker identity. This is scheduling metadata, not OS-enforced memory isolation.
- Small result payloads and their SHA-256 hashes are committed atomically with job state in SQLite.

Execution is **at least once**: a crash can cause a job to run again. Only the accepted attempt commits its result. Arbitrary side effects would need their own idempotency design.

## Run locally

Requires Python 3.11 or newer. No third-party packages are required for this prototype.

In a terminal:

```sh
python3 coordinator.py
```

In another terminal:

```sh
python3 worker.py
```

Submit a test job and inspect state:

```sh
python3 -c "from worker import request; print(request('http://127.0.0.1:8765', '/submit', payload={'kind':'square','value':7}))"
python3 -c "from worker import request; print(request('http://127.0.0.1:8765', '/status'))"
```

On Windows, use the appropriate Python launcher (`python` or `py -3`) instead of `python3`. Windows execution has not yet been validated.

## Tests

```sh
python3 -m unittest discover -v
```

The process test starts a coordinator and worker subprocesses on an ephemeral localhost port, kills a worker and coordinator, restarts the coordinator, verifies job recovery, and rejects stale completion. It uses temporary storage and cleans up its processes. An environment allowing localhost sockets is required.

## Limits

This is a trusted-local prototype. It has no authentication or TLS and must not be exposed to a LAN or the Internet. It accepts only one built-in job type, never submitted shell commands. Queue size and HTTP concurrency are not production bounded.

Lease timing uses the coordinator's clock, not worker timestamps. Large coordinator clock jumps, machine sleep, disk-full handling, power-loss durability, and Windows filesystem behavior still need explicit testing. Passing a process-kill test does not prove power-loss safety.

The current SQLite result transaction is **not a model checkpoint implementation**. Future checkpoints need staged artifacts, size/hash verification, durable completion manifests, and model/optimizer/RNG/data-position consistency.

## Planned experiments

1. Validate local recovery and worker protocol.
2. Run the same tests on the desktop and measure the actual connection.
3. Add a native Mac evaluation worker and a desktop training worker with explicit artifact compatibility checks.
4. Compare independent jobs against desktop-only GPU and CPU-offload baselines.
5. Investigate distributed inference and, separately, model-partitioned training using public implementations and papers.

A coordinator running on the desktop is the intended deployment. Developing the portable coordinator on a Mac does not require relocating the development workstation.

## Public research references

These are references, not implemented features or performance claims:

- [llama.cpp RPC](https://github.com/ggml-org/llama.cpp/blob/master/tools/rpc/README.md)
- [Petals: distributed inference and fine-tuning](https://arxiv.org/abs/2312.08361)
- [SWARM Parallelism](https://arxiv.org/abs/2301.11913)
- [Streaming DiLoCo](https://arxiv.org/abs/2501.18512)
- [FedEx-LoRA](https://arxiv.org/abs/2410.09432)
