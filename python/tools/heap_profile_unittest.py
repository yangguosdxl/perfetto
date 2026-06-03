#!/usr/bin/env python3
# Copyright (C) 2026 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import io
import unittest
from contextlib import redirect_stderr

import heap_profile


class HeapProfileHealthTest(unittest.TestCase):

  def test_warns_when_heap_profile_samples_are_discarded(self):
    rows = [
        {
            'name': 'heapprofd_buffer_overran',
            'idx': '0',
            'value': '2',
        },
        {
            'name': 'heapprofd_missing_packet',
            'idx': '0',
            'value': '3',
        },
        {
            'name': 'traced_buf_trace_writer_packet_loss',
            'idx': '0',
            'value': '4',
        },
    ]

    summary = heap_profile.summarize_trace_health(rows)
    stderr = io.StringIO()
    with redirect_stderr(stderr):
      heap_profile.warn_if_sample_data_discarded(summary)

    self.assertEqual(summary['heapprofd_data_loss'], 5)
    self.assertEqual(summary['perfetto_data_loss'], 4)
    warning = stderr.getvalue()
    self.assertIn('WARNING', warning)
    self.assertIn('sample data was discarded', warning)
    self.assertIn('heapprofd_data_loss=5', warning)
    self.assertIn('perfetto_data_loss=4', warning)

  def test_does_not_warn_when_no_samples_are_discarded(self):
    rows = [{
        'name': 'heapprofd_buffer_overran',
        'idx': '0',
        'value': '0',
    }]

    summary = heap_profile.summarize_trace_health(rows)
    stderr = io.StringIO()
    with redirect_stderr(stderr):
      heap_profile.warn_if_sample_data_discarded(summary)

    self.assertEqual('', stderr.getvalue())


if __name__ == '__main__':
  unittest.main()
