"""AI-generated clips for the movies in the library.

The pipeline is deliberately transcript-first: subtitles are read before any
model is asked anything, the model only ever sees text, and the picture is
touched only to cut the clips it chose. See `pipeline.run_clip_job` for the
stage order.

Nothing in this package imports the web application; the job runner passes in
a `pipeline.ClipMedia` and a set of callbacks, so the pipeline can be run and
tested without Flask.
"""

from __future__ import annotations

from .pipeline import PIPELINE_VERSION, ClipJobCancelled, ClipMedia, Hooks, run_clip_job

__all__ = [
    "PIPELINE_VERSION",
    "ClipJobCancelled",
    "ClipMedia",
    "Hooks",
    "run_clip_job",
]
