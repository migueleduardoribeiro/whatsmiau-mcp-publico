import importlib.util
from pathlib import Path
import tempfile
import unittest

MODULE = Path(__file__).parent / 'upstream/prepare_conversation_diagnostics.py'
spec = importlib.util.spec_from_file_location('conversation_diagnostics_patch', MODULE)
patch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patch)


class ConversationPatchTests(unittest.TestCase):
    def test_idempotent_and_preserves_existing_route(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path in ['server/routes', 'server/controllers', 'lib/whatsmiau']:
                (root / path).mkdir(parents=True)
            route = root / 'server/routes/chat.go'
            route.write_text('\tgroup.POST("/syncMessages", controller.SyncChatMessages)\n')
            (root / 'lib/whatsmiau/sync.go').write_text('\tch     chan *events.HistorySync\n\tFromMe     *bool\nanchorTS := time.Now().Unix()\n\t\tvar sinceTS int64\n')
            (root / 'lib/whatsmiau/event_emitter.go').write_text('historySyncMatchesChat(hs, w.chat)\n')
            patch.apply(root)
            once = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*.go')}
            patch.apply(root)
            twice = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*.go')}
            self.assertEqual(once, twice)
            self.assertEqual(route.read_text().count('group.GET('), 1)
            self.assertIn('group.POST("/syncMessages"', route.read_text())

    def test_unrecognized_upstream_changes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'server/routes').mkdir(parents=True)
            route = root / 'server/routes/chat.go'
            route.write_text('unexpected upstream')
            with self.assertRaises(ValueError):
                patch.apply(root)
            self.assertEqual(route.read_text(), 'unexpected upstream')
            self.assertEqual(len(list(root.rglob('*.go'))), 1)


if __name__ == '__main__':
    unittest.main()
