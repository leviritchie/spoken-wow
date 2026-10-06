# 1.5x Quest Speech: Validated Reference Tool

This fork adds the source of a tested local audio-tempo conversion to
[Rusty Key's Spoken project](https://github.com/rusty-key/spoken-wow). Upstream
addon code, filenames, lookup behavior, and license notices are unchanged.

**No original or converted recordings are published by this addition.** The
upstream MIT license covers project-owned code, not generated audio, game text,
artwork, or vendored libraries. Read the repository's `LICENSE` and
`THIRD_PARTY.md`. Obtain voice packs from the original project's official
distribution; do not interpret this fork as permission to redistribute them.

## Scope

The reference implementation applies FFmpeg `atempo=1.5` with unchanged pitch
and `libvorbis` quality 5 to Spoken Quests Audio 2.2.1's four English packs:
Alliance, Horde, Shared, and Gossip. It changes only their speech OGGs and the
matching numeric duration entries in `generated/sound_length_table.lua`.
Player/core code, settings, progression, and the player's non-speech WAVs are
not touched. The frozen corpus has 12,963 clips.

This is the **exact installation-specific implementation that was tested**,
not a universal installer. `SpokenAudioBatch/Program.cs` explicitly pins the
Windows AddOns directory, original/candidate staging directory, and the
earlier Altered Beings pilot directory. Its inventory requires that pilot's
validated manifest and preserved originals to prevent processing already-fast
clips twice. Those backups and recordings are deliberately not in this fork.
It will fail without that local prerequisite. Porting this profile to other
paths or replacing the pilot prerequisite requires new validation; do not
represent such changes as already tested.

The tested addon/player versions were SpokenQuests 2.2.1 / SpokenPlayer 2.3.2
on WoW Forever 1.60.1; these are the tested baseline, not a claim about the
latest upstream versions. Upstream subsequently consolidated its addon layout
in the 3.0.0 series. This tool does not migrate the player or claim compatibility with
new corpus versions, languages, books, or zone packs.

## Build And Actions

Requires the .NET 9 SDK, Windows, and FFmpeg with `libvorbis` on PATH. The
application uses only .NET standard libraries; no extra packages are required.
Keep build artifacts and audio staging on a drive with sufficient space.

```powershell
dotnet build scripts/audio-tempo/SpokenAudioBatch/SpokenAudioBatch.csproj `
  --configuration Release --artifacts-path V:\SpokenTempoBuild

$tool = 'V:\SpokenTempoBuild\bin\SpokenAudioBatch\release\SpokenAudioBatch.dll'
dotnet $tool help
# The following require the pinned installation and preserved pilot baseline:
dotnet $tool inventory
dotnet $tool benchmark --count 32
dotnet $tool build --workers 8
# Close the target WoW client before either live-write action:
dotnet $tool deploy
dotnet $tool validate
dotnet $tool restore
```

`inventory` refuses an existing stage. `benchmark` measures 1, 2, 4, and 8
workers; choose using measurements rather than assuming eight is optimal.
`build` performs no live installation writes. `deploy` requires every candidate
to be built and checks the entire live inventory before replacement. `validate`
requires a complete faster installation; original or recognized partial states
are failures. `restore` uses the preserved true originals, not official-download
assumptions or regenerated files. Full-corpus restore has not been round-trip
tested; the earlier two-clip pilot restore was tested.

## Validation And Recovery

Source and candidate OGGs are read natively with SHA-256 and complete page CRC,
stream/header, sequence, bounds, and EOS checks. Every candidate is fully
decoded by FFmpeg, in groups of 32 to amortize decoder startup. Encoding uses
bounded workers with one encoder thread each. Timing values come from actual
decoded-stream granule durations, not an assumed division of the old table.
The validator enforces fixed corpus coverage, known hashes, the pinned recipe,
codec/sample-rate/channel preservation, duration tolerance, exact measured
timings, and unchanged table text outside those numbers.

Completed immutable build checkpoints can be reused after a restart.
An interruption before the current chunk's checkpoint, during initial inventory,
or during manifest/checkpoint publication can leave incomplete evidence that
requires explicit inspection. There is no automatic retry or deletion/overwrite
of unfamiliar files. Replacement failure cleanup removes only a temporary file
that this invocation successfully created. A process-wide stage lock excludes simultaneous tool
writers. Installation is atomic **per file**, with each pack's table last,
not a multi-file transaction. A recognized partial deployment can converge
through `deploy` or `restore`; unknown changes are blockers.

`OggReaderTests` verifies the installed corpus and rejects generated truncated,
CRC-corrupt, chained-stream, and empty fixtures. Supply an AddOns directory
and a new fixture directory to its compiled executable. It writes fixtures
only to that directory, not to the game. See `VALIDATION.md` for achieved proof.

This is an unofficial fan-work fork, not an upstream release or endorsement by
Rusty Key, Blizzard Entertainment, ElevenLabs, or Fish Audio. Upstream copyright, MIT
notice, and third-party notices remain in their original files.
