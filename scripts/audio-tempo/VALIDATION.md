# Validation Record

Tested on October 5, 2026: Windows, a 6-core / 12-logical-processor desktop,
.NET SDK 9.0.304, FFmpeg 8.1.2 full build, WoW Forever 1.60.1.

| Pack | Speech clips |
| --- | ---: |
| Alliance | 2,832 |
| Horde | 2,373 |
| Shared | 3,646 |
| Gossip | 4,112 |
| Total | 12,963 |

- Every installed source passed native OGG structure/CRC parsing.
- Four negative fixtures were rejected: truncated, CRC-corrupt, chained, empty.
- The converter built with zero compiler warnings or errors.
- Every faster candidate passed full FFmpeg decode, format, and tempo checks.
- 406 decode/checkpoint groups covered the corpus exactly once.
- Every candidate timing literal matched its measured duration; other table
  text was unchanged. An independent review checked all checkpoints/tables.
- Both previously tested Altered Beings faster clips were reused unchanged;
  their true original backups seeded the new baseline, preventing double speed.
- All 12,963 speech files and four timing tables were installed and hash-verified.
- The standalone installed-state validator passed for all 12,967 target files.
- 263 player/core and other nonvoice files retained baseline hashes; no
  replacement temporary files remained after deployment.
- Source speech: 63.351830 hours. Faster speech: 42.255276 hours.
- Conversion/checkpoint interval: approximately 16 minutes. This excludes
  source inventory, benchmarking, final validation, and deployment.

The 32-clip benchmark included encoding and full decode:

| Workers | Clips per second |
| ---: | ---: |
| 1 | 3.37 |
| 2 | 5.36 |
| 4 | 7.31 |
| 8 | 8.64 |

Eight workers were selected for that machine. These are sample measurements,
not throughput guarantees for another corpus or machine.

## Proof Boundary

The user confirmed that the Altered Beings pilot worked well in-game before
authorizing the full rollout. The full corpus has build, full-decode, timing,
and installed-file proof, but broad in-game playback is not yet confirmed.
Full-corpus restoration is implemented but has not been round-trip tested.
The two-clip pilot round trip does not prove full-corpus restoration.

The four English 2.2.1 packs and the installation-specific baseline are the
tested scope. Other layouts, corpus versions, operating systems, paths, and
new recovery mechanisms require their own validation.

## Adaptive Pacing (October 7, 2026)

Same machine and corpus. faster-whisper 1.2.1 `small.en`, pinned revision, one
resident CUDA process, eight candidate workers.

- 12,963 / 12,963 clips analyzed from preserved originals: 10,708 proposals
  (7,780 tempo-encoded, 2,928 kept at factor 1.0) and 2,255 flagged clips kept
  as byte-identical originals (854 already above the fast-passage ceiling,
  most of the rest too short for phrase-window measurement).
- Analysis, build, and integrated validation: 104.3 minutes.
- Standalone validator passed: all source and candidate hashes, 12,963 full
  decodes, exact output coverage, four measured timing tables, source stage
  manifest unchanged.
- `deploy-adaptive` installed and post-verified all 12,967 target files; the
  standalone installed validator reported TargetState Adaptive; no replacement
  temporary files remained.

Not yet proven: in-game playback of the adaptive set, and an adaptive-to-original
full-corpus restore round trip. The calibration came from a two-quest listening
pilot, not a broad listening survey.
