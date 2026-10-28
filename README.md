# Distributed Storage Lab

A 4-node [SeaweedFS](https://github.com/seaweedfs/seaweedfs) object store on 2 simulated racks,
broken on purpose in six ways, with predictions written before each failure and measurements after.

> **Results** _(filled in after the experiments)_
> - _line 1: data durability across the six failures_
> - _line 2: the most surprising result_
> - _line 3: performance impact of a degraded node_

## Architecture

```
 load.py / verify.py / bench.py
            │  S3 API
            ▼
       S3 gateway ──► filer (namespace, metadata)
                         │
                         ▼
                      master (volume placement, topology)
                         │
        ┌────────────────┴────────────────┐
     rackA                              rackB
  vol1    vol2       ◄── 010 ──►     vol3    vol4
```

Replication `010`: every volume has one copy on each rack. The filer keeps the namespace; the S3
gateway gets file ids through it, then sends the data straight to the volume servers chosen by the master.

## Quick start

Prerequisites: Docker with Compose v2, `make`, `python3` and `python3-venv`.

```bash
make up        # start the cluster, wait until it serves traffic
make load      # write 1 000 objects (1 KiB to 16 MiB) and record their SHA-256
make verify    # read them back and compare hashes
make placement # check that every volume has a copy on each rack
make help      # every other target
```

On WSL2: clone inside the Linux file system (`~/`), not under `/mnt/c`, and give WSL at least
6 GB of memory in `%UserProfile%\.wslconfig`:

```ini
[wsl2]
memory=6GB
```

Endpoints (bound to localhost): master UI `http://localhost:9333`, filer `http://localhost:8888`,
S3 `http://localhost:8333` (keys in `config/s3.json`).

## Failure scenarios

| # | Failure | Injection |
|---|---|---|
| 1 | A volume server dies | `make chaos ARGS="kill vol3"` |
| 2 | A whole rack goes down | `make chaos ARGS="rack-down B"` |
| 3 | A disk is lost | `make chaos ARGS="wipe vol2"` |
| 4 | The master goes down | `make chaos ARGS="stop master"` |
| 5 | Network partition | `make chaos ARGS="partition vol1"` |
| 6 | A slow node (50 ms, 1 % loss) | `make chaos ARGS="slow vol2"` |

Predictions, observations and explanations: [results/scenarios.md](results/scenarios.md).
While a fault is active, `make traffic` logs every operation and `make watch-placement` shows
the number of copies per volume.

## Repository layout

```
docker-compose.yml   master, filer, s3, vol1-vol4 (+ vol5 with `make vol5`)
config/s3.json       S3 credentials
scripts/load.py      object writer, and continuous read/write traffic during a fault
scripts/verify.py    integrity check against the SHA-256 manifest
scripts/placement.py copies per volume and per rack, as seen by the master
scripts/chaos.sh     fault injection (kill, rack, disk wipe, partition, netem, tcpdump)
scripts/weed.sh      weed shell inside the master container
results/             scenario reports, raw CSV and captures (raw data not committed)
```

## Status

- [x] Nominal cluster, load and integrity check
- [ ] Six failure scenarios and runbook
- [ ] Benchmarks and Prometheus / Grafana dashboard
- [ ] CI
