# Two-machine validation notes

Results from the first run on the real hardware pair (planned experiments 1 and 2 in the
README). Addresses, user names and host names are placeholders on purpose; this
repository is public.

## Machines

| Role | Hardware | OS / Python |
| --- | --- | --- |
| Desktop (coordinator host) | 16 logical cores, NVIDIA RTX 5070 (12 GB, compute capability 12.0), ~16 GB RAM visible to WSL | Windows + WSL2 Ubuntu, Python 3.14 |
| Portable worker | MacBook Pro, Apple M4 Pro, 16-core GPU, 24 GB unified memory | macOS, Python 3.12 (managed by `uv`; the system 3.9 is too old) |

## Test suite on both machines

`python3 -m unittest discover` passes on both, on the same commit, including the process
test that kills a worker and the coordinator and verifies recovery. This retires the
README caveat that the intended desktop still needed validation for the coordinator.

The opt-in probe (`tools/hardware_probe.py --all`) was run for real on both machines and
its output was checked against the actual user name, host name, home path, Mac serial
number and hardware UUID: none appear. One defect was found on the desktop: newer NVIDIA
drivers label the banner field `CUDA UMD Version:`, so `cuda_version` came back `null`
(fixed separately).

## The link

The desktop has no Thunderbolt or USB4 controller, so the Mac's Thunderbolt ports cannot
be used for a direct link. The machines are connected with one Ethernet cable (the Mac
through a USB-C Ethernet adapter), no switch or router:

- Static addresses on a private /24 with **no gateway** on either side, so both machines
  keep using Wi-Fi for the Internet and only peer traffic uses the cable.
- macOS gotcha: each adapter ever attached gets its own network service. The address must
  be set on the service for the adapter that is actually plugged in, not a stale entry.

Measured desktop → Mac:

| Test | Result |
| --- | --- |
| Raw TCP, 8 s, stdlib sockets | 941 Mbit/s (118 MB/s), line rate for 1 GbE |
| 1 GB through SSH (encrypted) | ~107 MB/s |
| Round trip | ~0.4 ms |

These are single-stream, one-direction numbers from one run, not a benchmark. 1 GbE is
ample for job control and small results; it would be a bottleneck for model-partitioned
inference (experiment 5). The upgrade path is a 2.5/10 GbE NIC in the desktop.

## Cross-machine run without exposing the coordinator

The coordinator has no authentication or TLS and must stay on loopback. SSH provides both,
so the first cross-machine run used a reverse tunnel opened **from the desktop**:

```sh
# desktop, terminal 1: coordinator on 127.0.0.1 only
python3 coordinator.py

# desktop, terminal 2: run the worker on the Mac; its 127.0.0.1:8765 is tunnelled back
ssh -R 8765:127.0.0.1:8765 <mac-user>@<mac-ip> \
  'cd <repo-on-mac> && python3.12 worker.py --url http://127.0.0.1:8765 --once'
```

Three `square` jobs submitted on the desktop, with no worker running on the desktop, were
all completed by the Mac and committed with their SHA-256 digests. `ss -ltn` on the
desktop showed the coordinator bound to `127.0.0.1:8765` only. No code changes were
needed.

Both directions log in with SSH keys. The desktop's SSH server refuses passwords and its
firewall rule is scoped to the cable's adapter and subnet, so it is not reachable from
Wi-Fi. The Mac still uses the macOS Remote Login defaults.

## Follow-ups this exposed

- Workers identify themselves with a random UUID, so `/status` cannot show which machine
  ran a job. A worker label (not the host name, to keep reports shareable) would fix that.
- A coordinator hosted inside WSL2 sits behind NAT. The reverse tunnel sidesteps this; a
  Mac-initiated connection would need WSL mirrored networking or a native Windows
  coordinator.
