# Training boundary

## What enrollment is

Creating a voice copies an owner-supplied recording, selects a reference clip,
normalizes a mono 24 kHz reference, and uses that reference as **conditioning**
for Chatterbox inference. No optimizer runs, no weights change, and no
"trained model" is produced. The UI and CLI say **Create voice**, **Record
sample**, **Upload sample**, and **Generate speech** for this reason.

The service keeps the original and the normalized reference so a separate,
properly scoped training extension could reuse them with permission. It does not
keep a persistent embedding or conditioning cache; conditioning is prepared on
the model in the worker for each render and restored afterward.

## Why no `train` command ships

The inspected upstream inference documentation and the pinned Turbo
implementation (`src/chatterbox/tts_turbo.py`) describe loading a checkpoint,
preparing conditionals from a reference, and generating audio. They do not
establish a supported, turnkey trainer or training-to-inference checkpoint
contract for this exact variant. Consequently this repository provides no
`train` command and no copied training script. A command that only stored a
recording under a "training" label would be misleading and is not implemented.

## Gate for any future fine-tuning extension

Before adding gradient-based fine-tuning, independently verify all of:

1. A maintained trainer for the **exact** Chatterbox Turbo variant and pinned
   upstream commit. A generic TTS fine-tuning recipe is not enough.
2. Trainer license and checkpoint redistribution terms, including the model's
   MIT terms and any added dependencies.
3. Dataset contract: audio format/rate, text transcriptions, speaker split,
   validation strategy, and minimum data.
4. Checkpoint loader compatibility: can the produced checkpoint be loaded by
   the pinned inference code, and does it preserve watermark behavior?
5. Resource feasibility on the Pascal 8 GB target or a documented larger host.
6. A real training-to-inference round trip with held-out text and an actual
   listening evaluation, not just a loss number.
7. A separate consent, retention, and deletion policy for training data.

None of these is claimed today. Ordinary enrollment and TTS do not depend on
this gate, and no training code should be added to make a UI checkbox appear
complete.

## Current training-related records

- `metadata.model_weights_updated: false`
- `metadata.reference_conditioned: true`
- artifact manifest `notice`: reference conditioning only
- original and normalized references retained under private runtime storage
- profile deletion removes both; uninstall retains data unless deletion is
  explicitly requested
