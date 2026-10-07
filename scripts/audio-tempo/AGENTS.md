# Audio Tempo Fork Scope

- Preserve root LICENSE, THIRD_PARTY.md, and original upstream attribution.
- Never add recordings, personal stage manifests, saved settings, credentials,
  build output, or fixture audio to git or GitHub releases without separately
  established redistribution permission.
- Audio belongs outside git. This fork adds source/reference evidence only.
- Program.cs is an installation-specific, frozen 2.2.1 corpus profile. Its
  hardcoded paths and validated pilot prerequisite are intentional limitations,
  not a general supported installer. Read README.md before executing it.
- The validator owns corpus, recipe, path, known-hash, timing, and format
  contracts. Do not bypass unknown hashes, overwrite originals, or add silent
  fallback/retry behavior to make another installation appear compatible.
- Keep static/build/decode/installed/runtime proof distinct. VALIDATION.md is
  achieved proof, not permission to claim newly changed code already tested.
- Do not modify upstream player/core code for tempo changes. The native audio
  data-pack path owns this change. Preserve clip stems and lookup contracts.
- `adaptive/` produces candidate-only output and never deploys. Only
  `SpokenAudioBatch deploy-adaptive` installs an adaptive run, and only the
  pinned run whose candidates bind to the stage's original hashes. Flagged
  clips stay byte-identical originals; do not add guessed factors for them.
