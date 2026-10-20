"""Full tenant/object identity for country lookup and persistence."""

from dataclasses import dataclass


@dataclass(frozen=True)
class AssetIdentity:
    """An Assets object is identified by cloud, workspace and object together."""

    cloud_id: str
    workspace_id: str
    object_id: str
