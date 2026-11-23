"""Compile-time install of a board's declared libraries (multi-replica self-host).

The Library Manager's install and the compile can be served by different
replicas with different library dirs; the compiling replica installs whatever
of the manifest it lacks. No toolchain required: arduino-cli is faked.
"""

import asyncio
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / 'backend'))

from app.services import library_autoinstall as la


def _lib(root: Path, folder: str, name: str | None = None) -> None:
    d = root / folder
    d.mkdir(parents=True)
    if name:
        (d / 'library.properties').write_text(f'name={name}\nversion=1.0.0\n')


class _FakeCli:
    def __init__(self, libs_dir: Path, deps: dict[str, list[str]] | None = None):
        self.libs_dir = libs_dir
        self.deps = deps or {}
        self.calls: list[str] = []

    async def install_library(self, spec: str) -> dict:
        self.calls.append(spec)
        name = spec.split('@', 1)[0]
        for n in [name, *self.deps.get(name, [])]:
            folder = n.replace(' ', '_')
            if not (self.libs_dir / folder).exists():
                _lib(self.libs_dir, folder, n)
        return {'success': True}


class LibraryAutoInstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.libs = self.tmp / 'libraries'
        self.libs.mkdir()
        self.env = mock.patch.dict(
            'os.environ', {'ARDUINO_DIRECTORIES_USER': str(self.tmp)}, clear=False
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_ensure(self, specs, cli):
        return asyncio.run(la.ensure_manifest_libraries(specs, cli))

    def test_installed_names_match_folder_and_properties(self):
        _lib(self.libs, 'Adafruit_GFX_Library', 'Adafruit GFX Library')
        _lib(self.libs, 'U8g2')
        names = la.installed_names(self.libs)
        self.assertIn('adafruitgfxlibrary', names)
        self.assertIn('u8g2', names)

    def test_nothing_missing_installs_nothing(self):
        _lib(self.libs, 'Adafruit_SSD1306', 'Adafruit SSD1306')
        cli = _FakeCli(self.libs)
        self.assertEqual(self.run_ensure(['Adafruit SSD1306'], cli), [])
        self.assertEqual(cli.calls, [])

    def test_installs_only_missing_and_skips_ones_pulled_as_dependency(self):
        _lib(self.libs, 'U8g2', 'U8g2')
        cli = _FakeCli(self.libs, deps={'Adafruit SSD1306': ['Adafruit GFX Library', 'Adafruit BusIO']})
        log = self.run_ensure(
            ['U8g2', 'Adafruit SSD1306', 'Adafruit GFX Library', 'Adafruit BusIO'], cli
        )
        self.assertEqual(cli.calls, ['Adafruit SSD1306'])
        self.assertTrue(any('installed' in line for line in log))

    def test_pinned_spec_matches_by_base_name(self):
        _lib(self.libs, 'ArduinoJson', 'ArduinoJson')
        cli = _FakeCli(self.libs)
        self.assertEqual(self.run_ensure(['ArduinoJson@6.21.5'], cli), [])
        self.assertEqual(cli.calls, [])

    def test_failed_install_never_raises(self):
        class Broken:
            async def install_library(self, spec):
                raise RuntimeError('network down')
        log = self.run_ensure(['Missing Lib'], Broken())
        self.assertTrue(any('Could not install' in line for line in log))

    def test_disabled_by_env(self):
        cli = _FakeCli(self.libs)
        with mock.patch.dict('os.environ', {'VELXIO_AUTO_INSTALL_LIBRARIES': '0'}):
            self.assertEqual(self.run_ensure(['Some Lib'], cli), [])
        self.assertEqual(cli.calls, [])

    def test_no_manifest_is_a_noop(self):
        cli = _FakeCli(self.libs)
        self.assertEqual(self.run_ensure(None, cli), [])
        self.assertEqual(self.run_ensure([], cli), [])


if __name__ == '__main__':
    unittest.main()
