"""Research tooling (Phase 7).

Spec §17: implement the tools to answer basic questions first, and only reach
for machine learning once there is enough data to justify it. Hades V1 built a
12-model AI committee before establishing that any single component had an edge.
"""

from hades.research.analytics import Bucket, LabelledRecord, Summary, bucket_by, summarise

__all__ = ["Bucket", "LabelledRecord", "Summary", "bucket_by", "summarise"]
