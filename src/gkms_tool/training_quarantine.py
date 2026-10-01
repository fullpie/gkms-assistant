"""Apply the indexed user quarantine without changing source identities or tiers."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CURRENT_INDEX = ROOT / 'var/research/strategy_redesign_current.json'


@dataclass(frozen=True)
class TrainingQuarantine:
    manifest_path: str
    manifest_sha256: str
    episode_ids: frozenset[str]
    source_episode: Mapping[str, str]

    def match(self, label, payload, source_ref):
        """Only explicit episode identities and per-episode source hashes match.

        A raw query response hash is deliberately not a per-episode identity:
        one such response can contain multiple unrelated histories.
        """
        payload = payload if isinstance(payload, Mapping) else {}
        mappings = [label.get('identity', {}), payload, payload.get('source', {}),
                    payload.get('identity', {}), payload.get('metadata', {})]
        matched = set()
        for value in mappings:
            if not isinstance(value, Mapping):
                continue
            episode = value.get('episode_id')
            if isinstance(episode, str) and episode in self.episode_ids:
                matched.add(episode)
            for key in ('source_sha256', 'replay_source_sha256'):
                source = value.get(key)
                if isinstance(source, str) and source in self.source_episode:
                    matched.add(self.source_episode[source])
        source = source_ref.get('file_sha256')
        if isinstance(source, str) and source in self.source_episode:
            matched.add(self.source_episode[source])
        return sorted(matched)

    def reference(self):
        return {'path': self.manifest_path, 'sha256': self.manifest_sha256,
                'isolated_episodes': len(self.episode_ids)}


def load_current_training_quarantine(index_path=None):
    """Resolve the one current project index, rather than guessing newest files."""
    index_path = CURRENT_INDEX if index_path is None else Path(index_path)
    if not index_path.exists():
        return None
    index = json.loads(index_path.read_bytes().decode('utf8'))
    package = index.get('current_phase', {}).get('active_work_package', {})
    reference = package.get('user_training_quarantine')
    if reference is None:
        return None
    path = Path(reference['path'])
    if not path.is_absolute():
        path = ROOT / path
    content = path.read_bytes()
    sha = hashlib.sha256(content).hexdigest()
    if sha != reference['sha256']:
        raise ValueError('indexed training quarantine changed')
    value = json.loads(content.decode('utf8'))
    if value.get('schema') != 'gkms.original-pc-training-quarantine.v1':
        raise ValueError('unsupported indexed training quarantine')
    episodes, sources = set(), {}
    for case in value['cases']:
        episode, source = case['episode_id'], case['source']['sha256']
        if (not isinstance(episode, str) or not episode
                or not isinstance(source, str) or len(source) != 64
                or any(c not in '0123456789abcdef' for c in source)
                or case.get('training_allowed') is not False
                or episode in episodes or source in sources):
            raise ValueError('invalid or duplicated quarantine identity')
        episodes.add(episode); sources[source] = episode
    if value.get('isolated_episodes') != len(episodes):
        raise ValueError('quarantine episode count differs')
    return TrainingQuarantine(str(path.resolve()), sha, frozenset(episodes), sources)
