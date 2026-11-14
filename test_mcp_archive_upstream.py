import tempfile
import unittest
from pathlib import Path
from upstream.prepare_archive_metadata import apply


class UpstreamArchivePatchTests(unittest.TestCase):
    def test_unexpected_source_aborts_without_mutation(self):
        with tempfile.TemporaryDirectory(dir='.') as temporary:
            root = Path(temporary); directory = root / 'lib/whatsmiau'; directory.mkdir(parents=True)
            for name in ('models.go', 'event_emitter.go'): (directory / name).write_text('unexpected')
            with self.assertRaises(ValueError): apply(root)
            self.assertEqual((directory / 'models.go').read_text(), 'unexpected')
            self.assertEqual((directory / 'event_emitter.go').read_text(), 'unexpected')


if __name__ == '__main__': unittest.main()
