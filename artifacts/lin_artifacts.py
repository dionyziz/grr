#!/usr/bin/env python
# Copyright 2012 Google Inc.
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


"""Artifacts that are specific to Windows."""




from grr.lib import artifact

# Shorcut to make things cleaner.
Artifact = artifact.GenericArtifact
Collector = artifact.Collector


################################################################################
#  Linux Log Artifacts
################################################################################


class AuthLog(Artifact):
  """Linux auth log file."""
  SUPPORTED_OS = ["Linux"]
  LABELS = ["Logs", "Auth"]
  COLLECTORS = [
      Collector(action="GetFile", args={"path": "/var/log/auth.log"})
  ]


class Wtmp(Artifact):
  """Linux wtmp file."""
  SUPPORTED_OS = ["Linux"]
  LABELS = ["Logs", "Auth"]

  COLLECTORS = [
      Collector(action="GetFile", args={"path": "/var/log/wtmp"})
  ]
  PROCESSORS = ["WtmpParser"]
