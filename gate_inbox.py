"""Durable Telegram inbox: acknowledge the gate only after fsync, replay after restart."""
import json, os
from pathlib import Path

class GateInbox:
    def __init__(self, path: Path):
        self.path = path
        self.records = json.loads(path.read_text()) if path.exists() else {}

    def _save(self):
        temp = self.path.with_suffix('.tmp')
        with temp.open('w') as f:
            json.dump(self.records, f)
            f.flush(); os.fsync(f.fileno())
        os.replace(temp, self.path)

    def receive(self, updates):
        for update in updates:
            self.records.setdefault(str(update['update_id']), {'update': update, 'done': False})
        self._save()

    def pending(self):
        return [r['update'] for r in self.records.values() if not r['done']]

    def complete(self, ids):
        for uid in ids:
            self.records[str(uid)] = {'done': True}
        done = [k for k,r in self.records.items() if r['done']]
        for key in done[:-10000]:
            self.records.pop(key)
        self._save()
