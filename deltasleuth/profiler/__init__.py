"""ds-profiler: drafts a comparison spec and an evidence table from paired samples."""

from deltasleuth.profiler.draft import bootstrap
from deltasleuth.profiler.model import DraftSpec, EvidenceRow, ProfilerError

__all__ = ["DraftSpec", "EvidenceRow", "ProfilerError", "bootstrap"]
