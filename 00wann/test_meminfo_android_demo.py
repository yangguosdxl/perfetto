#!/usr/bin/env python3
"""真实 Android meminfo demo 的解析与判定测试。"""

import textwrap
import unittest

import meminfo_android_demo.verify_meminfo_demo as verifier


BASELINE = textwrap.dedent("""
    Applications Memory Usage (in Kilobytes):
    Uptime: 100 Realtime: 100

    ** MEMINFO in pid 123 [com.example.meminfodemo] **
                       Pss  Private  Private     Swap      Rss     Heap     Heap     Heap
                     Total    Dirty    Clean    Dirty    Total     Size    Alloc     Free
                    ------   ------   ------   ------   ------   ------   ------   ------
      Native Heap    12000    11900        0        0    13000    18000    12000     5000
      Dalvik Heap     3000     2900        0        0     8000    16000     4000    12000
       Other mmap     1000        0      900        0     1200
      GL mtrack       2000     2000        0        0     2000
        Unknown       4000     3900        0        0     4500
          TOTAL      22000    20700      900        0    28700    34000    16000    17000

     App Summary
                           Pss(KB)                        Rss(KB)
                            ------                         ------
             Native Heap:    11900                          13000
              Graphics:       2000                           2000
         Private Other:       4800
                System:        400

     SQL
             MEMORY_USED:       10
      PAGECACHE_OVERFLOW:        4          MALLOC_SIZE:        2

     DATABASES
          pgsz     dbsz   Lookaside(b) cache hits cache misses cache size  Dbname
    PER CONNECTION STATS
             4       16              1     1     2     3  /data/user/0/com.example.meminfodemo/databases/demo.db
    POOL STATS
         cache hits  cache misses    cache size  Dbname
                1            2             3  /data/user/0/com.example.meminfodemo/databases/demo.db
""")


AFTER = textwrap.dedent("""
    Applications Memory Usage (in Kilobytes):
    Uptime: 200 Realtime: 200

    ** MEMINFO in pid 456 [com.example.meminfodemo] **
                       Pss  Private  Private     Swap      Rss     Heap     Heap     Heap
                     Total    Dirty    Clean    Dirty    Total     Size    Alloc     Free
                    ------   ------   ------   ------   ------   ------   ------   ------
      Native Heap    85000    84500        0        0    87000    96000    85000     8000
      Dalvik Heap    35000    34000        0        0    46000    64000    36000    28000
       Other mmap    38000       64    37000        0    39000
      EGL mtrack     12000    12000        0        0    12000
       GL mtrack     64000    64000        0        0    64000
        Unknown      72000    71000        0        0    73000
          TOTAL     306000   265564    37000        0   321000   160000   121000    36000

     App Summary
                           Pss(KB)                        Rss(KB)
                            ------                         ------
             Native Heap:    84500                          87000
              Graphics:      76000                          76000
         Private Other:     105064
                System:       3440

     SQL
             MEMORY_USED:      512
      PAGECACHE_OVERFLOW:      256          MALLOC_SIZE:       64

     DATABASES
          pgsz     dbsz   Lookaside(b) cache hits cache misses cache size  Dbname
    PER CONNECTION STATS
             4     4096             12    40    10    32  /data/user/0/com.example.meminfodemo/databases/demo.db
    POOL STATS
         cache hits  cache misses    cache size  Dbname
               40           10            50  /data/user/0/com.example.meminfodemo/databases/demo.db
""")


class MeminfoAndroidDemoTest(unittest.TestCase):

  def test_parse_meminfo_table_and_summary(self):
    """解析主表、Summary、SQL 和 DATABASES 的关键字段。"""
    parsed = verifier.parse_meminfo(AFTER)

    self.assertEqual(parsed.pid, 456)
    self.assertEqual(parsed.process_name, "com.example.meminfodemo")
    self.assertEqual(parsed.table["Native Heap"].private_dirty, 84500)
    self.assertEqual(parsed.table["Other mmap"].private_clean, 37000)
    self.assertEqual(parsed.summary_pss["Graphics"], 76000)
    self.assertEqual(parsed.sql["MEMORY_USED"], 512)
    self.assertEqual(parsed.databases[0].lookaside_slots, 12)

  def test_demo_growth_checks_require_expected_rows_to_increase(self):
    """demo 验证必须覆盖 native、mmap、graphics 和 sqlite 增长。"""
    baseline = verifier.parse_meminfo(BASELINE)
    after = verifier.parse_meminfo(AFTER)

    checks = verifier.build_growth_checks(baseline, after)
    failed = [check for check in checks if not check.passed]

    self.assertEqual(failed, [])
    self.assertGreater(verifier.delta(after, baseline, "Native Heap", "private_dirty"), 64 * 1024)
    self.assertGreater(verifier.delta(after, baseline, "Other mmap", "pss"), 16 * 1024)
    self.assertGreater(verifier.delta(after, baseline, "Unknown", "pss"), 32 * 1024)
    self.assertGreater(after.summary_pss["Graphics"] - baseline.summary_pss["Graphics"],
                       32 * 1024)


if __name__ == "__main__":
  unittest.main()
