"""deptool — evidence gathering for AI-assisted dependency review.

Deterministic layer: discover declared dependencies, extract the API surface
we actually consume, resolve what exists upstream, and apply/verify bumps.
The judgement layer lives in the plugin's skill, not here.
"""

__version__ = "0.1.0"
