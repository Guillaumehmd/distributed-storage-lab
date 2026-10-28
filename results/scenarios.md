# Failure scenarios

Each scenario is written **before** it is run: the prediction is committed first, so the
Git history shows what was expected versus what happened.

## Protocol (same for every scenario)

1. Known state: `make chaos ARGS=heal-all`, then `make verify` and `make placement` both clean.
2. Write the prediction below, then `git commit -m "phase2: scenario N prediction"`.
3. Three terminals:
   - T1 `make traffic LABEL=sN DURATION=180` (read/write traffic, every operation logged)
   - T2 `make watch-placement` (copies per volume every 5 s)
   - T3 inject the fault about 30 s after T1 starts, using the command of the scenario.
4. After the measurement window: repair, then `make verify` and `make placement`.
5. Fill in observations and explanation, commit (`phase2: scenario N results`).

Raw data: `results/raw/sN-*.csv` (one line per operation) and `results/raw/events.log`
(UTC time of every injection), to line up faults with errors and latency.

---

## Scenario 1: a volume server dies

- **Injection:** `make chaos ARGS="kill vol3"` (crash: SIGKILL, no clean shutdown).
- **Question:** do reads and writes keep working? What happens to the volumes it held a copy of?
- **Measures:** error rate during 2 min; time to get back to 2 copies per volume.

### Prediction
- Reads: a few errors for less than a second, on requests already sent to vol3; then none, the other copy serves them.
- Writes: failures only on volumes vol3 holds, until the master notices the missing heartbeat (one interval, about 5 s). After that, writes go to volumes that still have both copies: no more client errors.
- Missing copies: the master re-creates them on its own, on vol4, without restarting vol3.

### Observations
| Measure | Value |
|---|---|
| Write errors during 2 min | |
| Read errors during 2 min | |
| Time back to 2 copies | |
| `make verify` | |

### Explanation

### Prediction vs result

---

## Scenario 2: a whole rack goes down

- **Injection:** `make chaos ARGS="rack-down B"` (stops vol3 and vol4), later `rack-up B`.
- **Question:** with replication 010, is everything still readable? Can the cluster still accept writes?
- **Measures:** objects readable out of 1 000; write errors.

### Prediction
- Reads: requests sent to rack B fail for about 5 s, then rack A serves everything.
- Writes: fully blocked while rack B is down, since 010 forbids both copies in rack A. After about 5 s, the client gets an error saying the servers are unavailable for writing.
- Rack B back: writes do not resume on their own; commands will be needed.

### Observations
| Measure | Value |
|---|---|
| Objects readable (`make verify` during the outage) | |
| Write errors | |
| `make verify` after recovery | |

### Explanation

### Prediction vs result

---

## Scenario 3: a disk is lost

- **Injection:** `make chaos ARGS="wipe vol3"` (stop, erase `/data`, restart empty).
- **Question:** does the cluster detect the missing copies by itself? What does it take to rebuild them?
- **Measures:** time back to 2 copies; `make verify`.

### Prediction
- Writes: continue normally; an assigned file id stays valid on an empty volume, no corruption.
- Detection: on restart, vol3's heartbeat lists no volume; the master, knowing the 010 rule, sees that the volumes vol3 held lack a copy.
- Repair: the master re-synchronises them on its own, in under 2 minutes.
- Reads of objects written before the wipe: those landing on vol3 fail until the resync ends; those landing on the other copy succeed.
- Reads of objects written after the wipe: succeed.

### Observations
| Measure | Value |
|---|---|
| Missing copies detected (how, when) | |
| Action needed to rebuild | |
| Time back to 2 copies | |
| `make verify` | |

### Explanation

### Prediction vs result

---

## Scenario 4: the master goes down

- **Injection:** `make chaos ARGS="stop master"`, 2 min later `start master`.
- **Question:** do reads work? Writes? Is the single master a single point of failure?
- **Measures:** read and write errors during 2 min.

### Prediction
- During the 2 minutes: 100 % write errors (no master, no file id) and 100 % read errors (the filer can no longer locate volumes).
- After restart: back to normal within a few seconds, once heartbeats reach the master again; no command needed.
- Side effect, not measured here: while the master is down, no volume fault can be detected or repaired.

### Observations
| Measure | Value |
|---|---|
| Read errors during 2 min | |
| Write errors during 2 min | |
| Recovery after restart | |

### Explanation

### Prediction vs result

---

## Scenario 5: network partition

- **Injection:** `make chaos ARGS="partition vol1"`, 2 min later `heal vol1`.
- **Question:** what happens to writes during the cut? Are the copies identical afterwards?
- **Measures:** `make verify`; state in `make volume-list`.

### Prediction
- For the master, the cut looks like the crash of scenario 1: errors on the volumes vol1 holds stop after about 5 s, once the missing heartbeat is noticed.
- After reconnection: the two copies of vol1's volumes are not identical; vol1 missed the writes made during the cut.

### Observations
| Measure | Value |
|---|---|
| Write errors during the cut | |
| Copies after reconnection | |
| `make verify` | |

### Explanation

### Prediction vs result

---

## Scenario 6: a slow node

- **Injection:** `make chaos ARGS="slow vol2 50ms 1%"`, capture with `make chaos ARGS="capture vol2 30"`,
  then `unslow vol2`.
- **Question:** does one slow node slow down the whole cluster? Rather p50 or p99?
- **Measures:** p50 and p99 latency versus the nominal run; TCP retransmissions in the capture.

### Prediction
- Errors: yes, a few occasional ones caused by packet loss: a transfer hits a loss and has to be sent again.
- Latency: the delay itself causes no errors, it only slows things down. Reads and writes that involve vol2 both get slower: for a write, vol2's acknowledgement is delayed; for a read, the data vol2 sends back is delayed.
- Capture: retransmissions visible, caused by the 1 % loss.

### Observations
| Measure | Nominal | Slow node |
|---|---|---|
| p50 read | | |
| p99 read | | |
| p50 write | | |
| p99 write | | |
| Retransmissions in the capture | | |

### Explanation

### Prediction vs result

---

## Scheduling: adding a 5th volume server

- **Injection:** `make vol5`, then rebalance with `weed shell` (see `make weed CMD="help volume.balance"`).
- **Measures:** rebalancing time; effect on latency while it runs.

### Observations

### Explanation

---

## Scenario 7 (bonus): erasure coding

- **Injection:** `ec.encode` on a collection, then stop volume servers one by one.
- **Question:** how many servers can be lost? What space is saved versus replication?
