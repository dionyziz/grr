#!/usr/bin/env python
# -*- mode: python; encoding: utf-8 -*-

# Copyright 2010 Google Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Test client vfs."""


import os
import platform
import stat


import psutil

from grr.client import conf
import logging
from grr.client import conf
from grr.client import vfs
from grr.client.vfs_handlers import files

from grr.lib import test_lib
from grr.lib import utils
from grr.proto import jobs_pb2

FLAGS = conf.PARSER.flags


def setUp():
  # Initialize the VFS system
  vfs.VFSInit()


class VFSTest(test_lib.GRRBaseTest):
  """Test the client VFS switch."""

  def GetNumbers(self):
    """Generate a test string."""
    result = ""
    for i in range(1, 1001):
      result += "%s\n" % i

    return result

  def TestFileHandling(self, fd):
    """Test the file like object behaviour."""
    original_string = self.GetNumbers()

    self.assertEqual(fd.size, len(original_string))

    fd.Seek(0)
    self.assertEqual(fd.Read(100), original_string[0:100])
    self.assertEqual(fd.Tell(), 100)

    fd.Seek(-10, 1)
    self.assertEqual(fd.Tell(), 90)
    self.assertEqual(fd.Read(10), original_string[90:100])

    fd.Seek(0, 2)
    self.assertEqual(fd.Tell(), len(original_string))
    self.assertEqual(fd.Read(10), "")
    self.assertEqual(fd.Tell(), len(original_string))

    # Raise if we try to list the contents of a file object.
    self.assertRaises(IOError, lambda: list(fd.ListFiles()))

  def testRegularFile(self):
    """Test our ability to read regular files."""
    path = os.path.join(self.base_path, "morenumbers.txt")
    fd = vfs.VFSOpen(jobs_pb2.Path(path=path, pathtype=jobs_pb2.Path.OS))

    self.TestFileHandling(fd)

  def testOpenFilehandles(self):
    """Test that file handles are cached."""
    current_process = psutil.Process(os.getpid())
    num_open_files = len(current_process.get_open_files())

    path = os.path.join(self.base_path, "morenumbers.txt")

    fds = []
    for _ in range(100):
      fd = vfs.VFSOpen(jobs_pb2.Path(path=path, pathtype=jobs_pb2.Path.OS))
      self.assertEqual(fd.read(20), "1\n2\n3\n4\n5\n6\n7\n8\n9\n10")
      fds.append(fd)

    # This should not create any new file handles.
    self.assertTrue(len(current_process.get_open_files()) - num_open_files < 5)

  def testOpenFilehandlesExpire(self):
    """Test that file handles expire from cache."""
    files.FILE_HANDLE_CACHE = utils.FastStore(max_size=10)

    current_process = psutil.Process(os.getpid())
    num_open_files = len(current_process.get_open_files())

    path = os.path.join(self.base_path, "morenumbers.txt")
    fd = vfs.VFSOpen(jobs_pb2.Path(path=path, pathtype=jobs_pb2.Path.OS))

    fds = []
    for filename in fd.ListNames():
      child_fd = vfs.VFSOpen(jobs_pb2.Path(path=os.path.join(path, filename),
                                           pathtype=jobs_pb2.Path.OS))
      fd.read(20)
      fds.append(child_fd)

    # This should not create any new file handles.
    self.assertTrue(len(current_process.get_open_files()) - num_open_files < 5)

    # Make sure we exceeded the size of the cache.
    self.assert_(fds > 20)

  def testFileCasing(self):
    """Test our ability to read the correct casing from filesystem."""
    path = os.path.join(self.base_path, "numbers.txt")
    try:
      os.lstat(os.path.join(self.base_path, "nUmBeRs.txt"))
      os.lstat(os.path.join(self.base_path, "nuMbErs.txt"))
      # If we reached this point we are on a case insensitive file system
      # and the tests below do not make any sense.
      logging.warning("Case insensitive file system detected. Skipping test.")
      return
    except (IOError, OSError):
      pass

    fd = vfs.VFSOpen(jobs_pb2.Path(path=path, pathtype=jobs_pb2.Path.OS))
    self.assertEqual(fd.pathspec.Basename(), "numbers.txt")

    path = os.path.join(self.base_path, "numbers.TXT")

    fd = vfs.VFSOpen(jobs_pb2.Path(path=path, pathtype=jobs_pb2.Path.OS))
    self.assertEqual(fd.pathspec.Basename(), "numbers.TXT")

    path = os.path.join(self.base_path, "Numbers.txt")
    fd = vfs.VFSOpen(jobs_pb2.Path(path=path, pathtype=jobs_pb2.Path.OS))
    read_path = fd.pathspec.Basename()

    # The exact file now is non deterministic but should be either of the two:
    if read_path != "numbers.txt" and read_path != "numbers.TXT":
      raise RuntimeError("read path is %s" % read_path)

    # Ensure that the produced pathspec specified no case folding:
    s = fd.Stat()
    self.assertEqual(s.pathspec.path_options, jobs_pb2.Path.CASE_LITERAL)

    # Case folding will only occur when requested - this should raise because we
    # have the CASE_LITERAL option:
    self.assertRaises(IOError, vfs.VFSOpen,
                      jobs_pb2.Path(path=path, pathtype=jobs_pb2.Path.OS,
                                    path_options=jobs_pb2.Path.CASE_LITERAL))

  def testTSKFile(self):
    """Test our ability to read from image files."""
    path = os.path.join(self.base_path, "test_img.dd")
    path2 = "Test Directory/numbers.txt"

    p2 = jobs_pb2.Path(path=path2,
                       pathtype=jobs_pb2.Path.TSK)
    p1 = jobs_pb2.Path(path=path,
                       pathtype=jobs_pb2.Path.OS,
                       nested_path=p2)

    fd = vfs.VFSOpen(p1)
    self.TestFileHandling(fd)

  def testTSKFileInode(self):
    """Test opening a file through an indirect pathspec."""
    pathspec = utils.Pathspec(path=os.path.join(self.base_path, "test_img.dd"),
                              pathtype=jobs_pb2.Path.OS)
    pathspec.Append(pathtype=jobs_pb2.Path.TSK, inode=12,
                    path="/Test Directory")
    pathspec.Append(pathtype=jobs_pb2.Path.TSK, path="numbers.txt")

    fd = vfs.VFSOpen(pathspec)

    # Check that the new pathspec is correctly reduced to two components.
    self.assertEqual(fd.pathspec.first.path,
                     os.path.join(self.base_path, "test_img.dd"))
    self.assertEqual(fd.pathspec[1].path, "/Test Directory/numbers.txt")

    # And the correct inode is placed in the final branch.
    self.assertEqual(fd.Stat().pathspec.nested_path.inode, 15)
    self.TestFileHandling(fd)

  def testTSKFileCasing(self):
    """Test our ability to read the correct casing from image."""
    path = os.path.join(self.base_path, "test_img.dd")
    path2 = os.path.join("test directory", "NuMbErS.TxT")

    pb2 = jobs_pb2.Path(path=path2,
                        pathtype=jobs_pb2.Path.TSK)

    fd = vfs.VFSOpen(jobs_pb2.Path(path=path, pathtype=jobs_pb2.Path.OS,
                                   nested_path=pb2))

    # This fixes Windows paths.
    path = path.replace("\\", "/")
    # The pathspec should have 2 components.

    self.assertEqual(fd.pathspec.first.path, utils.NormalizePath(path))
    self.assertEqual(fd.pathspec.first.pathtype, jobs_pb2.Path.OS)

    nested = fd.pathspec.last
    self.assertEqual(nested.path, u"/Test Directory/numbers.txt")
    self.assertEqual(nested.pathtype, jobs_pb2.Path.TSK)

  def testTSKInodeHandling(self):
    """Test that we can open files by inode."""
    path = os.path.join(self.base_path, "ntfs_img.dd")
    pb2 = jobs_pb2.Path(inode=65, ntfs_type=128, ntfs_id=0,
                        path="/this/will/be/ignored",
                        pathtype=jobs_pb2.Path.TSK)

    fd = vfs.VFSOpen(jobs_pb2.Path(path=path, pathtype=jobs_pb2.Path.OS,
                                   nested_path=pb2, offset=63*512))

    self.assertEqual(fd.Read(100), "Hello world\n")

    pb2 = jobs_pb2.Path(inode=65, ntfs_type=128, ntfs_id=4,
                        pathtype=jobs_pb2.Path.TSK)

    fd = vfs.VFSOpen(jobs_pb2.Path(path=path, pathtype=jobs_pb2.Path.OS,
                                   nested_path=pb2, offset=63*512))

    self.assertEqual(fd.read(100), "I am a real ADS\n")

    # Make sure the size is correct:
    self.assertEqual(fd.Stat().st_size, len("I am a real ADS\n"))

  def testTSKNTFSHandling(self):
    """Test that TSK can correctly encode NTFS features."""
    path = os.path.join(self.base_path, "ntfs_img.dd")
    path2 = "test directory"

    pb2 = jobs_pb2.Path(path=path2,
                        pathtype=jobs_pb2.Path.TSK)

    fd = vfs.VFSOpen(jobs_pb2.Path(path=path, pathtype=jobs_pb2.Path.OS,
                                   nested_path=pb2, offset=63*512))

    # This fixes Windows paths.
    path = path.replace("\\", "/")
    listing = []
    pathspecs = []

    for f in fd.ListFiles():
      # Make sure the CASE_LITERAL option is set for all drivers so we can just
      # resend this proto back.
      self.assertEqual(f.pathspec.path_options, jobs_pb2.Path.CASE_LITERAL)
      pathspec = f.pathspec.nested_path
      self.assertEqual(pathspec.path_options, jobs_pb2.Path.CASE_LITERAL)
      pathspecs.append(f.pathspec)
      listing.append((pathspec.inode, pathspec.ntfs_type, pathspec.ntfs_id))

    ref = [(65, jobs_pb2.Path.TSK_FS_ATTR_TYPE_DEFAULT, 0),
           (65, jobs_pb2.Path.TSK_FS_ATTR_TYPE_NTFS_DATA, 4),
           (66, jobs_pb2.Path.TSK_FS_ATTR_TYPE_DEFAULT, 0),
           (67, jobs_pb2.Path.TSK_FS_ATTR_TYPE_DEFAULT, 0)]

    # Make sure that the ADS is recovered.
    self.assertEqual(listing, ref)

    # Try to read the main file
    self.assertEqual(pathspecs[0].nested_path.path, "/Test Directory/notes.txt")
    fd = vfs.VFSOpen(pathspecs[0])
    self.assertEqual(fd.read(1000), "Hello world\n")

    s = fd.Stat()
    self.assertEqual(s.pathspec.nested_path.inode, 65)
    self.assertEqual(s.pathspec.nested_path.ntfs_type, 1)
    self.assertEqual(s.pathspec.nested_path.ntfs_id, 0)

    # Check that the name of the ads is consistent.
    self.assertEqual(pathspecs[1].nested_path.path,
                     "/Test Directory/notes.txt:ads")
    fd = vfs.VFSOpen(pathspecs[1])
    self.assertEqual(fd.read(1000), "I am a real ADS\n")

    # Test that the stat contains the inode:
    s = fd.Stat()
    self.assertEqual(s.pathspec.nested_path.inode, 65)
    self.assertEqual(s.pathspec.nested_path.ntfs_type, 128)
    self.assertEqual(s.pathspec.nested_path.ntfs_id, 4)

  def testUnicodeFile(self):
    """Test ability to read unicode files from images."""
    path = os.path.join(self.base_path, "test_img.dd")
    path2 = os.path.join(u"איןד ןד ש אקדא", u"איןד.txt")

    pb2 = jobs_pb2.Path(path=path2,
                        pathtype=jobs_pb2.Path.TSK)

    fd = vfs.VFSOpen(jobs_pb2.Path(path=path, pathtype=jobs_pb2.Path.OS,
                                   nested_path=pb2))
    self.TestFileHandling(fd)

  def testListDirectory(self):
    """Test our ability to list a directory."""
    directory = vfs.VFSOpen(jobs_pb2.Path(path=self.base_path,
                                          pathtype=jobs_pb2.Path.OS))

    self.CheckDirectoryListing(directory, "morenumbers.txt")

  def testTSKListDirectory(self):
    """Test directory listing in sleuthkit."""
    path = os.path.join(self.base_path, u"test_img.dd")
    pb2 = jobs_pb2.Path(path=u"入乡随俗 海外春节别样过法",
                        pathtype=jobs_pb2.Path.TSK)
    pb = jobs_pb2.Path(path=path,
                       pathtype=jobs_pb2.Path.OS,
                       nested_path=pb2)
    directory = vfs.VFSOpen(pb)
    self.CheckDirectoryListing(directory, u"入乡随俗.txt")

  def testRecursiveImages(self):
    """Test directory listing in sleuthkit."""
    p3 = jobs_pb2.Path(path="/home/a.txt",
                       pathtype=jobs_pb2.Path.TSK)
    p2 = jobs_pb2.Path(path="/home/image2.img",
                       pathtype=jobs_pb2.Path.TSK,
                       nested_path=p3)
    p1 = jobs_pb2.Path(path=os.path.join(self.base_path, "test_img.dd"),
                       pathtype=jobs_pb2.Path.OS,
                       nested_path=p2)
    f = vfs.VFSOpen(p1)

    self.assertEqual(f.read(3), "yay")

  def testGuessPathSpec(self):
    """Test that we can guess a pathspec from a path."""
    path = os.path.join(self.base_path, "test_img.dd", "home/image2.img",
                        "home/a.txt")

    pathspec = jobs_pb2.Path(path=path, pathtype=jobs_pb2.Path.OS)

    fd = vfs.VFSOpen(pathspec)
    self.assertEqual(fd.read(3), "yay")

  def testFileNotFound(self):
    """Test that we raise an IOError for file not found."""
    path = os.path.join(self.base_path, "test_img.dd", "home/image2.img",
                        "home/nosuchfile.txt")

    pathspec = jobs_pb2.Path(path=path, pathtype=jobs_pb2.Path.OS)
    self.assertRaises(IOError, vfs.VFSOpen, pathspec)

  def testGuessPathSpecPartial(self):
    """Test that we can guess a pathspec from a partial pathspec."""
    path = os.path.join(self.base_path, "test_img.dd")
    pathspec = jobs_pb2.Path(path=path, pathtype=jobs_pb2.Path.OS)
    pathspec.nested_path.path = "/home/image2.img/home/a.txt"
    pathspec.nested_path.pathtype = jobs_pb2.Path.TSK

    fd = vfs.VFSOpen(pathspec)
    self.assertEqual(fd.read(3), "yay")

    # Open as a directory
    pathspec.nested_path.path = "/home/image2.img/home/"
    fd = vfs.VFSOpen(pathspec)

    names = []
    for s in fd.ListFiles():
      # Make sure that the stat pathspec is correct - it should be 3 levels
      # deep.
      self.assertEqual(s.pathspec.nested_path.path, "/home/image2.img")
      names.append(s.pathspec.nested_path.nested_path.path)

    self.assertTrue("/home/a.txt" in names)

  def testRegistryListing(self):
    """Test our ability to list registry keys."""
    if platform.system() != "Windows":
      return

    # Make a value we can test for
    import _winreg

    key = _winreg.OpenKey(_winreg.HKEY_CURRENT_USER,
                          "Software",
                          0,
                          _winreg.KEY_CREATE_SUB_KEY)
    subkey = _winreg.CreateKey(key, "GRR_Test")
    _winreg.SetValueEx(subkey, "foo", 0, _winreg.REG_SZ, "bar")

    vfs_path = "HKEY_CURRENT_USER/Software/GRR_Test"

    pathspec = jobs_pb2.Path(path=vfs_path,
                             pathtype=jobs_pb2.Path.REGISTRY)
    for f in vfs.VFSOpen(pathspec).ListFiles():
      self.assertEqual(f.pathspec.path, "/" + vfs_path + "/foo")
      self.assertEqual(f.resident, "bar")

    _winreg.DeleteKey(key, "GRR_Test")

  def CheckDirectoryListing(self, directory, test_file):
    """Check that the directory listing is sensible."""

    found = False
    for f in directory.ListFiles():
      # TSK makes virtual files with $ if front of them
      path = utils.Pathspec(f.pathspec).Basename()
      if path.startswith("$"): continue

      # Check the time is reasonable
      self.assert_(f.st_mtime > 10000000)
      self.assert_(f.st_atime > 10000000)
      self.assert_(f.st_ctime > 10000000)

      if path == test_file:
        found = True
        # Make sure its a regular file with the right size
        self.assert_(stat.S_ISREG(f.st_mode))
        self.assertEqual(f.st_size, 3893)

    self.assertEqual(found, True)

    # Raise if we try to read the contents of a directory object.
    self.assertRaises(IOError, directory.Read, 5)


def main(argv):
  vfs.VFSInit()
  test_lib.main(argv)

if __name__ == "__main__":
  conf.StartMain(main)
