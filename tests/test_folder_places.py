# Copyright (c) 2026 KpiMinds LLC. Licensed under the Apache License, Version 2.0; see LICENSE. SPDX-License-Identifier: Apache-2.0
"""The folder dialog's server side: places (home, computer, connected drives) and New Folder."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import project_manager


class MountedVolumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        # Test folders live under /tmp, which is a system folder on a real machine.
        patch.object(project_manager, '_SYSTEM_MOUNTS', ()).start()
        self.addCleanup(patch.stopall)

    def volumes(self, *lines):
        return project_manager._mounted_volumes('\n'.join(lines) + '\n')

    def test_a_connected_drive_is_listed_with_its_label_even_with_spaces(self):
        drive = self.base / 'USB DISK'
        drive.mkdir()
        found = self.volumes('/dev/sda1 / ext4 rw 0 0', '/dev/sdb1 ' + str(drive).replace(' ', '\\040') + ' vfat rw 0 0')
        self.assertEqual(found, [{'name': 'USB DISK', 'path': str(drive), 'kind': 'drive'}])

    def test_system_and_pseudo_mounts_are_not_places(self):
        found = self.volumes('/dev/sda1 / ext4 rw 0 0', 'tmpfs /run tmpfs rw 0 0', '/dev/loop3 /snap/core/1 squashfs ro 0 0',
                             '/dev/sda2 /boot/efi vfat rw 0 0', 'proc /proc proc rw 0 0', 'garbage')
        self.assertEqual(found, [])

    def test_a_mount_that_is_gone_or_unreadable_is_left_out(self):
        gone = self.base / 'gone'
        self.assertEqual(self.volumes('/dev/sdb1 ' + str(gone) + ' ext4 rw 0 0'), [])

    def test_a_disk_mounted_inside_a_system_folder_is_not_a_place(self):
        drive = self.base / 'data'
        drive.mkdir()
        line = '/dev/sdb1 ' + str(drive) + ' ext4 rw 0 0'
        self.assertEqual(len(self.volumes(line)), 1)                                   # a second disk at /data would be
        with patch.object(project_manager, '_SYSTEM_MOUNTS', (str(self.base),)):       # ... but not inside a system folder
            self.assertEqual(self.volumes(line), [])

    def test_places_start_with_home_and_include_computer(self):
        mounts = self.base / 'mounts'
        mounts.write_text('')
        with patch.object(project_manager, 'PROC_MOUNTS', str(mounts)):
            names = [place['name'] for place in project_manager.folder_places()['places']]
        self.assertEqual(names[0], 'Home')
        self.assertIn('Computer', names)

    def test_a_missing_mount_table_still_gives_home_and_computer(self):
        with patch.object(project_manager, 'PROC_MOUNTS', str(self.base / 'nothing')):
            places = project_manager.folder_places()['places']
        self.assertEqual([place['kind'] for place in places if place['kind'] in {'home', 'root'}], ['home', 'root'])


class NewFolderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()

    def test_a_new_folder_is_made_and_listed(self):
        result = project_manager.create_folder(str(self.base), 'fresh')
        self.assertTrue((self.base / 'fresh').is_dir())
        self.assertEqual(result['created'], str(self.base / 'fresh'))
        self.assertIn('fresh', [folder['name'] for folder in result['folders']])

    def test_each_refusal_is_one_plain_sentence_and_creates_nothing(self):
        (self.base / 'there').mkdir()
        cases = {'': 'type a name', '   ': 'type a name', 'a/b': 'cannot contain /', '..': 'cannot contain /', '.': 'cannot contain /',
                 'there': 'already exists', 'x' * 300: '255 bytes'}
        for name, words in cases.items():
            with self.subTest(name=name[:20]):
                with self.assertRaises(ValueError) as caught:
                    project_manager.create_folder(str(self.base), name)
                self.assertIn(words, str(caught.exception))
        self.assertEqual(sorted(child.name for child in self.base.iterdir()), ['there'])

    def test_a_parent_that_does_not_exist_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            project_manager.create_folder(str(self.base / 'missing'), 'child')
        self.assertIn('there is no folder', str(caught.exception))

    @unittest.skipIf(os.geteuid() == 0, 'root can write anywhere')
    def test_a_folder_that_cannot_be_written_says_so_plainly(self):
        locked = self.base / 'locked'
        locked.mkdir()
        locked.chmod(0o500)
        self.addCleanup(lambda: locked.chmod(0o700))
        with self.assertRaises(ValueError) as caught:
            project_manager.create_folder(str(locked), 'child')
        self.assertIn('cannot create child here', str(caught.exception))
        self.assertEqual(list(locked.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
